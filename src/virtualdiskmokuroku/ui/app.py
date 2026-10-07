from __future__ import annotations

import sys

from PySide6.QtCore import QLibraryInfo, QTranslator
from PySide6.QtWidgets import QApplication

from ..core.catalog import cleanup_stale_sessions
from ..i18n import LANGUAGE_JA, current_language
from .main_window import APP_NAME, MainWindow


def install_qt_translator(app) -> bool:
    """Qt 自身の文言 (メッセージボックスのボタンなど) を表示言語に合わせる。英語は Qt の既定なので何もしない。"""
    if current_language() != LANGUAGE_JA:
        return False
    translator = QTranslator(app)
    if not translator.load("qtbase_ja", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        return False
    app.installTranslator(translator)
    return True


def run_gui(catalog_path: str | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    install_qt_translator(app)
    cleanup_stale_sessions()  # 前回の異常終了で残った、復号済み DB の一時フォルダを片付ける
    window = MainWindow()
    window.show()
    if catalog_path:
        window.open_catalog(catalog_path)
    return app.exec()
