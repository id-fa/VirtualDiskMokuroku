"""拡張コンテキストの抽出実行(ファイルリスト取得後の第 2 パス)。

files.db に載っているファイルのうち、有効な抽出器が対象とするものを実際に読んで context.db を作る。
ファイル内容を読むので対象ドライブが接続されている必要がある。再スキャン時は、前回から変わっていない
ファイル(相対パス・サイズ・更新日時が同じ)の結果を前回の context.db から引き継いで読み直しを避ける。
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..core.drive_db import ROOT_ID, DriveDB
from . import EXTRACTORS
from .base import EntryWriter, Extractor
from .context_db import STATUS_OK, create_context_db, finish_context_db, load_result, write_error, write_result

ProgressCallback = Callable[[str, int], None]

_COMMIT_INTERVAL = 500
_PROGRESS_INTERVAL = 20
_LONG_PATH = 248


@dataclass(slots=True)
class ContextStats:
    processed: int = 0  # 実ファイルを読んだ件数
    reused: int = 0  # 前回結果をそのまま引き継いだ件数
    errors: int = 0  # 抽出に失敗した (ファイル, 種別) の数
    by_kind: dict[str, int] = field(default_factory=dict)  # 種別ごとの成功件数(引き継ぎを含む)
    cancelled: bool = False  # 途中でキャンセルされた(DB には取得済みの分だけが入っている)


def active_extractors(settings: dict | None) -> list[Extractor]:
    """設定で有効かつ依存ライブラリが揃っている抽出器を表示順に生成する。"""
    extractors: list[Extractor] = []
    for kind, cls in EXTRACTORS.items():
        config = (settings or {}).get(kind) or {}
        if config.get("enabled") and cls.available():
            extractors.append(cls(config))
    return extractors


def unavailable_kinds(settings: dict | None) -> list[str]:
    """設定では有効だが依存ライブラリが無くて実行できない種別。"""
    return [
        kind for kind, cls in EXTRACTORS.items() if ((settings or {}).get(kind) or {}).get("enabled") and not cls.available()
    ]


def _remove_quietly(path: str | os.PathLike[str]) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _open_readonly(path: str | os.PathLike[str]) -> sqlite3.Connection:
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)


def _real_path(root: str, rel_path: str) -> str:
    path = root + rel_path if root.endswith("\\") else f"{root}\\{rel_path}"
    if len(path) >= _LONG_PATH and not path.startswith("\\\\"):
        path = "\\\\?\\" + path  # MAX_PATH を超えるパスも開けるようにする
    return path


class _Previous:
    """前回の files.db / context.db から、変わっていないファイルの抽出結果を探す。"""

    def __init__(self, files_db_path, context_db_path, extractors: list[Extractor]):
        self.files = _open_readonly(files_db_path)
        try:
            self.context = _open_readonly(context_db_path)
        except BaseException:
            self.files.close()
            raise
        self._dir_ids: dict[int, int | None] = {ROOT_ID: ROOT_ID}  # 今回のフォルダ id → 前回のフォルダ id
        try:
            meta = {key: json.loads(value) for key, value in self.context.execute("SELECT key, value FROM meta")}
        except (sqlite3.Error, ValueError):
            meta = {}
        # 抽出器のパラメータが前回と同じ種別だけ引き継げる
        self.kinds = {ex.kind for ex in extractors if meta.get(f"params:{ex.kind}") == ex.params}

    def close(self) -> None:
        self.files.close()
        self.context.close()

    def _dir_id(self, db: DriveDB, dir_id: int) -> int | None:
        cache = self._dir_ids
        if dir_id in cache:
            return cache[dir_id]
        # 未解決の祖先をたどり、上から順に前回のフォルダ id へ対応づける
        chain: list[tuple[int, str]] = []
        current = dir_id
        while current not in cache:
            entry = db.get(current)
            if entry is None:
                cache[dir_id] = None
                return None
            chain.append((current, entry.name))
            current = entry.parent_id
        previous_id = cache[current]
        for current_id, name in reversed(chain):
            if previous_id is not None:
                row = self.files.execute(
                    "SELECT id FROM entries WHERE parent_id = ? AND name = ? COLLATE NOCASE AND is_dir = 1",
                    (previous_id, name),
                ).fetchone()
                previous_id = row[0] if row else None
            cache[current_id] = previous_id
        return previous_id

    def find_unchanged(self, db: DriveDB, parent_id: int, name: str, size, mtime) -> int | None:
        """同じ場所に同じサイズ・更新日時のファイルが前回もあれば、その entry_id を返す。"""
        if size is None or mtime is None:
            return None
        previous_dir = self._dir_id(db, parent_id)
        if previous_dir is None:
            return None
        row = self.files.execute(
            "SELECT id, size, mtime FROM entries WHERE parent_id = ? AND name = ? COLLATE NOCASE AND is_dir = 0",
            (previous_dir, name),
        ).fetchone()
        if row is None or row[1] != size or row[2] != mtime:
            return None
        return row[0]

    def done_kinds(self, entry_id: int) -> set[str]:
        cursor = self.context.execute("SELECT kind FROM done WHERE entry_id = ? AND status = ?", (entry_id, STATUS_OK))
        return {row[0] for row in cursor} & self.kinds


def build_context_db(
    files_db_path: str | os.PathLike[str],
    context_db_path: str | os.PathLike[str],
    scan_root: str,
    settings: dict,
    *,
    previous: tuple[str | os.PathLike[str], str | os.PathLike[str]] | None = None,
    progress: ProgressCallback | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> ContextStats:
    """``files_db_path`` のファイルから拡張コンテキストを抽出し ``context_db_path`` に新しく作る。

    ``scan_root`` は対象ドライブの現在の場所("F:\\" やフォルダパス)。``settings`` はカタログ設定の
    ``settings["context"]``。``previous`` に (前回の files.db, 前回の context.db) を渡すと未変更ファイルの結果を引き継ぐ。
    キャンセルされた場合は例外にせず、そこまでに取得できた分(と、前回結果から引き継げる分)で DB を完成させ
    ``cancelled=True`` の統計を返す。続きは次回、この DB を ``previous`` に渡せば未取得のファイルだけが読まれる。
    失敗時は作りかけの DB を残さない。
    """
    context_db_path = Path(context_db_path)
    _remove_quietly(context_db_path)
    try:
        return _build(files_db_path, context_db_path, scan_root, settings, previous, progress, is_cancelled)
    except BaseException:
        _remove_quietly(context_db_path)
        raise


def _build(files_db_path, context_db_path, scan_root, settings, previous, progress, is_cancelled) -> ContextStats:
    def cancel_requested() -> bool:
        return is_cancelled is not None and is_cancelled()

    extractors = active_extractors(settings)
    root = os.path.abspath(scan_root)
    stats = ContextStats(by_kind={extractor.kind: 0 for extractor in extractors})

    with DriveDB(files_db_path) as db:
        # --- 1. 対象ファイルの洗い出し ---------------------------------------------------
        targets: list[tuple[int, int, str, int | None, int | None, tuple[Extractor, ...]]] = []
        if extractors:
            for count, entry in enumerate(db.iter_subtree(ROOT_ID), 1):
                if count % 20000 == 0 and cancel_requested():
                    stats.cancelled = True
                    break
                if entry.is_dir:
                    continue
                accepted = tuple(ex for ex in extractors if ex.accepts(entry.name, entry.size))
                if accepted:
                    targets.append((entry.id, entry.parent_id, entry.name, entry.size, entry.mtime, accepted))
        if progress:
            progress("context_total", len(targets))

        # --- 2. 抽出(前回結果があれば引き継ぐ) ------------------------------------------
        conn = create_context_db(context_db_path)
        old: _Previous | None = None
        try:
            if previous is not None and extractors and all(os.path.isfile(path) for path in previous):
                try:
                    old = _Previous(previous[0], previous[1], extractors)
                except sqlite3.Error:
                    old = None  # 前回分が読めなければ全件読み直す

            conn.execute("BEGIN")
            for index, (entry_id, parent_id, name, size, mtime, accepted) in enumerate(targets, 1):
                if not stats.cancelled and cancel_requested():
                    stats.cancelled = True
                if stats.cancelled and (old is None or not old.kinds):
                    break  # 引き継げる前回結果も無いので、ここで打ち切る
                remaining = list(accepted)
                if old is not None and old.kinds:
                    previous_id = old.find_unchanged(db, parent_id, name, size, mtime)
                    if previous_id is not None:
                        done = old.done_kinds(previous_id)
                        for extractor in accepted:
                            if extractor.kind in done:
                                carried = load_result(old.context, previous_id, extractor.kind, extractor.stores, entry_id)
                                write_result(conn, carried)
                                stats.by_kind[extractor.kind] += 1
                                remaining.remove(extractor)

                if not remaining:
                    stats.reused += 1
                elif stats.cancelled:
                    pass  # キャンセル後はファイルを読まない(前回結果の引き継ぎだけ続け、残りは次回の更新で取得する)
                else:
                    stats.processed += 1
                    directory = db.dir_path(parent_id)
                    path = _real_path(root, f"{directory}\\{name}" if directory else name)
                    for extractor in remaining:
                        writer = EntryWriter(entry_id, extractor.kind)
                        try:
                            extractor.extract(path, writer)
                        except Exception as error:  # noqa: BLE001 - ロック中・破損などはファイル単位で記録して続行
                            detail = str(error) or getattr(error, "strerror", "") or ""
                            write_error(conn, entry_id, extractor.kind, f"{type(error).__name__}: {detail}"[:500])
                            stats.errors += 1
                        else:
                            write_result(conn, writer)
                            stats.by_kind[extractor.kind] += 1

                if index % _COMMIT_INTERVAL == 0:
                    conn.execute("COMMIT")
                    conn.execute("BEGIN")
                if progress and not stats.cancelled and index % _PROGRESS_INTERVAL == 0:
                    progress("context", index)

            meta: dict = {f"params:{extractor.kind}": extractor.params for extractor in extractors}
            meta.update(
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                scan_root=root,
                kinds=[extractor.kind for extractor in extractors],
                unavailable=unavailable_kinds(settings),
                processed=stats.processed,
                reused=stats.reused,
                errors=stats.errors,
                partial=stats.cancelled,
            )
            finish_context_db(conn, meta)
            conn.execute("COMMIT")
            if progress and not stats.cancelled:
                progress("context", len(targets))
        finally:
            conn.close()
            if old is not None:
                old.close()
    return stats
