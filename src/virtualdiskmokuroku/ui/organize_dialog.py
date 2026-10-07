"""カタログ内のドライブを整理するダイアログ。

ツリー上でドライブの並び替えとグループ間の移動をドラッグ & ドロップ (または上へ / 下へ) で行い、複数ドライブの
一括削除・拡張コンテキストだけの削除・他のカタログへのコピーができる。並び替え・グループ変更・削除は OK を
押したときにまとめてカタログへ書き込む (書き換えは 1 回)。他のカタログへのコピーはその場で行う。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QMimeData, QModelIndex, Qt, Signal
from PySide6.QtGui import QBrush, QFont, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFileIconProvider,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeView,
    QVBoxLayout,
)

from ..core.catalog import CATALOG_EXTENSION, Catalog
from ..core.errors import CatalogError, PasswordError
from ..core.formatting import format_bytes, format_filetime, format_iso, format_size
from .settings_dialogs import ask_new_password, ask_password
from .style import apply_selection_style

ROLE_DRIVE = Qt.ItemDataRole.UserRole + 1  # ドライブの行: drive_id
ROLE_GROUP = Qt.ItemDataRole.UserRole + 2  # グループの行: グループ名

COL_NAME, COL_FILES, COL_SIZE, COL_CONTEXT, COL_LATEST = range(5)
HEADERS = ["名前", "ファイル数", "カタログ内サイズ", "拡張コンテキスト", "最新の更新日時"]
_MIME = "application/x-virtualdiskmokuroku-drives"
_CATALOG_FILTER = f"カタログ (*{CATALOG_EXTENSION})"
_TITLE = "ドライブの整理"


def _tree_position(item: QStandardItem) -> tuple[int, ...]:
    """ツリー上の並び順で比べるためのキー (最上位からの行番号の列)。"""
    rows: list[int] = []
    current: QStandardItem | None = item
    while current is not None:
        rows.append(current.row())
        current = current.parent()
    return tuple(reversed(rows))


class DriveTreeModel(QStandardItemModel):
    """グループとドライブの木。ドラッグ & ドロップでは、ドライブは最上位かグループの中へ、グループは最上位にだけ移せる。

    ドロップでは項目を自分で動かし (``dropMimeData`` は False を返す)、ビューに元の行を消させない。
    """

    moved = Signal()  # ドラッグ & ドロップで項目を動かした後

    def __init__(self, parent=None):
        super().__init__(0, len(HEADERS), parent)
        self.setHorizontalHeaderLabels(HEADERS)
        self._dragging: list[QStandardItem] = []

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction

    def mimeTypes(self) -> list[str]:
        return [_MIME]

    def mimeData(self, indexes) -> QMimeData:
        items = [self.itemFromIndex(index) for index in indexes if index.column() == COL_NAME]
        # グループごと動かすので、選んだグループの中のドライブは別には動かさない
        self._dragging = sorted(
            (item for item in items if not any(item.parent() is other for other in items)), key=_tree_position
        )
        data = QMimeData()
        data.setData(_MIME, b"drives")
        return data

    def canDropMimeData(self, data, action, row, column, parent) -> bool:
        if not data.hasFormat(_MIME) or not self._dragging or action != Qt.DropAction.MoveAction:
            return False
        target = self.itemFromIndex(parent) if parent.isValid() else None
        if target is None:
            return True  # 最上位へ
        if target.data(ROLE_GROUP) is None:
            return False  # ドライブの中には入れられない
        return all(item.data(ROLE_GROUP) is None for item in self._dragging)  # グループの中へはドライブだけ

    def dropMimeData(self, data, action, row, column, parent) -> bool:
        if not self.canDropMimeData(data, action, row, column, parent):
            return False
        target = self.itemFromIndex(parent) if parent.isValid() else self.invisibleRootItem()
        self.move_items(self._dragging, target, row)
        self._dragging = []
        self.moved.emit()
        return False  # 自分で動かしたので、ビューに元の行を消させない

    def move_items(self, items: list[QStandardItem], target: QStandardItem, row: int) -> None:
        """``items`` を ``target`` の ``row`` 番目 (-1 なら末尾) の前へ、並び順を保ったまま移す。"""
        marker = None
        if row >= 0:
            # 動かす項目自身の位置に落とされても、それ以外の次の項目の前に入れる
            for position in range(row, target.rowCount()):
                candidate = target.child(position)
                if not any(candidate is item for item in items):
                    marker = candidate
                    break
        taken = []
        for item in sorted(items, key=_tree_position):
            holder = item.parent() or self.invisibleRootItem()
            taken.append(holder.takeRow(item.row()))
        position = marker.row() if marker is not None else target.rowCount()
        for offset, cells in enumerate(taken):
            target.insertRow(position + offset, cells)


class OrganizeDialog(QDialog):
    def __init__(self, catalog: Catalog, parent=None, prepare_rewrite: Callable[[], None] | None = None):
        super().__init__(parent)
        self.catalog = catalog
        self._prepare_rewrite = prepare_rewrite
        self.changed = False  # OK でカタログを書き換えたか
        self.reload_needed = False  # 書き換えに備えて呼び出し側の DB 接続を閉じたか (失敗しても開き直しが要る)
        self._removed: dict[str, dict] = {}  # 削除予定のドライブ (id → 情報)
        self._context_drop: set[str] = set()  # 拡張コンテキストを削除する予定のドライブ
        icons = QFileIconProvider()
        self._drive_icon = icons.icon(QFileIconProvider.IconType.Drive)
        self._group_icon = icons.icon(QFileIconProvider.IconType.Folder)
        self.setWindowTitle(_TITLE)
        self.resize(900, 520)
        self._build_ui()
        self._fill_tree()
        self._original_layout = self.layout_of_tree()
        self._update_buttons()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.model = DriveTreeModel(self)
        self.model.moved.connect(self._after_drop)
        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.tree.setDragDropOverwriteMode(False)
        self.tree.setDropIndicatorShown(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setAllColumnsShowFocus(True)
        apply_selection_style(self.tree)
        self.tree.selectionModel().selectionChanged.connect(lambda *_args: self._update_buttons())

        def button(text: str, slot: Callable[[], object]) -> QPushButton:
            item = QPushButton(text)
            item.clicked.connect(lambda _checked=False: slot())
            return item

        self.up_button = button("上へ(&U)", self.move_up)
        self.down_button = button("下へ(&D)", self.move_down)
        self.new_group_button = button("新しいグループ(&N)…", self.new_group)
        self.rename_group_button = button("グループ名を変更(&R)…", self.rename_group)
        self.ungroup_button = button("グループから外す(&G)", self.ungroup)
        self.context_button = button("拡張コンテキストを削除(&X)", self.toggle_context_removal)
        self.remove_button = button("ドライブを削除(&L)", self.remove_selected)
        self.copy_button = button("他のカタログにコピー(&C)…", self.copy_to_catalog)
        self.copy_button.setToolTip(
            "選んだドライブの現在の内容 (ファイルリストと拡張コンテキスト) を別のカタログに追加します。\n"
            "バックアップ世代はコピーしません。コピーはすぐに行われ、グループはこのツリーでの状態になります"
        )
        self.context_button.setToolTip("ファイルリストは残し、サムネイル・テキスト内容などの拡張コンテキストだけを削除します。\nもう一度押すと取りやめます")

        buttons = QVBoxLayout()
        for item in (self.up_button, self.down_button):
            buttons.addWidget(item)
        buttons.addSpacing(12)
        for item in (self.new_group_button, self.rename_group_button, self.ungroup_button):
            buttons.addWidget(item)
        buttons.addSpacing(12)
        for item in (self.context_button, self.remove_button):
            buttons.addWidget(item)
        buttons.addSpacing(12)
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)

        body = QHBoxLayout()
        body.addWidget(self.tree, 1)
        body.addLayout(buttons)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "ドラッグ & ドロップで並び替えやグループ間の移動ができます (複数選択可)。"
            "並び替え・グループの変更・削除は OK を押したときにカタログへ書き込みます。"
        ))  # fmt: skip
        layout.addLayout(body, 1)
        layout.addWidget(self.summary)
        layout.addWidget(self.button_box)

    def _fill_tree(self) -> None:
        groups: dict[str, QStandardItem] = {}
        root = self.model.invisibleRootItem()
        try:
            sizes = self.catalog.storage_sizes()
        except (CatalogError, OSError):
            sizes = {}
        for drive in self.catalog.drives:
            cells = self._drive_cells(drive, sizes.get(drive["id"], (0, 0)))
            group = drive.get("group")
            if not group:
                root.appendRow(cells)
                continue
            parent = groups.get(group)
            if parent is None:
                parent = groups[group] = self._group_item(group)
                root.appendRow(self._group_cells(parent))
            parent.appendRow(cells)
        self.tree.expandAll()
        for column, width in enumerate((260, 80, 110, 120, 150)):
            self.tree.setColumnWidth(column, width)
        self.tree.header().setStretchLastSection(True)

    def _group_item(self, name: str) -> QStandardItem:
        item = QStandardItem(self._group_icon, name)
        item.setData(name, ROLE_GROUP)
        font = QFont()
        font.setBold(True)
        item.setFont(font)
        item.setFlags(
            Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled
        )  # fmt: skip
        return item

    def _group_cells(self, item: QStandardItem) -> list[QStandardItem]:
        cells = [item] + [QStandardItem() for _ in HEADERS[1:]]
        for cell in cells[1:]:
            cell.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        return cells

    def _drive_cells(self, drive: dict, storage: tuple[int, int]) -> list[QStandardItem]:
        """``storage`` はカタログ内で占めるバイト数 (現行世代, バックアップ世代の合計)。"""
        name = QStandardItem(self._drive_icon, drive["name"])
        name.setData(drive["id"], ROLE_DRIVE)
        files = QStandardItem(f"{drive.get('file_count') or 0:,}")
        files.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        current, backup = storage
        size = QStandardItem(format_size(current))
        size.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        tooltip = f"カタログ内でファイルリストと拡張コンテキストが占めるサイズ: {format_bytes(current)} バイト"
        if backup:
            tooltip += f"\nバックアップ世代: {format_size(backup)} ({format_bytes(backup)} バイト)"
        size.setToolTip(tooltip)
        context = QStandardItem(self._context_text(drive))
        latest = QStandardItem(format_filetime(self._latest_mtime(drive)))
        latest.setToolTip(
            "ドライブ内で最も新しいファイルの更新日時\n"
            f"スキャン (取り込み) 日時: {format_iso(drive.get('scanned_at'))}"
        )
        cells = [name, files, size, context, latest]
        for cell in cells:
            cell.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled)
        return cells

    def _latest_mtime(self, drive: dict) -> int | None:
        """最新の更新日時。古いカタログで記録が無ければ DB から求める (開けなければ空欄)。"""
        try:
            return self.catalog.latest_mtime(drive["id"])
        except (CatalogError, OSError, sqlite3.Error):
            return None

    @staticmethod
    def _context_text(drive: dict) -> str:
        if not drive.get("has_context"):
            return "なし"
        return "あり (一部)" if drive.get("context_partial") else "あり"

    # ------------------------------------------------------------------ 選択
    def _name_item(self, index: QModelIndex) -> QStandardItem | None:
        if not index.isValid():
            return None
        return self.model.itemFromIndex(index.siblingAtColumn(COL_NAME))

    def _selected_items(self) -> list[QStandardItem]:
        """選択中の行 (名前の項目) をツリーの並び順で。"""
        items = [self._name_item(index) for index in self.tree.selectionModel().selectedRows(COL_NAME)]
        return sorted((item for item in items if item is not None), key=_tree_position)

    def _selected_drive_items(self) -> list[QStandardItem]:
        """選択中のドライブ (選んだグループの中のドライブも含む)。"""
        found: list[QStandardItem] = []
        for item in self._selected_items():
            if item.data(ROLE_GROUP) is not None:
                found += [item.child(row) for row in range(item.rowCount())]
            elif not any(item is other for other in found):
                found.append(item)
        return found

    def _select_items(self, items: list[QStandardItem]) -> None:
        selection = self.tree.selectionModel()
        selection.clearSelection()
        flags = QItemSelectionModel.SelectionFlag
        for item in items:
            selection.select(item.index(), flags.Select | flags.Rows)
        if items:
            selection.setCurrentIndex(items[-1].index(), flags.NoUpdate)
            self.tree.scrollTo(items[-1].index())

    def _update_buttons(self) -> None:
        items = self._selected_items()
        drives = [item for item in items if item.data(ROLE_DRIVE) is not None]
        groups = [item for item in items if item.data(ROLE_GROUP) is not None]
        self.up_button.setEnabled(bool(items))
        self.down_button.setEnabled(bool(items))
        self.rename_group_button.setEnabled(len(groups) == 1 and not drives)
        self.ungroup_button.setEnabled(any(item.parent() is not None for item in drives))
        selected_drives = self._selected_drive_items()
        self.remove_button.setEnabled(bool(items))
        self.copy_button.setEnabled(bool(selected_drives))
        with_context = [item for item in selected_drives if self._has_context(item)]
        self.context_button.setEnabled(bool(with_context))
        if with_context and all(item.data(ROLE_DRIVE) in self._context_drop for item in with_context):
            self.context_button.setText("拡張コンテキストの削除を取りやめ(&X)")
        else:
            self.context_button.setText("拡張コンテキストを削除(&X)")
        self._update_summary()

    def _has_context(self, item: QStandardItem) -> bool:
        return bool(self.catalog.drive(item.data(ROLE_DRIVE)).get("has_context"))

    def _update_summary(self) -> None:
        parts = []
        if self._removed:
            parts.append(f"OK を押すと {len(self._removed)} 台のドライブを削除します (バックアップ世代も含む)")
        if self._context_drop:
            parts.append(f"{len(self._context_drop)} 台のドライブの拡張コンテキストを削除します")
        self.summary.setText("。".join(parts) + ("。キャンセルで取りやめます" if parts else ""))

    def _after_drop(self) -> None:
        self.tree.expandAll()
        self._update_buttons()

    # ------------------------------------------------------------------ 並び替え・グループ
    def move_up(self) -> None:
        self._shift(-1)

    def move_down(self) -> None:
        self._shift(1)

    def _shift(self, delta: int) -> None:
        """選択中の項目を同じ親の中で 1 つ上 (下) へ。端にあるものはそのまま。"""
        items = self._selected_items()
        if not items:
            return
        ordered = items if delta < 0 else list(reversed(items))
        for item in ordered:
            holder = item.parent() or self.model.invisibleRootItem()
            row = item.row()
            target_row = row + delta
            if not 0 <= target_row < holder.rowCount():
                continue
            neighbour = holder.child(target_row)
            if any(neighbour is other for other in items):
                continue  # 隣も動かす対象なら、全体として端に達している
            holder.insertRow(target_row, holder.takeRow(row))
        self.tree.expandAll()
        self._select_items(items)

    def new_group(self, name: str | None = None) -> None:
        """新しいグループを作り、選択中のドライブを入れる。"""
        drives = self._selected_drive_items()
        if name is None:
            name, accepted = QInputDialog.getText(self, "新しいグループ", "グループ名:")
            if not accepted:
                return
        name = name.strip()
        if not name:
            return
        existing = self._group_item_named(name)
        if existing is not None:
            QMessageBox.warning(self, _TITLE, f"グループ「{name}」は既にあります。")
            return
        group = self._group_item(name)
        position = self.model.rowCount()
        if drives:
            top = drives[0].parent() or drives[0]  # 最初に選んだドライブ (のグループ) の位置に作る
            position = top.row()
        self.model.invisibleRootItem().insertRow(position, self._group_cells(group))
        if drives:
            self.model.move_items(drives, group, -1)
        self.tree.expandAll()
        self._select_items([group])

    def _group_item_named(self, name: str) -> QStandardItem | None:
        root = self.model.invisibleRootItem()
        for row in range(root.rowCount()):
            item = root.child(row)
            if item.data(ROLE_GROUP) == name:
                return item
        return None

    def rename_group(self, name: str | None = None) -> None:
        items = [item for item in self._selected_items() if item.data(ROLE_GROUP) is not None]
        if len(items) != 1:
            return
        group = items[0]
        if name is None:
            name, accepted = QInputDialog.getText(self, "グループ名を変更", "グループ名:", text=group.text())
            if not accepted:
                return
        name = name.strip()
        if not name or name == group.data(ROLE_GROUP):
            return
        other = self._group_item_named(name)
        if other is not None:
            answer = QMessageBox.question(self, _TITLE, f"グループ「{name}」は既にあります。1 つにまとめますか?")
            if answer != QMessageBox.StandardButton.Yes:
                return
            drives = [group.child(row) for row in range(group.rowCount())]
            self.model.move_items(drives, other, -1)
            self.model.invisibleRootItem().takeRow(group.row())
            self.tree.expandAll()
            self._select_items([other])
            return
        group.setText(name)
        group.setData(name, ROLE_GROUP)
        self._update_buttons()

    def ungroup(self) -> None:
        """選択中のドライブをグループの外 (そのグループの直後) へ出す。"""
        drives = [item for item in self._selected_items() if item.data(ROLE_DRIVE) is not None and item.parent() is not None]
        if not drives:
            return
        root = self.model.invisibleRootItem()
        groups = sorted({_tree_position(item.parent()): item.parent() for item in drives}.items(), reverse=True)
        for _position, group in groups:
            members = [item for item in drives if item.parent() is group]
            self.model.move_items(members, root, group.row() + 1)
        self.tree.expandAll()
        self._select_items(drives)

    # ------------------------------------------------------------------ 削除
    def toggle_context_removal(self) -> None:
        drives = [item for item in self._selected_drive_items() if self._has_context(item)]
        if not drives:
            return
        ids = {item.data(ROLE_DRIVE) for item in drives}
        if ids <= self._context_drop:
            self._context_drop -= ids
        else:
            self._context_drop |= ids
        for item in drives:
            holder = item.parent() or self.model.invisibleRootItem()
            cell = holder.child(item.row(), COL_CONTEXT)
            drive = self.catalog.drive(item.data(ROLE_DRIVE))
            pending = item.data(ROLE_DRIVE) in self._context_drop
            cell.setText("削除予定" if pending else self._context_text(drive))
            cell.setData(QBrush(Qt.GlobalColor.red) if pending else None, Qt.ItemDataRole.ForegroundRole)
        self._update_buttons()

    def remove_selected(self) -> None:
        """選択中のドライブ (グループを選んだ場合はその中の全ドライブ) をツリーから外し、削除予定にする。"""
        items = self._selected_items()
        drives = self._selected_drive_items()
        if not items:
            return
        if drives:
            names = "\n".join(f"  {item.text()}" for item in drives[:10])
            if len(drives) > 10:
                names += f"\n  … ほか {len(drives) - 10} 台"
            answer = QMessageBox.question(
                self, _TITLE,
                f"{len(drives)} 台のドライブを削除予定にします (OK を押したときにカタログから削除されます)。\n\n{names}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
            )  # fmt: skip
            if answer != QMessageBox.StandardButton.Yes:
                return
        for item in drives:
            drive_id = item.data(ROLE_DRIVE)
            self._removed[drive_id] = self.catalog.drive(drive_id)
            self._context_drop.discard(drive_id)
        for item in sorted(items, key=_tree_position, reverse=True):
            holder = item.parent() or self.model.invisibleRootItem()
            if item.data(ROLE_GROUP) is not None or holder.child(item.row()) is item:
                holder.takeRow(item.row())
        # 選んだグループの中のドライブは、グループごと外れている
        self._update_buttons()

    # ------------------------------------------------------------------ 他のカタログへのコピー
    def copy_to_catalog(self, path: str | None = None, password: str | None = None) -> list[dict] | None:
        """選択中のドライブを別のカタログ (無ければ新規作成) へ追加する。その場で実行する。"""
        drives = self._selected_drive_items()
        if not drives:
            return None
        ids = [item.data(ROLE_DRIVE) for item in drives]
        if path is None:
            path, _filter = QFileDialog.getSaveFileName(
                self, "コピー先のカタログ (既存のカタログを選ぶとそこへ追加)", "", _CATALOG_FILTER,
                options=QFileDialog.Option.DontConfirmOverwrite,
            )  # fmt: skip
            if not path:
                return None
            if not path.lower().endswith(CATALOG_EXTENSION):
                path += CATALOG_EXTENSION
        try:
            if Path(path).resolve() == self.catalog.path.resolve():
                raise CatalogError("いま開いているカタログ自身にはコピーできません")
            target = self._open_target(path, password)
            if target is None:
                return None
        except (CatalogError, OSError) as error:
            QMessageBox.critical(self, _TITLE, f"コピー先のカタログを開けません。\n\n{error}")
            return None
        try:
            if self.catalog.encrypted and not target.encrypted:
                answer = QMessageBox.warning(
                    self, _TITLE,
                    "コピー先は暗号化されていないカタログです。コピーしたドライブの中身は誰でも読めます。続けますか?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
                )  # fmt: skip
                if answer != QMessageBox.StandardButton.Yes:
                    return None
            groups = dict(self.layout_of_tree())  # ツリー上でまだ確定していないグループも、コピー先ではそのまま使う
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                copied = self.catalog.copy_drives_to(target, ids, groups=groups)
            finally:
                QApplication.restoreOverrideCursor()
        except (CatalogError, OSError) as error:
            QMessageBox.critical(self, _TITLE, f"コピーに失敗しました。\n\n{error}")
            return None
        finally:
            target.close()
        QMessageBox.information(self, _TITLE, f"{len(copied)} 台のドライブを {path} に追加しました。")
        return copied

    def _open_target(self, path: str, password: str | None) -> Catalog | None:
        """コピー先を開く (無ければ作る)。キャンセルなら None。"""
        if os.path.exists(path):
            if not Catalog.is_password_protected(path):
                return Catalog.open(path)
            while True:
                if password is None:
                    password = ask_password(self, "パスワード", f"{Path(path).name} のパスワード:")
                    if password is None:
                        return None
                try:
                    return Catalog.open(path, password)
                except PasswordError as error:
                    QMessageBox.warning(self, _TITLE, str(error))
                    password = None
        encrypt = False
        if self.catalog.encrypted and password is None:
            answer = QMessageBox.question(
                self, _TITLE,
                "新しいカタログを作成します。いま開いているカタログと同じように暗号化しますか?\n(パスワードは新しく設定します)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )  # fmt: skip
            if answer == QMessageBox.StandardButton.Cancel:
                return None
            if answer == QMessageBox.StandardButton.Yes:
                password = ask_new_password(self, "暗号化のパスワード")
                if password is None:
                    return None
                encrypt = True
        elif password is not None:
            encrypt = True
        return Catalog.create(path, password, encrypt=encrypt)

    # ------------------------------------------------------------------ 確定
    def layout_of_tree(self) -> list[tuple[str, str | None]]:
        """ツリーの現在の並び (drive_id, グループ名)。ドライブの無いグループは含まれない。"""
        layout: list[tuple[str, str | None]] = []
        root = self.model.invisibleRootItem()
        for row in range(root.rowCount()):
            item = root.child(row)
            group = item.data(ROLE_GROUP)
            if group is None:
                layout.append((item.data(ROLE_DRIVE), None))
            else:
                layout += [(item.child(child).data(ROLE_DRIVE), group) for child in range(item.rowCount())]
        return layout

    def accept(self) -> None:
        layout = self.layout_of_tree()
        if layout == self._original_layout and not self._removed and not self._context_drop:
            super().accept()
            return
        if self._removed:
            answer = QMessageBox.question(
                self, _TITLE,
                f"{len(self._removed)} 台のドライブをカタログから削除します。バックアップ世代も含めて削除され、元に戻せません。"
                "\nよろしいですか?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
            )  # fmt: skip
            if answer != QMessageBox.StandardButton.Yes:
                return
        if self._prepare_rewrite is not None:
            self._prepare_rewrite()
            self.reload_needed = True
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.catalog.reorganize(layout, remove=list(self._removed), drop_context=sorted(self._context_drop))
        except (CatalogError, OSError) as error:
            QMessageBox.critical(self, _TITLE, f"カタログを書き換えられませんでした。\n\n{error}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.changed = True
        super().accept()
