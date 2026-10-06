"""カタログファイル (.vdmoku) の読み書き。

カタログは ZIP 書庫で、ドライブ単位の SQLite DB と manifest.json をまとめたもの::

    manifest.json
    drives/<drive_id>/files.db
    drives/<drive_id>/context.db              (拡張コンテキスト。任意)
    drives/<drive_id>/backup/<stamp>/files.db (旧世代)

更新は常に一時ファイルへ書き出してから ``os.replace`` で差し替えるので、途中で失敗しても元のカタログは壊れない。
閲覧時はドライブ DB をキャッシュフォルダへ展開して開く。
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import shutil
import struct
import uuid
import zipfile
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from .drive_db import DriveDB
from .errors import CatalogError, PasswordError
from .ignore import DEFAULT_IGNORE
from .scanner import ScanResult
from .volume import VolumeInfo, list_volumes

CATALOG_EXTENSION = ".vdmoku"
LEGACY_CATALOG_EXTENSIONS = (".pmcat",)  # 旧名 PyMediaCatalogue 時代の拡張子(開くことはできる)
FORMAT_NAME = "virtualdiskmokuroku"
_LEGACY_FORMAT_NAMES = ("pymediacatalogue",)
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
FILES_DB = "files.db"
CONTEXT_DB = "context.db"

_SCRYPT_PARAMS = {"n": 1 << 14, "r": 8, "p": 1}
_COPY_CHUNK = 1 << 20

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


def _copy_member_raw(zin: zipfile.ZipFile, zout: zipfile.ZipFile, info: zipfile.ZipInfo, new_name: str) -> None:
    """圧縮済みデータを再圧縮せずに別の ZIP へ写す(大きな DB を毎回再圧縮しないため)。"""
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


class Catalog:
    def __init__(self, path: str | os.PathLike[str], manifest: dict, cache_root: Path | None = None):
        self.path = Path(path)
        self.manifest = manifest
        self._cache_root = cache_root or default_cache_root()

    # ------------------------------------------------------------------ 生成・オープン
    @classmethod
    def create(cls, path: str | os.PathLike[str], password: str | None = None, cache_root: Path | None = None) -> Catalog:
        manifest = {
            "format": FORMAT_NAME,
            "format_version": FORMAT_VERSION,
            "catalog_id": uuid.uuid4().hex,
            "created_at": _now(),
            "updated_at": _now(),
            "settings": default_settings(),
            "drives": [],
        }
        if password:
            manifest["settings"]["password"] = make_password_record(password)
        catalog = cls(path, manifest, cache_root)
        if catalog.path.exists():
            raise CatalogError(f"既にファイルが存在します: {catalog.path}")
        catalog._rewrite()
        return catalog

    @staticmethod
    def read_manifest(path: str | os.PathLike[str]) -> dict:
        try:
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile) as error:
            raise CatalogError(f"カタログを読み込めません: {path} ({error})") from error
        if manifest.get("format") not in (FORMAT_NAME, *_LEGACY_FORMAT_NAMES):
            raise CatalogError(f"VirtualDiskMokuroku のカタログではありません: {path}")
        if manifest.get("format_version", 0) > FORMAT_VERSION:
            raise CatalogError("このカタログは新しいバージョンのアプリで作成されています")
        return manifest

    @classmethod
    def is_password_protected(cls, path: str | os.PathLike[str]) -> bool:
        return bool(cls.read_manifest(path).get("settings", {}).get("password"))

    @classmethod
    def open(cls, path: str | os.PathLike[str], password: str | None = None, cache_root: Path | None = None) -> Catalog:
        manifest = cls.read_manifest(path)
        settings = default_settings()
        settings.update(manifest.get("settings", {}))
        manifest["settings"] = settings
        manifest.setdefault("drives", [])
        record = settings.get("password")
        if record:
            if password is None:
                raise PasswordError("このカタログはパスワードで保護されています")
            if not verify_password(record, password):
                raise PasswordError("パスワードが違います")
        return cls(path, manifest, cache_root)

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

    # ------------------------------------------------------------------ 設定
    def save(self) -> None:
        """manifest(設定やドライブ名の変更)を書き戻す。"""
        self._rewrite()

    def set_password(self, password: str | None) -> None:
        self.settings["password"] = make_password_record(password) if password else None
        self._rewrite()

    # ------------------------------------------------------------------ ドライブ DB の参照
    @staticmethod
    def _member(drive_id: str, name: str, backup: str | None = None) -> str:
        if backup:
            return f"drives/{drive_id}/backup/{backup}/{name}"
        return f"drives/{drive_id}/{name}"

    @property
    def cache_dir(self) -> Path:
        return self._cache_root / self.catalog_id

    def _cache_path(self, member: str, info: zipfile.ZipInfo) -> Path:
        stem, dot, suffix = member.rpartition(".")
        return self.cache_dir / f"{stem}.{info.CRC:08x}-{info.file_size:x}{dot}{suffix}"

    def has_member(self, drive_id: str, name: str, backup: str | None = None) -> bool:
        with zipfile.ZipFile(self.path) as archive:
            return self._member(drive_id, name, backup) in archive.NameToInfo

    def extract_db(self, drive_id: str, name: str = FILES_DB, backup: str | None = None) -> Path:
        """カタログ内の DB をキャッシュへ展開し、そのパスを返す(展開済みなら再利用)。"""
        member = self._member(drive_id, name, backup)
        with zipfile.ZipFile(self.path) as archive:
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

    def open_drive_db(self, drive_id: str, backup: str | None = None) -> DriveDB:
        return DriveDB(self.extract_db(drive_id, FILES_DB, backup))

    def extract_context_db(self, drive_id: str, backup: str | None = None) -> Path | None:
        """拡張コンテキスト DB をキャッシュへ展開する。カタログに無ければ None。"""
        if not self.has_member(drive_id, CONTEXT_DB, backup):
            return None
        return self.extract_db(drive_id, CONTEXT_DB, backup)

    def replace_context_db(self, drive_id: str, context_db_path: str | os.PathLike[str]) -> None:
        """現行世代の拡張コンテキスト DB だけを差し替える(バックアップ世代は回さない)。"""
        drive = self.drive(drive_id)
        drive["has_context"] = True
        self._rewrite(add={self._member(drive_id, CONTEXT_DB): Path(context_db_path)})

    # ------------------------------------------------------------------ ドライブの追加・更新・削除
    def put_drive(
        self,
        db_path: str | os.PathLike[str],
        result: ScanResult,
        *,
        drive_id: str | None = None,
        name: str | None = None,
        context_db_path: str | os.PathLike[str] | None = None,
        context_partial: bool = False,
    ) -> dict:
        """スキャン結果の DB を取り込む。``drive_id`` 指定時は更新(現行はバックアップ世代へ回す)。

        ``context_partial`` は拡張コンテキストの取得が途中で打ち切られたことの記録。
        """
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

        add = {self._member(drive["id"], FILES_DB): Path(db_path)}
        context_member = self._member(drive["id"], CONTEXT_DB)
        if context_db_path is not None:
            add[context_member] = Path(context_db_path)
        elif context_member not in rename:
            # 新しい世代に拡張コンテキストが無いなら、旧世代のものを現行として残さない
            delete_prefixes.append(context_member)
        self._rewrite(add=add, rename=rename, delete_prefixes=delete_prefixes)
        return drive

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

    def remove_drive(self, drive_id: str) -> None:
        drive = self.drive(drive_id)
        self.drives.remove(drive)
        self._rewrite(delete_prefixes=[f"drives/{drive_id}/"])
        shutil.rmtree(self.cache_dir / "drives" / drive_id, ignore_errors=True)

    # ------------------------------------------------------------------ 書き出し
    def _rewrite(
        self,
        *,
        add: dict[str, Path] | None = None,
        rename: dict[str, str] | None = None,
        delete_prefixes: Iterable[str] = (),
    ) -> None:
        """カタログを一時ファイルに作り直して原子的に差し替える。

        元の各メンバーは ``rename`` にあれば改名、``delete_prefixes`` に該当するか ``add`` で置き換えられるなら破棄、
        それ以外はそのまま(再圧縮なしで)引き継ぐ。
        """
        add = add or {}
        rename = rename or {}
        delete_prefixes = tuple(delete_prefixes)
        self.manifest["updated_at"] = _now()
        temp_path = self.path.with_name(self.path.name + ".tmp")
        try:
            with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as zout:
                zout.writestr(MANIFEST_NAME, json.dumps(self.manifest, ensure_ascii=False, indent=1))
                if self.path.exists():
                    with zipfile.ZipFile(self.path) as zin:
                        for info in zin.infolist():
                            name = info.filename
                            if name == MANIFEST_NAME:
                                continue
                            if name in rename:
                                _copy_member_raw(zin, zout, info, rename[name])
                            elif name in add or (delete_prefixes and name.startswith(delete_prefixes)):
                                continue
                            else:
                                _copy_member_raw(zin, zout, info, name)
                for name, source in add.items():
                    zout.write(source, name)
            os.replace(temp_path, self.path)
        except BaseException:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise
