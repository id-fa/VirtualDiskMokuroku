"""コマンドラインインターフェース(スキャンの自動化や動作確認用)。"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__
from .core import scanner
from .core.catalog import CATALOG_EXTENSION, LEGACY_CATALOG_EXTENSIONS, Catalog, cleanup_stale_sessions
from .core.drive_db import ROOT_ID, split_terms
from .core.errors import CatalogError
from .core.es_client import EsClient, EsError, find_es_exe
from .core.formatting import format_iso, format_size
from .core.settings import AppSettings
from .core.volume import get_volume_info, list_volumes
from .i18n import set_language, tr
from .importer import import_vcdcase
from .pipeline import scan_into_catalog


_opened: list[Catalog] = []  # 終了時に閉じる(暗号化カタログの復号済みデータを手放す)


def _open_catalog(path: str, password: str | None, create: bool = False, encrypt: bool = False) -> Catalog:
    if create and not Path(path).exists():
        if encrypt and not password:
            raise CatalogError(tr('--encrypt には --password の指定が必要です'))
        catalog = Catalog.create(path, password, encrypt=encrypt)
    else:
        catalog = Catalog.open(path, password)
    _opened.append(catalog)
    return catalog


def cmd_volumes(_args: argparse.Namespace) -> int:
    for volume in list_volumes():
        if not volume.ready:
            print(tr('{root}  ({drive_type}, メディアなし)').format(root=volume.root, drive_type=volume.drive_type))
            continue
        device = " ".join(part for part in (volume.device_vendor, volume.device_model) if part)
        print(
            tr('{root}  {0:<16} {filesystem:<6} {serial}  {drive_type:<9} 空き {free_bytes} / {total_bytes}  [{bus_type}] {device}').format(volume.label or '-', root=volume.root, filesystem=volume.filesystem, serial=volume.serial, drive_type=volume.drive_type, free_bytes=format_size(volume.free_bytes), total_bytes=format_size(volume.total_bytes), bus_type=volume.bus_type, device=device)
        )
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    catalog = _open_catalog(args.catalog, args.password, create=True, encrypt=args.encrypt)
    es_path = find_es_exe(args.es)
    es = EsClient(es_path, args.instance) if es_path and args.source != scanner.SOURCE_WALK else None

    drive_id = None
    if not args.new:
        matches = catalog.find_matching_drives(get_volume_info(args.root), args.root)
        if len(matches) > 1:
            print(tr('同じボリュームとみなせるドライブが複数あります。--new を付けるか GUI で選択してください。'), file=sys.stderr)
            return 2
        if matches:
            drive_id = matches[0]["id"]

    last_report = 0.0

    def progress(phase: str, count: int) -> None:
        nonlocal last_report
        now = time.monotonic()
        if phase.startswith("source:") or now - last_report > 1.0:
            last_report = now
            print(f"  {phase} {count:,}" if count else f"  {phase}", file=sys.stderr)

    started = time.monotonic()
    outcome = scan_into_catalog(
        catalog, args.root, drive_id=drive_id, name=args.name, source=args.source, es=es, progress=progress
    )
    seconds = time.monotonic() - started
    result = outcome.result

    action = tr('更新') if drive_id else tr('追加')
    print(
        tr('{action}: {name}  取得元={source}  ファイル {file_count:,} / フォルダ {dir_count:,} / 合計 {total_size}  (無視 {ignored_count:,} 件, {seconds:.1f} 秒)').format(action=action, name=outcome.drive['name'], source=result.source, file_count=result.stats.file_count, dir_count=result.stats.dir_count, total_size=format_size(result.stats.total_size), ignored_count=result.stats.ignored_count, seconds=seconds)
    )
    stats = outcome.context_stats
    if stats is not None:
        print(tr('拡張コンテキスト: 取得 {processed:,} 件 / 引き継ぎ {reused:,} 件 / エラー {errors:,} 件').format(processed=stats.processed, reused=stats.reused, errors=stats.errors))  # type: ignore[attr-defined]
    for warning in result.warnings:
        print(tr('警告: {warning}').format(warning=warning), file=sys.stderr)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    catalog = _open_catalog(args.catalog, args.password, create=True, encrypt=args.encrypt)
    outcome = import_vcdcase(catalog, args.source)
    for drive in outcome.drives:
        print(
            tr('追加: {name}  ファイル {file_count:,} / フォルダ {dir_count:,} / 合計 {total_size}').format(name=drive['name'], file_count=drive['file_count'], dir_count=drive['dir_count'], total_size=format_size(drive['total_size']))
        )
    print(tr('{drives_count:,} ドライブを取り込みました (コメントなどの情報 {context_count:,} 件)').format(drives_count=len(outcome.drives), context_count=outcome.context_count))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    catalog = _open_catalog(args.catalog, args.password)
    if catalog.encrypted:
        print(tr('(暗号化カタログ)'))
    for drive in catalog.drives:
        group = f"{drive['group']} / " if drive.get("group") else ""
        print(
            tr('{group}{name}  [{label} / {serial} / {filesystem}]  ファイル {file_count:,}  合計 {total_size}  空き {free_bytes} / {total_bytes}  スキャン {scanned_at} ({source})  バックアップ {backups_count} 世代').format(group=group, name=drive['name'], label=drive.get('label', ''), serial=drive.get('serial', ''), filesystem=drive.get('filesystem', ''), file_count=drive.get('file_count', 0), total_size=format_size(drive.get('total_size')), free_bytes=format_size(drive.get('free_bytes')), total_bytes=format_size(drive.get('total_bytes')), scanned_at=format_iso(drive.get('scanned_at')), source=drive.get('source', ''), backups_count=len(drive.get('backups', [])))
        )
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    catalog = _open_catalog(args.catalog, args.password)
    terms = split_terms(" ".join(args.terms))
    count = 0
    for drive in catalog.drives:
        with catalog.open_drive_db(drive["id"]) as db:
            for entry in db.iter_subtree(ROOT_ID, terms, limit=args.limit):
                print(f"[{drive['name']}] {db.full_path(entry)}{'\\' if entry.is_dir else ''}")
                count += 1
    print(tr('{count:,} 件').format(count=count), file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="virtualdiskmokuroku", description=tr('オフライン ファイルリスト カタログ'))
    parser.add_argument("--version", action="version", version=f"VirtualDiskMokuroku {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("volumes", help=tr('接続中のボリューム一覧')).set_defaults(func=cmd_volumes)

    scan = sub.add_parser("scan", help=tr('ドライブをスキャンしてカタログに追加/更新'))
    scan.add_argument("root", help=tr('スキャン対象 (例: E:\\)'))
    scan.add_argument("catalog", help=tr('カタログファイル (.vdmoku)。無ければ作成'))
    scan.add_argument("--source", choices=[scanner.SOURCE_AUTO, scanner.SOURCE_EVERYTHING, scanner.SOURCE_WALK], default=scanner.SOURCE_AUTO)
    scan.add_argument("--es", help=tr('es.exe のパス'))
    scan.add_argument("--instance", help=tr('Everything のインスタンス名'))
    scan.add_argument("--name", help=tr('カタログ上の表示名'))
    scan.add_argument("--new", action="store_true", help=tr('既存ドライブと照合せず新規に追加'))
    scan.add_argument("--encrypt", action="store_true", help=tr('カタログを新規作成する場合に暗号化する (--password が必要)'))
    scan.add_argument("--password")
    scan.set_defaults(func=cmd_scan)

    import_ = sub.add_parser("import", help=tr('Virtual CD-ROM Case のカタログ (.cas) を取り込む'))
    import_.add_argument("source", help=tr('取り込む .cas ファイル'))
    import_.add_argument("catalog", help=tr('カタログファイル (.vdmoku)。無ければ作成'))
    import_.add_argument("--encrypt", action="store_true", help=tr('カタログを新規作成する場合に暗号化する (--password が必要)'))
    import_.add_argument("--password")
    import_.set_defaults(func=cmd_import)

    list_ = sub.add_parser("list", help=tr('カタログ内のドライブ一覧'))
    list_.add_argument("catalog")
    list_.add_argument("--password")
    list_.set_defaults(func=cmd_list)

    find = sub.add_parser("find", help=tr('カタログ内の全ドライブを名前で検索'))
    find.add_argument("catalog")
    find.add_argument("terms", nargs="+")
    find.add_argument("--limit", type=int, default=1000)
    find.add_argument("--password")
    find.set_defaults(func=cmd_find)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    argv = sys.argv[1:] if argv is None else argv
    set_language(AppSettings.load().language)  # 文言は表示するたびに tr() で引くが、argparse のヘルプのため先に決めておく
    # 引数なし、またはカタログファイルだけを渡された場合は GUI を起動する
    if not argv or (len(argv) == 1 and argv[0].lower().endswith((CATALOG_EXTENSION, *LEGACY_CATALOG_EXTENSIONS))):
        from .ui.app import run_gui

        return run_gui(argv[0] if argv else None)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    cleanup_stale_sessions()
    try:
        return args.func(args)
    except (CatalogError, EsError, OSError) as error:
        print(tr('エラー: {error}').format(error=error), file=sys.stderr)
        return 1
    finally:
        for catalog in _opened:
            catalog.close()
        _opened.clear()
