"""ウィジェット共通の見た目。"""

from __future__ import annotations

from PySide6.QtWidgets import QAbstractItemView

SELECTION_BACKGROUND = "#cce8ff"  # エクスプローラの選択行に近い薄い水色
SELECTION_TEXT = "#000000"

# OS のスタイル任せだと濃い青の背景に黒字となって読みにくいので、選択行の配色を固定する
# (フォーカスが外れているときも同じ色にする)
_ITEM_VIEW_STYLE = f"""
QAbstractItemView::item:selected,
QAbstractItemView::item:selected:active,
QAbstractItemView::item:selected:!active {{
    background-color: {SELECTION_BACKGROUND};
    color: {SELECTION_TEXT};
}}
"""


def apply_selection_style(*views: QAbstractItemView) -> None:
    for view in views:
        view.setStyleSheet(_ITEM_VIEW_STYLE)
