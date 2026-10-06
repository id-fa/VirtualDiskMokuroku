from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from ..core.catalog import cleanup_stale_sessions
from .main_window import APP_NAME, MainWindow


def run_gui(catalog_path: str | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    cleanup_stale_sessions()  # 前回の異常終了で残った、復号済み DB の一時フォルダを片付ける
    window = MainWindow()
    window.show()
    if catalog_path:
        window.open_catalog(catalog_path)
    return app.exec()
