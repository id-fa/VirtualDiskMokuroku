"""ドライブ単位の拡張コンテキストデータベース (context.db)。

ファイル基本情報 DB (files.db) とは別ファイルにし、``entries.id`` を ``entry_id`` として参照する。
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from ..core.drive_db import like_pattern
from .base import EntryWriter

SCHEMA_VERSION = 1
STATUS_OK = "ok"
STATUS_ERROR = "error"

_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE done(
  entry_id INTEGER NOT NULL,
  kind     TEXT NOT NULL,
  status   TEXT NOT NULL,
  message  TEXT,
  PRIMARY KEY(entry_id, kind)
) WITHOUT ROWID;
CREATE TABLE ctx(entry_id INTEGER NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT);
CREATE TABLE text_content(entry_id INTEGER PRIMARY KEY, encoding TEXT, content TEXT, raw BLOB);
CREATE TABLE thumbs(entry_id INTEGER PRIMARY KEY, width INTEGER, height INTEGER, data BLOB);
CREATE TABLE inner_entries(
  entry_id INTEGER NOT NULL,
  path     TEXT NOT NULL,
  size     INTEGER,
  mtime    INTEGER,
  is_dir   INTEGER NOT NULL
);
"""
_INDEXES = (
    "CREATE INDEX ix_ctx_entry ON ctx(entry_id)",
    "CREATE INDEX ix_inner_entry ON inner_entries(entry_id)",
)


class InnerEntry(NamedTuple):
    path: str  # 書庫/ISO 内のパス(区切りは ``/``)
    size: int | None
    mtime: int | None  # FILETIME
    is_dir: int


# --------------------------------------------------------------------------------------
# 生成 (runner から使う)
# --------------------------------------------------------------------------------------


