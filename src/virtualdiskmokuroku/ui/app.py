from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .main_window import APP_NAME, MainWindow


def run_gui(catalog_path: str | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    window = MainWindow()
    window.show()
    if catalog_path:
        window.open_catalog(catalog_path)
    return app.exec()
