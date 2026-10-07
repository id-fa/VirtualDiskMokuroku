"""書庫ファイル内のファイルリスト (zip / tar 系 / 7z / rar)。展開はせず一覧だけを読む。"""

from __future__ import annotations

import tarfile
import zipfile
from collections.abc import Iterable
from datetime import datetime

from .base import EntryWriter, Extractor, datetime_to_filetime, module_available, unix_to_filetime
from ..i18n import tr

_ZIP_EXTENSIONS = ("zip", "cbz", "jar", "epub", "apk")
_TAR_EXTENSIONS = ("tar", "tgz", "tbz2", "txz", "tar.gz", "tar.bz2", "tar.xz")
_7Z_EXTENSIONS = ("7z", "cb7")
_RAR_EXTENSIONS = ("rar", "cbr")
_TAR_SUFFIXES = tuple("." + ext for ext in _TAR_EXTENSIONS)
_ZIP_UTF8_FLAG = 0x800


def decode_zip_name(info: zipfile.ZipInfo) -> str:
    """ZIP 内のファイル名を復元する。

    UTF-8 フラグが無い名前は zipfile が cp437 として読むので、元のバイト列に戻して UTF-8 → cp932 の順に試す。
    """
    name = info.orig_filename
    if info.flag_bits & _ZIP_UTF8_FLAG or name.isascii():
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    for encoding in ("utf-8", "cp932"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return name


def _zip_mtime(info: zipfile.ZipInfo) -> int | None:
    try:
        return datetime_to_filetime(datetime(*info.date_time))
    except (ValueError, TypeError):
        return None


class _Collector:
    """件数上限つきで書庫内エントリを writer に積む。"""

    def __init__(self, writer: EntryWriter, max_entries: int):
        self.writer = writer
        self.max_entries = max_entries
        self.count = 0
        self.truncated = False

    def add(self, path: str, size: int | None, mtime: int | None, is_dir: bool) -> bool:
        """追加できたら True。上限に達していたら False(呼び出し側は走査を打ち切る)。"""
        if self.max_entries and self.count >= self.max_entries:
            self.truncated = True
            return False
        path = path.replace("\\", "/").strip("/")
        if path:
            self.writer.add_inner(path, None if is_dir else size, mtime, is_dir)
            self.count += 1
        return True

    def finish(self) -> None:
        self.writer.add_meta("inner_count", self.count)
        if self.truncated:
            self.writer.add_meta("inner_truncated", tr('先頭 {count} 件のみ').format(count=self.count))


class ArchiveExtractor(Extractor):
    kind = "archive"
    label = "書庫内ファイルリスト"
    description = "zip / tar / 7z / rar の中にあるファイルの一覧を保存します"
    default_params = {
        "max_entries": 10000,
        "max_size_mb": 0,  # これより大きい書庫は読まない (0 = 無制限)
        "extensions": [*_ZIP_EXTENSIONS, *_TAR_EXTENSIONS, *_7Z_EXTENSIONS, *_RAR_EXTENSIONS],
    }
    stores = ("ctx", "inner")
    packages = "py7zr rarfile"  # 7z / rar にのみ必要

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self._max_entries = max(0, int(self.params.get("max_entries", 10000)))
        self._max_size = max(0, int(self.params.get("max_size_mb", 0))) * 1024 * 1024

    def active_extensions(self) -> Iterable[str]:
        unavailable: set[str] = set()
        if not module_available("py7zr"):
            unavailable.update(_7Z_EXTENSIONS)
        if not module_available("rarfile"):
            unavailable.update(_RAR_EXTENSIONS)
        return [ext for ext in self.params.get("extensions", ()) if ext.lower().lstrip(".") not in unavailable]

    def accepts(self, name: str, size: int | None) -> bool:
        if self._max_size and size is not None and size > self._max_size:
            return False
        return super().accepts(name, size)

    def extract(self, path: str, writer: EntryWriter) -> None:
        with open(path, "rb") as f:
            signature = f.read(8)
        collector = _Collector(writer, self._max_entries)
        # 拡張子より中身のシグネチャを優先する(拡張子違いの 7z / rar に備える)
        if signature.startswith(b"7z\xbc\xaf\x27\x1c"):
            self._list_7z(path, collector)
        elif signature.startswith(b"Rar!\x1a\x07"):
            self._list_rar(path, collector)
        elif path.lower().endswith(_TAR_SUFFIXES):
            self._list_tar(path, collector)
        else:
            self._list_zip(path, collector)
        collector.finish()

    def _list_zip(self, path: str, collector: _Collector) -> None:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if not collector.add(decode_zip_name(info), info.file_size, _zip_mtime(info), info.is_dir()):
                    break

    def _list_tar(self, path: str, collector: _Collector) -> None:
        with tarfile.open(path, "r:*") as archive:
            for member in archive:
                if not collector.add(member.name, member.size, unix_to_filetime(member.mtime), member.isdir()):
                    break

    def _list_7z(self, path: str, collector: _Collector) -> None:
        import py7zr

        with py7zr.SevenZipFile(path, "r") as archive:
            for item in archive.list():
                mtime = datetime_to_filetime(getattr(item, "creationtime", None))
                if not collector.add(item.filename, item.uncompressed, mtime, item.is_directory):
                    break

    def _list_rar(self, path: str, collector: _Collector) -> None:
        import rarfile

        with rarfile.RarFile(path) as archive:
            for info in archive.infolist():
                mtime = datetime_to_filetime(getattr(info, "mtime", None))
                if mtime is None and info.date_time:
                    try:
                        mtime = datetime_to_filetime(datetime(*info.date_time))
                    except (ValueError, TypeError):
                        mtime = None
                if not collector.add(info.filename, info.file_size, mtime, info.is_dir()):
                    break
