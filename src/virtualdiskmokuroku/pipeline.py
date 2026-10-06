"""スキャン → 拡張コンテキスト取得 → カタログ保存の一連の処理(GUI と CLI で共用)。"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .core import scanner
from .core.catalog import CONTEXT_DB, FILES_DB, Catalog
from .core.drive_db import ProgressCallback
from .core.es_client import EsClient
from .core.ignore import IgnoreRules


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
    """
    settings = catalog.settings
    scan_settings = settings.get("scan", {})
    work_dir = Path(tempfile.mkdtemp(prefix="vdmoku_scan_"))
    try:
        files_db = work_dir / FILES_DB
        result = scanner.scan_to_db(
            root,
            files_db,
            es=es,
            source=source,
            ignore=IgnoreRules(settings.get("ignore_patterns", [])),
            with_ctime=scan_settings.get("with_ctime", True),
            with_attrs=scan_settings.get("with_attrs", True),
            progress=progress,
            is_cancelled=is_cancelled,
        )
        warnings = result.warnings

        context_db = None
        context_stats = None
        context_settings = enabled_context_settings(catalog)
        if context_settings:
            from .context import EXTRACTORS
            from .context.runner import build_context_db, unavailable_kinds

            missing = unavailable_kinds(context_settings)
            if missing:
                labels = "、".join(EXTRACTORS[kind].label for kind in missing)
                warnings.append(f"必要なライブラリが無いため取得しなかった拡張コンテキストがあります: {labels}")

            previous = None
            if drive_id and catalog.drive(drive_id).get("has_context"):
                previous_context = catalog.extract_context_db(drive_id)
                if previous_context is not None:
                    previous = (catalog.extract_db(drive_id), previous_context)
            context_db = work_dir / CONTEXT_DB
            context_stats = build_context_db(
                files_db, context_db, root, context_settings, previous=previous, progress=progress, is_cancelled=is_cancelled
            )

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
        shutil.rmtree(work_dir, ignore_errors=True)
