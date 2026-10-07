"""他のカタログソフトのデータを取り込むダイアログ(進捗表示とキャンセル)。"""

from __future__ import annotations

import os
import threading

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QMessageBox, QProgressBar, QVBoxLayout

from ..core.catalog import Catalog
from ..core.errors import ScanCancelled
from ..core.formatting import format_size
from ..importer import ImportOutcome, import_vcdcase
from ..i18n import tr


class ImportWorker(QThread):
    progress = Signal(str, int)
    succeeded = Signal(object)  # ImportOutcome
    failed = Signal(str)  # 空文字はキャンセル

    def __init__(self, catalog: Catalog, path: str, parent=None):
        super().__init__(parent)
        self._catalog = catalog
        self._path = path
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            outcome = import_vcdcase(
                self._catalog, self._path, progress=self.progress.emit, is_cancelled=self._cancel.is_set
            )
            self.succeeded.emit(outcome)
        except ScanCancelled:
            self.failed.emit("")
        except Exception as error:  # noqa: BLE001 - スレッド内の例外は UI に伝える
            self.failed.emit(str(error) or type(error).__name__)


class ImportDialog(QDialog):
    """Virtual CD-ROM Case のカタログ (.cas) を取り込む。開くとすぐに取り込みを始める。"""

    def __init__(self, catalog: Catalog, path: str | os.PathLike[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr('Virtual CD-ROM Case のカタログをインポート'))
        self.resize(560, 220)
        self.outcome: ImportOutcome | None = None  # 取り込みに成功すると設定される
        self.error: str | None = None  # 失敗した場合のメッセージ (キャンセルは空文字)
        self._total = 0

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(os.path.basename(path)))
        self._status = QLabel(tr('ファイルを読み込み中…'))
        self._status.setWordWrap(True)
        layout.addWidget(self._status, 1)
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        layout.addWidget(self._progress)
        buttons = QDialogButtonBox()
        self._close_button = buttons.addButton(tr('キャンセル'), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._worker: ImportWorker | None = ImportWorker(catalog, os.fspath(path), self)
        self._worker.progress.connect(self._on_progress)
        self._worker.succeeded.connect(self._on_succeeded)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_thread_finished)
        self._worker.start()

    def _on_progress(self, phase: str, count: int) -> None:
        if phase == "import_total":
            self._total = count
            self._progress.setRange(0, max(count, 1))
            self._progress.setValue(0)
            self._status.setText(tr('ドライブを取り込み中… 0 / {count:,}').format(count=count))
        elif phase == "import":
            self._progress.setValue(count)
            self._status.setText(tr('ドライブを取り込み中… {count:,} / {_total:,}').format(count=count, _total=self._total))
        elif phase == "save":
            self._progress.setRange(0, 0)
            self._status.setText(tr('カタログに保存中…'))

    def _on_succeeded(self, outcome: ImportOutcome) -> None:
        self.outcome = outcome
        lines = [
            tr('{drives_count:,} 台のドライブを追加しました。').format(drives_count=len(outcome.drives)),
            tr('ファイル {file_count:,} / フォルダ {dir_count:,} / 合計 {total_size}').format(file_count=outcome.file_count, dir_count=outcome.dir_count, total_size=format_size(outcome.total_size)),
            tr('コメントなどの情報: {context_count:,} 件').format(context_count=outcome.context_count),
        ]
        self._status.setText("\n".join(lines))

    def _on_failed(self, message: str) -> None:
        self.error = message
        if message:
            self._status.setText(tr('インポートに失敗しました: {message}').format(message=message))
            QMessageBox.critical(self, self.windowTitle(), tr('インポートに失敗しました。\n\n{message}').format(message=message))
        else:
            self._status.setText(tr('キャンセルしました。'))

    def _on_thread_finished(self) -> None:
        self._worker = None
        self._progress.setVisible(False)
        self._close_button.setText(tr('閉じる'))

    def reject(self) -> None:
        if self._worker is not None:
            self._status.setText(tr('キャンセルしています…'))
            self._worker.cancel()
            return
        if self.outcome is not None:
            super().accept()
        else:
            super().reject()
