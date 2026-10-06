"""一覧・フィルタ結果のエクスポート (テキスト / CSV)。"""

from __future__ import annotations

import csv
import os
from collections.abc import Iterable

from .formatting import format_attributes, format_filetime
from .search import Row

FORMAT_TXT = "txt"
FORMAT_CSV = "csv"

CSV_HEADER = ["名前", "場所", "サイズ", "更新日時", "作成日時", "属性", "種類", "ドライブ"]


def export_rows(path: str | os.PathLike[str], rows: Iterable[Row], fmt: str) -> int:
    """``rows`` を書き出して件数を返す。Excel でも文字化けしないよう UTF-8 (BOM 付き) で保存する。"""
    count = 0
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        if fmt == FORMAT_CSV:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADER)
            for row in rows:
                entry = row.entry
                writer.writerow([
                    entry.name,
                    row.location,
                    "" if entry.size is None else entry.size,
                    format_filetime(entry.mtime),
                    format_filetime(entry.ctime),
                    format_attributes(entry.attrs),
                    "フォルダ" if entry.is_dir else "ファイル",
                    row.drive_name,
                ])  # fmt: skip
                count += 1
        else:
            for row in rows:
                f.write(row.full_path + ("\\" if row.entry.is_dir else "") + "\r\n")
                count += 1
    return count
