"""表示用の整形(サイズ・日時・属性)。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
_ATTRIBUTE_LETTERS = (
    (0x1, "R"), (0x2, "H"), (0x4, "S"), (0x10, "D"), (0x20, "A"), (0x100, "T"),
    (0x400, "L"), (0x800, "C"), (0x1000, "O"), (0x2000, "I"), (0x4000, "E"),
)  # fmt: skip


def format_size(size: int | None) -> str:
    """エクスプローラ風の短い表記 (1.23 GB)。"""
    if size is None:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:,.2f} {unit}" if value < 100 else f"{value:,.1f} {unit}"
        value /= 1024
    return ""


def format_bytes(size: int | None) -> str:
    return "" if size is None else f"{size:,}"


def filetime_to_datetime(filetime: int | None) -> datetime | None:
    if not filetime:
        return None
    try:
        return (_FILETIME_EPOCH + timedelta(microseconds=filetime // 10)).astimezone()
    except (OverflowError, OSError, ValueError):
        return None


def format_filetime(filetime: int | None) -> str:
    moment = filetime_to_datetime(filetime)
    return moment.strftime("%Y/%m/%d %H:%M:%S") if moment else ""


def format_iso(timestamp: str | None) -> str:
    """ISO 8601 文字列をローカル時刻で表示する。"""
    if not timestamp:
        return ""
    try:
        return datetime.fromisoformat(timestamp).astimezone().strftime("%Y/%m/%d %H:%M")
    except ValueError:
        return timestamp


def format_attributes(attrs: int | None) -> str:
    if not attrs:
        return ""
    return "".join(letter for bit, letter in _ATTRIBUTE_LETTERS if attrs & bit)
