"""ドライブ単位のファイル基本情報データベース (files.db) の生成と参照。

エントリの id は深さ優先・先行順で採番する。フォルダは ``last_desc_id`` にサブツリー末尾の id を持つので、
「あるフォルダ以下すべて」は ``id > :id AND id <= :last_desc_id`` の主キー範囲で表せる。
フォルダの ``size`` / ``file_count`` / ``dir_count`` は配下の集計値(無視ルール適用後)。
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .errors import ScanCancelled
from .es_client import FILE_ATTRIBUTE_DIRECTORY, RawEntry
from .ignore import IgnoreRules
from .workdb import WorkDb, keep_temp_in_memory, remove_quietly

SCHEMA_VERSION = 1
ROOT_ID = 0

_SEP = "\x01"  # ファイル名に現れない最小の文字。区切りをこれに置換して並べると先行順になる
_BATCH = 20000

_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE entries(
  id           INTEGER PRIMARY KEY,
  parent_id    INTEGER NOT NULL,
  name         TEXT NOT NULL,
  is_dir       INTEGER NOT NULL,
  size         INTEGER,
  mtime        INTEGER,
  ctime        INTEGER,
  attrs        INTEGER,
  last_desc_id INTEGER,
  file_count   INTEGER,
  dir_count    INTEGER
);
"""
_INDEX = "CREATE INDEX ix_parent ON entries(parent_id, is_dir DESC, name COLLATE NOCASE)"
_COLUMNS = "id, parent_id, name, is_dir, size, mtime, ctime, attrs, last_desc_id, file_count, dir_count"

ProgressCallback = Callable[[str, int], None]


class Entry(NamedTuple):
    id: int
    parent_id: int
    name: str
    is_dir: int
    size: int | None
    mtime: int | None
    ctime: int | None
    attrs: int | None
    last_desc_id: int | None
    file_count: int | None
    dir_count: int | None


@dataclass(slots=True)
class BuildStats:
    file_count: int = 0
    dir_count: int = 0
    total_size: int = 0
    ignored_count: int = 0
    latest_mtime: int | None = None  # ドライブ内で最も新しいファイルの更新日時 (FILETIME)


class _OpenDir:
    __slots__ = ("id", "parent_id", "name", "mtime", "ctime", "attrs", "size", "files", "dirs")

    def __init__(self, id_: int, parent_id: int, name: str, mtime, ctime, attrs):
        self.id = id_
        self.parent_id = parent_id
        self.name = name
        self.mtime = mtime
        self.ctime = ctime
        self.attrs = attrs
        self.size = 0
        self.files = 0
        self.dirs = 0


