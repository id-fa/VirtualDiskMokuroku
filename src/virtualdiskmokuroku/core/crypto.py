"""カタログの暗号化。

鍵の構成:
  パスワード --scrypt--> 鍵暗号化鍵 --AES-256-GCM--> データ鍵 (カタログごとに乱数で生成)
  データ鍵 --HKDF(メンバーごとの salt)--> メンバー鍵 --AES-256-GCM--> 各メンバーの中身

パスワード変更はデータ鍵を包み直すだけで、中身の再暗号化は要らない。

メンバーの形式:
  ヘッダ (56 バイト) | チャンク | チャンク | …
  ヘッダ   = マジック 12 | フラグ 1 | 予約 3 | 平文サイズ 8 | salt 32
  チャンク = 長さ 4 (ビッグエンディアン) | 暗号文 + 認証タグ 16

平文は(必要なら zlib で圧縮してから)最大 1MiB ごとに区切る。ナンスは「通し番号 11 バイト + 最終フラグ 1 バイト」。
メンバー鍵がメンバーごとに違うので、同じ鍵とナンスの組は二度と使われない。認証対象には呼び出し側が渡す
文脈文字列(どのカタログのどのメンバーか)とヘッダを含めるので、別メンバーとの差し替え・並べ替え・切り詰めは検出できる。
"""

from __future__ import annotations

import hashlib
import io
import os
import struct
import unicodedata
import zlib
from typing import BinaryIO, NamedTuple

from .errors import CatalogError, PasswordError

MAGIC = b"VDMOKU-ENC1\0"
CHUNK_SIZE = 1 << 20
FLAG_ZLIB = 0x01
UNKNOWN_SIZE = (1 << 64) - 1

_HEADER = struct.Struct(">12sB3xQ32s")
_TAG_SIZE = 16
_KEY_SIZE = 32
_KEY_WRAP_AAD = b"vdmoku-key-wrap-v1"
_MEMBER_INFO = b"vdmoku-member-v1"
_READ_SIZE = 1 << 20

# scrypt の既定値。n=2^17, r=8 で約 128MB のメモリと 0.3 秒程度を使う
DEFAULT_KDF = {"n": 1 << 17, "r": 8, "p": 1}


class MemberHeader(NamedTuple):
    flags: int
    plain_size: int | None  # 平文 (圧縮前) のバイト数。不明なら None
    salt: bytes
    raw: bytes


def _backend():
    """cryptography を遅延 import する(暗号化カタログを使わない限り必須にしない)。"""
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as error:
        raise CatalogError(
            "暗号化カタログを扱うには cryptography ライブラリが必要です (pip install cryptography)"
        ) from error
    return AESGCM, HKDF, hashes, InvalidTag


# --------------------------------------------------------------------------------------
# 鍵
# --------------------------------------------------------------------------------------


