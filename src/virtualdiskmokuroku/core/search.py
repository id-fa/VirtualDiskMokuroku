"""一覧・フィルタ・全ドライブ検索の共通クエリ層(UI のワーカースレッドとエクスポートの両方から使う)。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .drive_db import ROOT_ID, DriveDB, Entry

SCOPE_FOLDER = "folder"  # 表示中のフォルダ直下
SCOPE_SUBTREE = "subtree"  # 表示中のフォルダ以下すべて
SCOPE_ALL = "all"  # カタログ内の全ドライブ


@dataclass(frozen=True)
class DriveRef:
    drive_id: str
    name: str
    files_db: Path
    context_db: Path | None = None


@dataclass(frozen=True)
class QuerySpec:
    scope: str
    terms: tuple[str, ...]
    drives: tuple[DriveRef, ...]  # folder/subtree は表示中のドライブ 1 つ、all は全ドライブ
    dir_id: int = ROOT_ID
    limit: int | None = None
    include_context: bool = False  # 拡張コンテキスト(メタ情報・本文・書庫内リスト)も検索対象にする

    @property
    def effective_scope(self) -> str:
        """フィルタ文字列が無いときは常にフォルダ直下の一覧。"""
        return self.scope if self.terms else SCOPE_FOLDER


class Row(NamedTuple):
    drive_id: str
    drive_name: str
    entry: Entry
    location: str  # 格納フォルダのフルパス(スキャン時のドライブレター基準)

    @property
    def full_path(self) -> str:
        return self.location + self.entry.name if self.location.endswith("\\") else f"{self.location}\\{self.entry.name}"


class DbPool:
    """スレッド専用の DB 接続プール。``interrupt_all`` だけは他スレッドから呼んでよい。"""

    def __init__(self) -> None:
        self._files: dict[Path, DriveDB] = {}
        self._contexts: dict[Path, object] = {}

    def files(self, path: Path) -> DriveDB:
        db = self._files.get(path)
        if db is None:
            db = self._files[path] = DriveDB(path)
        return db

    def context(self, path: Path):
        from ..context.context_db import ContextDB

        db = self._contexts.get(path)
        if db is None:
            db = self._contexts[path] = ContextDB(path)
        return db

    def interrupt_all(self) -> None:
        for db in list(self._files.values()) + list(self._contexts.values()):
            try:
                db.interrupt()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - 閉じた接続などは無視
                pass

    def close_all(self) -> None:
        for db in list(self._files.values()) + list(self._contexts.values()):
            try:
                db.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        self._files.clear()
        self._contexts.clear()


def _in_scope(entry: Entry, scope: str, dir_id: int, last_desc_id: int | None) -> bool:
    if scope == SCOPE_FOLDER:
        return entry.parent_id == dir_id
    if dir_id == ROOT_ID or scope == SCOPE_ALL:
        return True
    return last_desc_id is not None and dir_id < entry.id <= last_desc_id


def iter_rows(spec: QuerySpec, pool: DbPool) -> Iterator[Row]:
    """条件に合うエントリを順に返す。``spec.limit`` を超える分は呼び出し側で打ち切る(超過検出用に 1 件多く返す)。"""
    scope = spec.effective_scope
    remaining = None if spec.limit is None else spec.limit + 1
    for drive in spec.drives:
        if remaining is not None and remaining <= 0:
            return
        db = pool.files(drive.files_db)
        dir_id = ROOT_ID if scope == SCOPE_ALL else spec.dir_id
        if scope == SCOPE_FOLDER:
            entries = db.iter_children(dir_id, spec.terms, limit=remaining)
        else:
            entries = db.iter_subtree(dir_id, spec.terms, limit=remaining)

        seen: set[int] | None = set() if spec.include_context and spec.terms and drive.context_db else None
        for entry in entries:
            if seen is not None:
                seen.add(entry.id)
            yield Row(drive.drive_id, drive.name, entry, db.join_root(db.dir_path(entry.parent_id)))
            if remaining is not None:
                remaining -= 1
                if remaining <= 0:
                    return

        if seen is not None and drive.context_db is not None:
            scope_dir = db.get(dir_id) if dir_id != ROOT_ID else None
            last_desc_id = scope_dir.last_desc_id if scope_dir else None
            for entry_id in pool.context(drive.context_db).search_entry_ids(spec.terms, remaining):
                if entry_id in seen:
                    continue
                entry = db.get(entry_id)
                if entry is None or not _in_scope(entry, scope, dir_id, last_desc_id):
                    continue
                yield Row(drive.drive_id, drive.name, entry, db.join_root(db.dir_path(entry.parent_id)))
                if remaining is not None:
                    remaining -= 1
                    if remaining <= 0:
                        return


def extension_of(name: str) -> str:
    return os.path.splitext(name)[1][1:].lower()