def build_drive_db(
    db_path: str | os.PathLike[str],
    root: str,
    entries: Iterable[RawEntry],
    *,
    ignore: IgnoreRules | None = None,
    meta: dict | None = None,
    progress: ProgressCallback | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> BuildStats:
    """``entries`` (順不同) から ``db_path`` に新しいドライブ DB を作る。失敗時は作りかけを残さない。"""
    db_path = Path(db_path)
    stage_path = db_path.with_name(db_path.name + ".stage")
    for path in (db_path, stage_path):
        remove_quietly(path)
    out = WorkDb(db_path)
    stage = WorkDb(stage_path)
    try:
        stats = _build(out, stage, root, entries, ignore, meta or {}, progress, is_cancelled)
        out.finish()
    except BaseException:
        out.discard()
        raise
    finally:
        stage.discard()
    return stats


def build_drive_db_in_memory(
    root: str,
    entries: Iterable[RawEntry],
    *,
    memory_limit: int,
    spill_dir: Callable[[], Path],
    ignore: IgnoreRules | None = None,
    meta: dict | None = None,
    progress: ProgressCallback | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> tuple[bytes | Path, BuildStats]:
    """ドライブ DB をメモリ上に作る(暗号化カタログ用。平文をディスクに書かない)。

    戻り値は (DB のバイト列, 統計)。``memory_limit`` を超えた場合だけ ``spill_dir()`` の下の一時ファイルに
    退避し、バイト列の代わりにそのパスを返す。
    """
    out = WorkDb.in_memory(memory_limit, spill_dir, "files")
    stage = WorkDb.in_memory(memory_limit, spill_dir, "stage")
    try:
        stats = _build(out, stage, root, entries, ignore, meta or {}, progress, is_cancelled)
        return out.finish(), stats
    except BaseException:
        out.discard()
        raise
    finally:
        stage.discard()


def _build(out: WorkDb, stage: WorkDb, root, entries, ignore, meta, progress, is_cancelled) -> BuildStats:
    def check_cancel() -> None:
        if is_cancelled is not None and is_cancelled():
            raise ScanCancelled()

    prefix = os.path.abspath(root).rstrip("\\") + "\\"
    prefix_len = len(prefix)
    prefix_lower = prefix.lower()

    # --- 1. 作業用 DB に取り込み、パス順に並べ替える -----------------------------------
    stage.conn.execute("CREATE TABLE raw(k TEXT NOT NULL, is_dir INTEGER, size INTEGER, mtime INTEGER, ctime INTEGER, attrs INTEGER)")
    batch: list[tuple] = []
    staged = 0

    def flush_stage() -> None:
        nonlocal staged
        stage.conn.execute("BEGIN")
        stage.conn.executemany("INSERT INTO raw VALUES (?,?,?,?,?,?)", batch)
        stage.conn.execute("COMMIT")
        stage.checkpoint()
        staged += len(batch)
        batch.clear()

    for entry in entries:
        path = entry.path
        if path[:prefix_len].lower() != prefix_lower:
            continue
        rel = path[prefix_len:]
        if not rel:
            continue
        batch.append((rel.replace("\\", _SEP), int(entry.is_dir), entry.size, entry.mtime, entry.ctime, entry.attrs))
        if len(batch) >= _BATCH:
            flush_stage()
            check_cancel()
            if progress:
                progress("read", staged)
    if batch:
        flush_stage()
    if progress:
        progress("read", staged)
    check_cancel()

    # --- 2. 先行順に id を振りながら本 DB へ書き込む ------------------------------------
    if stage.on_disk:
        out.checkpoint(force=True)  # 作業用 DB がメモリに収まらなかったなら、本 DB も収まらない
    out.conn.executescript(_SCHEMA)
    stats = _write_entries(out, stage.conn, ignore, progress, check_cancel)
    out.conn.execute("BEGIN")
    out.conn.execute(_INDEX)
    full_meta = dict(meta)
    full_meta.update(
        schema_version=SCHEMA_VERSION,
        root=prefix if len(prefix) == 3 else prefix.rstrip("\\"),
        file_count=stats.file_count,
        dir_count=stats.dir_count,
        total_size=stats.total_size,
        latest_mtime=stats.latest_mtime,
        ignored_count=stats.ignored_count,
        ignore_patterns=list(ignore.patterns) if ignore else [],
    )
    out.conn.executemany(
        "INSERT INTO meta VALUES (?, ?)",
        [(key, json.dumps(value, ensure_ascii=False)) for key, value in full_meta.items()],
    )
    out.conn.execute("COMMIT")
    out.checkpoint()  # 最終的に上限を超えていたら、バイト列に複製せず一時ファイルとして渡す
    return stats


def _write_entries(out: WorkDb, stage, ignore, progress, check_cancel) -> BuildStats:
    ignore = ignore if ignore else None
    uses_paths = bool(ignore and ignore.uses_paths)
    insert = "INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?,?,?)"

    root_dir = _OpenDir(ROOT_ID, ROOT_ID, "", None, None, None)
    stack: list[_OpenDir] = [root_dir]
    names: list[str] = []  # stack[1:] の名前
    rows: list[tuple] = []
    next_id = 1
    written = 0
    ignored = 0
    latest_mtime: int | None = None
    skip_prefix: str | None = None
    previous_key: str | None = None

    def flush() -> None:
        nonlocal written
        out.conn.execute("BEGIN")
        out.conn.executemany(insert, rows)
        out.conn.execute("COMMIT")
        out.checkpoint()
        written += len(rows)
        rows.clear()

    def open_dir(name: str, mtime, ctime, attrs) -> None:
        nonlocal next_id
        stack.append(_OpenDir(next_id, stack[-1].id, name, mtime, ctime, attrs))
        names.append(name)
        next_id += 1

    def close_dir() -> None:
        closed = stack.pop()
        names.pop()
        rows.append((closed.id, closed.parent_id, closed.name, 1, closed.size, closed.mtime, closed.ctime,
                     closed.attrs, next_id - 1, closed.files, closed.dirs))  # fmt: skip
        parent = stack[-1]
        parent.size += closed.size
        parent.files += closed.files
        parent.dirs += closed.dirs + 1

    for key, is_dir, size, mtime, ctime, attrs in stage.execute(
        "SELECT k, is_dir, size, mtime, ctime, attrs FROM raw ORDER BY k"
    ):
        if key == previous_key:
            continue
        previous_key = key
        if skip_prefix is not None:
            if key.startswith(skip_prefix):
                ignored += 1
                continue
            skip_prefix = None

        parts = key.split(_SEP)
        parent_count = len(parts) - 1
        depth = 0
        limit = min(len(names), parent_count)
        while depth < limit and names[depth] == parts[depth]:
            depth += 1
        while len(names) > depth:
            close_dir()

        # 一覧に現れなかった中間フォルダを補う
        pruned = False
        for index in range(depth, parent_count):
            name = parts[index]
            if ignore and ignore.matches(name, True, "\\".join(parts[: index + 1]) if uses_paths else None):
                skip_prefix = _SEP.join(parts[: index + 1]) + _SEP
                pruned = True
                break
            open_dir(name, None, None, FILE_ATTRIBUTE_DIRECTORY)
        if pruned:
            ignored += 1
            continue

        name = parts[-1]
        if ignore and ignore.matches(name, bool(is_dir), key.replace(_SEP, "\\") if uses_paths else None):
            ignored += 1
            if is_dir:
                skip_prefix = key + _SEP
            continue

        if is_dir:
            open_dir(name, mtime, ctime, attrs)
        else:
            top = stack[-1]
            rows.append((next_id, top.id, name, 0, size, mtime, ctime, attrs, next_id, None, None))
            next_id += 1
            top.size += size or 0
            top.files += 1
            if mtime is not None and (latest_mtime is None or mtime > latest_mtime):
                latest_mtime = mtime

        if len(rows) >= _BATCH:
            flush()
            check_cancel()
            if progress:
                progress("build", written)

    while names:
        close_dir()
    if rows:
        flush()
    if progress:
        progress("build", written)
    return BuildStats(root_dir.files, root_dir.dirs, root_dir.size, ignored, latest_mtime)


# --------------------------------------------------------------------------------------
# 参照
# --------------------------------------------------------------------------------------

_SORT_COLUMNS = {
    "name": "name COLLATE NOCASE",
    "size": "size",
    "mtime": "mtime",
    "ctime": "ctime",
    "attrs": "attrs",
}


def readonly_uri(source: str | os.PathLike[str]) -> str:
    """SQLite を読み取り専用で開くための URI。``file:`` で始まる文字列は URI としてそのまま使う。"""
    if isinstance(source, str) and source.startswith("file:"):
        return source
    return f"{Path(source).resolve().as_uri()}?mode=ro"


def like_pattern(term: str) -> str:
    """部分一致用の LIKE パターン (``ESCAPE '\\'`` 前提)。"""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def split_terms(text: str) -> list[str]:
    """フィルタ文字列を空白区切りの AND 条件に分ける。"""
    return [term for term in text.split() if term]


class DriveDB:
    """読み取り専用で開いたドライブ DB。接続はスレッド間で共有せず、別スレッドでは ``clone()`` を使う。"""

    def __init__(self, source: str | os.PathLike[str]):
        self.source = source  # ファイルのパス、または ``file:`` で始まる SQLite の URI (メモリ上の共有 DB など)
        self._conn = sqlite3.connect(readonly_uri(source), uri=True, check_same_thread=False)
        keep_temp_in_memory(self._conn)  # 暗号化カタログの内容が SQLite の一時ファイルに出ないようにする
        self._meta: dict | None = None
        self._dir_paths: dict[int, str] = {ROOT_ID: ""}

    def clone(self) -> DriveDB:
        return DriveDB(self.source)

    def close(self) -> None:
        self._conn.close()

    def interrupt(self) -> None:
        """実行中のクエリを中断する(他スレッドから呼んでよい)。"""
        self._conn.interrupt()

    def __enter__(self) -> DriveDB:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def meta(self) -> dict:
        if self._meta is None:
            self._meta = {key: json.loads(value) for key, value in self._conn.execute("SELECT key, value FROM meta")}
        return self._meta

    @property
    def root(self) -> str:
        """スキャン時のルート ("X:\\" またはフォルダパス)。"""
        return self.meta.get("root", "")

    def get(self, entry_id: int) -> Entry | None:
        row = self._conn.execute(f"SELECT {_COLUMNS} FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return Entry(*row) if row else None

    def latest_mtime(self) -> int | None:
        """最も新しいファイルの更新日時 (FILETIME)。取込時に meta に記録した値があればそれを使う。"""
        if "latest_mtime" in self.meta:
            return self.meta["latest_mtime"]
        return self._conn.execute("SELECT MAX(mtime) FROM entries WHERE is_dir = 0").fetchone()[0]

    def _select(
        self,
        where: list[str],
        params: list,
        terms: Sequence[str],
        order_by: str | None,
        descending: bool,
        limit: int | None,
    ) -> Iterator[Entry]:
        for term in terms:
            where.append("name LIKE ? ESCAPE '\\'")
            params.append(like_pattern(term))
        sql = f"SELECT {_COLUMNS} FROM entries"
        if where:
            sql += " WHERE " + " AND ".join(where)
        if order_by:
            column = _SORT_COLUMNS[order_by]
            direction = " DESC" if descending else ""
            sql += f" ORDER BY is_dir DESC, {column}{direction}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        cursor = self._conn.execute(sql, params)
        return map(Entry._make, cursor)

    def iter_children(
        self,
        parent_id: int,
        terms: Sequence[str] = (),
        *,
        dirs_only: bool = False,
        order_by: str | None = "name",
        descending: bool = False,
        limit: int | None = None,
    ) -> Iterator[Entry]:
        """フォルダ直下のエントリ。``terms`` を渡すと名前の部分一致 (AND) で絞り込む。"""
        where = ["parent_id = ?"]
        params: list = [parent_id]
        if dirs_only:
            where.append("is_dir = 1")
        return self._select(where, params, terms, order_by, descending, limit)

    def children(self, parent_id: int, terms: Sequence[str] = (), **kwargs) -> list[Entry]:
        return list(self.iter_children(parent_id, terms, **kwargs))

    def iter_subtree(
        self,
        dir_id: int,
        terms: Sequence[str] = (),
        *,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> Iterator[Entry]:
        """フォルダ以下すべて(下位フォルダを含む)のエントリ。``dir_id=ROOT_ID`` でドライブ全体。"""
        where: list[str] = []
        params: list = []
        if dir_id != ROOT_ID:
            entry = self.get(dir_id)
            if entry is None or not entry.is_dir:
                return iter(())
            where.append("id > ? AND id <= ?")
            params += [dir_id, entry.last_desc_id]
        return self._select(where, params, terms, order_by, descending, limit)

    def subtree(self, dir_id: int, terms: Sequence[str] = (), **kwargs) -> list[Entry]:
        return list(self.iter_subtree(dir_id, terms, **kwargs))

    def dir_path(self, dir_id: int) -> str:
        """フォルダのルートからの相対パス(ルートは空文字)。"""
        cache = self._dir_paths
        if dir_id in cache:
            return cache[dir_id]
        chain: list[tuple[int, str]] = []
        current = dir_id
        while current not in cache:
            row = self._conn.execute("SELECT parent_id, name FROM entries WHERE id = ?", (current,)).fetchone()
            if row is None:
                return ""
            chain.append((current, row[1]))
            current = row[0]
        base = cache[current]
        for entry_id, name in reversed(chain):
            base = f"{base}\\{name}" if base else name
            cache[entry_id] = base
        return base

    def rel_path(self, entry: Entry) -> str:
        parent = self.dir_path(entry.parent_id)
        return f"{parent}\\{entry.name}" if parent else entry.name

    def full_path(self, entry: Entry) -> str:
        """スキャン時のルートを付けたフルパス。"""
        return self.join_root(self.rel_path(entry))

    def join_root(self, rel_path: str) -> str:
        root = self.root
        if not rel_path:
            return root
        return root + rel_path if root.endswith("\\") else f"{root}\\{rel_path}"

    def find_path(self, rel_path: str) -> Entry | None:
        """相対パスからエントリを引く(大文字小文字は区別しない)。"""
        parent_id = ROOT_ID
        entry: Entry | None = None
        for name in (part for part in rel_path.replace("/", "\\").split("\\") if part):
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM entries WHERE parent_id = ? AND name = ? COLLATE NOCASE", (parent_id, name)
            ).fetchone()
            if row is None:
                return None
            entry = Entry(*row)
            parent_id = entry.id
        return entry
