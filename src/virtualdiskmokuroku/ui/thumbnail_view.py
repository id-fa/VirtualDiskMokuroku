"""サムネイル表示 (敷き詰め / 情報付き) 用のビュー。

拡張コンテキストに保存された画像サムネイルを、一覧と同じモデル (FileTableModel) の上に描画する。
サムネイルの無い項目(フォルダや画像以外)は種類アイコンで表示する。
"""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from collections.abc import Callable, Iterable
from typing import NamedTuple

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListView, QStyle, QStyledItemDelegate, QStyleOptionViewItem

from ..core.formatting import format_filetime, format_size
from ..core.search import Row
from .models import type_text
from .style import SELECTION_BACKGROUND, SELECTION_TEXT
from ..i18n import tr

VIEW_DETAILS = "details"  # 従来の詳細一覧 (表)
VIEW_TILES = "tiles"  # サムネイルを敷き詰める
VIEW_THUMB_LIST = "thumb_list"  # サムネイルの横にファイル情報

CAPTION_NAME = "name"
CAPTION_RESOLUTION = "resolution"
CAPTION_SIZE = "size"
CAPTION_MTIME = "mtime"
# 敷き詰め表示でサムネイルの下に出せる項目(この順に並べる)
CAPTION_LABELS = {
    CAPTION_NAME: "ファイル名",
    CAPTION_RESOLUTION: "解像度",
    CAPTION_SIZE: "ファイル容量",
    CAPTION_MTIME: "更新日時",
}

DEFAULT_THUMB_SIZE = 160
_HOVER_BACKGROUND = "#e5f3ff"
_PADDING = 6
_GAP = 3
_ICON_SIZE = 48
_SMALL_FONT_SCALE = 0.85

_AnyIndex = QModelIndex | QPersistentModelIndex


class ThumbInfo(NamedTuple):
    pixmap: QPixmap | None
    resolution: str  # "1920 x 1080"。不明なら空


_NO_THUMB = ThumbInfo(None, "")


def format_resolution(size: tuple[int, int] | None) -> str:
    return f"{size[0]} x {size[1]}" if size else ""


class ThumbnailProvider:
    """サムネイルと元画像の解像度を拡張コンテキスト DB から読み、直近の分をキャッシュする。"""

    def __init__(self, context_for: Callable[[str], object | None], capacity: int = 4000):
        self._context_for = context_for  # drive_id -> ContextDB | None (UI スレッドの接続)
        self._capacity = capacity
        self._cache: OrderedDict[tuple[str, int], ThumbInfo] = OrderedDict()

    def clear(self) -> None:
        self._cache.clear()

    def get(self, row: Row) -> ThumbInfo:
        if row.entry.is_dir:
            return _NO_THUMB
        key = (row.drive_id, row.entry.id)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        info = self._load(row.drive_id, row.entry.id)
        self._cache[key] = info
        if len(self._cache) > self._capacity:
            self._cache.popitem(last=False)
        return info

    def _load(self, drive_id: str, entry_id: int) -> ThumbInfo:
        context = self._context_for(drive_id)
        if context is None:
            return _NO_THUMB
        try:
            thumb = context.get_thumb(entry_id)  # type: ignore[attr-defined]
            size = context.get_image_size(entry_id)  # type: ignore[attr-defined]
        except sqlite3.Error:
            return _NO_THUMB
        pixmap = None
        if thumb is not None:
            pixmap = QPixmap()
            if not pixmap.loadFromData(thumb[2]):
                pixmap = None
        if pixmap is None and size is None:
            return _NO_THUMB
        return ThumbInfo(pixmap, format_resolution(size))


