"""他のカタログソフトのデータの取り込み(GUI と CLI で共用)。

Virtual CD-ROM Case の .cas を読み、ドライブごとに files.db (と、コメントなどがあれば context.db) を作って
カタログへ新規登録する。コメントはテキスト内容として、プロパティ(HTML のタイトルや実行ファイルのバージョン情報)・
分類・CRC32 はメタ情報として取り込む。中身を展開して登録されていた書庫は、このアプリの書庫の扱いに合わせて、
書庫内リストを持つ 1 つのファイルとして取り込む(書庫内のエントリが持つ CRC などは取り込まない)。
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .core import vcdcase
from .core.catalog import CONTEXT_DB, FILES_DB, Catalog, NewDrive
from .core.drive_db import ROOT_ID, ProgressCallback, build_drive_db, build_drive_db_in_memory, readonly_uri
from .core.errors import CatalogError, ScanCancelled
from .core.es_client import RawEntry
from .core.scanner import ScanResult
from .core.volume import VolumeInfo
from .core.workdb import WorkDb, keep_temp_in_memory, open_shared_memory_db, remove_quietly
from .i18n import tr

SOURCE_VCDCASE = "vcdcase"
CONTEXT_KIND = "vcdcase"
# .cas にはドライブレターが記録されていないので、スキャン時のパスは "?:\" で始める
IMPORT_ROOT = "?:\\"

_MEDIA_DRIVE_TYPES = {1: "cdrom"}
_FLUSH_BYTES = 64 * 1024 * 1024  # 作った DB がこれだけ溜まるごとにカタログへ書き込む
_COMMIT_INTERVAL = 500
_DATE_COMMENT = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})")


@dataclass(slots=True)
class ImportOutcome:
    drives: list[dict] = field(default_factory=list)  # 登録したドライブ (カタログ上の情報)
    file_count: int = 0
    dir_count: int = 0
    total_size: int = 0
    context_count: int = 0  # コメントなどを取り込んだエントリの数


def import_vcdcase(
    catalog: Catalog,
    path: str | os.PathLike[str],
    *,
    progress: ProgressCallback | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> ImportOutcome:
    """Virtual CD-ROM Case のカタログ (.cas) の全ドライブを ``catalog`` へ新規登録する。

    進捗は ``progress(phase, count)`` で通知する。phase は ``import_total`` (ドライブ数) / ``import`` (処理済みの
    ドライブ数) / ``save``。キャンセルすると ``ScanCancelled`` を送出する(ドライブが多い場合は途中で何度か
    カタログへ書き込むので、それまでに書き込んだドライブは登録されたまま残る)。

    暗号化カタログでは DB をメモリ上で作り、平文をディスクに書かない(``pipeline.scan_into_catalog`` と同じ)。
    """
    path = Path(path)
    case = vcdcase.read_case(path, is_cancelled=is_cancelled)
    if not case.drives:
        raise CatalogError(tr('取り込めるドライブがありません: {path}').format(path=path))
    file_time = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")

    work_dir = None if catalog.encrypted else Path(tempfile.mkdtemp(prefix="vdmoku_import_"))
    outcome = ImportOutcome()
    pending: list[NewDrive] = []
    pending_bytes = 0

    def discard_pending() -> None:
        for item in pending:
            for artifact in (item.database, item.context_db):
                if isinstance(artifact, Path):
                    remove_quietly(artifact)
        pending.clear()

    def flush() -> None:
        nonlocal pending_bytes
        if not pending:
            return
        drives = catalog.add_drives(pending)
        outcome.drives += drives
        for drive in drives:
            outcome.file_count += drive["file_count"]
            outcome.dir_count += drive["dir_count"]
            outcome.total_size += drive["total_size"]
        discard_pending()
        pending_bytes = 0

    try:
        if progress:
            progress("import_total", len(case.drives))
        for index, drive in enumerate(case.drives):
            if is_cancelled is not None and is_cancelled():
                raise ScanCancelled()
            item, context_count = _build_drive(catalog, drive, work_dir, index, path.name, file_time, is_cancelled)
            pending.append(item)
            pending_bytes += _size(item.database) + _size(item.context_db)
            outcome.context_count += context_count
            if pending_bytes >= _FLUSH_BYTES:
                flush()
            if progress:
                progress("import", index + 1)
        if progress:
            progress("save", 0)
        flush()
        return outcome
    finally:
        discard_pending()
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)


def _size(artifact: Path | bytes | None) -> int:
    if artifact is None:
        return 0
    return len(artifact) if isinstance(artifact, bytes) else artifact.stat().st_size


def _registered_at(comment: str) -> str | None:
    """ドライブのコメントが登録日 ("2026/10/06") のままなら、その日付を返す。"""
    match = _DATE_COMMENT.fullmatch(comment)
    if match is None:
        return None
    try:
        year, month, day = map(int, match.groups())
        moment = datetime(year, month, day).astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.isoformat(timespec="seconds")


def _build_drive(
    catalog: Catalog, drive: vcdcase.CaseDrive, work_dir: Path | None, index: int, source_name: str, file_time: str, is_cancelled
) -> tuple[NewDrive, int]:
    has_volume = bool(drive.total_bytes)
    volume = VolumeInfo(
        root=IMPORT_ROOT,
        drive_type=_MEDIA_DRIVE_TYPES.get(drive.media_type, "unknown"),
        ready=True,
        label=drive.label,
        serial=drive.serial,
        filesystem=drive.filesystem,
        total_bytes=drive.total_bytes if has_volume else None,
        free_bytes=drive.free_bytes if has_volume else None,
    )
    # グループは 1 段だけなので、入れ子になっていた場合は最も外側のグループに入れる
    group = drive.group[0] if drive.group else ""
    group_comment = drive.group_comments[0] if drive.group_comments else ""
    scanned_at = _registered_at(drive.comment) or _registered_at(group_comment) or file_time
    meta = {f"volume_{key}": value for key, value in volume.to_dict().items()}
    meta.update(
        scanned_at=scanned_at,
        source=SOURCE_VCDCASE,
        imported_from=source_name,
        vcdcase_media_type=drive.media_type,
        vcdcase_cluster_size=drive.cluster_size,
    )
    entries = (
        RawEntry(IMPORT_ROOT + entry.path, entry.is_dir, entry.size, entry.mtime, entry.ctime, entry.attrs)
        for entry in drive.entries
    )
    database: Path | bytes
    if work_dir is None:
        database, stats = build_drive_db_in_memory(
            IMPORT_ROOT, entries, memory_limit=catalog.memory_limit, spill_dir=catalog.session_dir,
            meta=meta, is_cancelled=is_cancelled,
        )  # fmt: skip
    else:
        database = work_dir / f"{index}-{FILES_DB}"
        stats = build_drive_db(database, IMPORT_ROOT, entries, meta=meta, is_cancelled=is_cancelled)
    try:
        context_db, context_count = _build_context_db(catalog, drive, database, work_dir, index, source_name)
    except BaseException:
        if isinstance(database, Path):
            remove_quietly(database)
        raise

    result = ScanResult(IMPORT_ROOT, SOURCE_VCDCASE, stats, volume, scanned_at, meta)
    extra = {
        key: value
        for key, value in (
            ("comment", drive.comment), ("category", drive.category), ("group", group), ("group_comment", group_comment),
        )
        if value
    }
    return NewDrive(database, result, drive.label or tr('(名前なし)'), context_db, extra), context_count


def _build_context_db(
    catalog: Catalog, drive: vcdcase.CaseDrive, files_db: Path | bytes, work_dir: Path | None, index: int, source_name: str
) -> tuple[Path | bytes | None, int]:
    """コメントなどを持つエントリがあれば context.db を作る。戻り値は (DB, 取り込んだエントリ数)。"""
    wanted = {entry.path: entry for entry in drive.entries if entry.has_context}
    if not wanted:
        return None, 0
    from .context.base import EntryWriter
    from .context.context_db import finish_context_db, init_context_db, write_result

    if work_dir is None:
        work = WorkDb.in_memory(catalog.memory_limit, catalog.session_dir, "context")
    else:
        work = WorkDb(work_dir / f"{index}-{CONTEXT_DB}")
    owner: sqlite3.Connection | None = None
    files: sqlite3.Connection | None = None
    try:
        if isinstance(files_db, bytes):
            source, owner = open_shared_memory_db(files_db)
        else:
            source = readonly_uri(files_db)
        files = sqlite3.connect(source, uri=True)
        keep_temp_in_memory(files)

        init_context_db(work.conn)
        work.conn.execute("BEGIN")
        count = 0
        dir_paths = {ROOT_ID: ""}  # id は先行順なので、親フォルダは必ず先に現れる
        for entry_id, parent_id, name, is_dir in files.execute("SELECT id, parent_id, name, is_dir FROM entries ORDER BY id"):
            parent = dir_paths[parent_id]
            entry_path = f"{parent}\\{name}" if parent else name
            if is_dir:
                dir_paths[entry_id] = entry_path
            entry = wanted.get(entry_path)
            if entry is None or bool(is_dir) != entry.is_dir:
                continue
            writer = EntryWriter(entry_id, CONTEXT_KIND)
            _fill_writer(writer, entry)
            if writer.is_empty:
                continue
            write_result(work.conn, writer)
            count += 1
            if count % _COMMIT_INTERVAL == 0:
                work.conn.execute("COMMIT")
                work.checkpoint()  # メモリ上で大きくなりすぎたら一時ファイルへ退避 (接続が入れ替わる)
                work.conn.execute("BEGIN")
        meta = {
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kinds": [CONTEXT_KIND],
            "imported_from": source_name,
        }
        finish_context_db(work.conn, meta)
        work.conn.execute("COMMIT")
        work.checkpoint()
        files.close()
        files = None
        return work.finish(), count
    except BaseException:
        work.discard()
        raise
    finally:
        if files is not None:
            files.close()
        if owner is not None:
            owner.close()


def _fill_writer(writer, entry: vcdcase.CaseEntry) -> None:
    if entry.comment:
        encoding, text = vcdcase.decode_text(entry.comment)
        if text.strip():
            writer.set_text(encoding, text, entry.comment)
    if entry.category:
        writer.add_meta("category", entry.category)
    for key, value in vcdcase.property_items(entry.properties):
        writer.add_meta(key, value)
    if entry.crc is not None:
        writer.add_meta("crc32", f"{entry.crc:08X}")
    if entry.inner:
        for inner_path, size, mtime, is_dir in entry.inner:
            writer.add_inner(inner_path, size, mtime, is_dir)
        writer.add_meta("inner_count", len(entry.inner))
