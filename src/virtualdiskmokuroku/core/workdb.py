"""構築中の SQLite DB の置き場所。

通常のカタログではファイルに作る。暗号化カタログでは平文をディスクに残さないようメモリ上に作り、
大きくなりすぎた場合だけセッション用の一時フォルダへ退避する。
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path

DEFAULT_MEMORY_LIMIT = 512 * 1024 * 1024


def keep_temp_in_memory(conn: sqlite3.Connection) -> None:
    """SQLite が並べ替えや一時索引のために作る一時ファイル (%TEMP% の etilqs_*) を使わせない。

    一時ファイルには平文のファイル名などが入るので、暗号化カタログを扱う接続では必ず呼ぶ。
    """
    conn.execute("PRAGMA temp_store=MEMORY")


def fast_connection(target: str | os.PathLike[str], *, secure: bool = False) -> sqlite3.Connection:
    """作り直しの効く DB 用の接続(ジャーナルと同期を切って速くする)。"""
    conn = sqlite3.connect(str(target), isolation_level=None)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    if secure:
        keep_temp_in_memory(conn)
    return conn


def remove_quietly(path: str | os.PathLike[str]) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def open_shared_memory_db(data: bytes) -> tuple[str, sqlite3.Connection]:
    """DB のバイト列を名前付きのメモリ DB に載せ、(読み取り専用で開く URI, 保持用の接続) を返す。

    同じ URI を開けば、別スレッドの接続からも同じ内容を(複製せずに)読める。保持用の接続を閉じ、
    ほかの接続もすべて閉じられると解放される。
    """
    name = f"/vdmoku-{uuid.uuid4().hex}"
    owner = sqlite3.connect(f"file:{name}?vfs=memdb", uri=True, check_same_thread=False)
    keep_temp_in_memory(owner)
    loader = sqlite3.connect(":memory:")
    try:
        loader.deserialize(data)
        loader.backup(owner)
    except BaseException:
        owner.close()
        raise
    finally:
        loader.close()
    return f"file:{name}?vfs=memdb&mode=ro", owner


class WorkDb:
    """構築中の DB。``conn`` は退避で入れ替わることがあるので、使う側は毎回 ``work.conn`` を参照する。"""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        memory_limit: int | None = None,
        spill_dir: Callable[[], Path] | None = None,
        label: str = "work",
    ):
        self.path: Path | None = Path(path) if path is not None else None
        self._memory_limit = memory_limit
        self._spill_dir = spill_dir
        self._label = label
        # メモリ上に作る (= 暗号化カタログ用) 場合は、一時ファイルへ退避した後も SQLite の一時領域をメモリに保つ
        self._secure = self.path is None
        self.conn = fast_connection(self.path if self.path is not None else ":memory:", secure=self._secure)

    @classmethod
    def in_memory(cls, memory_limit: int, spill_dir: Callable[[], Path], label: str) -> WorkDb:
        return cls(None, memory_limit=memory_limit, spill_dir=spill_dir, label=label)

    @property
    def on_disk(self) -> bool:
        return self.path is not None

    def size(self) -> int:
        page_count = self.conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = self.conn.execute("PRAGMA page_size").fetchone()[0]
        return page_count * page_size

    def checkpoint(self, force: bool = False) -> bool:
        """メモリ上の DB が上限を超えていたら(または ``force`` なら)一時ファイルへ退避する。

        トランザクションの外で呼ぶこと。退避したら True。
        """
        if self.path is not None or self._spill_dir is None:
            return False
        if not force and (self._memory_limit is None or self.size() <= self._memory_limit):
            return False
        target = self._spill_dir() / f"{self._label}-{uuid.uuid4().hex}.db"
        file_conn = fast_connection(target, secure=self._secure)
        try:
            self.conn.backup(file_conn)
        except BaseException:
            file_conn.close()
            remove_quietly(target)
            raise
        self.conn.close()
        self.conn = file_conn
        self.path = target
        return True

    def finish(self) -> Path | bytes:
        """接続を閉じ、ファイルならそのパス、メモリ上なら DB のバイト列を返す。"""
        if self.path is None:
            data = self.conn.serialize()
            self.conn.close()
            return data
        self.conn.close()
        return self.path

    def discard(self) -> None:
        """作りかけを破棄する。"""
        try:
            self.conn.close()
        except sqlite3.Error:
            pass
        if self.path is not None:
            remove_quietly(self.path)
