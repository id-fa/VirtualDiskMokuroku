"""カタログファイル (.vdmoku) の読み書き。

カタログは ZIP 書庫で、ドライブ単位の SQLite DB と manifest.json をまとめたもの::

    manifest.json
    drives/<drive_id>/files.db
    drives/<drive_id>/context.db              (拡張コンテキスト。任意)
    drives/<drive_id>/backup/<stamp>/files.db (旧世代)

更新は常に一時ファイルへ書き出してから ``os.replace`` で差し替えるので、途中で失敗しても元のカタログは壊れない。

暗号化カタログ (形式バージョン 2) では、manifest.json には形式と鍵の記録だけを平文で置き、本来の manifest は
``manifest.enc`` に、各 DB は同じメンバー名のまま ``core.crypto`` の形式で暗号化して無圧縮で格納する。

閲覧時の DB の開き方:
  - 通常のカタログ: キャッシュフォルダへ展開して開く
  - 暗号化カタログ: 復号した内容をメモリ上の共有 DB に載せて開く(平文をディスクに書かない)。
    ``memory_limit`` を超える大きな DB だけ、セッション用の一時フォルダに復号して開き、解放時に削除する
"""

from __future__ import annotations

import copy
import ctypes
import hashlib
import hmac
import io
import json
import os
import shutil
import sqlite3
import struct
import threading
import time
import uuid
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import crypto
from .drive_db import DriveDB, readonly_uri
from .errors import CatalogError, PasswordError
from .ignore import DEFAULT_IGNORE
from .scanner import ScanResult
from .volume import VolumeInfo, list_volumes
from .workdb import DEFAULT_MEMORY_LIMIT, open_shared_memory_db, remove_quietly

CATALOG_EXTENSION = ".vdmoku"
LEGACY_CATALOG_EXTENSIONS = (".pmcat",)  # 旧名 PyMediaCatalogue 時代の拡張子(開くことはできる)
FORMAT_NAME = "virtualdiskmokuroku"
_LEGACY_FORMAT_NAMES = ("pymediacatalogue",)
FORMAT_VERSION = 1  # 通常のカタログ
ENCRYPTED_FORMAT_VERSION = 2  # 暗号化カタログ
MANIFEST_NAME = "manifest.json"
MANIFEST_ENC = "manifest.enc"
FILES_DB = "files.db"
CONTEXT_DB = "context.db"

_SCRYPT_PARAMS = {"n": 1 << 14, "r": 8, "p": 1}
_COPY_CHUNK = 1 << 20
_SESSION_PREFIX = "session-"

# ドライブ 1 世代分の要約として manifest に持つ項目(バックアップ世代にも同じ項目を持たせる)
_SNAPSHOT_KEYS = (
    "label", "serial", "filesystem", "volume_guid", "drive_type", "device_vendor", "device_model",
    "device_serial", "bus_type", "root", "total_bytes", "free_bytes", "scanned_at", "source",
    "file_count", "dir_count", "total_size", "has_context", "context_partial",
)  # fmt: skip


def default_settings() -> dict:
    return {
        "ignore_patterns": list(DEFAULT_IGNORE),
        "backup_generations": 1,
        "password": None,
        "scan": {"with_ctime": True, "with_attrs": True},
        "context": {},
    }


def default_cache_root() -> Path:
    override = os.environ.get("VIRTUALDISKMOKUROKU_CACHE")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "VirtualDiskMokuroku" / "cache"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash_password(password: str, salt: bytes, params: dict) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=params["n"], r=params["r"], p=params["p"], dklen=32)


def make_password_record(password: str) -> dict:
    salt = os.urandom(16)
    return {
        "algo": "scrypt",
        **_SCRYPT_PARAMS,
        "salt": salt.hex(),
        "hash": _hash_password(password, salt, _SCRYPT_PARAMS).hex(),
    }


def verify_password(record: dict, password: str) -> bool:
    try:
        expected = bytes.fromhex(record["hash"])
        actual = _hash_password(password, bytes.fromhex(record["salt"]), record)
    except (KeyError, ValueError):
        return False
    return hmac.compare_digest(expected, actual)


