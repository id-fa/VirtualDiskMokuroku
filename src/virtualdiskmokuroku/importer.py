"""他のカタログソフトのデータの取り込み(GUI と CLI で共用)。

対応しているのは Virtual CD-ROM Case の .cas / .caz (``core/vcdcase.py``。圧縮して保存したものも可) とメディアステンシルの .fsr
(``core/stencil.py``)。どちらも、ドライブごとに files.db (と、コメントなどがあれば context.db) を作ってカタログへ
新規登録する。

- Virtual CD-ROM Case: コメントはテキスト内容として、プロパティ(HTML のタイトルや実行ファイルのバージョン情報)・
  分類・CRC32 はメタ情報として取り込む
- メディアステンシル: 記録されたアイコンはサムネイルとして取り込む。メディアのコメントは「ドライブ情報」に入る

中身を展開して登録されていた書庫は、このアプリの書庫の扱いに合わせて、書庫内リストを持つ 1 つのファイルとして
取り込む(書庫内のエントリが持つ CRC などは取り込まない)。
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
from functools import partial
from pathlib import Path

from .core import stencil, vcdcase
from .core.catalog import CONTEXT_DB, FILES_DB, Catalog, NewDrive
from .core.drive_db import ROOT_ID, ProgressCallback, build_drive_db, build_drive_db_in_memory, readonly_uri
from .core.errors import CatalogError, ScanCancelled
from .core.es_client import RawEntry
from .core.scanner import ScanResult
from .core.volume import VolumeInfo
from .core.workdb import WorkDb, keep_temp_in_memory, open_shared_memory_db, remove_quietly
from .i18n import tr

SOURCE_VCDCASE = "vcdcase"  # ドライブの取得元 (source) と、拡張コンテキストの種別 (kind) に使う
SOURCE_STENCIL = "stencil"
CONTEXT_KIND = SOURCE_VCDCASE
# どちらの形式にもドライブレターが記録されていないので、スキャン時のパスは "?:\" で始める
IMPORT_ROOT = "?:\\"
STENCIL_EXTENSION = ".fsr"

_MEDIA_DRIVE_TYPES = {1: "cdrom"}
_FLUSH_BYTES = 64 * 1024 * 1024  # 作った DB がこれだけ溜まるごとにカタログへ書き込む
_COMMIT_INTERVAL = 500
_DATE_COMMENT = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})")
_ICON_SIZE = 32


@dataclass(slots=True)
class ImportOutcome:
    drives: list[dict] = field(default_factory=list)  # 登録したドライブ (カタログ上の情報)
    file_count: int = 0
    dir_count: int = 0
    total_size: int = 0
    context_count: int = 0  # コメントなどを取り込んだエントリの数


@dataclass(slots=True)
class _DriveSpec:
    """取り込み元の形式によらない、ドライブ 1 台分の登録内容。"""

    name: str
    root: str  # "?:\\" またはフォルダ単位の記録なら "?:\\フォルダ"
    source: str  # ドライブの取得元。拡張コンテキストの種別 (kind) にも使う
    volume: VolumeInfo
    scanned_at: str
    meta: dict
    extra: dict
    entries: list[RawEntry]
    # 拡張コンテキストを持つエントリ: 相対パス -> (フォルダか, EntryWriter に書き込む関数)
    context: dict[str, tuple[bool, Callable]]


def import_catalog(
    catalog: Catalog,
    path: str | os.PathLike[str],
    *,
    progress: ProgressCallback | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> ImportOutcome:
    """他のカタログソフトのファイル (.cas / .caz / .fsr) の全ドライブを ``catalog`` へ新規登録する。形式は中身で判定する。

    進捗は ``progress(phase, count)`` で次の順に通知する (スペックの低い PC では各段階に時間がかかるので、
    画面には今どの作業をしているかを出す):

    - ``decompress`` (圧縮後のサイズ): 圧縮された .cas / .caz を展開している (圧縮されていない場合は来ない)
    - ``parse`` (読み取ったレコード数): ファイルの中身を読んでいる
    - ``import_total`` (ドライブ数)
    - ドライブごとに ``drive:<名前>`` (エントリ数) → ``read`` / ``build`` (DB に入れたエントリ数) →
      ``context`` (コメントなどを取り込んだエントリ数。無ければ来ない) → ``import`` (処理済みのドライブ数)
    - ``save`` (0): カタログへ書き込んでいる (ドライブが多い場合は途中でも来る)

    キャンセルすると ``ScanCancelled`` を送出する(ドライブが多い場合は途中で何度かカタログへ書き込むので、
    それまでに書き込んだドライブは登録されたまま残る)。

    暗号化カタログでは DB をメモリ上で作り、平文をディスクに書かない(``pipeline.scan_into_catalog`` と同じ)。
    """
    path = Path(path)
    with open(path, "rb") as stream:
        data = stream.read()
    file_time = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    if stencil.is_stencil(data) or path.suffix.lower() == STENCIL_EXTENSION:
        record = stencil.parse_stencil(data, is_cancelled=is_cancelled, progress=progress)
        specs = [_stencil_spec(root, path.name, file_time) for root in record.roots]
    else:
        case = vcdcase.parse_case(data, is_cancelled=is_cancelled, progress=progress, path=path)
        specs = [_vcdcase_spec(drive, path.name, file_time) for drive in case.drives]
    del data
    if not specs:
        raise CatalogError(tr('取り込めるドライブがありません: {path}').format(path=path))

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
        if progress:
            progress("save", 0)
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
            progress("import_total", len(specs))
        for index, spec in enumerate(specs):
            if is_cancelled is not None and is_cancelled():
                raise ScanCancelled()
            if progress:
                progress(f"drive:{spec.name}", len(spec.entries))
            item, context_count = _build_drive(catalog, spec, work_dir, index, path.name, progress, is_cancelled)
            pending.append(item)
            pending_bytes += _size(item.database) + _size(item.context_db)
            outcome.context_count += context_count
            if progress:
                progress("import", index + 1)
            if pending_bytes >= _FLUSH_BYTES:
                flush()
        flush()
        return outcome
    finally:
        discard_pending()
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)


import_vcdcase = import_catalog  # 旧名


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


def _volume_meta(volume: VolumeInfo) -> dict:
    return {f"volume_{key}": value for key, value in volume.to_dict().items()}


# --------------------------------------------------------------------------------------
# Virtual CD-ROM Case
# --------------------------------------------------------------------------------------


def _vcdcase_spec(drive: vcdcase.CaseDrive, source_name: str, file_time: str) -> _DriveSpec:
    has_volume = bool(drive.total_bytes)
    # フォルダ登録は "?:\フォルダ" をルートにする (このアプリでフォルダをスキャンした場合と同じ扱い)
    scan_root = IMPORT_ROOT + drive.folder if drive.folder else IMPORT_ROOT
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
    meta = _volume_meta(volume)
    meta.update(
        scanned_at=scanned_at,
        source=SOURCE_VCDCASE,
        imported_from=source_name,
        vcdcase_media_type=drive.media_type,
        vcdcase_cluster_size=drive.cluster_size,
    )
    prefix = scan_root.rstrip("\\") + "\\"
    entries = [
        RawEntry(prefix + entry.path, entry.is_dir, entry.size, entry.mtime, entry.ctime, entry.attrs)
        for entry in drive.entries
    ]
    context = {entry.path: (entry.is_dir, partial(_fill_vcdcase, entry)) for entry in drive.entries if entry.has_context}
    extra = {
        key: value
        for key, value in (
            ("comment", drive.comment), ("category", drive.category), ("group", group), ("group_comment", group_comment),
        )
        if value
    }  # fmt: skip
    name = drive.label or (drive.folder.rsplit("\\", 1)[-1] if drive.folder else "") or tr('(名前なし)')
    return _DriveSpec(name, scan_root, SOURCE_VCDCASE, volume, scanned_at, meta, extra, entries, context)


def _fill_vcdcase(entry: vcdcase.CaseEntry, writer) -> None:
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


# --------------------------------------------------------------------------------------
# メディアステンシル
# --------------------------------------------------------------------------------------


def _stencil_spec(root: stencil.StencilRoot, source_name: str, file_time: str) -> _DriveSpec:
    # フォルダ単位の記録は "?:\フォルダ" をルートにする (このアプリでフォルダをスキャンした場合と同じ扱い)
    scan_root = IMPORT_ROOT + root.folder.strip("\\") if root.folder else IMPORT_ROOT
    volume = VolumeInfo(root=IMPORT_ROOT, drive_type=root.drive_type, ready=True, label=root.label, serial=root.serial)
    if root.recorded_at:
        scanned_at = datetime.fromtimestamp(root.recorded_at, timezone.utc).isoformat(timespec="seconds")
    else:
        scanned_at = file_time
    meta = _volume_meta(volume)
    meta.update(
        scanned_at=scanned_at,
        source=SOURCE_STENCIL,
        imported_from=source_name,
        stencil_used_bytes=root.used_bytes,
        stencil_folder_count=root.folder_count,
        stencil_file_count=root.file_count,
    )
    prefix = scan_root.rstrip("\\") + "\\"
    entries = [RawEntry(prefix + entry.path, entry.is_dir, entry.size, entry.mtime, None, entry.attrs) for entry in root.entries]
    context = {
        entry.path: (entry.is_dir, partial(_fill_stencil, entry, root.icons)) for entry in root.entries if entry.has_context
    }
    extra = {"comment": root.comment} if root.comment else {}
    return _DriveSpec(root.title or tr('(名前なし)'), scan_root, SOURCE_STENCIL, volume, scanned_at, meta, extra, entries, context)


def _fill_stencil(entry: stencil.StencilEntry, icons: list[bytes], writer) -> None:
    if 0 < entry.icon <= len(icons):
        png = stencil.icon_png(icons[entry.icon - 1], _ICON_SIZE)
        if png is not None:
            writer.set_thumb(_ICON_SIZE, _ICON_SIZE, png)
    if entry.inner:
        for inner_path, size, mtime, is_dir in entry.inner:
            writer.add_inner(inner_path, size, mtime, is_dir)
        writer.add_meta("inner_count", len(entry.inner))


# --------------------------------------------------------------------------------------
# DB の作成
# --------------------------------------------------------------------------------------


def _build_drive(
    catalog: Catalog, spec: _DriveSpec, work_dir: Path | None, index: int, source_name: str, progress, is_cancelled
) -> tuple[NewDrive, int]:
    database: Path | bytes
    if work_dir is None:
        database, stats = build_drive_db_in_memory(
            spec.root, spec.entries, memory_limit=catalog.memory_limit, spill_dir=catalog.session_dir,
            meta=spec.meta, progress=progress, is_cancelled=is_cancelled,
        )  # fmt: skip
    else:
        database = work_dir / f"{index}-{FILES_DB}"
        stats = build_drive_db(
            database, spec.root, spec.entries, meta=spec.meta, progress=progress, is_cancelled=is_cancelled
        )
    try:
        context_db, context_count = _build_context_db(
            catalog, spec, database, work_dir, index, source_name, progress, is_cancelled
        )
    except BaseException:
        if isinstance(database, Path):
            remove_quietly(database)
        raise

    result = ScanResult(spec.root, spec.source, stats, spec.volume, spec.scanned_at, spec.meta)
    return NewDrive(database, result, spec.name, context_db, spec.extra), context_count


def _build_context_db(
    catalog: Catalog,
    spec: _DriveSpec,
    files_db: Path | bytes,
    work_dir: Path | None,
    index: int,
    source_name: str,
    progress: ProgressCallback | None,
    is_cancelled: Callable[[], bool] | None,
) -> tuple[Path | bytes | None, int]:
    """コメントなどを持つエントリがあれば context.db を作る。戻り値は (DB, 取り込んだエントリ数)。

    進捗は ``progress("context", 取り込んだエントリ数)`` で通知する (サムネイルの変換などで時間がかかる)。
    """
    wanted = spec.context
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
            found = wanted.get(entry_path)
            if found is None or bool(is_dir) != found[0]:
                continue
            writer = EntryWriter(entry_id, spec.source)
            found[1](writer)
            if writer.is_empty:
                continue
            write_result(work.conn, writer)
            count += 1
            if count % _COMMIT_INTERVAL == 0:
                work.conn.execute("COMMIT")
                work.checkpoint()  # メモリ上で大きくなりすぎたら一時ファイルへ退避 (接続が入れ替わる)
                work.conn.execute("BEGIN")
                if is_cancelled is not None and is_cancelled():
                    raise ScanCancelled()
                if progress:
                    progress("context", count)
        if progress:
            progress("context", count)
        meta = {
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kinds": [spec.source],
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
