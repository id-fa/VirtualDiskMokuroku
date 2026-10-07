"""ドライブのファイルリスト取得。

通常は Everything (es.exe) から取得する。Everything のインデックス対象外のボリューム
(CD/DVD, FAT/exFAT など) は ``os.scandir`` による走査にフォールバックする。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .drive_db import BuildStats, ProgressCallback, build_drive_db, build_drive_db_in_memory
from .errors import ScanCancelled
from .es_client import EsClient, EsError, RawEntry
from .ignore import IgnoreRules
from .volume import VolumeInfo, get_volume_info
from .workdb import DEFAULT_MEMORY_LIMIT
from ..i18n import tr

SOURCE_AUTO = "auto"
SOURCE_EVERYTHING = "everything"
SOURCE_WALK = "walk"

_FILETIME_EPOCH_OFFSET = 116444736000000000  # 1601-01-01 から 1970-01-01 までの 100ns 単位
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _filetime(ns: int | None) -> int | None:
    if ns is None:
        return None
    return ns // 100 + _FILETIME_EPOCH_OFFSET


def walk_entries(
    root: str,
    ignore: IgnoreRules | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    errors: list[str] | None = None,
) -> Iterator[RawEntry]:
    """``os.scandir`` で ``root`` 以下を走査する。無視対象のフォルダには降りない。"""
    root = os.path.abspath(root)
    prefix_len = len(root.rstrip("\\")) + 1
    uses_paths = bool(ignore and ignore.uses_paths)
    pending = [root]
    while pending:
        if is_cancelled is not None and is_cancelled():
            raise ScanCancelled()
        directory = pending.pop()
        try:
            iterator = os.scandir(directory)
        except OSError as error:
            if errors is not None:
                errors.append(f"{directory}: {error.strerror or error}")
            continue
        with iterator:
            while True:
                try:
                    item = next(iterator)
                except StopIteration:
                    break
                except OSError as error:
                    if errors is not None:
                        errors.append(f"{directory}: {error.strerror or error}")
                    break
                try:
                    stat = item.stat(follow_symlinks=False)
                    attrs = stat.st_file_attributes
                    is_dir = item.is_dir(follow_symlinks=False) or bool(attrs & 0x10)
                except OSError as error:
                    if errors is not None:
                        errors.append(f"{item.path}: {error.strerror or error}")
                    continue
                if ignore and ignore.matches(item.name, is_dir, item.path[prefix_len:] if uses_paths else None):
                    continue
                yield RawEntry(
                    path=item.path,
                    is_dir=is_dir,
                    size=None if is_dir else stat.st_size,
                    mtime=_filetime(stat.st_mtime_ns),
                    ctime=_filetime(getattr(stat, "st_birthtime_ns", None)),
                    attrs=attrs,
                )
                # ジャンクション/シンボリックリンクの先は辿らない(Everything の一覧と同じ扱い)
                if is_dir and not attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
                    pending.append(item.path)


@dataclass(slots=True)
class ScanResult:
    root: str  # スキャンしたルート ("X:\\" またはフォルダパス)
    source: str
    stats: BuildStats
    volume: VolumeInfo
    scanned_at: str
    meta: dict
    warnings: list[str] = field(default_factory=list)
    # 作成したドライブ DB。ファイルに作った場合はそのパス、メモリ上に作った場合はバイト列
    database: bytes | Path | None = None


def choose_source(root: str, es: EsClient | None, requested: str = SOURCE_AUTO) -> tuple[str, list[str]]:
    """取得元を決める。戻り値は (source, 警告メッセージ)。"""
    warnings: list[str] = []
    if requested == SOURCE_WALK:
        return SOURCE_WALK, warnings
    if es is None:
        if requested == SOURCE_EVERYTHING:
            raise EsError(tr('es.exe が設定されていません'))
        warnings.append(tr('es.exe が見つからないため、直接走査で取得しました'))
        return SOURCE_WALK, warnings
    try:
        count = es.result_count(root)
    except EsError as error:
        if requested == SOURCE_EVERYTHING:
            raise
        warnings.append(tr('Everything を利用できないため、直接走査で取得しました ({error})').format(error=error))
        return SOURCE_WALK, warnings
    if count > 0 or requested == SOURCE_EVERYTHING:
        return SOURCE_EVERYTHING, warnings
    # Everything のインデックスに無いボリューム (CD/DVD, FAT 系など)
    return SOURCE_WALK, warnings


def scan_to_db(
    root: str,
    db_path: str | os.PathLike[str] | None,
    *,
    es: EsClient | None = None,
    source: str = SOURCE_AUTO,
    ignore: IgnoreRules | None = None,
    with_ctime: bool = True,
    with_attrs: bool = True,
    progress: ProgressCallback | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    memory_limit: int = DEFAULT_MEMORY_LIMIT,
    spill_dir: Callable[[], Path] | None = None,
) -> ScanResult:
    """``root`` 以下をスキャンしてドライブ DB を作る。

    ``db_path`` を指定するとそのファイルに作る。``None`` ならメモリ上に作り(暗号化カタログ用)、結果の
    ``ScanResult.database`` にバイト列を入れる。その場合は ``spill_dir`` (上限超過時の退避先を返す関数) が必要。
    """
    root = os.path.abspath(root)
    volume = get_volume_info(root)
    if not volume.ready:
        raise OSError(tr('ドライブ {root} の準備ができていません(メディア未挿入など)').format(root=volume.root))

    chosen, warnings = choose_source(root, es, source)
    if progress:
        progress("source:" + chosen, 0)

    walk_errors: list[str] = []
    if chosen == SOURCE_EVERYTHING:
        assert es is not None
        entries = es.iter_entries(root, with_ctime=with_ctime, with_attrs=with_attrs, is_cancelled=is_cancelled)
    else:
        entries = walk_entries(root, ignore, is_cancelled, walk_errors)

    scanned_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta = {f"volume_{key}": value for key, value in volume.to_dict().items()}
    meta.update(scanned_at=scanned_at, source=chosen)
    if chosen == SOURCE_EVERYTHING and es is not None:
        try:
            meta["everything_version"] = es.everything_version()
        except EsError:
            pass

    common = {"ignore": ignore, "meta": meta, "progress": progress, "is_cancelled": is_cancelled}
    database: bytes | Path
    if db_path is not None:
        stats = build_drive_db(db_path, root, entries, **common)
        database = Path(db_path)
    else:
        if spill_dir is None:
            raise ValueError(tr('メモリ上に作る場合は spill_dir が必要です'))
        database, stats = build_drive_db_in_memory(root, entries, memory_limit=memory_limit, spill_dir=spill_dir, **common)
    if walk_errors:
        warnings.append(tr('読み取れなかったフォルダ/ファイルが {walk_errors_count} 件あります(例: {0})').format(walk_errors[0], walk_errors_count=len(walk_errors)))
    return ScanResult(root, chosen, stats, volume, scanned_at, meta, warnings, database)