def _process_alive(pid: int) -> bool:
    """指定のプロセスがまだ動いているか。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong(0)
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def cleanup_stale_sessions(cache_root: Path | None = None) -> None:
    """異常終了などで残ったセッション用一時フォルダ(復号済みの DB が入り得る)を削除する。起動時に呼ぶ。"""
    root = cache_root or default_cache_root()
    try:
        candidates = list(root.glob(_SESSION_PREFIX + "*"))
    except OSError:
        return
    for folder in candidates:
        try:
            pid = int(folder.name.split("-")[1])
        except (IndexError, ValueError):
            pid = 0
        if pid and pid != os.getpid() and _process_alive(pid):
            continue  # 別のインスタンスが使用中
        if pid == os.getpid():
            continue
        shutil.rmtree(folder, ignore_errors=True)


def _copy_member_raw(zin: zipfile.ZipFile, zout: zipfile.ZipFile, info: zipfile.ZipInfo, new_name: str) -> None:
    """圧縮済みデータを再圧縮せずに別の ZIP へ写す(大きな DB を毎回再圧縮・再暗号化しないため)。"""
    source = zin.fp
    assert source is not None and zout.fp is not None
    source.seek(info.header_offset)
    local_header = source.read(30)
    if len(local_header) != 30 or local_header[:4] != b"PK\x03\x04":
        raise CatalogError(f"カタログ内のエントリが壊れています: {info.filename}")
    name_len, extra_len = struct.unpack("<HH", local_header[26:30])
    source.seek(info.header_offset + 30 + name_len + extra_len)

    new_info = copy.copy(info)
    new_info.filename = new_name
    new_info.orig_filename = new_name
    new_info.extra = b""  # ZIP64 拡張は FileHeader() が必要に応じて作り直す
    new_info.flag_bits &= ~0x08  # サイズと CRC はヘッダに書くのでデータ記述子は不要
    new_info.header_offset = zout.fp.tell()
    zout.fp.write(new_info.FileHeader())
    remaining = info.compress_size
    while remaining > 0:
        chunk = source.read(min(_COPY_CHUNK, remaining))
        if not chunk:
            raise CatalogError(f"カタログ内のエントリが途中で切れています: {info.filename}")
        zout.fp.write(chunk)
        remaining -= len(chunk)
    zout.filelist.append(new_info)
    zout.NameToInfo[new_info.filename] = new_info
    zout.start_dir = zout.fp.tell()
    zout._didModify = True  # noqa: SLF001


@dataclass
class _Source:
    """暗号化カタログから復号して開いている DB。"""

    uri: str
    owner: sqlite3.Connection | None = None  # メモリ上の共有 DB を生かしておくための接続
    path: Path | None = None  # 大きすぎてセッション用一時フォルダに復号した場合のファイル


@dataclass(frozen=True)
class MemberCopy:
    """``_rewrite`` の ``add`` に渡せる、別のカタログのメンバー (DB) を写す指示。"""

    catalog: Catalog
    member: str


@dataclass(slots=True)
class NewDrive:
    """``Catalog.add_drives`` に渡す、新規登録するドライブ 1 台分。DB はファイルのパスかバイト列。"""

    database: Path | bytes
    result: ScanResult
    name: str | None = None
    context_db: Path | bytes | None = None
    extra: dict | None = None  # ドライブ情報に追加で記録する項目 (コメントなど)


class Catalog:
    def __init__(
        self,
        path: str | os.PathLike[str],
        manifest: dict,
        cache_root: Path | None = None,
        *,
        data_key: bytes | None = None,
        key_record: dict | None = None,
    ):
        self.path = Path(path)
        self.manifest = manifest
        self._cache_root = cache_root or default_cache_root()
        self._data_key = data_key
        self._key_record = key_record
        self.memory_limit = DEFAULT_MEMORY_LIMIT  # 暗号化カタログの DB をメモリに載せる上限 (バイト)
        self._sources: dict[str, _Source] = {}
        self._session_dir: Path | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 生成・オープン
    @classmethod
    def create(
        cls,
        path: str | os.PathLike[str],
        password: str | None = None,
        cache_root: Path | None = None,
        *,
        encrypt: bool = False,
        kdf: dict | None = None,
    ) -> Catalog:
        """新しいカタログを作る。``encrypt=True`` なら ``password`` で暗号化する。

        ``encrypt=False`` で ``password`` を渡した場合は、開くときに照合するだけの保護(暗号化なし)になる。
        """
        manifest = {
            "format": FORMAT_NAME,
            "format_version": FORMAT_VERSION,
            "catalog_id": uuid.uuid4().hex,
            "created_at": _now(),
            "updated_at": _now(),
            "settings": default_settings(),
            "drives": [],
        }
        data_key = key_record = None
        if encrypt:
            key_record, data_key = crypto.create_key(password or "", kdf)
        elif password:
            manifest["settings"]["password"] = make_password_record(password)
        catalog = cls(path, manifest, cache_root, data_key=data_key, key_record=key_record)
        if catalog.path.exists():
            raise CatalogError(f"既にファイルが存在します: {catalog.path}")
        catalog._rewrite()
        return catalog

    @staticmethod
    def read_manifest(path: str | os.PathLike[str]) -> dict:
        """平文の manifest.json を読む。暗号化カタログでは形式と鍵の記録だけが入っている。"""
        try:
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile) as error:
            raise CatalogError(f"カタログを読み込めません: {path} ({error})") from error
        if manifest.get("format") not in (FORMAT_NAME, *_LEGACY_FORMAT_NAMES):
            raise CatalogError(f"VirtualDiskMokuroku のカタログではありません: {path}")
        if manifest.get("format_version", 0) > ENCRYPTED_FORMAT_VERSION:
            raise CatalogError("このカタログは新しいバージョンのアプリで作成されています")
        return manifest

    @classmethod
    def is_encrypted_file(cls, path: str | os.PathLike[str]) -> bool:
        return bool(cls.read_manifest(path).get("encryption"))

    @classmethod
    def is_password_protected(cls, path: str | os.PathLike[str]) -> bool:
        """開くのにパスワードが要るか(暗号化、または照合のみの保護)。"""
        manifest = cls.read_manifest(path)
        return bool(manifest.get("encryption") or manifest.get("settings", {}).get("password"))

    @classmethod
    def open(cls, path: str | os.PathLike[str], password: str | None = None, cache_root: Path | None = None) -> Catalog:
        manifest = cls.read_manifest(path)
        data_key = None
        key_record = manifest.get("encryption")
        if key_record:
            if password is None:
                raise PasswordError("このカタログは暗号化されています。パスワードが必要です")
            data_key = crypto.unlock_key(key_record, password)
            catalog_id = manifest.get("catalog_id", "")
            try:
                with zipfile.ZipFile(path) as archive:
                    blob = archive.read(MANIFEST_ENC)
                manifest = json.loads(crypto.decrypt_bytes(data_key, f"{catalog_id}/manifest", blob).decode("utf-8"))
            except (OSError, KeyError, ValueError, zipfile.BadZipFile) as error:
                raise CatalogError(f"カタログを読み込めません: {path} ({error})") from error
            if manifest.get("catalog_id") != catalog_id:
                raise CatalogError("カタログの内容が一致しません(改ざんされている可能性があります)")

        settings = default_settings()
        settings.update(manifest.get("settings", {}))
        manifest["settings"] = settings
        manifest.setdefault("drives", [])
        record = settings.get("password")
        if record and not key_record:
            if password is None:
                raise PasswordError("このカタログはパスワードで保護されています")
            if not verify_password(record, password):
                raise PasswordError("パスワードが違います")
        return cls(path, manifest, cache_root, data_key=data_key, key_record=key_record)

    # ------------------------------------------------------------------ 基本情報
    @property
    def catalog_id(self) -> str:
        return self.manifest["catalog_id"]

    @property
    def settings(self) -> dict:
        return self.manifest["settings"]

    @property
    def drives(self) -> list[dict]:
        return self.manifest["drives"]

    @property
    def encrypted(self) -> bool:
        return self._data_key is not None

    def drive(self, drive_id: str) -> dict:
        for drive in self.drives:
            if drive["id"] == drive_id:
                return drive
        raise CatalogError(f"ドライブが見つかりません: {drive_id}")

    def find_matching_drives(self, volume: VolumeInfo, root: str) -> list[dict]:
        """同じメディアとみなせる登録済みドライブ(ドライブレターには依存しない)。"""
        sub_path = os.path.splitdrive(os.path.abspath(root))[1].rstrip("\\").casefold()
        matches = []
        for drive in self.drives:
            drive_sub = os.path.splitdrive(drive.get("root", ""))[1].rstrip("\\").casefold()
            if drive_sub != sub_path:
                continue
            if volume.serial and drive.get("serial") == volume.serial and drive.get("filesystem") == volume.filesystem:
                matches.append(drive)
        # シリアルが同じものが複数ある場合(複製ディスクなど)はラベル一致を優先
        exact = [drive for drive in matches if drive.get("label") == volume.label]
        return exact or matches

    def connected_root(self, drive_id: str) -> str | None:
        """登録済みドライブと同じボリュームがいま接続されていれば、その現在の場所を返す。

        判定はスキャン時と同じくシリアル・ファイルシステム(複数あればラベル)で行うので、ドライブレターが
        変わっていても見つかる。戻り値はスキャンルートに対応する現在のパス("G:\\" など)。未接続なら None。
        """
        drive = self.drive(drive_id)
        serial = drive.get("serial")
        if not serial:
            return None
        scan_drive, scan_sub = os.path.splitdrive(drive.get("root", ""))
        candidates = [
            volume
            for volume in list_volumes(include_device=False)
            if volume.ready and volume.serial == serial and volume.filesystem == drive.get("filesystem")
        ]
        if not candidates:
            return None

        def preference(volume: VolumeInfo) -> tuple[bool, bool]:
            # ラベルが一致するもの、次にスキャン時と同じドライブレターのものを優先
            return (volume.label != drive.get("label"), volume.root[:2].casefold() != scan_drive.casefold())

        root = min(candidates, key=preference).root
        sub_path = scan_sub.strip("\\")
        return root + sub_path if sub_path else root

    # ------------------------------------------------------------------ 設定・パスワード・暗号化
    def save(self) -> None:
        """manifest(設定やドライブ名の変更)を書き戻す。"""
        self._rewrite()

    def set_password(self, password: str | None, kdf: dict | None = None) -> None:
        """パスワードを設定・変更する。

        暗号化カタログではデータ鍵を包み直すだけなので、中身の再暗号化は起きない。
        通常のカタログでは開くときの照合用の記録を更新する(``None`` で解除)。
        """
        if self.encrypted:
            if not password:
                raise CatalogError("暗号化カタログのパスワードは空にできません。保護をやめるには暗号化を解除してください")
            assert self._data_key is not None
            self._key_record = crypto.wrap_key(self._data_key, password, kdf or self._current_kdf())
        else:
            self.settings["password"] = make_password_record(password) if password else None
        self._rewrite()

    def _current_kdf(self) -> dict | None:
        record = self._key_record or {}
        if all(key in record for key in ("n", "r", "p")):
            return {"n": record["n"], "r": record["r"], "p": record["p"]}
        return None

    def encrypt(self, password: str, kdf: dict | None = None) -> None:
        """通常のカタログを暗号化カタログに変換する。平文のキャッシュも削除する。"""
        if self.encrypted:
            raise CatalogError("このカタログは既に暗号化されています")
        key_record, data_key = crypto.create_key(password, kdf)
        previous = self.settings.get("password")
        self.settings["password"] = None  # 照合用の記録は不要になる
        try:
            with self._lock:
                self._write_converted(self.path, data_key, key_record)
        except BaseException:
            self.settings["password"] = previous
            raise
        self._data_key, self._key_record = data_key, key_record
        self.release_sources()
        shutil.rmtree(self.cache_dir, ignore_errors=True)

    def decrypt(self) -> None:
        """暗号化カタログを通常の(暗号化しない)カタログに戻す。"""
        if not self.encrypted:
            raise CatalogError("このカタログは暗号化されていません")
        with self._lock:
            self._write_converted(self.path, None, None)
        self._data_key = self._key_record = None
        self.release_sources()

    def export_decrypted(self, target: str | os.PathLike[str]) -> None:
        """暗号化カタログの内容を、通常のカタログとして別のファイルに書き出す(元のカタログは変えない)。"""
        target = Path(target)
        if target.resolve() == self.path.resolve():
            raise CatalogError("書き出し先に元のカタログと同じファイルは指定できません")
        if not self.encrypted:
            shutil.copyfile(self.path, target)
            return
        with self._lock:
            self._write_converted(target, None, None)

    # ------------------------------------------------------------------ ドライブ DB の参照
    @staticmethod
    def _member(drive_id: str, name: str, backup: str | None = None) -> str:
        if backup:
            return f"drives/{drive_id}/backup/{backup}/{name}"
        return f"drives/{drive_id}/{name}"

    def _associated(self, member: str) -> str:
        """暗号化の認証に使う文脈文字列。世代の入れ替え(メンバーの改名)では変わらないようにする。"""
        if member == MANIFEST_ENC:
            return f"{self.catalog_id}/manifest"
        parts = member.split("/")  # drives/<drive_id>/[backup/<stamp>/]<name>
        return f"{self.catalog_id}/{parts[1]}/{parts[-1]}"

    @property
    def cache_dir(self) -> Path:
        return self._cache_root / self.catalog_id

    def _cache_path(self, member: str, info: zipfile.ZipInfo) -> Path:
        stem, dot, suffix = member.rpartition(".")
        return self.cache_dir / f"{stem}.{info.CRC:08x}-{info.file_size:x}{dot}{suffix}"

    def has_member(self, drive_id: str, name: str, backup: str | None = None) -> bool:
        with self._lock, zipfile.ZipFile(self.path) as archive:
            return self._member(drive_id, name, backup) in archive.NameToInfo

    def extract_db(self, drive_id: str, name: str = FILES_DB, backup: str | None = None) -> Path:
        """通常のカタログ内の DB をキャッシュへ展開し、そのパスを返す(展開済みなら再利用)。"""
        if self.encrypted:
            raise CatalogError("暗号化カタログの DB はディスクに展開しません (db_source を使ってください)")
        member = self._member(drive_id, name, backup)
        with self._lock, zipfile.ZipFile(self.path) as archive:
            try:
                info = archive.getinfo(member)
            except KeyError:
                raise CatalogError(f"カタログ内に {member} がありません") from None
            target = self._cache_path(member, info)
            if target.is_file() and target.stat().st_size == info.file_size:
                return target
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + ".part")
            with archive.open(info) as source, open(partial, "wb") as destination:
                shutil.copyfileobj(source, destination, _COPY_CHUNK)
            os.replace(partial, target)
        self._prune_cache(target)
        return target

    def _prune_cache(self, keep: Path) -> None:
        """同じメンバーの古い展開物を消す(開いている最中のものは消せないので無視)。"""
        base = keep.name.split(".")[0]
        suffix = keep.suffix
        for other in keep.parent.glob(f"{base}.*{suffix}"):
            if other != keep:
                try:
                    other.unlink()
                except OSError:
                    pass

    def db_source(self, drive_id: str, name: str = FILES_DB, backup: str | None = None) -> str:
        """カタログ内の DB を読み取り専用で開くための SQLite の URI を返す。

        通常のカタログではキャッシュに展開したファイル、暗号化カタログではメモリ上の共有 DB を指す。
        返した URI は ``release_sources()`` を呼ぶまで有効。
        """
        if not self.encrypted:
            return readonly_uri(self.extract_db(drive_id, name, backup))
        member = self._member(drive_id, name, backup)
        with self._lock:
            source = self._sources.get(member)
            if source is None:
                source = self._sources[member] = self._load_encrypted(member)
            return source.uri

    def context_source(self, drive_id: str, backup: str | None = None) -> str | None:
        """拡張コンテキスト DB の URI。カタログに無ければ None。"""
        if not self.has_member(drive_id, CONTEXT_DB, backup):
            return None
        return self.db_source(drive_id, CONTEXT_DB, backup)

    def read_db_bytes(self, drive_id: str, name: str = FILES_DB, backup: str | None = None) -> bytes:
        """カタログ内の DB の中身(平文)をバイト列で返す。"""
        member = self._member(drive_id, name, backup)
        with self._lock, zipfile.ZipFile(self.path) as archive:
            try:
                info = archive.getinfo(member)
            except KeyError:
                raise CatalogError(f"カタログ内に {member} がありません") from None
            with archive.open(info) as stream:
                if not self.encrypted:
                    return stream.read()
                assert self._data_key is not None
                buffer = io.BytesIO()
                crypto.decrypt_stream(self._data_key, self._associated(member), stream, buffer)
                return buffer.getvalue()

    def _load_encrypted(self, member: str) -> _Source:
        with zipfile.ZipFile(self.path) as archive:
            try:
                info = archive.getinfo(member)
            except KeyError:
                raise CatalogError(f"カタログ内に {member} がありません") from None
            plain = self._decrypt_member(archive, info)
        if isinstance(plain, Path):
            return _Source(readonly_uri(plain), path=plain)
        # 名前付きのメモリ DB に載せる。同じ URI を開けば UI と検索スレッドの接続が同じ内容を共有できる
        uri, owner = open_shared_memory_db(plain)
        return _Source(uri, owner=owner)

    def _decrypt_member(self, archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes | Path:
        """暗号化メンバーの平文をメモリ上に取り出す。``memory_limit`` を超える大きさなら、セッション用の
        一時フォルダに復号したファイルのパスを返す (呼び出し側が不要になったら削除する)。"""
        assert self._data_key is not None
        member = info.filename
        with archive.open(info) as stream:
            header = crypto.read_header(stream)
        too_large = header.plain_size is None or header.plain_size > self.memory_limit
        with archive.open(info) as stream:
            if too_large:
                path = self.session_dir() / f"{uuid.uuid4().hex}.db"
                try:
                    with open(path, "wb") as destination:
                        crypto.decrypt_stream(self._data_key, self._associated(member), stream, destination)
                except BaseException:
                    remove_quietly(path)
                    raise
                return path
            buffer = io.BytesIO()
            crypto.decrypt_stream(self._data_key, self._associated(member), stream, buffer)
        return buffer.getvalue()

    def session_dir(self) -> Path:
        """このプロセス専用の一時フォルダ(メモリに載らない大きな DB の復号先・作業用)。"""
        with self._lock:
            if self._session_dir is None:
                folder = self._cache_root / f"{_SESSION_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:8]}"
                folder.mkdir(parents=True, exist_ok=True)
                self._session_dir = folder
            return self._session_dir

    def release_sources(self) -> None:
        """復号して開いている DB を解放し、一時フォルダを削除する。先に DB への接続を閉じておくこと。"""
        with self._lock:
            for source in self._sources.values():
                if source.owner is not None:
                    try:
                        source.owner.close()
                    except sqlite3.Error:
                        pass
                if source.path is not None:
                    remove_quietly(source.path)
            self._sources.clear()
            if self._session_dir is not None:
                shutil.rmtree(self._session_dir, ignore_errors=True)
                if not self._session_dir.exists():
                    self._session_dir = None

    def close(self) -> None:
        self.release_sources()

    def open_drive_db(self, drive_id: str, backup: str | None = None) -> DriveDB:
        return DriveDB(self.db_source(drive_id, FILES_DB, backup))

    def extract_context_db(self, drive_id: str, backup: str | None = None) -> Path | None:
        """(通常のカタログ用) 拡張コンテキスト DB をキャッシュへ展開する。カタログに無ければ None。"""
        if not self.has_member(drive_id, CONTEXT_DB, backup):
            return None
        return self.extract_db(drive_id, CONTEXT_DB, backup)

    def replace_context_db(self, drive_id: str, context_db: str | os.PathLike[str] | bytes) -> None:
        """現行世代の拡張コンテキスト DB だけを差し替える(バックアップ世代は回さない)。"""
        drive = self.drive(drive_id)
        drive["has_context"] = True
        self._rewrite(add={self._member(drive_id, CONTEXT_DB): self._as_source(context_db)})

    @staticmethod
    def _as_source(value: str | os.PathLike[str] | bytes) -> Path | bytes:
        return value if isinstance(value, bytes) else Path(value)

    # ------------------------------------------------------------------ ドライブの追加・更新・削除
    def put_drive(
        self,
        db_path: str | os.PathLike[str] | bytes,
        result: ScanResult,
        *,
        drive_id: str | None = None,
        name: str | None = None,
        context_db_path: str | os.PathLike[str] | bytes | None = None,
        context_partial: bool = False,
    ) -> dict:
        """スキャン結果の DB を取り込む。``drive_id`` 指定時は更新(現行はバックアップ世代へ回す)。

        DB はファイルのパスでも、メモリ上で作ったバイト列でもよい。
        ``context_partial`` は拡張コンテキストの取得が途中で打ち切られたことの記録。
        """
        drive, add, rename, delete_prefixes = self._plan_drive(
            db_path, result, drive_id, name, context_db_path, context_partial
        )
        self._rewrite(add=add, rename=rename, delete_prefixes=delete_prefixes)
        return drive

    def add_drives(self, items: Iterable[NewDrive]) -> list[dict]:
        """複数のドライブをまとめて新規登録する(カタログの書き換えは 1 回で済ませる)。"""
        count = len(self.drives)
        drives: list[dict] = []
        add: dict[str, Path | bytes] = {}
        delete_prefixes: list[str] = []
        try:
            for item in items:
                drive, members, _rename, obsolete = self._plan_drive(
                    item.database, item.result, None, item.name, item.context_db, False
                )
                drive.update(item.extra or {})
                drives.append(drive)
                add.update(members)
                delete_prefixes += obsolete
            if drives:
                self._rewrite(add=add, delete_prefixes=delete_prefixes)
        except BaseException:
            del self.drives[count:]
            raise
        return drives

    def _plan_drive(
        self,
        db_path: str | os.PathLike[str] | bytes,
        result: ScanResult,
        drive_id: str | None,
        name: str | None,
        context_db_path: str | os.PathLike[str] | bytes | None,
        context_partial: bool,
    ) -> tuple[dict, dict[str, Path | bytes], dict[str, str], list[str]]:
        """ドライブの登録内容を manifest に反映し、カタログの書き換え計画 (追加・改名・削除) を返す。"""
        snapshot = {key: getattr(result.volume, key) for key in _SNAPSHOT_KEYS if hasattr(result.volume, key)}
        snapshot.update(
            root=result.root,
            scanned_at=result.scanned_at,
            source=result.source,
            file_count=result.stats.file_count,
            dir_count=result.stats.dir_count,
            total_size=result.stats.total_size,
            has_context=context_db_path is not None,
            context_partial=context_db_path is not None and context_partial,
        )

        rename: dict[str, str] = {}
        delete_prefixes: list[str] = []
        if drive_id is None:
            drive = {"id": uuid.uuid4().hex, "name": name or result.volume.display_name, "backups": []}
            self.drives.append(drive)
        else:
            drive = self.drive(drive_id)
            if name:
                drive["name"] = name
            rename, delete_prefixes = self._rotate_backups(drive)
        drive.update(snapshot)

        add: dict[str, Path | bytes] = {self._member(drive["id"], FILES_DB): self._as_source(db_path)}
        context_member = self._member(drive["id"], CONTEXT_DB)
        if context_db_path is not None:
            add[context_member] = self._as_source(context_db_path)
        elif context_member not in rename:
            # 新しい世代に拡張コンテキストが無いなら、旧世代のものを現行として残さない
            delete_prefixes.append(context_member)
        return drive, add, rename, delete_prefixes

    def _rotate_backups(self, drive: dict) -> tuple[dict[str, str], list[str]]:
        """現行世代をバックアップへ回す計画を立て、manifest 上の世代リストを更新する。"""
        generations = max(0, int(self.settings.get("backup_generations", 1)))
        drive_id = drive["id"]
        backups: list[dict] = drive.setdefault("backups", [])
        rename: dict[str, str] = {}
        delete_prefixes: list[str] = []

        if generations > 0:
            stamp = self._backup_stamp(drive, backups)
            record = {"stamp": stamp, **{key: drive.get(key) for key in _SNAPSHOT_KEYS}}
            backups.insert(0, record)
            for member_name in (FILES_DB, CONTEXT_DB):
                rename[self._member(drive_id, member_name)] = self._member(drive_id, member_name, stamp)
        for expired in backups[generations:]:
            delete_prefixes.append(f"drives/{drive_id}/backup/{expired['stamp']}/")
        del backups[generations:]
        return rename, delete_prefixes

    @staticmethod
    def _backup_stamp(drive: dict, backups: list[dict]) -> str:
        try:
            moment = datetime.fromisoformat(drive.get("scanned_at") or "")
        except ValueError:
            moment = datetime.now(timezone.utc)
        base = moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        used = {backup["stamp"] for backup in backups}
        stamp, counter = base, 1
        while stamp in used:
            counter += 1
            stamp = f"{base}-{counter}"
        return stamp

    def restore_backup(self, drive_id: str, stamp: str) -> None:
        """バックアップ世代を現行に戻す(現行は新しいバックアップ世代になる)。"""
        drive = self.drive(drive_id)
        backups: list[dict] = drive.setdefault("backups", [])
        chosen = next((backup for backup in backups if backup["stamp"] == stamp), None)
        if chosen is None:
            raise CatalogError(f"バックアップが見つかりません: {stamp}")
        backups.remove(chosen)

        current_stamp = self._backup_stamp(drive, backups + [chosen])
        current_record = {"stamp": current_stamp, **{key: drive.get(key) for key in _SNAPSHOT_KEYS}}
        rename: dict[str, str] = {}
        for member_name in (FILES_DB, CONTEXT_DB):
            rename[self._member(drive_id, member_name)] = self._member(drive_id, member_name, current_stamp)
            rename[self._member(drive_id, member_name, stamp)] = self._member(drive_id, member_name)
        backups.insert(0, current_record)
        drive.update({key: chosen.get(key) for key in _SNAPSHOT_KEYS})

        generations = max(1, int(self.settings.get("backup_generations", 1)))
        delete_prefixes = [f"drives/{drive_id}/backup/{expired['stamp']}/" for expired in backups[generations:]]
        del backups[generations:]
        self._rewrite(rename=rename, delete_prefixes=delete_prefixes)

    # ------------------------------------------------------------------ グループ
    # グループはドライブに付ける名前 (drive["group"]) で、1 段だけ。同じ名前のドライブがツリーでまとめて表示される。
    # ドライブが 1 台も無いグループは存在しない
    def group_names(self) -> list[str]:
        """使われているグループ名(最初に現れた順)。"""
        return list(dict.fromkeys(drive["group"] for drive in self.drives if drive.get("group")))

    def set_drive_group(self, drive_id: str, group: str | None) -> None:
        """ドライブをグループに入れる。空文字か ``None`` でグループから外す。"""
        drive = self.drive(drive_id)
        group = (group or "").strip()
        if group == drive.get("group", ""):
            return
        drive.pop("group_comment", None)  # 取り込み元のグループのコメントは、グループを変えたら残さない
        if group:
            drive["group"] = group
        else:
            drive.pop("group", None)
        self._rewrite()

    def rename_group(self, old_name: str, new_name: str) -> None:
        """グループ名を変える。既にある名前にすると、そのグループと 1 つにまとまる。"""
        new_name = new_name.strip()
        if not new_name:
            raise CatalogError("グループ名が空です")
        members = [drive for drive in self.drives if drive.get("group") == old_name]
        if not members:
            raise CatalogError(f"グループが見つかりません: {old_name}")
        if new_name == old_name:
            return
        for drive in members:
            drive["group"] = new_name
        self._rewrite()

    def remove_drive(self, drive_id: str) -> None:
        self.remove_drives([drive_id])

    def remove_drives(self, drive_ids: Iterable[str]) -> None:
        """複数のドライブをまとめて削除する (バックアップ世代も含む。書き換えは 1 回)。"""
        self.reorganize(remove=drive_ids)

    def remove_context(self, drive_ids: Iterable[str]) -> None:
        """ドライブの拡張コンテキストだけを削除する (ファイルリストは残す)。"""
        self.reorganize(drop_context=drive_ids)

    # ------------------------------------------------------------------ 整理 (並び替え・一括削除・複製)
    def reorganize(
        self,
        layout: Iterable[tuple[str, str | None]] | None = None,
        *,
        remove: Iterable[str] = (),
        drop_context: Iterable[str] = (),
    ) -> None:
        """ドライブの並び順とグループをまとめて変え、不要なドライブや拡張コンテキストを削除する (書き換えは 1 回)。

        ``layout`` は残すドライブ全部を新しい順に並べた ``(drive_id, グループ名または None)``。``None`` なら
        並び順とグループは変えない。``remove`` のドライブはバックアップ世代ごと削除し、``drop_context`` の
        ドライブは拡張コンテキスト (バックアップ世代の分も) だけを削除する。
        """
        remove_ids = list(dict.fromkeys(remove))
        drop_ids = [drive_id for drive_id in dict.fromkeys(drop_context) if drive_id not in remove_ids]
        by_id = {drive["id"]: drive for drive in self.drives}
        for drive_id in (*remove_ids, *drop_ids):
            if drive_id not in by_id:
                raise CatalogError(f"ドライブが見つかりません: {drive_id}")

        original = copy.deepcopy(self.drives)
        if layout is None:
            ordered = [drive for drive in self.drives if drive["id"] not in remove_ids]
        else:
            ordered = []
            for drive_id, group in layout:
                drive = by_id.get(drive_id)
                if drive is None or drive_id in remove_ids:
                    raise CatalogError(f"ドライブが見つかりません: {drive_id}")
                group = (group or "").strip()
                if group != drive.get("group", ""):
                    drive.pop("group_comment", None)  # 取り込み元のグループのコメントは、グループを変えたら残さない
                    if group:
                        drive["group"] = group
                    else:
                        drive.pop("group", None)
                ordered.append(drive)
            expected = {drive_id for drive_id in by_id if drive_id not in remove_ids}
            if len(ordered) != len(expected) or {drive["id"] for drive in ordered} != expected:
                self.drives[:] = original
                raise CatalogError("並び順にすべてのドライブが含まれていません")

        delete_prefixes = [f"drives/{drive_id}/" for drive_id in remove_ids]
        for drive_id in drop_ids:
            drive = by_id[drive_id]
            drive["has_context"] = False
            drive["context_partial"] = False
            delete_prefixes.append(self._member(drive_id, CONTEXT_DB))
            for backup in drive.get("backups", []):
                backup["has_context"] = False
                backup["context_partial"] = False
                delete_prefixes.append(self._member(drive_id, CONTEXT_DB, backup["stamp"]))

        self.drives[:] = ordered
        try:
            self._rewrite(delete_prefixes=delete_prefixes)
        except BaseException:
            self.drives[:] = original
            raise
        for drive_id in remove_ids:
            shutil.rmtree(self.cache_dir / "drives" / drive_id, ignore_errors=True)
        for drive_id in drop_ids:
            for cached in (self.cache_dir / "drives" / drive_id).glob(f"**/{CONTEXT_DB.rpartition('.')[0]}.*"):
                remove_quietly(cached)

    def copy_drives_to(
        self, target: Catalog, drive_ids: Iterable[str], *, groups: dict[str, str | None] | None = None
    ) -> list[dict]:
        """ドライブの現行世代 (ファイルリストと拡張コンテキスト) を別のカタログへ複製する。

        バックアップ世代は写さない。ドライブの ID はコピー先に同じ ID が無ければそのまま使い、あれば新しく振る。
        ``groups`` (drive_id → グループ名) を渡すと、コピー先ではそのグループに入れる (無ければ今のグループのまま)。
        暗号化の有無が違っても写せる (コピー先の鍵で暗号化し直す)。戻り値はコピー先に登録したドライブの情報。
        """
        if target is self or target.path.resolve() == self.path.resolve():
            raise CatalogError("同じカタログにはコピーできません")
        with self._lock, zipfile.ZipFile(self.path) as archive:
            members = set(archive.namelist())
        taken = {drive["id"] for drive in target.drives}
        add: dict[str, Path | bytes | MemberCopy] = {}
        copied: list[dict] = []
        for drive_id in dict.fromkeys(drive_ids):
            drive = self.drive(drive_id)
            files_member = self._member(drive_id, FILES_DB)
            if files_member not in members:
                raise CatalogError(f"カタログ内に {files_member} がありません")
            record = copy.deepcopy({key: value for key, value in drive.items() if key != "backups"})
            record["id"] = drive_id if drive_id not in taken else uuid.uuid4().hex
            record["backups"] = []
            taken.add(record["id"])
            if groups is not None and drive_id in groups:
                group = (groups[drive_id] or "").strip()
                if group != drive.get("group", ""):
                    record.pop("group_comment", None)
                    if group:
                        record["group"] = group
                    else:
                        record.pop("group", None)
            add[target._member(record["id"], FILES_DB)] = MemberCopy(self, files_member)
            context_member = self._member(drive_id, CONTEXT_DB)
            if context_member in members:
                add[target._member(record["id"], CONTEXT_DB)] = MemberCopy(self, context_member)
            else:
                record["has_context"] = False
                record["context_partial"] = False
            copied.append(record)
        count = len(target.drives)
        target.drives.extend(copied)
        try:
            target._rewrite(add=add)
        except BaseException:
            del target.drives[count:]
            raise
        return copied

    # ------------------------------------------------------------------ 書き出し
    def _write_manifest(self, zout: zipfile.ZipFile, data_key: bytes | None, key_record: dict | None) -> None:
        manifest = dict(self.manifest)
        manifest["format_version"] = ENCRYPTED_FORMAT_VERSION if data_key is not None else FORMAT_VERSION
        payload = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
        if data_key is None:
            zout.writestr(MANIFEST_NAME, payload)
            return
        # 平文で置くのは形式と鍵の記録だけ。古い版のアプリには「新しい版で作成された」と表示される
        public = {
            "format": FORMAT_NAME,
            "format_version": ENCRYPTED_FORMAT_VERSION,
            "catalog_id": self.catalog_id,
            "encryption": key_record,
        }
        zout.writestr(MANIFEST_NAME, json.dumps(public, ensure_ascii=False, indent=1))
        sealed = crypto.encrypt_bytes(data_key, self._associated(MANIFEST_ENC), payload)
        zout.writestr(zipfile.ZipInfo(MANIFEST_ENC, date_time=time.localtime()[:6]), sealed, zipfile.ZIP_STORED)

    def _add_member(
        self, zout: zipfile.ZipFile, name: str, source: Path | bytes | MemberCopy, data_key: bytes | None
    ) -> None:
        if isinstance(source, MemberCopy):
            self._copy_foreign_member(zout, name, source, data_key)
            return
        if data_key is None:
            if isinstance(source, bytes):
                zout.writestr(name, source)
            else:
                zout.write(source, name)
            return
        if isinstance(source, bytes):
            stream, size = io.BytesIO(source), len(source)
        else:
            stream, size = open(source, "rb"), os.path.getsize(source)
        with stream:
            self._write_stream(zout, name, stream, size, data_key)

    def _write_stream(self, zout: zipfile.ZipFile, name: str, stream, size: int, data_key: bytes | None) -> None:
        """平文のストリームをメンバーとして書く (``data_key`` があれば暗号化する)。"""
        info = zipfile.ZipInfo(name, date_time=time.localtime()[:6])
        info.compress_type = zipfile.ZIP_DEFLATED if data_key is None else zipfile.ZIP_STORED  # 暗号化の前に圧縮済み
        with zout.open(info, "w", force_zip64=True) as destination:
            if data_key is None:
                shutil.copyfileobj(stream, destination, _COPY_CHUNK)
            else:
                crypto.encrypt_stream(data_key, self._associated(name), stream, destination, plain_size=size)

    def _copy_foreign_member(self, zout: zipfile.ZipFile, name: str, source: MemberCopy, data_key: bytes | None) -> None:
        """別のカタログのメンバーを ``name`` として写す。どちらも暗号化されていなければ再圧縮せずに写し、
        そうでなければ復号して (必要ならこのカタログの鍵で暗号化し直して) 書く。平文は一時ファイルに残さない。"""
        origin = source.catalog
        with origin._lock, zipfile.ZipFile(origin.path) as zin:
            try:
                info = zin.getinfo(source.member)
            except KeyError:
                raise CatalogError(f"カタログ内に {source.member} がありません") from None
            if not origin.encrypted:
                if data_key is None:
                    _copy_member_raw(zin, zout, info, name)
                else:
                    with zin.open(info) as stream:
                        self._write_stream(zout, name, stream, info.file_size, data_key)
                return
            if data_key is None:
                # 暗号化カタログ → 通常のカタログ: 復号しながら直接書く
                stored = zipfile.ZipInfo(name, date_time=time.localtime()[:6])
                stored.compress_type = zipfile.ZIP_DEFLATED
                assert origin._data_key is not None
                with zin.open(info) as stream, zout.open(stored, "w", force_zip64=True) as destination:
                    crypto.decrypt_stream(origin._data_key, origin._associated(source.member), stream, destination)
                return
            # 暗号化カタログ同士: 鍵が違うので、いったん平文をメモリ (大きければセッション用フォルダ) に置く
            plain = origin._decrypt_member(zin, info)
        try:
            if isinstance(plain, bytes):
                self._write_stream(zout, name, io.BytesIO(plain), len(plain), data_key)
            else:
                with open(plain, "rb") as stream:
                    self._write_stream(zout, name, stream, os.path.getsize(plain), data_key)
        finally:
            if isinstance(plain, Path):
                remove_quietly(plain)

    def _rewrite(
        self,
        *,
        add: Mapping[str, Path | bytes | MemberCopy] | None = None,
        rename: dict[str, str] | None = None,
        delete_prefixes: Iterable[str] = (),
    ) -> None:
        """カタログを一時ファイルに作り直して原子的に差し替える。

        元の各メンバーは ``rename`` にあれば改名、``delete_prefixes`` に該当するか ``add`` で置き換えられるなら破棄、
        それ以外はそのまま(再圧縮・再暗号化なしで)引き継ぐ。
        """
        add = add or {}
        rename = rename or {}
        delete_prefixes = tuple(delete_prefixes)
        self.manifest["updated_at"] = _now()
        temp_path = self.path.with_name(self.path.name + ".tmp")
        with self._lock:
            try:
                with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as zout:
                    self._write_manifest(zout, self._data_key, self._key_record)
                    if self.path.exists():
                        with zipfile.ZipFile(self.path) as zin:
                            for info in zin.infolist():
                                name = info.filename
                                if name in (MANIFEST_NAME, MANIFEST_ENC):
                                    continue
                                if name in rename:
                                    _copy_member_raw(zin, zout, info, rename[name])
                                elif name in add or (delete_prefixes and name.startswith(delete_prefixes)):
                                    continue
                                else:
                                    _copy_member_raw(zin, zout, info, name)
                    for name, source in add.items():
                        self._add_member(zout, name, source, self._data_key)
                os.replace(temp_path, self.path)
            except BaseException:
                remove_quietly(temp_path)
                raise

    def _write_converted(self, target: Path, new_key: bytes | None, new_record: dict | None) -> None:
        """全メンバーを暗号化(または復号)し直したカタログを ``target`` に書く。平文の一時ファイルは作らない。"""
        if (self._data_key is None) == (new_key is None):
            raise CatalogError("暗号化の状態が変わらない変換はできません")
        temp_path = target.with_name(target.name + ".tmp")
        try:
            with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as zout, zipfile.ZipFile(self.path) as zin:
                self._write_manifest(zout, new_key, new_record)
                for info in zin.infolist():
                    name = info.filename
                    if name in (MANIFEST_NAME, MANIFEST_ENC):
                        continue
                    with zin.open(info) as source:
                        if new_key is None:
                            assert self._data_key is not None
                            with zout.open(name, "w", force_zip64=True) as destination:
                                crypto.decrypt_stream(self._data_key, self._associated(name), source, destination)
                        else:
                            stored = zipfile.ZipInfo(name, date_time=info.date_time)
                            stored.compress_type = zipfile.ZIP_STORED
                            with zout.open(stored, "w", force_zip64=True) as destination:
                                crypto.encrypt_stream(
                                    new_key, self._associated(name), source, destination, plain_size=info.file_size
                                )
            os.replace(temp_path, target)
        except BaseException:
            remove_quietly(temp_path)
            raise
