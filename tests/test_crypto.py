import io
import os

import pytest

pytest.importorskip("cryptography")

from virtualdiskmokuroku.core import crypto  # noqa: E402
from virtualdiskmokuroku.core.errors import CatalogError, PasswordError  # noqa: E402

FAST_KDF = {"n": 1 << 10, "r": 8, "p": 1}  # テストを速くするための弱い設定


@pytest.fixture
def key():
    return os.urandom(32)


def test_key_wrap_and_password_change():
    record, data_key = crypto.create_key("合言葉 パスワード", FAST_KDF)
    assert len(data_key) == 32 and data_key.hex() not in str(record)
    assert crypto.unlock_key(record, "合言葉 パスワード") == data_key
    with pytest.raises(PasswordError):
        crypto.unlock_key(record, "違うパスワード")
    with pytest.raises(PasswordError):
        crypto.create_key("")

    # パスワード変更はデータ鍵を包み直すだけ(データ鍵そのものは変わらない)
    changed = crypto.wrap_key(data_key, "new password", FAST_KDF)
    assert crypto.unlock_key(changed, "new password") == data_key
    with pytest.raises(PasswordError):
        crypto.unlock_key(changed, "合言葉 パスワード")

    # 合成済み文字と結合文字の違いは同じパスワードとして扱う
    composed, decomposed = "パ", "パ"
    record, data_key = crypto.create_key(composed, FAST_KDF)
    assert crypto.unlock_key(record, decomposed) == data_key

    broken = dict(record, salt="zz")
    with pytest.raises(CatalogError):
        crypto.unlock_key(broken, composed)


@pytest.mark.parametrize("size", [0, 1, 1000, crypto.CHUNK_SIZE, crypto.CHUNK_SIZE + 1, crypto.CHUNK_SIZE * 3 + 12345])
@pytest.mark.parametrize("compress", [True, False])
def test_stream_roundtrip(key, size, compress):
    data = os.urandom(size)  # 乱数は圧縮が効かないので、圧縮ありでも複数チャンクになる
    blob = crypto.encrypt_bytes(key, "catalog/drive/files.db", data, compress=compress)
    assert size < 64 or data[:64] not in blob
    assert crypto.decrypt_bytes(key, "catalog/drive/files.db", blob) == data
    header = crypto.read_header(io.BytesIO(blob))
    assert header.plain_size == size and bool(header.flags & crypto.FLAG_ZLIB) == compress


def test_compression_and_unique_ciphertext(key):
    data = b"SQLite format 3\0" + b"abc" * 500_000
    first = crypto.encrypt_bytes(key, "ctx", data)
    second = crypto.encrypt_bytes(key, "ctx", data)
    assert len(first) < len(data) // 10  # 圧縮してから暗号化している
    assert first != second  # salt が毎回違うので同じ平文でも暗号文は一致しない
    assert b"SQLite format 3" not in first


def test_tampering_is_detected(key):
    data = os.urandom(crypto.CHUNK_SIZE * 2 + 500)
    blob = crypto.encrypt_bytes(key, "ctx", data, compress=False)

    def assert_damaged(candidate, context="ctx", candidate_key=None):
        with pytest.raises(CatalogError):
            crypto.decrypt_bytes(candidate_key or key, context, candidate)

    for position in (5, 13, 30, 70, len(blob) // 2, len(blob) - 1):  # マジック・フラグ・salt・本文・末尾
        flipped = bytearray(blob)
        flipped[position] ^= 0x01
        assert_damaged(bytes(flipped))

    assert_damaged(blob, context="other-member")  # 別メンバーとして読ませる
    assert_damaged(blob, candidate_key=os.urandom(32))  # 鍵違い
    assert_damaged(blob[:-1])
    assert_damaged(blob[:60])
    assert_damaged(blob + b"\0\0\0\x10" + b"x" * 16)  # 末尾への追加

    # チャンク境界での切り詰め・入れ替え
    header_size = 56
    chunk_length = 4 + crypto.CHUNK_SIZE + 16
    first = blob[header_size : header_size + chunk_length]
    second = blob[header_size + chunk_length : header_size + chunk_length * 2]
    rest = blob[header_size + chunk_length * 2 :]
    assert blob == blob[:header_size] + first + second + rest
    assert_damaged(blob[:header_size] + first + second)  # 最後のチャンクを落とす
    assert_damaged(blob[:header_size] + first)
    assert_damaged(blob[:header_size] + second + first + rest)  # 順序の入れ替え
    assert_damaged(blob[:header_size] + first + rest)  # 途中を抜く
