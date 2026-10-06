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
from .core.volume import get_volume_info, list_volumes
from .importer import import_vcdcase
from .pipeline import scan_into_catalog


_opened: list[Catalog] = []  # 終了時に閉じる(暗号化カタログの復号済みデータを手放す)


def _open_catalog(path: str, password: str | None, create: bool = False, encrypt: bool = False) -> Catalog:
    if create and not Path(path).exists():
        if encrypt and not password:
            raise CatalogError("--encrypt には --password の指定が必要です")
        catalog = Catalog.create(path, password, encrypt=encrypt)
    else:
        catalog = Catalog.open(path, password)
    _opened.append(catalog)
    return catalog


def cmd_volumes(_args: argparse.Namespace) -> int:
    for volume in list_volumes():
        if not volume.ready:
            print(f"{volume.root}  ({volume.drive_type}, メディアなし)")
            continue
        device = " ".join(part for part in (volume.device_vendor, volume.device_model) if part)
        print(
            f"{volume.root}  {volume.label or '-':<16} {volume.filesystem:<6} {volume.serial}  "
            f"{volume.drive_type:<9} 空き {format_size(volume.free_bytes)} / {format_size(volume.total_bytes)}  "
            f"[{volume.bus_type}] {device}"
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
            print("同じボリュームとみなせるドライブが複数あります。--new を付けるか GUI で選択してください。", file=sys.stderr)
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

    action = "更新" if drive_id else "追加"
    print(
        f"{action}: {outcome.drive['name']}  取得元={result.source}  ファイル {result.stats.file_count:,} / "
        f"フォルダ {result.stats.dir_count:,} / 合計 {format_size(result.stats.total_size)}  "
        f"(無視 {result.stats.ignored_count:,} 件, {seconds:.1f} 秒)"
    )
    stats = outcome.context_stats
    if stats is not None:
        print(f"拡張コンテキスト: 取得 {stats.processed:,} 件 / 引き継ぎ {stats.reused:,} 件 / エラー {stats.errors:,} 件")  # type: ignore[attr-defined]
    for warning in result.warnings:
        print(f"警告: {warning}", file=sys.stderr)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    catalog = _open_catalog(args.catalog, args.password, create=True, encrypt=args.encrypt)
    outcome = import_vcdcase(catalog, args.source)
    for drive in outcome.drives:
        print(
            f"追加: {drive['name']}  ファイル {drive['file_count']:,} / フォルダ {drive['dir_count']:,} / "
            f"合計 {format_size(drive['total_size'])}"
        )
    print(f"{len(outcome.drives):,} ドライブを取り込みました (コメントなどの情報 {outcome.context_count:,} 件)")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    catalog = _open_catalog(args.catalog, args.password)
    if catalog.encrypted:
        print("(暗号化カタログ)")
    for drive in catalog.drives:
        group = f"{drive['group']} / " if drive.get("group") else ""
        print(
            f"{group}{drive['name']}  [{drive.get('label', '')} / {drive.get('serial', '')} / {drive.get('filesystem', '')}]  "
            f"ファイル {drive.get('file_count', 0):,}  合計 {format_size(drive.get('total_size'))}  "
            f"空き {format_size(drive.get('free_bytes'))} / {format_size(drive.get('total_bytes'))}  "
            f"スキャン {format_iso(drive.get('scanned_at'))} ({drive.get('source', '')})  "
            f"バックアップ {len(drive.get('backups', []))} 世代"
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
    print(f"{count:,} 件", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="virtualdiskmokuroku", description="オフライン ファイルリスト カタログ")
    parser.add_argument("--version", action="version", version=f"VirtualDiskMokuroku {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("volumes", help="接続中のボリューム一覧").set_defaults(func=cmd_volumes)

    scan = sub.add_parser("scan", help="ドライブをスキャンしてカタログに追加/更新")
    scan.add_argument("root", help="スキャン対象 (例: E:\\)")
    scan.add_argument("catalog", help="カタログファイル (.vdmoku)。無ければ作成")
    scan.add_argument("--source", choices=[scanner.SOURCE_AUTO, scanner.SOURCE_EVERYTHING, scanner.SOURCE_WALK], default=scanner.SOURCE_AUTO)
    scan.add_argument("--es", help="es.exe のパス")
    scan.add_argument("--instance", help="Everything のインスタンス名")
    scan.add_argument("--name", help="カタログ上の表示名")
    scan.add_argument("--new", action="store_true", help="既存ドライブと照合せず新規に追加")
    scan.add_argument("--encrypt", action="store_true", help="カタログを新規作成する場合に暗号化する (--password が必要)")
    scan.add_argument("--password")
    scan.set_defaults(func=cmd_scan)

    import_ = sub.add_parser("import", help="Virtual CD-ROM Case のカタログ (.cas) を取り込む")
    import_.add_argument("source", help="取り込む .cas ファイル")
    import_.add_argument("catalog", help="カタログファイル (.vdmoku)。無ければ作成")
    import_.add_argument("--encrypt", action="store_true", help="カタログを新規作成する場合に暗号化する (--password が必要)")
    import_.add_argument("--password")
    import_.set_defaults(func=cmd_import)

    list_ = sub.add_parser("list", help="カタログ内のドライブ一覧")
    list_.add_argument("catalog")
    list_.add_argument("--password")
    list_.set_defaults(func=cmd_list)

    find = sub.add_parser("find", help="カタログ内の全ドライブを名前で検索")
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
        print(f"エラー: {error}", file=sys.stderr)
        return 1
    finally:
        for catalog in _opened:
            catalog.close()
        _opened.clear()