def create_context_db(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """空の context.db を作り、書き込み用の接続を返す。索引は ``finish_context_db`` で張る。"""
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.executescript(_SCHEMA)
    return conn


def finish_context_db(conn: sqlite3.Connection, meta: dict) -> None:
    for statement in _INDEXES:
        conn.execute(statement)
    full_meta = {"schema_version": SCHEMA_VERSION, **meta}
    conn.executemany(
        "INSERT OR REPLACE INTO meta VALUES (?, ?)",
        [(key, json.dumps(value, ensure_ascii=False, sort_keys=True)) for key, value in full_meta.items()],
    )


def write_result(conn: sqlite3.Connection, writer: EntryWriter) -> None:
    """抽出に成功した 1 件分を書き込む。"""
    entry_id = writer.entry_id
    conn.execute("INSERT OR REPLACE INTO done VALUES (?, ?, ?, NULL)", (entry_id, writer.kind, STATUS_OK))
    if writer.meta:
        conn.executemany(
            "INSERT INTO ctx VALUES (?, ?, ?, ?)", [(entry_id, writer.kind, key, value) for key, value in writer.meta]
        )
    if writer.text is not None:
        conn.execute("INSERT OR REPLACE INTO text_content VALUES (?, ?, ?, ?)", (entry_id, *writer.text))
    if writer.thumb is not None:
        conn.execute("INSERT OR REPLACE INTO thumbs VALUES (?, ?, ?, ?)", (entry_id, *writer.thumb))
    if writer.inner:
        conn.executemany("INSERT INTO inner_entries VALUES (?, ?, ?, ?, ?)", [(entry_id, *row) for row in writer.inner])


def write_error(conn: sqlite3.Connection, entry_id: int, kind: str, message: str) -> None:
    conn.execute("INSERT OR REPLACE INTO done VALUES (?, ?, ?, ?)", (entry_id, kind, STATUS_ERROR, message))


def load_result(conn: sqlite3.Connection, entry_id: int, kind: str, stores: Sequence[str], new_entry_id: int) -> EntryWriter:
    """保存済みの 1 件分を読み、``new_entry_id`` 向けの EntryWriter として返す(前回結果の引き継ぎ用)。"""
    writer = EntryWriter(new_entry_id, kind)
    writer.meta = list(conn.execute("SELECT key, value FROM ctx WHERE entry_id = ? AND kind = ?", (entry_id, kind)))
    if "text" in stores:
        writer.text = conn.execute(
            "SELECT encoding, content, raw FROM text_content WHERE entry_id = ?", (entry_id,)
        ).fetchone()
    if "thumb" in stores:
        writer.thumb = conn.execute("SELECT width, height, data FROM thumbs WHERE entry_id = ?", (entry_id,)).fetchone()
    if "inner" in stores:
        writer.inner = list(
            conn.execute("SELECT path, size, mtime, is_dir FROM inner_entries WHERE entry_id = ?", (entry_id,))
        )
    return writer


# --------------------------------------------------------------------------------------
# 参照
# --------------------------------------------------------------------------------------


class ContextDB:
    """読み取り専用で開いた context.db。接続はスレッド間で共有せず、別スレッドでは ``clone()`` を使う。"""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
        self._meta: dict | None = None

    def clone(self) -> ContextDB:
        return ContextDB(self.path)

    def close(self) -> None:
        self._conn.close()

    def interrupt(self) -> None:
        """実行中のクエリを中断する(他スレッドから呼んでよい)。"""
        self._conn.interrupt()

    def __enter__(self) -> ContextDB:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def meta(self) -> dict:
        if self._meta is None:
            self._meta = {key: json.loads(value) for key, value in self._conn.execute("SELECT key, value FROM meta")}
        return self._meta

    def summary(self) -> dict[str, int]:
        """種別ごとの抽出済み(成功)件数。"""
        return dict(self._conn.execute("SELECT kind, COUNT(*) FROM done WHERE status = ? GROUP BY kind", (STATUS_OK,)))

    def errors(self, limit: int | None = None) -> list[tuple[int, str, str]]:
        """抽出に失敗した (entry_id, kind, message) の一覧。"""
        sql = "SELECT entry_id, kind, message FROM done WHERE status = ? ORDER BY entry_id"
        params: list = [STATUS_ERROR]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return list(self._conn.execute(sql, params))

    def get_meta(self, entry_id: int) -> list[tuple[str, str, str]]:
        """メタ情報 (kind, key, value) を保存順に返す。"""
        return list(self._conn.execute("SELECT kind, key, value FROM ctx WHERE entry_id = ? ORDER BY rowid", (entry_id,)))

    def get_text(self, entry_id: int) -> tuple[str, str] | None:
        return self._conn.execute("SELECT encoding, content FROM text_content WHERE entry_id = ?", (entry_id,)).fetchone()

    def get_thumb(self, entry_id: int) -> tuple[int, int, bytes] | None:
        return self._conn.execute("SELECT width, height, data FROM thumbs WHERE entry_id = ?", (entry_id,)).fetchone()

    def get_inner(self, entry_id: int) -> list[InnerEntry]:
        cursor = self._conn.execute(
            "SELECT path, size, mtime, is_dir FROM inner_entries WHERE entry_id = ? ORDER BY rowid", (entry_id,)
        )
        return [InnerEntry(*row) for row in cursor]

    def has_context(self, entry_id: int) -> bool:
        """このエントリに表示できる拡張コンテキストがあるか。"""
        row = self._conn.execute(
            "SELECT EXISTS(SELECT 1 FROM ctx WHERE entry_id = :id)"
            " OR EXISTS(SELECT 1 FROM text_content WHERE entry_id = :id)"
            " OR EXISTS(SELECT 1 FROM thumbs WHERE entry_id = :id)"
            " OR EXISTS(SELECT 1 FROM inner_entries WHERE entry_id = :id)",
            {"id": entry_id},
        ).fetchone()
        return bool(row[0])

    def search_entry_ids(self, terms: Sequence[str], limit: int | None = None) -> list[int]:
        """各語がメタ情報の値・テキスト本文・書庫内パスのいずれかに部分一致するエントリの id (語どうしは AND)。"""
        terms = [term for term in terms if term]
        if not terms:
            return []
        per_term = (
            "SELECT entry_id FROM ("
            "SELECT entry_id FROM ctx WHERE value LIKE ? ESCAPE '\\'"
            " UNION SELECT entry_id FROM text_content WHERE content LIKE ? ESCAPE '\\'"
            " UNION SELECT entry_id FROM inner_entries WHERE path LIKE ? ESCAPE '\\')"
        )
        params: list = []
        for term in terms:
            params += [like_pattern(term)] * 3
        sql = " INTERSECT ".join([per_term] * len(terms)) + " ORDER BY 1"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [row[0] for row in self._conn.execute(sql, params)]


def redecode_text(context_db_path: str | os.PathLike[str], entry_id: int, encoding: str) -> str:
    """保存済みの生データを指定の文字コードでデコードし直して保存する(ドライブ未接続でも実行できる)。

    不明な文字コード名は ``LookupError``、対象のテキストが無い場合は ``KeyError``。
    """
    b"".decode(encoding)  # 文字コード名の検証
    conn = sqlite3.connect(str(context_db_path))
    try:
        row = conn.execute("SELECT raw FROM text_content WHERE entry_id = ?", (entry_id,)).fetchone()
        if row is None or row[0] is None:
            raise KeyError(f"再デコードできるテキストがありません: entry_id={entry_id}")
        content = bytes(row[0]).decode(encoding, errors="replace").removeprefix("﻿")
        with conn:
            conn.execute("UPDATE text_content SET encoding = ?, content = ? WHERE entry_id = ?", (encoding, content, entry_id))
    finally:
        conn.close()
    return content
