"""ファイル一覧のテーブルモデル。"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt
from PySide6.QtGui import QIcon

from ..core.formatting import format_attributes, format_bytes, format_filetime, format_size
from ..core.search import Row, extension_of

COL_NAME, COL_SIZE, COL_MTIME, COL_TYPE, COL_ATTRS, COL_LOCATION, COL_DRIVE = range(7)
HEADERS = ["名前", "サイズ", "更新日時", "種類", "属性", "場所", "ドライブ"]

_AnyIndex = QModelIndex | QPersistentModelIndex


def type_text(row: Row) -> str:
    if row.entry.is_dir:
        return "ファイル フォルダー"
    extension = extension_of(row.entry.name)
    return f"{extension.upper()} ファイル" if extension else "ファイル"


_SORT_KEYS = {
    COL_NAME: lambda row: row.entry.name.casefold(),
    COL_SIZE: lambda row: row.entry.size or 0,
    COL_MTIME: lambda row: row.entry.mtime or 0,
    COL_TYPE: lambda row: (extension_of(row.entry.name), row.entry.name.casefold()),
    COL_ATTRS: lambda row: row.entry.attrs or 0,
    COL_LOCATION: lambda row: (row.location.casefold(), row.entry.name.casefold()),
    COL_DRIVE: lambda row: (row.drive_name.casefold(), row.location.casefold()),
}


class FileTableModel(QAbstractTableModel):
    def __init__(self, folder_icon: QIcon, file_icon: QIcon, parent=None):
        super().__init__(parent)
        self._rows: list[Row] = []
        self._folder_icon = folder_icon
        self._file_icon = file_icon
        self._sort_column = COL_NAME
        self._sort_order = Qt.SortOrder.AscendingOrder

    # ------------------------------------------------------------------ データ
    def set_rows(self, rows: list[Row]) -> None:
        self.beginResetModel()
        self._rows = rows
        self._sort_rows()
        self.endResetModel()

    @property
    def rows(self) -> list[Row]:
        return self._rows

    def row_at(self, index: _AnyIndex) -> Row | None:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        return self._rows[index.row()]

    def find_row(self, drive_id: str, entry_id: int) -> int:
        for position, row in enumerate(self._rows):
            if row.entry.id == entry_id and row.drive_id == drive_id:
                return position
        return -1

    # ------------------------------------------------------------------ QAbstractTableModel
    def rowCount(self, parent: _AnyIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: _AnyIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[section]
        return None

    def data(self, index: _AnyIndex, role: int = Qt.ItemDataRole.DisplayRole):
        row = self.row_at(index)
        if row is None:
            return None
        column = index.column()
        entry = row.entry
        if role == Qt.ItemDataRole.DisplayRole:
            if column == COL_NAME:
                return entry.name
            if column == COL_SIZE:
                return format_size(entry.size)
            if column == COL_MTIME:
                return format_filetime(entry.mtime)
            if column == COL_TYPE:
                return type_text(row)
            if column == COL_ATTRS:
                return format_attributes(entry.attrs)
            if column == COL_LOCATION:
                return row.location
            if column == COL_DRIVE:
                return row.drive_name
        elif role == Qt.ItemDataRole.DecorationRole:
            if column == COL_NAME:
                return self._folder_icon if entry.is_dir else self._file_icon
        elif role == Qt.ItemDataRole.TextAlignmentRole:
            if column == COL_SIZE:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        elif role == Qt.ItemDataRole.ToolTipRole:
            if column == COL_SIZE and entry.size is not None:
                detail = f"{format_bytes(entry.size)} バイト"
                if entry.is_dir:
                    detail += f"\nファイル {entry.file_count or 0:,} / フォルダ {entry.dir_count or 0:,}"
                return detail
            if column == COL_NAME:
                return row.full_path
        return None

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        self._sort_column = column
        self._sort_order = order
        self.layoutAboutToBeChanged.emit()
        self._sort_rows()
        self.layoutChanged.emit()

    def _sort_rows(self) -> None:
        key = _SORT_KEYS.get(self._sort_column)
        if key is None:
            return
        descending = self._sort_order == Qt.SortOrder.DescendingOrder
        self._rows.sort(key=key, reverse=descending)
        # 並び順に関わらずフォルダを先頭にまとめる(安定ソート)
        self._rows.sort(key=lambda row: not row.entry.is_dir)
