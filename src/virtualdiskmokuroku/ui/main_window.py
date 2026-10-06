"""エクスプローラ風のカタログビューア。"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QModelIndex, QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QGuiApplication, QKeySequence, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFileIconProvider,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
    QStyle,
    QTableView,
    QToolBar,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core import export
from ..core.catalog import CATALOG_EXTENSION, LEGACY_CATALOG_EXTENSIONS, Catalog
from ..core.drive_db import ROOT_ID, DriveDB, split_terms
from ..core.errors import CatalogError, PasswordError
from ..core.formatting import format_bytes, format_iso, format_size
from ..core.search import SCOPE_ALL, SCOPE_FOLDER, SCOPE_SUBTREE, DbPool, DriveRef, QuerySpec, Row, iter_rows
from ..core.settings import AppSettings
from .models import COL_DRIVE, COL_LOCATION, COL_NAME, FileTableModel
from .properties_panel import PropertiesPanel
from .query_worker import QueryWorker
from .scan_dialog import ScanDialog
from .settings_dialogs import AppSettingsDialog, CatalogSettingsDialog, ask_password
from .style import apply_selection_style

APP_NAME = "VirtualDiskMokuroku"
_CATALOG_FILTER = f"カタログ (*{CATALOG_EXTENSION})"
_OPEN_FILTER = "カタログ (" + " ".join(f"*{ext}" for ext in (CATALOG_EXTENSION, *LEGACY_CATALOG_EXTENSIONS)) + ")"
_FILTER_DELAY_MS = 180

ROLE_DRIVE = Qt.ItemDataRole.UserRole + 1
ROLE_DIR = Qt.ItemDataRole.UserRole + 2
ROLE_LOADED = Qt.ItemDataRole.UserRole + 3


@contextmanager
def wait_cursor():
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        yield
    finally:
        QApplication.restoreOverrideCursor()


class MainWindow(QMainWindow):
    queryRequested = Signal(int, object)
    workerCloseRequested = Signal()

    def __init__(self, app_settings: AppSettings | None = None):
        super().__init__()
        self.app_settings = app_settings or AppSettings.load()
        self.catalog: Catalog | None = None
        self._refs: dict[str, DriveRef] = {}
        self._dbs: dict[str, DriveDB] = {}
        self._contexts: dict[str, object] = {}
        self._location: tuple[str, int] | None = None
        self._history: list[tuple[str, int]] = []
        self._history_index = -1
        self._generation = 0
        self._spec: QuerySpec | None = None
        self._truncated = False
        self._pending_select: tuple[str, int] | None = None

        icons = QFileIconProvider()
        self._drive_icon = icons.icon(QFileIconProvider.IconType.Drive)
        self._folder_icon = icons.icon(QFileIconProvider.IconType.Folder)
        self._file_icon = icons.icon(QFileIconProvider.IconType.File)

        self._build_ui()
        self._build_actions()
        self._start_worker()
        self._restore_window_state()
        self._update_ui_state()

    # ================================================================== UI 構築
    def _build_ui(self) -> None:
        self.resize(1200, 720)

        # --- 左: フォルダツリー
        self.tree_model = QStandardItemModel(self)
        self.tree = QTreeView()
        self.tree.setModel(self.tree_model)
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.expanded.connect(self._on_tree_expanded)
        self.tree.selectionModel().currentChanged.connect(self._on_tree_current_changed)
        self.tree.customContextMenuRequested.connect(self._show_tree_menu)

        # --- 右: フィルタ + 一覧
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("フィルタ (入力するとすぐに絞り込み。空白区切りで AND 条件)")
        self.filter_edit.setClearButtonEnabled(True)
        self.scope_combo = QComboBox()
        self.scope_combo.addItem("このフォルダ", SCOPE_FOLDER)
        self.scope_combo.addItem("下位フォルダを含む", SCOPE_SUBTREE)
        self.scope_combo.addItem("全ドライブ", SCOPE_ALL)
        self.context_check = QCheckBox("拡張コンテキストも検索")
        self.context_check.setToolTip("メタ情報・テキスト内容・書庫内のファイル名も検索対象にします")
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(_FILTER_DELAY_MS)
        self._filter_timer.timeout.connect(self._run_query)
        self.filter_edit.textChanged.connect(lambda _text: self._filter_timer.start())
        self.scope_combo.currentIndexChanged.connect(lambda _index: self._run_query())
        self.context_check.toggled.connect(lambda _checked: self._run_query())

        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.addWidget(self.filter_edit, 1)
        filter_row.addWidget(QLabel("範囲:"))
        filter_row.addWidget(self.scope_combo)
        filter_row.addWidget(self.context_check)

        self.table_model = FileTableModel(self._folder_icon, self._file_icon, self)
        self.table = QTableView()
        self.table.setModel(self.table_model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        header = self.table.horizontalHeader()
        header.setSortIndicator(COL_NAME, Qt.SortOrder.AscendingOrder)
        header.setStretchLastSection(True)
        header.setSectionsMovable(True)
        self.table.setSortingEnabled(True)
        for column, width in enumerate((320, 90, 140, 120, 60, 320, 140)):
            self.table.setColumnWidth(column, width)
        # 検索時に「場所」が画面外へ押し出されないよう、名前のすぐ右に置く
        header.moveSection(header.visualIndex(COL_LOCATION), 1)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_table_menu)
        self.table.doubleClicked.connect(self._on_table_double_clicked)
        self.table.selectionModel().selectionChanged.connect(lambda *_args: self._on_selection_changed())

        apply_selection_style(self.tree, self.table)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 4, 4, 0)
        right_layout.addLayout(filter_row)
        right_layout.addWidget(self.table, 1)

        self.splitter = QSplitter()
        self.splitter.addWidget(self.tree)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([300, 900])
        self.setCentralWidget(self.splitter)

        # --- プロパティ
        self.properties = PropertiesPanel()
        self.properties.redecodeRequested.connect(self._redecode_text)
        self.properties_dock = QDockWidget("プロパティ", self)
        self.properties_dock.setObjectName("propertiesDock")
        self.properties_dock.setWidget(self.properties)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.properties_dock)
        self.resizeDocks([self.properties_dock], [340], Qt.Orientation.Horizontal)

        # --- ステータスバー
        self.items_label = QLabel()
        self.drive_label = QLabel()
        self.statusBar().addWidget(self.items_label, 1)
        self.statusBar().addPermanentWidget(self.drive_label)

    def _build_actions(self) -> None:
        style = self.style()

        def action(text: str, slot, shortcut=None, icon: QStyle.StandardPixmap | None = None) -> QAction:
            item = QAction(text, self)
            item.triggered.connect(lambda _checked=False: slot())
            if shortcut is not None:
                item.setShortcut(shortcut)
            if icon is not None:
                item.setIcon(style.standardIcon(icon))
            return item

        self.act_new = action("新しいカタログ(&N)…", self.new_catalog, QKeySequence.StandardKey.New)
        self.act_open = action("カタログを開く(&O)…", self.open_catalog_dialog, QKeySequence.StandardKey.Open)
        self.act_close = action("カタログを閉じる(&C)", self.close_catalog)
        self.act_exit = action("終了(&X)", self.close)
        self.act_export = action("表示中の一覧をエクスポート(&E)…", self.export_rows, "Ctrl+E")
        self.act_copy_names = action("名前をコピー(&C)", self.copy_names, QKeySequence.StandardKey.Copy)
        self.act_copy_paths = action("フルパスをコピー(&P)", self.copy_paths, "Ctrl+Shift+C")
        self.act_select_all = action("すべて選択(&A)", self.table.selectAll, QKeySequence.StandardKey.SelectAll)
        self.act_open_location = action("場所を開く(&L)", self.open_location)
        self.act_find = action("フィルタにフォーカス(&F)", self._focus_filter, QKeySequence.StandardKey.Find)
        self.act_refresh = action("最新の情報に更新(&R)", self._run_query, "F5")

        self.act_back = action("戻る", self.go_back, "Alt+Left", QStyle.StandardPixmap.SP_ArrowBack)
        self.act_forward = action("進む", self.go_forward, "Alt+Right", QStyle.StandardPixmap.SP_ArrowForward)
        self.act_up = action("上へ", self.go_up, "Alt+Up", QStyle.StandardPixmap.SP_ArrowUp)

        self.act_scan = action("ドライブの追加 / 更新(&A)…", self.scan_drive, "Ctrl+D")
        self.act_rescan = action("このドライブを更新 (再スキャン)(&U)…", self.rescan_current_drive)
        self.act_rename = action("ドライブの表示名を変更(&R)…", self.rename_current_drive)
        self.act_drive_info = action("ドライブ情報(&I)…", self.show_drive_info)
        self.act_remove = action("ドライブをカタログから削除(&D)…", self.remove_current_drive)
        self.restore_menu = QMenu("バックアップから復元(&B)", self)
        self.restore_menu.aboutToShow.connect(self._fill_restore_menu)

        self.act_catalog_settings = action("カタログ設定(&C)…", self.edit_catalog_settings)
        self.act_app_settings = action("アプリ設定(&A)…", self.edit_app_settings)
        self.act_everything_help = action("Everything の導入方法(&E)", self.show_everything_help)
        self.act_about = action("バージョン情報(&A)", self.show_about)

        menu = self.menuBar()
        file_menu = menu.addMenu("ファイル(&F)")
        file_menu.addAction(self.act_new)
        file_menu.addAction(self.act_open)
        self.recent_menu = file_menu.addMenu("最近使ったカタログ(&R)")
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        file_menu.addAction(self.act_close)
        file_menu.addSeparator()
        file_menu.addAction(self.act_export)
        file_menu.addSeparator()
        file_menu.addAction(self.act_exit)

        edit_menu = menu.addMenu("編集(&E)")
        for item in (self.act_copy_names, self.act_copy_paths, self.act_select_all, self.act_find):
            edit_menu.addAction(item)

        view_menu = menu.addMenu("表示(&V)")
        for item in (self.act_back, self.act_forward, self.act_up, self.act_refresh):
            view_menu.addAction(item)
        view_menu.addSeparator()
        view_menu.addAction(self.properties_dock.toggleViewAction())

        drive_menu = menu.addMenu("ドライブ(&D)")
        drive_menu.addAction(self.act_scan)
        drive_menu.addAction(self.act_rescan)
        drive_menu.addSeparator()
        drive_menu.addAction(self.act_rename)
        drive_menu.addAction(self.act_drive_info)
        drive_menu.addMenu(self.restore_menu)
        drive_menu.addAction(self.act_remove)

        settings_menu = menu.addMenu("設定(&S)")
        settings_menu.addAction(self.act_catalog_settings)
        settings_menu.addAction(self.act_app_settings)

        help_menu = menu.addMenu("ヘルプ(&H)")
        help_menu.addAction(self.act_everything_help)
        help_menu.addAction(self.act_about)

        toolbar = QToolBar("ナビゲーション", self)
        toolbar.setObjectName("navigationToolbar")
        toolbar.setMovable(False)
        for item in (self.act_back, self.act_forward, self.act_up):
            toolbar.addAction(item)
        self.address = QLineEdit()
        self.address.setPlaceholderText("カタログを開くか、新しいカタログを作成してください")
        self.address.returnPressed.connect(self._on_address_entered)
        toolbar.addWidget(self.address)
        self.addToolBar(toolbar)

    def _start_worker(self) -> None:
        self._thread = QThread(self)
        self._worker = QueryWorker()
        self._worker.moveToThread(self._thread)
        self.queryRequested.connect(self._worker.run)
        self.workerCloseRequested.connect(self._worker.close_all)
        self._worker.finished.connect(self._on_query_finished)
        self._thread.start()

    # ================================================================== ウィンドウ状態
    def _restore_window_state(self) -> None:
        state = QSettings(APP_NAME, APP_NAME)
        geometry = state.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        window_state = state.value("windowState")
        if window_state is not None:
            self.restoreState(window_state)
        splitter = state.value("splitter")
        if splitter is not None:
            self.splitter.restoreState(splitter)

    def closeEvent(self, event: QCloseEvent) -> None:
        state = QSettings(APP_NAME, APP_NAME)
        state.setValue("geometry", self.saveGeometry())
        state.setValue("windowState", self.saveState())
        state.setValue("splitter", self.splitter.saveState())
        self._generation += 1
        self._worker.supersede(self._generation)
        self._thread.quit()
        self._thread.wait(3000)
        self._close_databases()
        self._worker.close_all()
        super().closeEvent(event)

    def _update_ui_state(self) -> None:
        has_catalog = self.catalog is not None
        has_drive = self._location is not None
        for item in (self.act_close, self.act_scan, self.act_catalog_settings):
            item.setEnabled(has_catalog)
        for item in (self.act_rescan, self.act_rename, self.act_drive_info, self.act_remove, self.act_export):
            item.setEnabled(has_drive)
        self.restore_menu.setEnabled(has_drive)
        for widget in (self.filter_edit, self.scope_combo, self.address):
            widget.setEnabled(has_catalog)
        has_context = has_catalog and any(drive.get("has_context") for drive in self.catalog.drives)  # type: ignore[union-attr]
        self.context_check.setEnabled(bool(has_context))
        self.act_back.setEnabled(self._history_index > 0)
        self.act_forward.setEnabled(self._history_index < len(self._history) - 1)
        self.act_up.setEnabled(has_drive and self._location[1] != ROOT_ID)  # type: ignore[index]
        title = APP_NAME
        if self.catalog is not None:
            title = f"{self.catalog.path.name} - {APP_NAME}"
        self.setWindowTitle(title)

    # ================================================================== カタログの開閉
    def new_catalog(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(self, "新しいカタログ", "", _CATALOG_FILTER)
        if not path:
            return
        if not path.lower().endswith(CATALOG_EXTENSION):
            path += CATALOG_EXTENSION
        try:
            if os.path.exists(path):
                os.remove(path)  # 上書きはファイルダイアログで確認済み
            catalog = Catalog.create(path)
        except (CatalogError, OSError) as error:
            QMessageBox.critical(self, APP_NAME, f"カタログを作成できません。\n\n{error}")
            return
        self._set_catalog(catalog)
        answer = QMessageBox.question(self, APP_NAME, "カタログを作成しました。続けてドライブを追加しますか?")
        if answer == QMessageBox.StandardButton.Yes:
            self.scan_drive()

    def open_catalog_dialog(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, "カタログを開く", "", _OPEN_FILTER)
        if path:
            self.open_catalog(path)

    def open_catalog(self, path: str | os.PathLike[str]) -> bool:
        try:
            password = None
            if Catalog.is_password_protected(path):
                while True:
                    password = ask_password(self, "パスワード", f"{Path(path).name} のパスワード:")
                    if password is None:
                        return False
                    try:
                        catalog = Catalog.open(path, password)
                        break
                    except PasswordError as error:
                        QMessageBox.warning(self, APP_NAME, str(error))
            else:
                catalog = Catalog.open(path)
        except (CatalogError, OSError) as error:
            QMessageBox.critical(self, APP_NAME, f"カタログを開けません。\n\n{error}")
            return False
        self._set_catalog(catalog)
        return True

    def _set_catalog(self, catalog: Catalog | None) -> None:
        self._close_databases()
        self.catalog = catalog
        self._location = None
        self._history.clear()
        self._history_index = -1
        self.filter_edit.blockSignals(True)
        self.filter_edit.clear()
        self.filter_edit.blockSignals(False)
        self.address.clear()
        self.table_model.set_rows([])
        self.properties.show_row(None, None)
        if catalog is not None:
            self.app_settings.add_recent(catalog.path)
            self.app_settings.save()
        self._reload_tree()
        if catalog is not None and catalog.drives:
            self.navigate(catalog.drives[0]["id"], ROOT_ID)
        else:
            self._update_status()
        self._update_ui_state()

    def close_catalog(self) -> None:
        self._set_catalog(None)

    def _fill_recent_menu(self) -> None:
        self.recent_menu.clear()
        for path in self.app_settings.recent_catalogs:
            item = self.recent_menu.addAction(path)
            item.triggered.connect(lambda _checked=False, target=path: self.open_catalog(target))
        if not self.app_settings.recent_catalogs:
            self.recent_menu.addAction("(なし)").setEnabled(False)

    # ================================================================== DB 接続
    def _ref(self, drive_id: str) -> DriveRef:
        ref = self._refs.get(drive_id)
        if ref is None:
            assert self.catalog is not None
            drive = self.catalog.drive(drive_id)
            with wait_cursor():
                files_db = self.catalog.extract_db(drive_id)
                context_db = self.catalog.extract_context_db(drive_id) if drive.get("has_context") else None
            ref = self._refs[drive_id] = DriveRef(drive_id, drive["name"], files_db, context_db)
        return ref

    def _db(self, drive_id: str) -> DriveDB:
        db = self._dbs.get(drive_id)
        if db is None:
            db = self._dbs[drive_id] = DriveDB(self._ref(drive_id).files_db)
        return db

    def _context_db(self, drive_id: str):
        if drive_id not in self._contexts:
            ref = self._ref(drive_id)
            context = None
            if ref.context_db is not None:
                try:
                    from ..context.context_db import ContextDB

                    context = ContextDB(ref.context_db)
                except Exception:  # noqa: BLE001 - 壊れていても基本情報の閲覧は続ける
                    context = None
            self._contexts[drive_id] = context
        return self._contexts[drive_id]

    def _close_databases(self) -> None:
        """UI スレッドとワーカーの DB 接続をすべて閉じる(カタログ更新・切替の前に呼ぶ)。"""
        self._generation += 1
        if hasattr(self, "_worker"):
            self._worker.supersede(self._generation)
            self.workerCloseRequested.emit()
        for db in list(self._dbs.values()) + [context for context in self._contexts.values() if context is not None]:
            try:
                db.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        self._dbs.clear()
        self._contexts.clear()
        self._refs.clear()

    # ================================================================== ツリー
    def _drive_text(self, drive: dict) -> str:
        text = drive["name"]
        if drive.get("total_bytes"):
            text += f"   [空き {format_size(drive.get('free_bytes'))} / {format_size(drive.get('total_bytes'))}]"
        return text

    def _drive_tooltip(self, drive: dict) -> str:
        device = " ".join(part for part in (drive.get("device_vendor"), drive.get("device_model")) if part)
        lines = [
            f"ラベル: {drive.get('label') or '(なし)'}    シリアル: {drive.get('serial', '')}    形式: {drive.get('filesystem', '')}",
            f"デバイス: {device or '-'}  [{drive.get('bus_type', '')}]",
            f"スキャン時のパス: {drive.get('root', '')}",
            f"ファイル {drive.get('file_count', 0):,} / フォルダ {drive.get('dir_count', 0):,} / 合計 {format_size(drive.get('total_size'))}",
            f"スキャン日時: {format_iso(drive.get('scanned_at'))}",
        ]
        return "\n".join(lines)

    def _reload_tree(self) -> None:
        self.tree_model.clear()
        if self.catalog is None:
            return
        for drive in self.catalog.drives:
            item = QStandardItem(self._drive_icon, self._drive_text(drive))
            item.setData(drive["id"], ROLE_DRIVE)
            item.setData(ROOT_ID, ROLE_DIR)
            item.setToolTip(self._drive_tooltip(drive))
            if drive.get("dir_count"):
                item.appendRow(QStandardItem())  # 展開時に読み込むためのプレースホルダ
            else:
                item.setData(True, ROLE_LOADED)
            self.tree_model.appendRow(item)

    def _load_tree_children(self, item: QStandardItem) -> None:
        if item.data(ROLE_LOADED):
            return
        item.setData(True, ROLE_LOADED)
        item.removeRows(0, item.rowCount())
        drive_id = item.data(ROLE_DRIVE)
        try:
            folders = self._db(drive_id).children(item.data(ROLE_DIR), dirs_only=True)
        except (CatalogError, OSError) as error:
            self.statusBar().showMessage(f"読み込みに失敗しました: {error}", 8000)
            return
        children = []
        for folder in folders:
            child = QStandardItem(self._folder_icon, folder.name)
            child.setData(drive_id, ROLE_DRIVE)
            child.setData(folder.id, ROLE_DIR)
            if folder.dir_count:
                child.appendRow(QStandardItem())
            else:
                child.setData(True, ROLE_LOADED)
            children.append(child)
        if children:
            item.appendRows(children)

    def _on_tree_expanded(self, index: QModelIndex) -> None:
        item = self.tree_model.itemFromIndex(index)
        if item is not None:
            self._load_tree_children(item)

    def _on_tree_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        item = self.tree_model.itemFromIndex(current)
        if item is None or item.data(ROLE_DRIVE) is None:
            return
        self.navigate(item.data(ROLE_DRIVE), item.data(ROLE_DIR), from_tree=True)

    def _drive_item(self, drive_id: str) -> QStandardItem | None:
        for row in range(self.tree_model.rowCount()):
            item = self.tree_model.item(row)
            if item.data(ROLE_DRIVE) == drive_id:
                return item
        return None

    def _select_in_tree(self, drive_id: str, dir_id: int) -> None:
        """一覧側の移動に合わせてツリーの該当フォルダを選択状態にする。"""
        item = self._drive_item(drive_id)
        if item is None:
            return
        db = self._db(drive_id)
        chain: list[int] = []
        current = dir_id
        while current != ROOT_ID:
            entry = db.get(current)
            if entry is None:
                return
            chain.append(current)
            current = entry.parent_id
        for folder_id in reversed(chain):
            self._load_tree_children(item)
            self.tree.setExpanded(item.index(), True)
            found = None
            for row in range(item.rowCount()):
                child = item.child(row)
                if child.data(ROLE_DIR) == folder_id:
                    found = child
                    break
            if found is None:
                break
            item = found
        selection = self.tree.selectionModel()
        selection.blockSignals(True)
        selection.setCurrentIndex(item.index(), QItemSelectionModel.SelectionFlag.ClearAndSelect)
        selection.blockSignals(False)
        self.tree.scrollTo(item.index())
        self.tree.viewport().update()

    # ================================================================== ナビゲーション
    def navigate(self, drive_id: str, dir_id: int, *, from_tree: bool = False, add_history: bool = True,
                 select_entry: int | None = None) -> None:  # fmt: skip
        if self.catalog is None:
            return
        try:
            self._db(drive_id)
        except (CatalogError, OSError) as error:
            QMessageBox.critical(self, APP_NAME, f"ドライブのデータを開けません。\n\n{error}")
            return
        location = (drive_id, dir_id)
        if add_history and location != self._location:
            del self._history[self._history_index + 1 :]
            self._history.append(location)
            self._history_index = len(self._history) - 1
        self._location = location
        self._pending_select = (drive_id, select_entry) if select_entry is not None else None
        if self.filter_edit.text():
            self.filter_edit.blockSignals(True)
            self.filter_edit.clear()
            self.filter_edit.blockSignals(False)
        if not from_tree:
            self._select_in_tree(drive_id, dir_id)
        self._update_address()
        self._update_ui_state()
        self._run_query()

    def _update_address(self) -> None:
        if self._location is None:
            self.address.clear()
            return
        drive_id, dir_id = self._location
        db = self._db(drive_id)
        self.address.setText(db.join_root(db.dir_path(dir_id)))

    def _on_address_entered(self) -> None:
        if self.catalog is None:
            return
        text = self.address.text().strip().strip('"').replace("/", "\\")
        candidates = [drive["id"] for drive in self.catalog.drives]
        if self._location is not None:
            candidates.remove(self._location[0])
            candidates.insert(0, self._location[0])
        for drive_id in candidates:
            root = self.catalog.drive(drive_id).get("root", "")
            if not root or not text.casefold().startswith(root.rstrip("\\").casefold()):
                continue
            rel_path = text[len(root.rstrip("\\")) :].strip("\\")
            db = self._db(drive_id)
            if not rel_path:
                self.navigate(drive_id, ROOT_ID)
                return
            entry = db.find_path(rel_path)
            if entry is not None:
                if entry.is_dir:
                    self.navigate(drive_id, entry.id)
                else:
                    self.navigate(drive_id, entry.parent_id, select_entry=entry.id)
                return
        self.statusBar().showMessage(f"カタログ内に見つかりません: {text}", 6000)
        self._update_address()

    def _go_history(self, index: int) -> None:
        if not 0 <= index < len(self._history):
            return
        self._history_index = index
        drive_id, dir_id = self._history[index]
        if self.catalog is None or not any(drive["id"] == drive_id for drive in self.catalog.drives):
            return
        self.navigate(drive_id, dir_id, add_history=False)

    def go_back(self) -> None:
        self._go_history(self._history_index - 1)

    def go_forward(self) -> None:
        self._go_history(self._history_index + 1)

    def go_up(self) -> None:
        if self._location is None or self._location[1] == ROOT_ID:
            return
        drive_id, dir_id = self._location
        entry = self._db(drive_id).get(dir_id)
        if entry is not None:
            self.navigate(drive_id, entry.parent_id, select_entry=dir_id)

    def _focus_filter(self) -> None:
        self.filter_edit.setFocus()
        self.filter_edit.selectAll()

    # ================================================================== クエリ
    def _build_spec(self, limit: int | None) -> QuerySpec | None:
        if self.catalog is None:
            return None
        terms = tuple(split_terms(self.filter_edit.text()))
        scope = self.scope_combo.currentData()
        try:
            if scope == SCOPE_ALL and terms:
                drives = tuple(self._ref(drive["id"]) for drive in self.catalog.drives)
                dir_id = ROOT_ID
            elif self._location is not None:
                drives = (self._ref(self._location[0]),)
                dir_id = self._location[1]
            else:
                return None
        except (CatalogError, OSError) as error:
            self.statusBar().showMessage(f"ドライブのデータを開けません: {error}", 8000)
            return None
        include_context = self.context_check.isEnabled() and self.context_check.isChecked()
        return QuerySpec(scope, terms, drives, dir_id, limit, include_context)

    def _run_query(self) -> None:
        self._filter_timer.stop()
        spec = self._build_spec(self.app_settings.result_limit)
        self._generation += 1
        self._worker.supersede(self._generation)
        self._spec = spec
        if spec is None:
            self.table_model.set_rows([])
            self._update_status()
            return
        if spec.terms:
            self.items_label.setText("検索中…")
        self.queryRequested.emit(self._generation, spec)

    def _on_query_finished(self, generation: int, rows: list[Row], truncated: bool, error: str) -> None:
        if generation != self._generation or self._spec is None:
            return
        self._truncated = truncated
        searching = self._spec.effective_scope != SCOPE_FOLDER
        self.table.setColumnHidden(COL_LOCATION, not searching)
        self.table.setColumnHidden(COL_DRIVE, self._spec.effective_scope != SCOPE_ALL)
        self.table_model.set_rows(rows)
        self.table.scrollToTop()
        if self._pending_select is not None:
            position = self.table_model.find_row(*self._pending_select)
            self._pending_select = None
            if position >= 0:
                index = self.table_model.index(position, COL_NAME)
                self.table.selectionModel().setCurrentIndex(
                    index, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows
                )
                self.table.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
        if error:
            self.statusBar().showMessage(f"検索に失敗しました: {error}", 8000)
        self._on_selection_changed()

    # ================================================================== 選択・ステータス
    def _selected_rows(self) -> list[Row]:
        indexes = self.table.selectionModel().selectedRows()
        rows = [self.table_model.row_at(index) for index in sorted(indexes, key=lambda index: index.row())]
        return [row for row in rows if row is not None]

    def _on_selection_changed(self) -> None:
        current = self.table_model.row_at(self.table.currentIndex())
        selected = self.table.selectionModel().isSelected(self.table.currentIndex()) if current else False
        if current is not None and selected and self.properties_dock.isVisible():
            self.properties.show_row(current, self._context_db(current.drive_id))
        else:
            self.properties.show_row(None, None)
        self._update_status()

    def _update_status(self) -> None:
        rows = self.table_model.rows
        if self.catalog is None:
            self.items_label.setText("カタログが開かれていません")
            self.drive_label.setText("")
            return
        if not self.catalog.drives:
            self.items_label.setText("ドライブが登録されていません。「ドライブ」→「ドライブの追加 / 更新」でスキャンしてください。")
            self.drive_label.setText("")
            return
        folders = sum(1 for row in rows if row.entry.is_dir)
        files = len(rows) - folders
        text = f"{len(rows):,} 個の項目 (フォルダ {folders:,} / ファイル {files:,})"
        if self._spec is not None and self._spec.effective_scope == SCOPE_FOLDER and self._location is not None:
            folder = self._db(self._location[0]).get(self._location[1]) if self._location[1] != ROOT_ID else None
            total = folder.size if folder else self.catalog.drive(self._location[0]).get("total_size")
            text += f"   フォルダ合計 {format_size(total)}"
        else:
            text += f"   ファイル合計 {format_size(sum(row.entry.size or 0 for row in rows if not row.entry.is_dir))}"
        if self._truncated:
            text += f"   ※ 上限の {len(rows):,} 件まで表示 (エクスポートは全件)"
        selected = self.table.selectionModel().selectedRows()
        if selected:
            size = sum((row.entry.size or 0) for row in self._selected_rows())
            text += f"   |   {len(selected):,} 個選択  {format_size(size)} ({format_bytes(size)} バイト)"
        self.items_label.setText(text)

        if self._location is not None:
            drive = self.catalog.drive(self._location[0])
            self.drive_label.setText(
                f"{drive['name']}:  空き {format_size(drive.get('free_bytes'))} / {format_size(drive.get('total_bytes'))}"
                f"   スキャン {format_iso(drive.get('scanned_at'))}"
            )
        else:
            self.drive_label.setText("")

    # ================================================================== 一覧の操作
    def _on_table_double_clicked(self, index: QModelIndex) -> None:
        row = self.table_model.row_at(index)
        if row is None:
            return
        if row.entry.is_dir:
            self.navigate(row.drive_id, row.entry.id)
        elif self._spec is not None and self._spec.effective_scope != SCOPE_FOLDER:
            self.navigate(row.drive_id, row.entry.parent_id, select_entry=row.entry.id)

    def open_location(self) -> None:
        row = self.table_model.row_at(self.table.currentIndex())
        if row is not None:
            self.navigate(row.drive_id, row.entry.parent_id, select_entry=row.entry.id)

    def _show_table_menu(self, position) -> None:
        menu = QMenu(self)
        has_selection = bool(self.table.selectionModel().selectedRows())
        for item in (self.act_copy_names, self.act_copy_paths):
            item.setEnabled(has_selection)
            menu.addAction(item)
        if self._spec is not None and self._spec.effective_scope != SCOPE_FOLDER:
            self.act_open_location.setEnabled(has_selection)
            menu.addAction(self.act_open_location)
        menu.addSeparator()
        menu.addAction(self.act_select_all)
        menu.addAction(self.act_export)
        menu.exec(self.table.viewport().mapToGlobal(position))
        for item in (self.act_copy_names, self.act_copy_paths):
            item.setEnabled(True)

    def copy_names(self) -> None:
        if not self.table.hasFocus() and QApplication.focusWidget() not in (None, self.table):
            widget = QApplication.focusWidget()
            if hasattr(widget, "copy"):
                widget.copy()  # type: ignore[union-attr]
                return
        rows = self._selected_rows()
        if rows:
            QGuiApplication.clipboard().setText("\r\n".join(row.entry.name for row in rows))
            self.statusBar().showMessage(f"{len(rows):,} 件の名前をコピーしました", 3000)

    def copy_paths(self) -> None:
        rows = self._selected_rows()
        if rows:
            QGuiApplication.clipboard().setText("\r\n".join(row.full_path for row in rows))
            self.statusBar().showMessage(f"{len(rows):,} 件のフルパスをコピーしました", 3000)

    def export_rows(self) -> None:
        if self._spec is None:
            return
        path, chosen = QFileDialog.getSaveFileName(
            self, "一覧をエクスポート", "", "テキスト (*.txt);;CSV (*.csv)"
        )
        if not path:
            return
        fmt = export.FORMAT_CSV if path.lower().endswith(".csv") or ("csv" in chosen and "." not in Path(path).name) else export.FORMAT_TXT
        if "." not in Path(path).name:
            path += "." + fmt
        try:
            with wait_cursor():
                if self._truncated:
                    # 表示は上限で打ち切られているので、全件を読み直して書き出す
                    spec = self._build_spec(None)
                    pool = DbPool()
                    try:
                        count = export.export_rows(path, iter_rows(spec, pool), fmt) if spec else 0
                    finally:
                        pool.close_all()
                else:
                    count = export.export_rows(path, self.table_model.rows, fmt)
        except OSError as error:
            QMessageBox.critical(self, APP_NAME, f"エクスポートに失敗しました。\n\n{error}")
            return
        self.statusBar().showMessage(f"{count:,} 件を {path} に書き出しました", 8000)

    # ================================================================== 拡張コンテキスト
    def _redecode_text(self, drive_id: str, entry_id: int, encoding: str) -> None:
        if self.catalog is None or not encoding:
            return
        ref = self._ref(drive_id)
        if ref.context_db is None:
            return
        work_dir = Path(tempfile.mkdtemp(prefix="vdmoku_ctx_"))
        try:
            from ..context.context_db import redecode_text

            with wait_cursor():
                copy_path = work_dir / "context.db"
                shutil.copyfile(ref.context_db, copy_path)
                redecode_text(copy_path, entry_id, encoding)
                location, selected = self._location, self.table_model.row_at(self.table.currentIndex())
                self._close_databases()
                self.catalog.replace_context_db(drive_id, copy_path)
        except (LookupError, UnicodeError, ValueError) as error:
            QMessageBox.warning(self, APP_NAME, f"文字コード「{encoding}」で読み直せません。\n\n{error}")
            return
        except (CatalogError, OSError) as error:
            QMessageBox.critical(self, APP_NAME, f"カタログを更新できません。\n\n{error}")
            return
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        if location is not None:
            self._pending_select = (drive_id, selected.entry.id) if selected else None
            self._run_query()

    # ================================================================== ドライブ操作
    def _current_drive(self) -> dict | None:
        if self.catalog is None or self._location is None:
            return None
        return self.catalog.drive(self._location[0])

    def _show_tree_menu(self, position) -> None:
        index = self.tree.indexAt(position)
        menu = QMenu(self)
        if index.isValid():
            self.tree.setCurrentIndex(index)
            for item in (self.act_rescan, self.act_rename, self.act_drive_info):
                menu.addAction(item)
            menu.addMenu(self.restore_menu)
            menu.addAction(self.act_remove)
            menu.addSeparator()
        menu.addAction(self.act_scan)
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def scan_drive(self, target_drive_id: str | None = None) -> None:
        if self.catalog is None:
            return
        dialog = ScanDialog(self.catalog, self.app_settings, self, target_drive_id)
        dialog.exec()
        if dialog.updated_drive_id:
            self._after_catalog_changed(dialog.updated_drive_id)

    def rescan_current_drive(self) -> None:
        drive = self._current_drive()
        if drive is not None:
            self.scan_drive(drive["id"])

    def _after_catalog_changed(self, select_drive_id: str | None = None) -> None:
        """ドライブの追加・更新・削除の後に表示を作り直す。"""
        assert self.catalog is not None
        previous = self._location
        self._close_databases()
        self._reload_tree()
        self._location = None
        self._history.clear()
        self._history_index = -1
        drive_ids = [drive["id"] for drive in self.catalog.drives]
        target = select_drive_id if select_drive_id in drive_ids else (previous[0] if previous and previous[0] in drive_ids else None)
        if target is None and drive_ids:
            target = drive_ids[0]
        if target is not None:
            self.navigate(target, ROOT_ID)
        else:
            self.address.clear()
            self._spec = None
            self.table_model.set_rows([])
            self._update_status()
        self._update_ui_state()

    def rename_current_drive(self) -> None:
        drive = self._current_drive()
        if drive is None or self.catalog is None:
            return
        name, accepted = QInputDialog.getText(self, "ドライブの表示名", "表示名:", text=drive["name"])
        name = name.strip()
        if not accepted or not name or name == drive["name"]:
            return
        drive["name"] = name
        try:
            self.catalog.save()
        except OSError as error:
            QMessageBox.critical(self, APP_NAME, f"カタログを保存できません。\n\n{error}")
            return
        self._after_catalog_changed(drive["id"])

    def remove_current_drive(self) -> None:
        drive = self._current_drive()
        if drive is None or self.catalog is None:
            return
        answer = QMessageBox.question(
            self, APP_NAME,
            f"「{drive['name']}」をカタログから削除しますか?\nバックアップ世代も含めて削除され、元に戻せません。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )  # fmt: skip
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._close_databases()
        try:
            with wait_cursor():
                self.catalog.remove_drive(drive["id"])
        except (CatalogError, OSError) as error:
            QMessageBox.critical(self, APP_NAME, f"削除に失敗しました。\n\n{error}")
        self._after_catalog_changed()

    def _fill_restore_menu(self) -> None:
        self.restore_menu.clear()
        drive = self._current_drive()
        backups = drive.get("backups", []) if drive else []
        for backup in backups:
            text = (
                f"{format_iso(backup.get('scanned_at'))}  ファイル {backup.get('file_count') or 0:,}"
                f" / 合計 {format_size(backup.get('total_size'))}"
            )
            item = self.restore_menu.addAction(text)
            item.triggered.connect(lambda _checked=False, stamp=backup["stamp"]: self.restore_backup(stamp))
        if not backups:
            self.restore_menu.addAction("(バックアップはありません)").setEnabled(False)

    def restore_backup(self, stamp: str) -> None:
        drive = self._current_drive()
        if drive is None or self.catalog is None:
            return
        answer = QMessageBox.question(
            self, APP_NAME,
            f"「{drive['name']}」をバックアップの状態に戻しますか?\n現在の内容はバックアップ世代として残ります。",
        )  # fmt: skip
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._close_databases()
        try:
            with wait_cursor():
                self.catalog.restore_backup(drive["id"], stamp)
        except (CatalogError, OSError) as error:
            QMessageBox.critical(self, APP_NAME, f"復元に失敗しました。\n\n{error}")
        self._after_catalog_changed(drive["id"])

    def show_drive_info(self) -> None:
        drive = self._current_drive()
        if drive is None:
            return
        backups = drive.get("backups", [])
        lines = [self._drive_tooltip(drive)]
        lines.append(f"容量: {format_size(drive.get('total_bytes'))}    空き: {format_size(drive.get('free_bytes'))} (スキャン時点)")
        lines.append(f"取得元: {'Everything' if drive.get('source') == 'everything' else '直接走査'}")
        context = "なし"
        if drive.get("has_context"):
            context = "あり (取得を途中でキャンセル。次回の更新で続きを取得)" if drive.get("context_partial") else "あり"
        lines.append(f"拡張コンテキスト: {context}")
        lines.append(f"バックアップ: {len(backups)} 世代")
        lines += [f"   {format_iso(backup.get('scanned_at'))}  ファイル {backup.get('file_count') or 0:,}" for backup in backups]
        QMessageBox.information(self, drive["name"], "\n".join(lines))

    # ================================================================== 設定・ヘルプ
    def edit_catalog_settings(self) -> None:
        if self.catalog is None:
            return
        CatalogSettingsDialog(self.catalog, self).exec()
        self._update_ui_state()

    def edit_app_settings(self) -> None:
        if AppSettingsDialog(self.app_settings, self).exec():
            self._run_query()

    def show_everything_help(self) -> None:
        QMessageBox.information(
            self, "Everything の導入方法",
            "このアプリはファイルリストの取得に voidtools の Everything を利用します。\n\n"
            "1. https://www.voidtools.com/ から Everything 本体と、コマンドライン版 (ES: es.exe) を入手します。\n"
            "2. Everything を起動したままにします (NTFS ドライブのインデックス作成には管理者権限、または Everything サービスが必要です)。\n"
            "3. es.exe を PATH の通った場所か Everything と同じフォルダに置くか、「設定」→「アプリ設定」で場所を指定します。\n"
            "4. 「アプリ設定」の「接続テスト」で動作を確認できます。\n\n"
            "Everything のインデックス対象外のドライブ (CD/DVD、FAT/exFAT など) や Everything が使えない場合は、"
            "自動的に直接走査でファイルリストを取得します。",
        )  # fmt: skip

    def show_about(self) -> None:
        QMessageBox.about(
            self, APP_NAME,
            f"{APP_NAME} {__version__}\n\nEverything (es.exe) 連携のオフライン ファイルリスト カタログツール",
        )  # fmt: skip
