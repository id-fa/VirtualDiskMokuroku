"""エクスプローラ風のカタログビューア。"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QModelIndex, QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QGuiApplication,
    QKeySequence,
    QStandardItem,
    QStandardItemModel,
)
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
    QStackedWidget,
    QStyle,
    QTableView,
    QToolBar,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core import export
from ..core.catalog import CATALOG_EXTENSION, CONTEXT_DB, LEGACY_CATALOG_EXTENSIONS, Catalog
from ..core.drive_db import ROOT_ID, DriveDB, split_terms
from ..core.errors import CatalogError, PasswordError
from ..core.formatting import format_bytes, format_iso, format_size
from ..core.search import SCOPE_ALL, SCOPE_FOLDER, SCOPE_SUBTREE, DbPool, DriveRef, QuerySpec, Row, iter_rows
from ..core.settings import AppSettings
from .import_dialog import ImportDialog
from .models import COL_DRIVE, COL_LOCATION, COL_MTIME, COL_NAME, COL_SIZE, COL_TYPE, FileTableModel
from .organize_dialog import OrganizeDialog
from .properties_panel import PropertiesPanel
from .query_worker import QueryWorker
from .scan_dialog import ScanDialog
from .settings_dialogs import AppSettingsDialog, CatalogSettingsDialog, ask_new_password, ask_password
from .style import apply_selection_style
from .thumbnail_view import (
    CAPTION_LABELS,
    DEFAULT_THUMB_SIZE,
    VIEW_DETAILS,
    VIEW_THUMB_LIST,
    VIEW_TILES,
    ThumbnailProvider,
    ThumbnailView,
)

APP_NAME = "VirtualDiskMokuroku"
_CATALOG_FILTER = f"カタログ (*{CATALOG_EXTENSION})"
_OPEN_FILTER = "カタログ (" + " ".join(f"*{ext}" for ext in (CATALOG_EXTENSION, *LEGACY_CATALOG_EXTENSIONS)) + ")"
_IMPORT_FILTER = "Virtual CD-ROM Case のカタログ (*.cas);;すべてのファイル (*)"
_SOURCE_NAMES = {"everything": "Everything", "walk": "直接走査", "vcdcase": "Virtual CD-ROM Case からインポート"}
_FILTER_DELAY_MS = 180

ROLE_DRIVE = Qt.ItemDataRole.UserRole + 1
ROLE_DIR = Qt.ItemDataRole.UserRole + 2
ROLE_LOADED = Qt.ItemDataRole.UserRole + 3
ROLE_GROUP = Qt.ItemDataRole.UserRole + 4  # グループの行 (ドライブをまとめる見出し) のグループ名


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
        self._collapsed_groups: set[str] = set()  # 折りたたんであるグループ (ツリーを作り直しても保つ)
        self.tree.expanded.connect(self._on_tree_expanded)
        self.tree.collapsed.connect(self._on_tree_collapsed)
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

        # --- サムネイル表示。詳細一覧と同じモデル・同じ選択状態を使う
        self.thumb_provider = ThumbnailProvider(self._context_db)
        self.thumb_view = ThumbnailView(self.thumb_provider, self._folder_icon, self._file_icon)
        self.thumb_view.setModel(self.table_model)
        own_selection = self.thumb_view.selectionModel()
        self.thumb_view.setSelectionModel(self.table.selectionModel())
        own_selection.deleteLater()
        self.thumb_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.thumb_view.customContextMenuRequested.connect(self._show_table_menu)
        self.thumb_view.doubleClicked.connect(self._on_table_double_clicked)

        self.view_stack = QStackedWidget()
        self.view_stack.addWidget(self.table)
        self.view_stack.addWidget(self.thumb_view)
        self._thumb_config: tuple | None = None
        self._build_view_mode_menu()
        self.view_button = QToolButton()
        self.view_button.setText("表示形式")
        self.view_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.view_button.setMenu(self.view_mode_menu)
        filter_row.addWidget(self.view_button)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 4, 4, 0)
        right_layout.addLayout(filter_row)
        right_layout.addWidget(self.view_stack, 1)

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

    def _build_view_mode_menu(self) -> None:
        """表示形式 (詳細 / サムネイル 2 種) とサムネイル表示の設定メニュー。"""
        settings = self.app_settings
        self.view_mode_menu = QMenu("表示形式(&L)", self)
        self._mode_actions: dict[str, QAction] = {}
        group = QActionGroup(self)
        group.setExclusive(True)
        for mode, text in (
            (VIEW_DETAILS, "詳細(&D)"),
            (VIEW_TILES, "サムネイル (敷き詰め)(&T)"),
            (VIEW_THUMB_LIST, "サムネイル (情報付き)(&I)"),
        ):
            item = self.view_mode_menu.addAction(text)
            item.setCheckable(True)
            group.addAction(item)
            item.triggered.connect(lambda _checked=False, value=mode: self.set_view_mode(value))
            self._mode_actions[mode] = item

        self.view_mode_menu.addSeparator()
        self.act_thumb_zoom = self.view_mode_menu.addAction("サムネイルを 2 倍に拡大(&2)")
        self.act_thumb_zoom.setCheckable(True)
        self.act_thumb_zoom.setChecked(settings.thumb_zoom)
        self.act_thumb_zoom.toggled.connect(lambda _checked: self._on_thumb_option_changed())
        self.caption_menu = self.view_mode_menu.addMenu("敷き詰め表示でサムネイルの下に出す項目(&C)")
        self._caption_actions: dict[str, QAction] = {}
        for key, label in CAPTION_LABELS.items():
            item = self.caption_menu.addAction(label)
            item.setCheckable(True)
            item.setChecked(key in settings.thumb_captions)
            item.toggled.connect(lambda _checked: self._on_thumb_option_changed())
            self._caption_actions[key] = item

        self.view_mode_menu.addSeparator()
        sort_menu = self.view_mode_menu.addMenu("並べ替え(&S)")
        for column, text in ((COL_NAME, "名前"), (COL_SIZE, "サイズ"), (COL_MTIME, "更新日時"), (COL_TYPE, "種類")):
            item = sort_menu.addAction(text)
            item.setToolTip("同じ項目をもう一度選ぶと昇順/降順が入れ替わります")
            item.triggered.connect(lambda _checked=False, value=column: self.sort_by(value))

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
        self.act_import_vcdcase = action("Virtual CD-ROM Case のカタログをインポート(&I)…", self.import_vcdcase)
        self.act_export = action("表示中の一覧をエクスポート(&E)…", self.export_rows, "Ctrl+E")
        self.act_export_decrypted = action("復号して別のカタログに書き出す(&D)…", self.export_decrypted)
        self.act_copy_names = action("名前をコピー(&C)", self.copy_names, QKeySequence.StandardKey.Copy)
        self.act_copy_paths = action("フルパスをコピー(&P)", self.copy_paths, "Ctrl+Shift+C")
        self.act_select_all = action("すべて選択(&A)", self._select_all, QKeySequence.StandardKey.SelectAll)
        self.act_open_location = action("場所を開く(&L)", self.open_location)
        self.act_find = action("フィルタにフォーカス(&F)", self._focus_filter, QKeySequence.StandardKey.Find)
        self.act_refresh = action("最新の情報に更新(&R)", self._run_query, "F5")

        self.act_back = action("戻る", self.go_back, "Alt+Left", QStyle.StandardPixmap.SP_ArrowBack)
        self.act_forward = action("進む", self.go_forward, "Alt+Right", QStyle.StandardPixmap.SP_ArrowForward)
        self.act_up = action("上へ", self.go_up, "Alt+Up", QStyle.StandardPixmap.SP_ArrowUp)

        self.act_scan = action("ドライブの追加 / 更新(&A)…", self.scan_drive, "Ctrl+D")
        self.act_rescan = action("このドライブを更新 (再スキャン)(&U)…", self.rescan_current_drive)
        self.act_rename = action("ドライブの表示名を変更(&R)…", self.rename_current_drive)
        self.act_set_group = action("グループを変更(&G)…", self.set_current_drive_group)
        self.act_drive_info = action("ドライブ情報(&I)…", self.show_drive_info)
        self.act_remove = action("ドライブをカタログから削除(&D)…", self.remove_current_drive)
        self.act_organize = action("ドライブの整理(&O)…", self.organize_drives)
        self.act_organize.setToolTip("並び替え・グループ間の移動・一括削除・拡張コンテキストの削除・他のカタログへのコピー")
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
        file_menu.addAction(self.act_import_vcdcase)
        file_menu.addAction(self.act_export)
        file_menu.addAction(self.act_export_decrypted)
        file_menu.addSeparator()
        file_menu.addAction(self.act_exit)

        edit_menu = menu.addMenu("編集(&E)")
        for item in (self.act_copy_names, self.act_copy_paths, self.act_select_all, self.act_find):
            edit_menu.addAction(item)

        view_menu = menu.addMenu("表示(&V)")
        for item in (self.act_back, self.act_forward, self.act_up, self.act_refresh):
            view_menu.addAction(item)
        view_menu.addSeparator()
        view_menu.addMenu(self.view_mode_menu)
        view_menu.addAction(self.properties_dock.toggleViewAction())

        drive_menu = menu.addMenu("ドライブ(&D)")
        drive_menu.addAction(self.act_scan)
        drive_menu.addAction(self.act_rescan)
        drive_menu.addSeparator()
        drive_menu.addAction(self.act_rename)
        drive_menu.addAction(self.act_set_group)
        drive_menu.addAction(self.act_drive_info)
        drive_menu.addMenu(self.restore_menu)
        drive_menu.addAction(self.act_remove)
        drive_menu.addSeparator()
        drive_menu.addAction(self.act_organize)

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
        if self.catalog is not None:
            self.catalog.release_sources()
        super().closeEvent(event)

    def _update_ui_state(self) -> None:
        has_catalog = self.catalog is not None
        has_drive = self._location is not None
        for item in (self.act_close, self.act_scan, self.act_catalog_settings, self.act_import_vcdcase):
            item.setEnabled(has_catalog)
        self.act_organize.setEnabled(has_catalog and bool(self.catalog.drives))  # type: ignore[union-attr]
        for item in (
            self.act_rescan, self.act_rename, self.act_set_group, self.act_drive_info, self.act_remove, self.act_export,
        ):  # fmt: skip
            item.setEnabled(has_drive)
        self.restore_menu.setEnabled(has_drive)
        for widget in (self.filter_edit, self.scope_combo, self.address):
            widget.setEnabled(has_catalog)
        has_context = has_catalog and any(drive.get("has_context") for drive in self.catalog.drives)  # type: ignore[union-attr]
        self.context_check.setEnabled(bool(has_context))
        self.act_back.setEnabled(self._history_index > 0)
        self.act_forward.setEnabled(self._history_index < len(self._history) - 1)
        self.act_up.setEnabled(has_drive and self._location[1] != ROOT_ID)  # type: ignore[index]
        self.act_export_decrypted.setEnabled(has_catalog and self.catalog.encrypted)  # type: ignore[union-attr]
        title = f"{APP_NAME} {__version__}"
        if self.catalog is not None and self.catalog.encrypted:
            title = f"{self.catalog.path.name} [暗号化] - {title}"
        elif self.catalog is not None:
            title = f"{self.catalog.path.name} - {title}"
        self.setWindowTitle(title)
        self._apply_view_mode()

    # ================================================================== 表示形式
    def _view(self) -> QAbstractItemView:
        """いま表示している一覧 (詳細の表、またはサムネイル表示)。"""
        return self.thumb_view if self.view_stack.currentWidget() is self.thumb_view else self.table

    def _thumbnails_available(self) -> bool:
        """サムネイル表示を選べるカタログか(サムネイル保存が有効、または拡張コンテキストを持つドライブがある)。"""
        if self.catalog is None:
            return False
        if self.catalog.settings.get("context", {}).get("thumbnail", {}).get("enabled"):
            return True
        return any(drive.get("has_context") for drive in self.catalog.drives)

    def _thumb_base_size(self) -> int:
        """収蔵サムネイルの長辺 (カタログ設定の値)。"""
        size = DEFAULT_THUMB_SIZE
        if self.catalog is not None:
            try:
                size = int(self.catalog.settings.get("context", {}).get("thumbnail", {}).get("size", size))
            except (TypeError, ValueError):
                pass
        return min(512, max(32, size))

    def effective_view_mode(self) -> str:
        mode = self.app_settings.view_mode
        if mode in (VIEW_TILES, VIEW_THUMB_LIST) and self._thumbnails_available():
            return mode
        return VIEW_DETAILS

    def set_view_mode(self, mode: str) -> None:
        self.app_settings.view_mode = mode
        self.app_settings.save()
        self._apply_view_mode()

    def _on_thumb_option_changed(self) -> None:
        self.app_settings.thumb_zoom = self.act_thumb_zoom.isChecked()
        self.app_settings.thumb_captions = [key for key, item in self._caption_actions.items() if item.isChecked()]
        self.app_settings.save()
        self._apply_view_mode()

    def _apply_view_mode(self) -> None:
        """設定とカタログの状態に合わせて、一覧の表示形式を切り替える。"""
        settings = self.app_settings
        available = self._thumbnails_available()
        mode = self.effective_view_mode()
        for key, item in self._mode_actions.items():
            item.setEnabled(key == VIEW_DETAILS or available)
            item.setChecked(key == mode)
        self.act_thumb_zoom.setEnabled(available)
        self.caption_menu.setEnabled(available)

        selection = self.table.selectionModel()
        if mode == VIEW_DETAILS:
            self._thumb_config = None
            if self.view_stack.currentWidget() is not self.table:
                # サムネイル表示で選んだ項目を、表では行全体の選択として見せる
                selection.select(
                    selection.selection(),
                    QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
                )
                self.view_stack.setCurrentWidget(self.table)
                self.table.scrollTo(self.table.currentIndex())
            return

        config = (mode, self._thumb_base_size(), 2 if settings.thumb_zoom else 1, tuple(settings.thumb_captions))
        if config != self._thumb_config:
            self._thumb_config = config
            self.thumb_view.configure(*config)
        if self.view_stack.currentWidget() is not self.thumb_view:
            self.view_stack.setCurrentWidget(self.thumb_view)
            self.thumb_view.scrollTo(self.thumb_view.currentIndex())

    def sort_by(self, column: int) -> None:
        """並べ替え (サムネイル表示には列見出しが無いのでメニューから行う)。同じ列なら昇順/降順を入れ替える。"""
        header = self.table.horizontalHeader()
        order = Qt.SortOrder.AscendingOrder
        if header.sortIndicatorSection() == column and header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder:
            order = Qt.SortOrder.DescendingOrder
        self.table.sortByColumn(column, order)

    def _select_all(self) -> None:
        self._view().selectAll()

    # ================================================================== カタログの開閉
    def new_catalog(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(self, "新しいカタログ", "", _CATALOG_FILTER)
        if not path:
            return
        if not path.lower().endswith(CATALOG_EXTENSION):
            path += CATALOG_EXTENSION
        password = None
        answer = QMessageBox.question(
            self, APP_NAME,
            "このカタログを暗号化しますか?\n\n"
            "暗号化すると、カタログの中身 (ファイル名・サムネイルなど) はパスワードが無いと読めなくなります。\n"
            "パスワードを忘れると開けなくなり、復旧する方法はありません。暗号化は後から設定・解除できます。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.No,
        )  # fmt: skip
        if answer == QMessageBox.StandardButton.Cancel:
            return
        if answer == QMessageBox.StandardButton.Yes:
            password = ask_new_password(self, "暗号化のパスワード")
            if password is None:
                return
        try:
            if os.path.exists(path):
                os.remove(path)  # 上書きはファイルダイアログで確認済み
            with wait_cursor():
                catalog = Catalog.create(path, password, encrypt=password is not None)
        except (CatalogError, OSError) as error:
            QMessageBox.critical(self, APP_NAME, f"カタログを作成できません。\n\n{error}")
            return
        self._set_catalog(catalog)
        answer = QMessageBox.question(
            self, APP_NAME,
            "カタログを作成しました。続けてドライブを追加しますか?\n\n"
            "拡張コンテキスト (サムネイルやテキスト内容など) を登録する場合は、ドライブを追加する前に"
            "「設定」→「カタログ設定」で有効にしてください。",
        )  # fmt: skip
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
        if catalog is not None:
            catalog.memory_limit = self.app_settings.memory_limit_mb * 1024 * 1024
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
                # 通常のカタログはキャッシュに展開したファイル、暗号化カタログはメモリ上の DB を指す URI
                files_db = self.catalog.db_source(drive_id)
                context_db = self.catalog.context_source(drive_id) if drive.get("has_context") else None
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
        self.thumb_provider.clear()
        if self.catalog is not None:
            self.catalog.release_sources()  # 暗号化カタログの復号済み DB (メモリ・一時フォルダ) を手放す

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
        # 作り直しの途中で、消える前の項目が「選択中」として通知されないようにする
        selection = self.tree.selectionModel()
        selection.blockSignals(True)
        try:
            self.tree_model.clear()
        finally:
            selection.blockSignals(False)
        if self.catalog is None:
            return
        groups: dict[str, QStandardItem] = {}
        for drive in self.catalog.drives:
            item = QStandardItem(self._drive_icon, self._drive_text(drive))
            item.setData(drive["id"], ROLE_DRIVE)
            item.setData(ROOT_ID, ROLE_DIR)
            item.setToolTip(self._drive_tooltip(drive))
            if drive.get("dir_count"):
                item.appendRow(QStandardItem())  # 展開時に読み込むためのプレースホルダ
            else:
                item.setData(True, ROLE_LOADED)
            group = drive.get("group")
            if not group:
                self.tree_model.appendRow(item)
                continue
            # グループの行は、最初のドライブの位置に置く。選択はできない (展開・折りたたみと右クリックだけ)
            parent = groups.get(group)
            if parent is None:
                parent = groups[group] = QStandardItem(self._folder_icon, group)
                parent.setData(group, ROLE_GROUP)
                parent.setData(True, ROLE_LOADED)
                parent.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.tree_model.appendRow(parent)
            parent.appendRow(item)
        self._collapsed_groups &= set(groups)
        for group, parent in groups.items():
            comments = {drive.get("group_comment") for drive in self.catalog.drives if drive.get("group") == group}
            if len(comments) == 1 and None not in comments:
                parent.setToolTip(comments.pop())
            self.tree.setExpanded(parent.index(), group not in self._collapsed_groups)

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
            self._collapsed_groups.discard(item.data(ROLE_GROUP))
            self._load_tree_children(item)

    def _on_tree_collapsed(self, index: QModelIndex) -> None:
        item = self.tree_model.itemFromIndex(index)
        if item is not None and item.data(ROLE_GROUP) is not None:
            self._collapsed_groups.add(item.data(ROLE_GROUP))

    def _on_tree_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        item = self.tree_model.itemFromIndex(current)
        if item is None or item.data(ROLE_DRIVE) is None or self.catalog is None:
            return
        drive_id = item.data(ROLE_DRIVE)
        if not any(drive["id"] == drive_id for drive in self.catalog.drives):
            return  # 閉じたカタログや削除したドライブの項目
        self.navigate(drive_id, item.data(ROLE_DIR), from_tree=True)

    def _drive_item(self, drive_id: str) -> QStandardItem | None:
        for row in range(self.tree_model.rowCount()):
            item = self.tree_model.item(row)
            members = [item.child(index) for index in range(item.rowCount())] if item.data(ROLE_GROUP) is not None else [item]
            for member in members:
                if member.data(ROLE_DRIVE) == drive_id:
                    return member
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
        self.thumb_view.set_show_location(searching)
        self.table_model.set_rows(rows)
        self.table.scrollToTop()
        self.thumb_view.scrollToTop()
        if self._pending_select is not None:
            position = self.table_model.find_row(*self._pending_select)
            self._pending_select = None
            if position >= 0:
                index = self.table_model.index(position, COL_NAME)
                self.table.selectionModel().setCurrentIndex(
                    index, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows
                )
                self._view().scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
        if error:
            self.statusBar().showMessage(f"検索に失敗しました: {error}", 8000)
        self._on_selection_changed()

    # ================================================================== 選択・ステータス
    def _selected_row_numbers(self) -> list[int]:
        """選択されている行番号 (昇順)。表とサムネイル表示のどちらで選んでも同じ結果になる。"""
        selection = self.table.selectionModel()
        numbers = {index.row() for index in selection.selectedRows()}
        if not numbers and selection.hasSelection():
            # 行全体ではなく名前のセルだけが選択されている場合
            numbers = {index.row() for index in selection.selectedIndexes()}
        return sorted(numbers)

    def _selected_rows(self) -> list[Row]:
        rows = self.table_model.rows
        return [rows[number] for number in self._selected_row_numbers() if 0 <= number < len(rows)]

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
        selected = self._selected_rows()
        if selected:
            size = sum((row.entry.size or 0) for row in selected)
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

    def _build_table_menu(self) -> QMenu:
        menu = QMenu(self)
        has_selection = self.table.selectionModel().hasSelection()
        for item in (self.act_copy_names, self.act_copy_paths):
            item.setEnabled(has_selection)
            menu.addAction(item)
        if self._spec is not None and self._spec.effective_scope != SCOPE_FOLDER:
            self.act_open_location.setEnabled(has_selection)
            menu.addAction(self.act_open_location)

        # 選択行がフォルダならそのフォルダ、ファイルならその格納フォルダ、選択なしなら表示中のフォルダを開く
        row = self.table_model.row_at(self.table.currentIndex()) if has_selection else None
        if row is not None:
            self._add_explorer_action(menu, row.drive_id, row.entry.id if row.entry.is_dir else row.entry.parent_id)
        elif self._location is not None:
            self._add_explorer_action(menu, *self._location)

        menu.addSeparator()
        menu.addAction(self.act_select_all)
        menu.addAction(self.act_export)
        return menu

    def _show_table_menu(self, position) -> None:
        self._build_table_menu().exec(self._view().viewport().mapToGlobal(position))
        for item in (self.act_copy_names, self.act_copy_paths):
            item.setEnabled(True)

    # ================================================================== エクスプローラ連携
    def _connected_folder(self, drive_id: str, dir_id: int) -> str | None:
        """カタログ上のフォルダに対応する実際のパス。同じドライブが接続されていなければ None。"""
        if self.catalog is None:
            return None
        try:
            root = self.catalog.connected_root(drive_id)
            if root is None:
                return None
            rel_path = self._db(drive_id).dir_path(dir_id)
        except (CatalogError, OSError):
            return None
        if not rel_path:
            return root
        return root + rel_path if root.endswith("\\") else f"{root}\\{rel_path}"

    def _add_explorer_action(self, menu: QMenu, drive_id: str, dir_id: int) -> None:
        """同じドライブが接続されているときだけ「エクスプローラで開く」をメニューに加える。"""
        folder = self._connected_folder(drive_id, dir_id)
        if folder is None:
            return
        item = menu.addAction("このフォルダをエクスプローラで開く(&X)")
        item.setToolTip(folder)
        item.triggered.connect(lambda _checked=False, target=folder: self.open_in_explorer(target))

    def open_in_explorer(self, folder: str) -> None:
        if not os.path.isdir(folder):
            QMessageBox.warning(
                self, APP_NAME,
                f"フォルダが見つかりません。\n\n{folder}\n\n"
                "カタログ作成後に移動・削除されたか、名前が変更された可能性があります。"
                "ドライブを更新 (再スキャン) するとカタログが現在の内容になります。",
            )  # fmt: skip
            return
        try:
            os.startfile(folder)
        except OSError as error:
            QMessageBox.warning(self, APP_NAME, f"エクスプローラで開けません。\n\n{folder}\n\n{error}")

    def copy_names(self) -> None:
        view = self._view()
        if not view.hasFocus() and QApplication.focusWidget() not in (None, view):
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
        if self._ref(drive_id).context_db is None:
            return
        encrypted = self.catalog.encrypted
        work_dir = None if encrypted else Path(tempfile.mkdtemp(prefix="vdmoku_ctx_"))
        try:
            from ..context.context_db import redecode_text

            with wait_cursor():
                updated: bytes | Path
                if work_dir is None:
                    # 暗号化カタログ: 平文をディスクに書かないよう、メモリ上で書き換える
                    conn = sqlite3.connect(":memory:")
                    try:
                        conn.execute("PRAGMA temp_store=MEMORY")
                        conn.deserialize(self.catalog.read_db_bytes(drive_id, CONTEXT_DB))
                        redecode_text(conn, entry_id, encoding)
                        updated = conn.serialize()
                    finally:
                        conn.close()
                else:
                    updated = work_dir / "context.db"
                    shutil.copyfile(self.catalog.extract_db(drive_id, CONTEXT_DB), updated)
                    redecode_text(updated, entry_id, encoding)
                location, selected = self._location, self.table_model.row_at(self.table.currentIndex())
                self._close_databases()
                self.catalog.replace_context_db(drive_id, updated)
        except (LookupError, UnicodeError, ValueError) as error:
            QMessageBox.warning(self, APP_NAME, f"文字コード「{encoding}」で読み直せません。\n\n{error}")
            return
        except (CatalogError, OSError) as error:
            QMessageBox.critical(self, APP_NAME, f"カタログを更新できません。\n\n{error}")
            return
        finally:
            if work_dir is not None:
                shutil.rmtree(work_dir, ignore_errors=True)
        if location is not None:
            self._pending_select = (drive_id, selected.entry.id) if selected else None
            self._run_query()

    # ================================================================== ドライブ操作
    def _current_drive(self) -> dict | None:
        if self.catalog is None or self._location is None:
            return None
        return self.catalog.drive(self._location[0])

    def _build_tree_menu(self, index: QModelIndex) -> QMenu:
        menu = QMenu(self)
        item = self.tree_model.itemFromIndex(index) if index.isValid() else None
        if item is not None and item.data(ROLE_GROUP) is not None:
            rename = menu.addAction("グループ名を変更(&R)…")
            rename.triggered.connect(lambda _checked=False, name=item.data(ROLE_GROUP): self.rename_group(name))
            menu.addSeparator()
        elif index.isValid():
            self.tree.setCurrentIndex(index)
            if item is not None and item.data(ROLE_DRIVE) is not None:
                self._add_explorer_action(menu, item.data(ROLE_DRIVE), item.data(ROLE_DIR))
                if not menu.isEmpty():
                    menu.addSeparator()
            for action in (self.act_rescan, self.act_rename, self.act_set_group, self.act_drive_info):
                menu.addAction(action)
            menu.addMenu(self.restore_menu)
            menu.addAction(self.act_remove)
            menu.addSeparator()
        menu.addAction(self.act_scan)
        menu.addAction(self.act_organize)
        return menu

    def _show_tree_menu(self, position) -> None:
        self._build_tree_menu(self.tree.indexAt(position)).exec(self.tree.viewport().mapToGlobal(position))

    def scan_drive(self, target_drive_id: str | None = None) -> None:
        if self.catalog is None:
            return
        dialog = ScanDialog(self.catalog, self.app_settings, self, target_drive_id)
        dialog.exec()
        if dialog.updated_drive_id:
            self._after_catalog_changed(dialog.updated_drive_id)

    def import_vcdcase(self, path: str | None = None) -> None:
        """Virtual CD-ROM Case のカタログ (.cas) の全ドライブを、開いているカタログへ追加する。"""
        if self.catalog is None:
            return
        if not path:
            path, _filter = QFileDialog.getOpenFileName(self, "Virtual CD-ROM Case のカタログをインポート", "", _IMPORT_FILTER)
            if not path:
                return
        known = {drive["id"] for drive in self.catalog.drives}
        ImportDialog(self.catalog, path, self).exec()
        added = [drive["id"] for drive in self.catalog.drives if drive["id"] not in known]
        if added:
            self._after_catalog_changed(added[0])

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

    def set_current_drive_group(self) -> None:
        """表示中のドライブを入れるグループを変える (既存のグループを選ぶか、新しい名前を入力する)。"""
        drive = self._current_drive()
        catalog = self.catalog
        if drive is None or catalog is None:
            return
        current = drive.get("group", "")
        choices = ["", *catalog.group_names()]
        group, accepted = QInputDialog.getItem(
            self, "グループを変更",
            f"「{drive['name']}」を入れるグループ:\n(一覧から選ぶか、新しい名前を入力します。空欄にするとグループから外します)",
            choices, choices.index(current), True,
        )  # fmt: skip
        if accepted and group.strip() != current:
            self._change_groups(lambda: catalog.set_drive_group(drive["id"], group), drive["id"])

    def rename_group(self, name: str) -> None:
        catalog = self.catalog
        if catalog is None:
            return
        new_name, accepted = QInputDialog.getText(self, "グループ名を変更", "グループ名:", text=name)
        new_name = new_name.strip()
        if not accepted or not new_name or new_name == name:
            return
        if new_name in catalog.group_names():
            answer = QMessageBox.question(self, APP_NAME, f"グループ「{new_name}」は既にあります。1 つにまとめますか?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        if name in self._collapsed_groups:
            self._collapsed_groups.add(new_name)
        self._change_groups(lambda: catalog.rename_group(name, new_name), None)

    def _change_groups(self, change, select_drive_id: str | None) -> None:
        try:
            change()
        except (CatalogError, OSError) as error:
            QMessageBox.critical(self, APP_NAME, f"カタログを保存できません。\n\n{error}")
            return
        self._after_catalog_changed(select_drive_id)

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

    def organize_drives(self) -> None:
        """ドライブの整理 (並び替え・グループ間の移動・一括削除・拡張コンテキストの削除・他のカタログへのコピー)。"""
        if self.catalog is None:
            return
        dialog = OrganizeDialog(self.catalog, self, prepare_rewrite=self._close_databases)
        dialog.exec()
        if dialog.reload_needed:
            self._after_catalog_changed(self._location[0] if self._location is not None else None)

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
        lines.append(f"取得元: {_SOURCE_NAMES.get(drive.get('source', ''), '直接走査')}")
        if drive.get("group"):
            group_comment = f" ({drive['group_comment']})" if drive.get("group_comment") else ""
            lines.append(f"グループ: {drive['group']}{group_comment}")
        if drive.get("comment"):
            lines.append(f"コメント: {drive['comment']}")
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
        dialog = CatalogSettingsDialog(self.catalog, self, prepare_rewrite=self._close_databases)
        dialog.exec()
        # 保存や暗号化の切り替えの前に DB を閉じている。ツリーや現在位置はそのまま使えるので、一覧だけ読み直す
        self._update_ui_state()
        self._update_status()
        if self._location is not None:
            self._run_query()

    def export_decrypted(self) -> None:
        """暗号化カタログの内容を、暗号化しないカタログとして別のファイルに書き出す。"""
        if self.catalog is None or not self.catalog.encrypted:
            return
        answer = QMessageBox.warning(
            self, APP_NAME,
            "暗号化していないカタログとして書き出します。書き出したファイルの中身は誰でも読めます。続けますか?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )  # fmt: skip
        if answer != QMessageBox.StandardButton.Yes:
            return
        path, _filter = QFileDialog.getSaveFileName(self, "復号して書き出す", "", _CATALOG_FILTER)
        if not path:
            return
        if not path.lower().endswith(CATALOG_EXTENSION):
            path += CATALOG_EXTENSION
        try:
            with wait_cursor():
                self.catalog.export_decrypted(path)
        except (CatalogError, OSError) as error:
            QMessageBox.critical(self, APP_NAME, f"書き出しに失敗しました。\n\n{error}")
            return
        self.statusBar().showMessage(f"{path} に書き出しました", 8000)

    def edit_app_settings(self) -> None:
        if AppSettingsDialog(self.app_settings, self).exec():
            if self.catalog is not None:
                self.catalog.memory_limit = self.app_settings.memory_limit_mb * 1024 * 1024
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