def _key_encryption_key(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    normalized = unicodedata.normalize("NFC", password).encode("utf-8")
    return hashlib.scrypt(normalized, salt=salt, n=n, r=r, p=p, maxmem=256 * n * r + (1 << 20), dklen=_KEY_SIZE)


def wrap_key(data_key: bytes, password: str, kdf: dict | None = None) -> dict:
    """データ鍵をパスワードで包み、カタログに平文で保存する記録を返す。"""
    AESGCM, _hkdf, _hashes, _invalid = _backend()
    params = dict(kdf or DEFAULT_KDF)
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _key_encryption_key(password, salt, params["n"], params["r"], params["p"])
    return {
        "cipher": "AES-256-GCM",
        "chunk_size": CHUNK_SIZE,
        "kdf": "scrypt",
        "n": params["n"],
        "r": params["r"],
        "p": params["p"],
        "salt": salt.hex(),
        "nonce": nonce.hex(),
        "wrapped_key": AESGCM(key).encrypt(nonce, data_key, _KEY_WRAP_AAD).hex(),
    }


def create_key(password: str, kdf: dict | None = None) -> tuple[dict, bytes]:
    """新しいデータ鍵を作る。戻り値は (保存する記録, データ鍵)。"""
    if not password:
        raise PasswordError("暗号化にはパスワードが必要です")
    data_key = os.urandom(_KEY_SIZE)
    return wrap_key(data_key, password, kdf), data_key


def unlock_key(record: dict, password: str) -> bytes:
    """保存された記録とパスワードからデータ鍵を取り出す。パスワード違いは ``PasswordError``。"""
    AESGCM, _hkdf, _hashes, InvalidTag = _backend()
    try:
        if record.get("kdf") != "scrypt" or record.get("cipher") != "AES-256-GCM":
            raise CatalogError("このカタログの暗号化方式には対応していません")
        key = _key_encryption_key(
            password, bytes.fromhex(record["salt"]), int(record["n"]), int(record["r"]), int(record["p"])
        )
        nonce = bytes.fromhex(record["nonce"])
        wrapped = bytes.fromhex(record["wrapped_key"])
    except (KeyError, ValueError, TypeError) as error:
        raise CatalogError(f"カタログの暗号化情報が壊れています ({error})") from error
    try:
        return AESGCM(key).decrypt(nonce, wrapped, _KEY_WRAP_AAD)
    except InvalidTag:
        raise PasswordError("パスワードが違います") from None


# --------------------------------------------------------------------------------------
# メンバーの暗号化・復号
# --------------------------------------------------------------------------------------


def _member_cipher(data_key: bytes, salt: bytes):
    AESGCM, HKDF, hashes, _invalid = _backend()
    member_key = HKDF(algorithm=hashes.SHA256(), length=_KEY_SIZE, salt=salt, info=_MEMBER_INFO).derive(data_key)
    return AESGCM(member_key)


def _nonce(counter: int, last: bool) -> bytes:
    return counter.to_bytes(11, "big") + (b"\x01" if last else b"\x00")


def _associated_data(context: str, header: bytes) -> bytes:
    return context.encode("utf-8") + b"\0" + header


def encrypt_stream(
    data_key: bytes,
    context: str,
    source: BinaryIO,
    dest: BinaryIO,
    *,
    plain_size: int | None = None,
    compress: bool = True,
) -> None:
    """``source`` を読み切って暗号化し ``dest`` に書く。``context`` は復号時にも同じ文字列を渡す。"""
    salt = os.urandom(32)
    flags = FLAG_ZLIB if compress else 0
    header = _HEADER.pack(MAGIC, flags, UNKNOWN_SIZE if plain_size is None else plain_size, salt)
    cipher = _member_cipher(data_key, salt)
    associated = _associated_data(context, header)
    dest.write(header)

    compressor = zlib.compressobj(6) if compress else None
    pending = bytearray()
    counter = 0

    def emit(block: bytes, last: bool) -> None:
        nonlocal counter
        sealed = cipher.encrypt(_nonce(counter, last), block, associated)
        dest.write(len(sealed).to_bytes(4, "big"))
        dest.write(sealed)
        counter += 1

    def drain() -> None:
        # 最後のチャンクには最終フラグを付けるので、ちょうど 1 チャンク分以下は残しておく
        while len(pending) > CHUNK_SIZE:
            emit(bytes(pending[:CHUNK_SIZE]), False)
            del pending[:CHUNK_SIZE]

    while True:
        block = source.read(_READ_SIZE)
        if not block:
            break
        pending += compressor.compress(block) if compressor else block
        drain()
    if compressor:
        pending += compressor.flush()
        drain()
    emit(bytes(pending), True)


def read_header(source: BinaryIO) -> MemberHeader:
    raw = source.read(_HEADER.size)
    if len(raw) != _HEADER.size:
        raise CatalogError("暗号化データが壊れています(ヘッダが足りません)")
    magic, flags, plain_size, salt = _HEADER.unpack(raw)
    if magic != MAGIC:
        raise CatalogError("暗号化データの形式が違います")
    return MemberHeader(flags, None if plain_size == UNKNOWN_SIZE else plain_size, salt, raw)


def decrypt_stream(data_key: bytes, context: str, source: BinaryIO, dest: BinaryIO) -> MemberHeader:
    """``source`` の暗号化メンバーを復号・検証して ``dest`` に書く。改ざん・破損は ``CatalogError``。"""
    _aesgcm, _hkdf, _hashes, InvalidTag = _backend()
    header = read_header(source)
    cipher = _member_cipher(data_key, header.salt)
    associated = _associated_data(context, header.raw)
    decompressor = zlib.decompressobj() if header.flags & FLAG_ZLIB else None
    damaged = CatalogError("暗号化データが壊れているか、改ざんされています")

    counter = 0
    prefix = source.read(4)
    while True:
        if len(prefix) != 4:
            raise damaged
        length = int.from_bytes(prefix, "big")
        if not _TAG_SIZE <= length <= CHUNK_SIZE + _TAG_SIZE:
            raise damaged
        sealed = source.read(length)
        if len(sealed) != length:
            raise damaged
        # 後ろに何も無ければ最終チャンク。途中で切り詰められていれば最終フラグが合わず検証に失敗する
        prefix = source.read(4)
        last = not prefix
        try:
            block = cipher.decrypt(_nonce(counter, last), sealed, associated)
        except InvalidTag:
            raise damaged from None
        counter += 1
        try:
            dest.write(decompressor.decompress(block) if decompressor else block)
        except zlib.error:
            raise damaged from None
        if last:
            break
    if decompressor:
        try:
            dest.write(decompressor.flush())
        except zlib.error:
            raise damaged from None
        if not decompressor.eof:
            raise damaged
    return header


def encrypt_bytes(data_key: bytes, context: str, data: bytes, *, compress: bool = True) -> bytes:
    dest = io.BytesIO()
    encrypt_stream(data_key, context, io.BytesIO(data), dest, plain_size=len(data), compress=compress)
    return dest.getvalue()


def decrypt_bytes(data_key: bytes, context: str, blob: bytes) -> bytes:
    dest = io.BytesIO()
    decrypt_stream(data_key, context, io.BytesIO(blob), dest)
    return dest.getvalue()
