"""選択中エントリの詳細と拡張コンテキスト(メタ情報・本文・サムネイル・書庫内リスト)の表示。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.formatting import format_attributes, format_bytes, format_filetime, format_size
from ..core.search import Row
from .models import type_text
from .style import apply_selection_style

_ENCODINGS = [
    "utf-8", "cp932", "euc_jp", "iso2022_jp", "utf-16", "utf-16-le", "utf-16-be",
    "cp1252", "latin-1", "gbk", "big5", "euc_kr",
]  # fmt: skip
_MAX_INNER_ROWS = 5000

try:
    from ..context import EXTRACTORS, KEY_LABELS
except ImportError:  # 拡張コンテキスト機能が無くても基本情報は表示できる
    EXTRACTORS = {}
    KEY_LABELS = {}


def _selectable(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(22)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    table.horizontalHeader().setStretchLastSection(True)
    apply_selection_style(table)
    return table


class PropertiesPanel(QScrollArea):
    redecodeRequested = Signal(str, int, str)  # drive_id, entry_id, encoding

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setMinimumWidth(300)
        self._row: Row | None = None
        body = QWidget()
        self.setWidget(body)
        layout = QVBoxLayout(body)

        self._title = _selectable()
        font = self._title.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.15)
        self._title.setFont(font)
        layout.addWidget(self._title)

        form = QFormLayout()
        self._fields: dict[str, QLabel] = {}
        for key, caption in (
            ("location", "場所"), ("type", "種類"), ("size", "サイズ"), ("contents", "内容"),
            ("mtime", "更新日時"), ("ctime", "作成日時"), ("attrs", "属性"), ("drive", "ドライブ"),
        ):  # fmt: skip
            label = _selectable()
            self._fields[key] = label
            form.addRow(caption + ":", label)
        layout.addLayout(form)

        self._thumb = QLabel()
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._thumb)

        self._meta_caption = QLabel("メタ情報")
        layout.addWidget(self._meta_caption)
        self._meta = _table(["項目", "値"])
        layout.addWidget(self._meta)

        self._text_box = QWidget()
        text_layout = QVBoxLayout(self._text_box)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_row = QHBoxLayout()
        text_row.addWidget(QLabel("テキスト内容  文字コード:"))
        self._encoding = QComboBox()
        self._encoding.setEditable(True)
        self._encoding.addItems(_ENCODINGS)
        text_row.addWidget(self._encoding, 1)
        text_layout.addLayout(text_row)
        self._redecode = QPushButton("この文字コードで再取込")
        self._redecode.setToolTip("文字化けしている場合、保存済みのデータを指定した文字コードで読み直します")
        self._redecode.clicked.connect(self._emit_redecode)
        text_layout.addWidget(self._redecode, 0, Qt.AlignmentFlag.AlignRight)
        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setMinimumHeight(160)
        text_layout.addWidget(self._text)
        layout.addWidget(self._text_box)

        self._inner_caption = QLabel()
        layout.addWidget(self._inner_caption)
        self._inner = _table(["パス", "サイズ", "更新日時"])
        self._inner.setMinimumHeight(200)
        layout.addWidget(self._inner)

        layout.addStretch(1)
        self.show_row(None, None)

    def _emit_redecode(self) -> None:
        if self._row is not None:
            self.redecodeRequested.emit(self._row.drive_id, self._row.entry.id, self._encoding.currentText().strip())

    def show_row(self, row: Row | None, context_db) -> None:
        """``context_db`` は該当ドライブの ContextDB (無ければ None)。"""
        self._row = row
        for widget in (self._thumb, self._meta_caption, self._meta, self._text_box, self._inner_caption, self._inner):
            widget.setVisible(False)
        if row is None:
            self._title.setText("(選択なし)")
            for label in self._fields.values():
                label.setText("")
            return

        entry = row.entry
        self._title.setText(entry.name)
        self._fields["location"].setText(row.location)
        self._fields["type"].setText(type_text(row))
        size = "" if entry.size is None else f"{format_size(entry.size)} ({format_bytes(entry.size)} バイト)"
        self._fields["size"].setText(size)
        contents = f"ファイル {entry.file_count or 0:,} / フォルダ {entry.dir_count or 0:,}" if entry.is_dir else ""
        self._fields["contents"].setText(contents)
        self._fields["mtime"].setText(format_filetime(entry.mtime))
        self._fields["ctime"].setText(format_filetime(entry.ctime))
        self._fields["attrs"].setText(format_attributes(entry.attrs))
        self._fields["drive"].setText(row.drive_name)

        if context_db is None or entry.is_dir:
            return
        self._show_context(entry.id, context_db)

    def _show_context(self, entry_id: int, context_db) -> None:
        thumb = context_db.get_thumb(entry_id)
        if thumb is not None:
            pixmap = QPixmap()
            if pixmap.loadFromData(thumb[2]):
                self._thumb.setPixmap(pixmap)
                self._thumb.setVisible(True)

        meta = context_db.get_meta(entry_id)
        if meta:
            self._meta.setRowCount(len(meta))
            for index, (kind, key, value) in enumerate(meta):
                caption = KEY_LABELS.get(key, key)
                item = QTableWidgetItem(caption)
                extractor = EXTRACTORS.get(kind)
                item.setToolTip(extractor.label if extractor else kind)
                self._meta.setItem(index, 0, item)
                self._meta.setItem(index, 1, QTableWidgetItem(value))
            self._meta.resizeColumnToContents(0)
            self._meta.setFixedHeight(min(26 + 22 * len(meta) + 4, 320))
            self._meta_caption.setVisible(True)
            self._meta.setVisible(True)

        text = context_db.get_text(entry_id)
        if text is not None:
            encoding, content = text
            self._encoding.setCurrentText(encoding or "")
            self._text.setPlainText(content)
            self._text_box.setVisible(True)

        inner = context_db.get_inner(entry_id)
        if inner:
            shown = inner[:_MAX_INNER_ROWS]
            self._inner.setRowCount(len(shown))
            for index, item in enumerate(shown):
                self._inner.setItem(index, 0, QTableWidgetItem(item.path + ("\\" if item.is_dir else "")))
                size_item = QTableWidgetItem("" if item.is_dir else format_size(item.size))
                size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self._inner.setItem(index, 1, size_item)
                self._inner.setItem(index, 2, QTableWidgetItem(format_filetime(item.mtime)))
            self._inner.resizeColumnToContents(0)
            caption = f"書庫 / イメージ内のファイルリスト ({len(inner):,} 件)"
            if len(inner) > len(shown):
                caption += f" — 先頭 {len(shown):,} 件を表示"
            self._inner_caption.setText(caption)
            self._inner_caption.setVisible(True)
            self._inner.setVisible(True)
