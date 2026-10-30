from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QTranslator
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from ..core.catalog import cleanup_stale_sessions
from ..i18n import LANGUAGE_JA, current_language
from .main_window import APP_NAME, MainWindow

# ウィンドウ左上・タスクバー用のアイコン。exe に埋め込むアイコン (scripts\build_exe.ps1 の --icon) と同じファイルで、
# 作り直すときは scripts\make_icon.py を実行する
ICON_PATH = Path(__file__).resolve().parent / "VirtualDiskMokuroku.ico"


def app_icon() -> QIcon:
    """アプリのアイコンを返す。ファイルが無い (配布物から欠けた) ときは空のアイコン。"""
    return QIcon(str(ICON_PATH)) if ICON_PATH.is_file() else QIcon()


def set_app_user_model_id() -> bool:
    """タスクバーがウィンドウを python.exe ではなくこのアプリとしてまとめ、ウィンドウのアイコンを使うようにする。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"id-fa.{APP_NAME}")
    except (AttributeError, OSError):
        return False
    return True


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
    set_app_user_model_id()  # ウィンドウを作る前に設定しないとタスクバーに反映されない
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(app_icon())
    install_qt_translator(app)
    cleanup_stale_sessions()  # 前回の異常終了で残った、復号済み DB の一時フォルダを片付ける
    window = MainWindow()
    window.show()
    if catalog_path:
        window.open_catalog(catalog_path)
    return app.exec()
