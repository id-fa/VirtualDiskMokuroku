"""ドライブの追加・更新(スキャン)ダイアログ。"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..core import scanner
from ..core.catalog import Catalog
from ..core.errors import ScanCancelled
from ..core.es_client import EsClient, EsError, find_es_exe
from ..core.formatting import format_size
from ..core.settings import AppSettings
from ..core.volume import VolumeInfo, list_volumes
from ..pipeline import scan_into_catalog
from .style import apply_selection_style

_DRIVE_TYPE_LABELS = {
    "fixed": "ローカル",
    "removable": "リムーバブル",
    "cdrom": "CD/DVD",
    "remote": "ネットワーク",
    "ramdisk": "RAM ディスク",
}
_SOURCE_LABELS = [
    (scanner.SOURCE_AUTO, "自動 (Everything 優先、対象外ドライブは直接走査)"),
    (scanner.SOURCE_EVERYTHING, "Everything (es.exe)"),
    (scanner.SOURCE_WALK, "直接走査 (Everything を使わない)"),
]


class ScanWorker(QThread):
    progress = Signal(str, int)
    succeeded = Signal(object, object, object)  # drive(dict), ScanResult, ContextStats | None
    failed = Signal(str)  # 空文字はキャンセル

    def __init__(self, catalog: Catalog, root: str, drive_id: str | None, name: str, source: str,
                 es: EsClient | None, parent=None):  # fmt: skip
        super().__init__(parent)
        self._catalog = catalog
        self._root = root
        self._drive_id = drive_id
        self._name = name
        self._source = source
        self._es = es
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            outcome = scan_into_catalog(
                self._catalog,
                self._root,
                drive_id=self._drive_id,
                name=self._name,
                source=self._source,
                es=self._es,
                progress=self.progress.emit,
                is_cancelled=self._cancel.is_set,
            )
            self.succeeded.emit(outcome.drive, outcome.result, outcome.context_stats)
        except ScanCancelled:
            self.failed.emit("")
        except Exception as error:  # noqa: BLE001 - スレッド内の例外は UI に伝える
            self.failed.emit(f"{type(error).__name__}: {error}")


class ScanDialog(QDialog):
    """接続中のドライブを選んでスキャンし、カタログへ追加または更新する。"""

    def __init__(self, catalog: Catalog, app_settings: AppSettings, parent=None, target_drive_id: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("ドライブの追加 / 更新")
        self.resize(760, 520)
        self._catalog = catalog
        self._app_settings = app_settings
        self._target_drive_id = target_drive_id
        self._volumes: list[VolumeInfo] = []
        self._worker: ScanWorker | None = None
        self._context_total = 0
        self._in_context_phase = False
        self.updated_drive_id: str | None = None  # スキャン成功時に設定

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("スキャンするドライブを選択してください。"))

        self._table = QTableWidget(0, 7)
        self._table.setHorizontalHeaderLabels(["ドライブ", "ラベル", "種類", "形式", "容量", "空き", "カタログ登録"])
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(24)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setStretchLastSection(True)
        apply_selection_style(self._table)
        self._table.itemSelectionChanged.connect(self._on_volume_selected)
        layout.addWidget(self._table, 1)

        form = QFormLayout()
        self._target = QComboBox()
        self._target.currentIndexChanged.connect(self._on_target_changed)
        form.addRow("登録先:", self._target)
        self._name = QLineEdit()
        form.addRow("表示名:", self._name)
        self._source = QComboBox()
        for value, label in _SOURCE_LABELS:
            self._source.addItem(label, value)
        form.addRow("取得方法:", self._source)
        layout.addLayout(form)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        buttons = QDialogButtonBox()
        self._refresh_button = QPushButton("再読み込み")
        self._refresh_button.clicked.connect(self._load_volumes)
        buttons.addButton(self._refresh_button, QDialogButtonBox.ButtonRole.ResetRole)
        self._start_button = QPushButton("スキャン開始")
        self._start_button.setDefault(True)
        self._start_button.clicked.connect(self._start)
        buttons.addButton(self._start_button, QDialogButtonBox.ButtonRole.ActionRole)
        self._close_button = QPushButton("閉じる")
        self._close_button.clicked.connect(self.reject)
        buttons.addButton(self._close_button, QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(buttons)

        self._load_volumes()

    # ------------------------------------------------------------------ ドライブ一覧
    def _load_volumes(self) -> None:
        self._volumes = [volume for volume in list_volumes() if volume.ready]
        self._table.setRowCount(len(self._volumes))
        preselect = -1
        for row, volume in enumerate(self._volumes):
            matches = self._catalog.find_matching_drives(volume, volume.root)
            registered = "、".join(drive["name"] for drive in matches)
            kind = _DRIVE_TYPE_LABELS.get(volume.drive_type, volume.drive_type)
            if volume.bus_type:
                kind += f" ({volume.bus_type})"
            values = [
                volume.root[:2],
                volume.label,
                kind,
                volume.filesystem,
                format_size(volume.total_bytes),
                format_size(volume.free_bytes),
                registered,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in (4, 5):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self._table.setItem(row, column, item)
            if self._target_drive_id and any(drive["id"] == self._target_drive_id for drive in matches):
                preselect = row
        if preselect >= 0:
            self._table.selectRow(preselect)
        elif self._target_drive_id:
            name = self._catalog.drive(self._target_drive_id)["name"]
            self._status.setText(f"「{name}」に一致するドライブが接続されていません。接続してから「再読み込み」を押してください。")
        self._on_volume_selected()

    def _selected_volume(self) -> VolumeInfo | None:
        rows = self._table.selectionModel().selectedRows()
        return self._volumes[rows[0].row()] if rows else None

    def _on_volume_selected(self) -> None:
        volume = self._selected_volume()
        self._target.blockSignals(True)
        self._target.clear()
        if volume is not None:
            matches = self._catalog.find_matching_drives(volume, volume.root)
            for drive in matches:
                self._target.addItem(f"更新: {drive['name']}", drive["id"])
            self._target.addItem("新規に追加", None)
            if self._target_drive_id:
                index = self._target.findData(self._target_drive_id)
                if index >= 0:
                    self._target.setCurrentIndex(index)
        self._target.blockSignals(False)
        self._on_target_changed()
        self._start_button.setEnabled(volume is not None and self._worker is None)

    def _on_target_changed(self) -> None:
        volume = self._selected_volume()
        drive_id = self._target.currentData()
        if drive_id:
            self._name.setText(self._catalog.drive(drive_id)["name"])
        elif volume is not None:
            self._name.setText(volume.display_name)
        else:
            self._name.clear()

    # ------------------------------------------------------------------ スキャン
    def _make_es_client(self, source: str) -> EsClient | None:
        if source == scanner.SOURCE_WALK:
            return None
        es_path = find_es_exe(self._app_settings.es_path or None)
        if es_path is None:
            return None
        try:
            return EsClient(es_path, self._app_settings.es_instance or None)
        except EsError:
            return None

    def _start(self) -> None:
        volume = self._selected_volume()
        if volume is None:
            return
        source = self._source.currentData()
        es = self._make_es_client(source)
        if es is None and source == scanner.SOURCE_EVERYTHING:
            QMessageBox.warning(
                self, self.windowTitle(),
                "es.exe が見つかりません。「設定」→「アプリ設定」で es.exe の場所を指定してください。",
            )  # fmt: skip
            return
        self._set_running(True)
        self._context_total = 0
        self._in_context_phase = False
        self._worker = ScanWorker(
            self._catalog, volume.root, self._target.currentData(), self._name.text().strip(), source, es, self
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.succeeded.connect(self._on_succeeded)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_thread_finished)
        self._worker.start()

    def _set_running(self, running: bool) -> None:
        for widget in (self._table, self._target, self._name, self._source, self._refresh_button):
            widget.setEnabled(not running)
        self._start_button.setEnabled(not running)
        self._close_button.setText("キャンセル" if running else "閉じる")
        self._progress.setVisible(running)
        self._progress.setRange(0, 0)

    def _on_progress(self, phase: str, count: int) -> None:
        if phase.startswith("source:"):
            text = "ファイルリストを取得中… (直接走査)"
            if phase.endswith(scanner.SOURCE_EVERYTHING):
                text = "ファイルリストを取得中… (Everything)"
                scan_settings = self._catalog.settings.get("scan", {})
                if scan_settings.get("with_ctime", True) or scan_settings.get("with_attrs", True):
                    text += (
                        "\nEverything が作成日時・属性をインデックスしていない場合、ファイル数に応じて数十秒〜数分かかります"
                        "(カタログ設定で取得をオフにすると数秒で終わります)。"
                    )
            self._status.setText(text)
        elif phase == "read":
            self._status.setText(f"ファイルリストを取得中… {count:,} 件")
        elif phase == "build":
            self._status.setText(f"データベースを作成中… {count:,} 件")
        elif phase == "context_total":
            self._in_context_phase = True
            self._context_total = count
            self._progress.setRange(0, max(count, 1))
            self._progress.setValue(0)
            self._status.setText(f"拡張コンテキストを取得中… 0 / {count:,} 件")
        elif phase == "context":
            self._progress.setValue(count)
            self._status.setText(f"拡張コンテキストを取得中… {count:,} / {self._context_total:,} 件")
        elif phase == "save":
            self._in_context_phase = False
            self._progress.setRange(0, 0)
            self._status.setText("カタログに保存中…")

    def _on_succeeded(self, drive: dict, result: scanner.ScanResult, context_stats) -> None:
        self.updated_drive_id = drive["id"]
        stats = result.stats
        lines = [
            f"「{drive['name']}」を登録しました。",
            f"ファイル {stats.file_count:,} / フォルダ {stats.dir_count:,} / 合計 {format_size(stats.total_size)}"
            f" (無視 {stats.ignored_count:,} 件、取得元: {'Everything' if result.source == scanner.SOURCE_EVERYTHING else '直接走査'})",
        ]
        if context_stats is not None:
            lines.append(
                f"拡張コンテキスト: 取得 {context_stats.processed:,} 件 / 引き継ぎ {context_stats.reused:,} 件"
                f" / エラー {context_stats.errors:,} 件"
            )
            if context_stats.cancelled:
                lines.append(
                    "拡張コンテキストの取得は途中でキャンセルされました。取得できた分までを登録しています"
                    "(次回このドライブを更新すると、残りのファイルだけを取得します)。"
                )
        lines += [f"注意: {warning}" for warning in result.warnings]
        self._status.setText("\n".join(lines))

    def _on_failed(self, message: str) -> None:
        if message:
            self._status.setText(f"スキャンに失敗しました: {message}")
            QMessageBox.critical(self, self.windowTitle(), f"スキャンに失敗しました。\n\n{message}")
        else:
            self._status.setText("キャンセルしました。")

    def _on_thread_finished(self) -> None:
        self._worker = None
        self._set_running(False)
        self._load_volumes_keep_status()

    def _load_volumes_keep_status(self) -> None:
        status = self._status.text()
        self._load_volumes()
        self._status.setText(status)

    def reject(self) -> None:
        if self._worker is not None:
            if self._in_context_phase:
                self._status.setText("キャンセルしています… (取得できた拡張コンテキストまでを登録します)")
            else:
                self._status.setText("キャンセルしています…")
            self._worker.cancel()
            return
        if self.updated_drive_id:
            super().accept()
        else:
            super().reject()
