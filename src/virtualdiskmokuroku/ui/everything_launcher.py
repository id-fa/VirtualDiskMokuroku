"""Everything 本体が起動していないときに、起動するか確認して起動する。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog, QWidget

from ..core.es_client import (
    EVERYTHING_START_TIMEOUT,
    EsClient,
    EsError,
    find_everything_exe,
    start_everything,
    wait_for_everything,
)
from ..i18n import tr

START_TIMEOUT = EVERYTHING_START_TIMEOUT


def ask_to_start_everything(parent: QWidget | None, es: EsClient, title: str) -> bool:
    """Everything が起動していなければ、es.exe の近くにある Everything.exe を起動するか確認して起動する。

    戻り値は起動したか (= 接続できるようになるまで待つ必要があるか)。起動済み・Everything.exe が見つからない・
    断られた・起動に失敗したときは False。呼び出し側はどちらでもそのまま続け、Everything が無いときの扱い
    (自動なら直接走査へ切り替え) はこれまでどおりスキャン側に任せる。
    """
    try:
        if es.is_everything_running():
            return False
    except EsError:
        return False  # 接続できない以外の問題。従来どおりスキャン時のエラーに任せる
    exe = find_everything_exe(es.es_path)
    if exe is None:
        return False
    answer = QMessageBox.question(
        parent, title,
        tr('Everything が起動していません。起動しますか?\n\n{exe}').format(exe=exe),
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes,
    )  # fmt: skip
    if answer != QMessageBox.StandardButton.Yes:
        return False
    try:
        start_everything(exe, es.instance)
    except OSError as error:
        QMessageBox.warning(parent, title, tr('Everything を起動できませんでした: {error}').format(error=error))
        return False
    return True


def wait_for_everything_with_dialog(parent: QWidget | None, es: EsClient, title: str) -> bool:
    """起動した Everything に接続できるようになるまで、キャンセルできる進捗ダイアログを出して待つ。

    時間内に応答しなければ警告を出して False。キャンセルしたときは何も出さずに False。
    """
    progress = QProgressDialog(tr('Everything を起動しています…'), tr('キャンセル'), 0, 0, parent)
    progress.setWindowTitle(title)
    progress.setWindowModality(Qt.WindowModality.WindowModal)
    progress.setMinimumDuration(0)
    progress.show()

    def should_stop() -> bool:
        QApplication.processEvents()
        return progress.wasCanceled()

    try:
        running = wait_for_everything(es, START_TIMEOUT, should_stop=should_stop)
        cancelled = progress.wasCanceled()  # close() でもキャンセル扱いになるので、閉じる前に見る
    finally:
        progress.close()
    if not running and not cancelled:
        QMessageBox.warning(
            parent, title,
            tr('Everything を起動しましたが、応答がありません。Everything 1.5 アルファ版の場合は「アプリ設定」の「Everything インスタンス名」を確認してください。'),
        )  # fmt: skip
    return running


def offer_to_start_everything(parent: QWidget | None, es: EsClient, title: str) -> bool:
    """起動の確認から接続できるまでの待機までをまとめて行う (接続テストなど、その場で結果が要るとき用)。

    戻り値は Everything に接続できる見込みがあるか (起動済み、または起動して応答した)。
    """
    if not ask_to_start_everything(parent, es, title):
        try:
            return es.is_everything_running()
        except EsError:
            return True
    return wait_for_everything_with_dialog(parent, es, title)