class ThumbnailDelegate(QStyledItemDelegate):
    def __init__(self, provider: ThumbnailProvider, folder_icon: QIcon, file_icon: QIcon, parent=None):
        super().__init__(parent)
        self._provider = provider
        self._folder_icon = folder_icon
        self._file_icon = file_icon
        self.mode = VIEW_TILES
        self.base_size = DEFAULT_THUMB_SIZE  # 収蔵サムネイルの長辺
        self.zoom = 1  # 2 なら 2 倍に拡大して並べる
        self.captions: tuple[str, ...] = tuple(CAPTION_LABELS)
        self.show_location = False  # 情報付き表示で「場所」も出すか(検索結果のとき)

    # ------------------------------------------------------------------ 寸法
    @property
    def box_size(self) -> int:
        return self.base_size * self.zoom

    @staticmethod
    def _small_font(font: QFont) -> QFont:
        small = QFont(font)
        if font.pointSizeF() > 0:
            small.setPointSizeF(font.pointSizeF() * _SMALL_FONT_SCALE)
        return small

    def _list_lines(self, row: Row, info: ThumbInfo) -> list[str]:
        entry = row.entry
        lines = []
        if info.resolution:
            lines.append(tr('解像度: {resolution}').format(resolution=info.resolution))
        if entry.size is not None:
            lines.append(tr('サイズ: {size}').format(size=format_size(entry.size)))
        lines.append(tr('更新日時: {mtime}').format(mtime=format_filetime(entry.mtime)))
        lines.append(tr('種類: {type_text}').format(type_text=type_text(row)))
        if self.show_location:
            lines.append(tr('場所: {location}').format(location=row.location))
        return lines

    def item_size(self, font: QFont) -> QSize:
        """1 項目の大きさ(全項目で同じ)。"""
        box = self.box_size
        small_height = QFontMetrics(self._small_font(font)).height()
        if self.mode == VIEW_TILES:
            height = _PADDING + box + _GAP + small_height * len(self.captions) + _PADDING
            return QSize(box + _PADDING * 2, height)
        text_height = QFontMetrics(font).height() + small_height * 5  # 名前 + 最大 5 行
        return QSize(box + 360, max(box, text_height) + _PADDING * 2)

    def sizeHint(self, option: QStyleOptionViewItem, index: _AnyIndex) -> QSize:
        return self.item_size(option.font)

    # ------------------------------------------------------------------ 描画
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: _AnyIndex) -> None:
        model = index.model()
        row: Row | None = model.row_at(index) if hasattr(model, "row_at") else None  # type: ignore[union-attr]
        if row is None:
            return
        info = self._provider.get(row)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            frame = option.rect.adjusted(1, 1, -1, -1)
            if selected or hovered:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(SELECTION_BACKGROUND if selected else _HOVER_BACKGROUND))
                painter.drawRoundedRect(frame, 4, 4)

            text_color = QColor(SELECTION_TEXT) if selected or hovered else option.palette.color(QPalette.ColorRole.Text)
            dim_color = QColor(text_color)
            dim_color.setAlphaF(0.65)
            if self.mode == VIEW_TILES:
                self._paint_tile(painter, option, row, info, text_color, dim_color)
            else:
                self._paint_list_item(painter, option, row, info, text_color, dim_color)
        finally:
            painter.restore()

    def _paint_thumb(self, painter: QPainter, box: QRect, row: Row, info: ThumbInfo) -> None:
        if info.pixmap is not None:
            size = info.pixmap.size() * self.zoom
            if size.width() > box.width() or size.height() > box.height():
                size = size.scaled(box.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRect(0, 0, max(1, size.width()), max(1, size.height()))
            target.moveCenter(box.center())
            painter.drawPixmap(target, info.pixmap)
            return
        side = min(box.width(), box.height(), _ICON_SIZE * self.zoom)
        target = QRect(0, 0, side, side)
        target.moveCenter(box.center())
        (self._folder_icon if row.entry.is_dir else self._file_icon).paint(painter, target)

    def _caption_text(self, caption: str, row: Row, info: ThumbInfo) -> str:
        if caption == CAPTION_NAME:
            return row.entry.name
        if caption == CAPTION_RESOLUTION:
            return info.resolution
        if caption == CAPTION_SIZE:
            return format_size(row.entry.size)
        if caption == CAPTION_MTIME:
            return format_filetime(row.entry.mtime)
        return ""

    def _paint_tile(self, painter, option, row: Row, info: ThumbInfo, text_color: QColor, dim_color: QColor) -> None:
        rect = option.rect
        box = QRect(rect.x() + (rect.width() - self.box_size) // 2, rect.y() + _PADDING, self.box_size, self.box_size)
        self._paint_thumb(painter, box, row, info)

        font = self._small_font(option.font)
        metrics = QFontMetrics(font)
        painter.setFont(font)
        width = rect.width() - 8
        y = box.bottom() + 1 + _GAP
        for caption in self.captions:
            text = self._caption_text(caption, row, info)
            is_name = caption == CAPTION_NAME
            painter.setPen(text_color if is_name else dim_color)
            elide = Qt.TextElideMode.ElideMiddle if is_name else Qt.TextElideMode.ElideRight
            line = QRect(rect.x() + 4, y, width, metrics.height())
            painter.drawText(line, Qt.AlignmentFlag.AlignCenter, metrics.elidedText(text, elide, width))
            y += metrics.height()

    def _paint_list_item(self, painter, option, row: Row, info: ThumbInfo, text_color: QColor, dim_color: QColor) -> None:
        rect = option.rect
        box = QRect(rect.x() + _PADDING, rect.y() + (rect.height() - self.box_size) // 2, self.box_size, self.box_size)
        self._paint_thumb(painter, box, row, info)

        x = box.right() + 1 + _PADDING * 2
        width = max(10, rect.right() - x - _PADDING)
        name_metrics = QFontMetrics(option.font)
        small_font = self._small_font(option.font)
        small_metrics = QFontMetrics(small_font)
        lines = self._list_lines(row, info)
        total = name_metrics.height() + small_metrics.height() * len(lines)
        y = rect.y() + max(_PADDING, (rect.height() - total) // 2)

        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        painter.setFont(option.font)
        painter.setPen(text_color)
        name = name_metrics.elidedText(row.entry.name, Qt.TextElideMode.ElideMiddle, width)
        painter.drawText(QRect(x, y, width, name_metrics.height()), align, name)
        y += name_metrics.height()

        painter.setFont(small_font)
        painter.setPen(dim_color)
        for text in lines:
            elided = small_metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, width)
            painter.drawText(QRect(x, y, width, small_metrics.height()), align, elided)
            y += small_metrics.height()


class ThumbnailView(QListView):
    """サムネイル表示用のリストビュー。``configure`` でレイアウトを切り替える。"""

    def __init__(self, provider: ThumbnailProvider, folder_icon: QIcon, file_icon: QIcon, parent=None):
        super().__init__(parent)
        self.thumb_delegate = ThumbnailDelegate(provider, folder_icon, file_icon, self)
        self.setItemDelegate(self.thumb_delegate)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setUniformItemSizes(True)
        self.setMouseTracking(True)
        # 件数が多くても固まらないよう、配置は分割して行う
        self.setLayoutMode(QListView.LayoutMode.Batched)
        self.setBatchSize(500)

    def configure(self, mode: str, base_size: int, zoom: int, captions: Iterable[str]) -> None:
        delegate = self.thumb_delegate
        delegate.mode = mode
        delegate.base_size = base_size
        delegate.zoom = zoom
        enabled = set(captions)
        delegate.captions = tuple(caption for caption in CAPTION_LABELS if caption in enabled)

        if mode == VIEW_TILES:
            self.setViewMode(QListView.ViewMode.IconMode)
            self.setFlow(QListView.Flow.LeftToRight)
            self.setWrapping(True)
            self.setSpacing(2)
            self.setGridSize(delegate.item_size(self.font()) + QSize(4, 4))
        else:
            self.setViewMode(QListView.ViewMode.ListMode)
            self.setFlow(QListView.Flow.TopToBottom)
            self.setWrapping(False)
            self.setSpacing(0)
            self.setGridSize(QSize())
        # setViewMode が既定値に戻す項目を設定し直す
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setDragEnabled(False)
        self.setAcceptDrops(False)
        self.setWordWrap(False)
        self.verticalScrollBar().setSingleStep(max(20, delegate.box_size // 4))
        self.scheduleDelayedItemsLayout()
        self.viewport().update()

    def set_show_location(self, show: bool) -> None:
        if self.thumb_delegate.show_location != show:
            self.thumb_delegate.show_location = show
            self.viewport().update()
