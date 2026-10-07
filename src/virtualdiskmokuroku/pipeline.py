"""スキャン → 拡張コンテキスト取得 → カタログ保存の一連の処理(GUI と CLI で共用)。"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .core import scanner
from .core.catalog import CONTEXT_DB, FILES_DB, Catalog
from .core.drive_db import ProgressCallback
from .core.es_client import EsClient
from .core.ignore import IgnoreRules
from .core.workdb import open_shared_memory_db, remove_quietly
from .i18n import tr


@dataclass(slots=True)
class ScanOutcome:
    drive: dict  # カタログ上のドライブ情報
    result: scanner.ScanResult
    context_stats: object | None = None  # context.runner.ContextStats (拡張コンテキスト無効時は None)


def enabled_context_settings(catalog: Catalog) -> dict:
    """有効な拡張コンテキスト設定だけを返す(1 つも無ければ空)。"""
    return {kind: params for kind, params in catalog.settings.get("context", {}).items() if params.get("enabled")}


def scan_into_catalog(
    catalog: Catalog,
    root: str,
    *,
    drive_id: str | None = None,
    name: str | None = None,
    source: str = scanner.SOURCE_AUTO,
    es: EsClient | None = None,
    progress: ProgressCallback | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> ScanOutcome:
    """``root`` をスキャンしてカタログへ追加する。``drive_id`` 指定時はそのドライブを更新する。

    進捗は ``progress(phase, count)`` で通知する。phase は ``source:<取得元>`` / ``read`` / ``build`` /
    ``context_total`` / ``context`` / ``save``。

    ファイルリスト取得中のキャンセルは ``ScanCancelled`` を送出して何も登録しない。拡張コンテキスト取得中の
    キャンセルは、取得できた分までを登録する(``context_stats.cancelled`` が真になる)。

    暗号化カタログでは DB をメモリ上で作り、平文をディスクに書かない(メモリの上限を超える場合だけ、
    カタログのセッション用一時フォルダを使い、終わったら削除する)。
    """
    settings = catalog.settings
    scan_settings = settings.get("scan", {})
    secure = catalog.encrypted
    work_dir = None if secure else Path(tempfile.mkdtemp(prefix="vdmoku_scan_"))
    artifacts: list[bytes | Path] = []
    memory_owner: sqlite3.Connection | None = None
    result: scanner.ScanResult | None = None
    try:
        result = scanner.scan_to_db(
            root,
            None if work_dir is None else work_dir / FILES_DB,
            es=es,
            source=source,
            ignore=IgnoreRules(settings.get("ignore_patterns", [])),
            with_ctime=scan_settings.get("with_ctime", True),
            with_attrs=scan_settings.get("with_attrs", True),
            progress=progress,
            is_cancelled=is_cancelled,
            memory_limit=catalog.memory_limit,
            spill_dir=catalog.session_dir,
        )
        files_db = result.database
        assert files_db is not None
        artifacts.append(files_db)
        warnings = result.warnings

        context_db: bytes | Path | None = None
        context_stats = None
        context_settings = enabled_context_settings(catalog)
        if context_settings:
            from .context import EXTRACTORS
            from .context.runner import build_context_db, build_context_db_in_memory, unavailable_kinds

            missing = unavailable_kinds(context_settings)
            if missing:
                labels = tr("、").join(tr(EXTRACTORS[kind].label) for kind in missing)
                warnings.append(tr('必要なライブラリが無いため取得しなかった拡張コンテキストがあります: {labels}').format(labels=labels))

            previous = None
            if drive_id and catalog.drive(drive_id).get("has_context"):
                previous_context = catalog.context_source(drive_id)
                if previous_context is not None:
                    previous = (catalog.db_source(drive_id), previous_context)
            common = {"previous": previous, "progress": progress, "is_cancelled": is_cancelled}
            if work_dir is not None:
                context_db = work_dir / CONTEXT_DB
                context_stats = build_context_db(files_db, context_db, root, context_settings, **common)
            else:
                files_source: str | Path
                if isinstance(files_db, bytes):
                    files_source, memory_owner = open_shared_memory_db(files_db)
                else:
                    files_source = files_db
                context_db, context_stats = build_context_db_in_memory(
                    files_source,
                    root,
                    context_settings,
                    memory_limit=catalog.memory_limit,
                    spill_dir=catalog.session_dir,
                    **common,
                )
                artifacts.append(context_db)

        if progress:
            progress("save", 0)
        drive = catalog.put_drive(
            files_db,
            result,
            drive_id=drive_id,
            name=name or None,
            context_db_path=context_db,
            context_partial=bool(context_stats and context_stats.cancelled),
        )
        return ScanOutcome(drive, result, context_stats)
    finally:
        if memory_owner is not None:
            memory_owner.close()
        if result is not None:
            result.database = None  # 大きなバイト列を結果オブジェクトに抱えたままにしない
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
            for artifact in artifacts:
                if isinstance(artifact, Path):
                    remove_quietly(artifact)  # 上限超過で一時フォルダへ退避した DB
