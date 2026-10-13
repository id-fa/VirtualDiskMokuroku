"""他のカタログソフトのデータを取り込むダイアログ(進捗表示とキャンセル)。"""

from __future__ import annotations

import os
import threading

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QMessageBox, QProgressBar, QVBoxLayout

from ..core.catalog import Catalog
from ..core.errors import ScanCancelled
from ..core.formatting import format_size
from ..importer import ImportOutcome, import_catalog
from ..i18n import tr

_BAR_SCALE = 1000  # プログレスバーはドライブ 1 台をこの目盛りで表し、ドライブ内の進み具合も見せる


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
            outcome = import_catalog(
                self._catalog, self._path, progress=self.progress.emit, is_cancelled=self._cancel.is_set
            )
            self.succeeded.emit(outcome)
        except ScanCancelled:
            self.failed.emit("")
        except Exception as error:  # noqa: BLE001 - スレッド内の例外は UI に伝える
            self.failed.emit(str(error) or type(error).__name__)


class ImportDialog(QDialog):
    """他のカタログソフトのファイル (.cas / .caz / .fsr) を取り込む。開くとすぐに取り込みを始める。"""

    def __init__(self, catalog: Catalog, path: str | os.PathLike[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr('他のソフトのカタログをインポート'))
        self.resize(560, 220)
        self.outcome: ImportOutcome | None = None  # 取り込みに成功すると設定される
        self.error: str | None = None  # 失敗した場合のメッセージ (キャンセルは空文字)
        self._total = 0  # ドライブ数
        self._done = 0  # 処理の終わったドライブ数
        self._entry_total = 0  # 今取り込んでいるドライブのエントリ数
        self._headline = tr('ファイルを読み込み中…')  # 状態表示の 1 行目 (全体の進み具合)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(os.path.basename(path)))
        self._status = QLabel(self._headline)
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

    def _set_status(self, headline: str | None = None, detail: str = "") -> None:
        """1 行目に全体の進み具合、2 行目に今している作業を出す。``headline`` を省くと 1 行目はそのまま。"""
        if headline is not None:
            self._headline = headline
        self._status.setText(f"{self._headline}\n{detail}" if detail else self._headline)

    def _set_bar(self, fraction: float) -> None:
        """今のドライブの進み具合 (0〜1) をプログレスバーに反映する。"""
        if self._total:
            self._progress.setValue(min(self._done * _BAR_SCALE + int(fraction * _BAR_SCALE), self._total * _BAR_SCALE))

    def _on_progress(self, phase: str, count: int) -> None:
        # phase の順序は importer.import_catalog の docstring を参照
        if phase == "decompress":
            self._set_status(tr('圧縮されたファイルを展開中… ({size})').format(size=format_size(count)))
        elif phase == "parse":
            self._set_status(tr('ファイルを読み込み中… {count:,} 件').format(count=count))
        elif phase == "import_total":
            self._total = count
            self._done = 0
            self._progress.setRange(0, max(count, 1) * _BAR_SCALE)
            self._progress.setValue(0)
            self._set_status(tr('ドライブを取り込み中… 0 / {count:,}').format(count=count))
        elif phase.startswith("drive:"):
            self._entry_total = count
            name = phase[len("drive:") :]
            self._set_status(
                tr('ドライブを取り込み中… {current:,} / {total:,}: {name}').format(current=self._done + 1, total=self._total, name=name),
                tr('エントリ {count:,} 件').format(count=count),
            )
        elif phase == "read":
            # DB 作成の前半 (一時テーブルへの書き込み)。バーの前半 3 割を使う
            self._set_bar(0.3 * min(count / max(self._entry_total, 1), 1.0))
            self._set_status(detail=tr('ファイルリストを準備中… {count:,} / {total:,} 件').format(count=count, total=self._entry_total))
        elif phase == "build":
            self._set_bar(0.3 + 0.6 * min(count / max(self._entry_total, 1), 1.0))
            self._set_status(detail=tr('データベースを作成中… {count:,} / {total:,} 件').format(count=count, total=self._entry_total))
        elif phase == "context":
            self._set_bar(0.9)
            self._set_status(detail=tr('コメントなどの情報を取り込み中… {count:,} 件').format(count=count))
        elif phase == "import":
            self._done = count
            self._set_bar(0.0)
            self._set_status(tr('ドライブを取り込み中… {count:,} / {_total:,}').format(count=count, _total=self._total))
        elif phase == "save":
            if self._done >= self._total:
                self._progress.setRange(0, 0)
            self._set_status(detail=tr('カタログに保存中…'))

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
