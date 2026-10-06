"""一覧・フィルタのクエリをバックグラウンドで実行するワーカー。"""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import QObject, Signal, Slot

from ..core.search import DbPool, QuerySpec, iter_rows


class QueryWorker(QObject):
    """専用スレッド上で動かす。新しい要求が来たら古いクエリは ``supersede`` で中断・破棄される。"""

    finished = Signal(int, object, bool, str)  # generation, rows, truncated, error

    def __init__(self) -> None:
        super().__init__()
        self._pool = DbPool()
        self._latest = 0

    def supersede(self, generation: int) -> None:
        """UI スレッドから呼ぶ。以降 ``generation`` より古い要求の結果は捨てる。"""
        self._latest = generation
        self._pool.interrupt_all()

    @Slot(int, object)
    def run(self, generation: int, spec: QuerySpec) -> None:
        if generation != self._latest:
            return
        rows = []
        try:
            for row in iter_rows(spec, self._pool):
                rows.append(row)
                if not len(rows) & 0x3FF and generation != self._latest:
                    return
        except sqlite3.Error as error:
            if generation == self._latest:
                self.finished.emit(generation, [], False, str(error))
            return
        truncated = spec.limit is not None and len(rows) > spec.limit
        if truncated:
            del rows[spec.limit :]
        self.finished.emit(generation, rows, truncated, "")

    @Slot()
    def close_all(self) -> None:
        """開いている DB を閉じる(カタログ更新でキャッシュファイルが入れ替わる前に呼ぶ)。"""
        self._pool.close_all()
