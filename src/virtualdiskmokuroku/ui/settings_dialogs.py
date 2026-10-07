"""カタログ設定・アプリ設定のダイアログ。"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.catalog import Catalog
from ..core.errors import CatalogError
from ..core.es_client import EsClient, EsError, find_es_exe
from ..core.settings import AppSettings
from ..i18n import LANGUAGE_AUTO, LANGUAGE_EN, LANGUAGE_JA, tr


def ask_password(parent, title: str, label: str) -> str | None:
    text, accepted = QInputDialog.getText(parent, title, label, QLineEdit.EchoMode.Password)
    return text if accepted else None


def ask_new_password(parent, title: str) -> str | None:
    """新しいパスワードを 2 回入力させる。キャンセル時は None。"""
    while True:
        first = ask_password(parent, title, tr('新しいパスワード:'))
        if first is None:
            return None
        if not first:
            QMessageBox.warning(parent, title, tr('パスワードを入力してください。'))
            continue
        second = ask_password(parent, title, tr('確認のためもう一度入力:'))
        if second is None:
            return None
        if first == second:
            return first
        QMessageBox.warning(parent, title, tr('パスワードが一致しません。'))


class _ExtractorEditor(QGroupBox):
    """拡張コンテキスト 1 種類分の有効/無効とパラメータ編集。"""

    def __init__(self, kind: str, extractor_class, values: dict, parent=None):
        super().__init__(tr(extractor_class.label), parent)
        self.kind = kind
        self._defaults = dict(extractor_class.default_params)
        self._editors: dict[str, QWidget] = {}
        self.setCheckable(True)
        self.setChecked(bool(values.get("enabled")))

        form = QFormLayout(self)
        description = QLabel(tr(extractor_class.description))
        description.setWordWrap(True)
        form.addRow(description)
        if not extractor_class.available():
            warning = QLabel(tr('必要なライブラリがありません: pip install {requirement}').format(requirement=extractor_class.requirement()))
            warning.setStyleSheet("color: #c0392b;")
            form.addRow(warning)
        for key, default in self._defaults.items():
            value = values.get(key, default)
            if isinstance(default, bool):
                editor: QWidget = QCheckBox()
                editor.setChecked(bool(value))
            elif isinstance(default, int):
                editor = QSpinBox()
                editor.setRange(0, 10_000_000)
                editor.setValue(int(value))
            elif isinstance(default, (list, tuple)):
                editor = QLineEdit(" ".join(str(item) for item in value))
                editor.setToolTip(tr('空白区切りで指定'))
            else:
                editor = QLineEdit(str(value))
            self._editors[key] = editor
            form.addRow(tr(_PARAM_LABELS.get(key, key)) + ":", editor)

    def values(self) -> dict:
        result: dict = {"enabled": self.isChecked()}
        for key, editor in self._editors.items():
            default = self._defaults[key]
            if isinstance(editor, QCheckBox):
                result[key] = editor.isChecked()
            elif isinstance(editor, QSpinBox):
                result[key] = editor.value()
            elif isinstance(default, (list, tuple)):
                assert isinstance(editor, QLineEdit)
                result[key] = [item.lstrip(".").lower() for item in editor.text().replace(",", " ").split()]
            else:
                assert isinstance(editor, QLineEdit)
                result[key] = editor.text()
        return result


_PARAM_LABELS = {
    "extensions": "対象の拡張子",
    "max_bytes": "最大ファイルサイズ (バイト)",
    "size": "長辺のサイズ (px)",
    "quality": "JPEG 品質",
    "max_entries": "書庫 1 つあたりの最大件数",
}


class CatalogSettingsDialog(QDialog):
    """カタログ毎の設定(カタログ自体に保存される)。"""

    def __init__(self, catalog: Catalog, parent=None, prepare_rewrite: Callable[[], None] | None = None):
        """``prepare_rewrite`` は暗号化の切り替えなどでカタログを書き換える直前に呼ばれる(開いている DB を閉じる用)。"""
        super().__init__(parent)
        self.setWindowTitle(tr('カタログ設定'))
        self.resize(620, 560)
        self._catalog = catalog
        self._prepare_rewrite = prepare_rewrite
        self.catalog_rewritten = False  # 暗号化の切り替えやパスワード変更をその場で実行したか
        settings = catalog.settings
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        # --- 一般
        general = QWidget()
        form = QFormLayout(general)
        self._generations = QSpinBox()
        self._generations.setRange(0, 99)
        self._generations.setValue(int(settings.get("backup_generations", 1)))
        self._generations.setToolTip(tr('ドライブ更新時に、更新前のデータベースをカタログ内に何世代残すか'))
        form.addRow(tr('バックアップ世代数:'), self._generations)
        scan = settings.get("scan", {})
        self._with_ctime = QCheckBox(tr('作成日時を取得する'))
        self._with_ctime.setChecked(scan.get("with_ctime", True))
        self._with_attrs = QCheckBox(tr('属性を取得する'))
        self._with_attrs.setChecked(scan.get("with_attrs", True))
        form.addRow(tr('スキャン:'), self._with_ctime)
        form.addRow("", self._with_attrs)
        hint = QLabel(
            tr('Everything 側で作成日時・属性をインデックスしていない場合、大容量ドライブでは取得に時間がかかります。遅い場合はオフにしてください。')
        )
        hint.setWordWrap(True)
        form.addRow(hint)
        tabs.addTab(general, tr('一般'))

        # --- 無視リスト
        ignore = QWidget()
        ignore_layout = QVBoxLayout(ignore)
        ignore_layout.addWidget(QLabel(
            tr('カタログに収蔵しないファイル/フォルダ名を 1 行に 1 つ指定します。\n末尾に \\ を付けるとフォルダのみ、* ? はワイルドカード、途中に \\ を含むとルートからの相対パスに一致します。')
        ))  # fmt: skip
        self._ignore = QPlainTextEdit("\n".join(settings.get("ignore_patterns", [])))
        ignore_layout.addWidget(self._ignore)
        tabs.addTab(ignore, tr('無視リスト'))

        # --- 暗号化 / パスワード
        password = QWidget()
        password_layout = QVBoxLayout(password)
        self._password_state = QLabel()
        password_layout.addWidget(self._password_state)

        encryption_box = QGroupBox(tr('暗号化'))
        encryption_layout = QVBoxLayout(encryption_box)
        row = QHBoxLayout()
        self._encrypt_button = QPushButton(tr('このカタログを暗号化…'))
        self._encrypt_button.clicked.connect(self._encrypt)
        self._change_key_button = QPushButton(tr('パスワードを変更…'))
        self._change_key_button.clicked.connect(self._change_encryption_password)
        self._decrypt_button = QPushButton(tr('暗号化を解除…'))
        self._decrypt_button.clicked.connect(self._decrypt)
        for button in (self._encrypt_button, self._change_key_button, self._decrypt_button):
            row.addWidget(button)
        row.addStretch(1)
        encryption_layout.addLayout(row)
        encryption_note = QLabel(
            tr('カタログの中身 (ファイル名・サムネイル・テキスト内容など) を AES-256 で暗号化します。暗号化したカタログは、閲覧やスキャンのときもディスクに平文を書きません。\nパスワードを忘れるとカタログを開けなくなり、復旧する方法はありません。これらの操作はボタンを押した時点で実行されます (OK / キャンセルとは無関係)。')
        )
        encryption_note.setWordWrap(True)
        encryption_layout.addWidget(encryption_note)
        password_layout.addWidget(encryption_box)

        self._gate_box = QGroupBox(tr('パスワードの確認のみ (暗号化なし)'))
        gate_layout = QVBoxLayout(self._gate_box)
        row = QHBoxLayout()
        self._set_password = QPushButton(tr('パスワードを設定 / 変更…'))
        self._set_password.clicked.connect(self._change_password)
        self._clear_password = QPushButton(tr('パスワードを解除'))
        self._clear_password.clicked.connect(self._remove_password)
        row.addWidget(self._set_password)
        row.addWidget(self._clear_password)
        row.addStretch(1)
        gate_layout.addLayout(row)
        note = QLabel(
            tr('このアプリでカタログを開くときにパスワードを確認するだけの保護です。カタログファイル自体は暗号化されないため、ZIP として展開すれば中身を読むことができます。中身を守るには上の「暗号化」を使ってください。')
        )
        note.setWordWrap(True)
        gate_layout.addWidget(note)
        password_layout.addWidget(self._gate_box)
        password_layout.addStretch(1)
        tabs.addTab(password, tr('暗号化 / パスワード'))
        self._pending_password: str | None | bool = False  # False = 変更なし / None = 解除 / str = 新パスワード
        self._update_password_state()

        # --- 拡張コンテキスト
        self._extractor_editors: list[_ExtractorEditor] = []
        context_tab = QWidget()
        context_layout = QVBoxLayout(context_tab)
        context_layout.addWidget(QLabel(
            tr('ファイルの中身から追加情報を取得して保存・検索できるようにします(スキャン時にファイルを読むため時間がかかります)。\n変更は次回のドライブ追加/更新から反映されます。')
        ))  # fmt: skip
        try:
            from ..context import EXTRACTORS, default_context_settings
        except ImportError as error:
            context_layout.addWidget(QLabel(tr('拡張コンテキスト機能を読み込めません: {error}').format(error=error)))
            context_layout.addStretch(1)
        else:
            defaults = default_context_settings()
            current = settings.get("context", {})
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            inner = QWidget()
            inner_layout = QVBoxLayout(inner)
            for kind, extractor_class in EXTRACTORS.items():
                values = {**defaults.get(kind, {}), **current.get(kind, {})}
                editor = _ExtractorEditor(kind, extractor_class, values)
                self._extractor_editors.append(editor)
                inner_layout.addWidget(editor)
            inner_layout.addStretch(1)
            scroll.setWidget(inner)
            context_layout.addWidget(scroll)
        tabs.addTab(context_tab, tr('拡張コンテキスト'))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _has_password(self) -> bool:
        if self._pending_password is False:
            return bool(self._catalog.settings.get("password"))
        return self._pending_password is not None

    def _update_password_state(self) -> None:
        encrypted = self._catalog.encrypted
        protected = self._has_password()
        if encrypted:
            state = tr('状態: 暗号化されています')
        elif protected:
            state = tr('状態: パスワードの確認あり (暗号化なし)')
        else:
            state = tr('状態: 保護なし')
        self._password_state.setText(state)
        self._encrypt_button.setVisible(not encrypted)
        self._change_key_button.setVisible(encrypted)
        self._decrypt_button.setVisible(encrypted)
        self._gate_box.setEnabled(not encrypted)
        self._clear_password.setEnabled(not encrypted and protected)

    def _rewrite_catalog(self, action: Callable[[], None], failure: str) -> bool:
        """暗号化の切り替えなど、カタログ全体を書き換える操作をその場で実行する。"""
        if self._prepare_rewrite is not None:
            self._prepare_rewrite()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            action()
        except (CatalogError, OSError) as error:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, self.windowTitle(), f"{failure}\n\n{error}")
            return False
        else:
            QApplication.restoreOverrideCursor()
        self.catalog_rewritten = True
        self._pending_password = False
        self._update_password_state()
        return True

    def _encrypt(self) -> None:
        answer = QMessageBox.warning(
            self, tr('カタログの暗号化'),
            tr('このカタログを暗号化します。\n\nパスワードを忘れるとカタログを開けなくなり、復旧する方法はありません。続けますか?'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )  # fmt: skip
        if answer != QMessageBox.StandardButton.Yes:
            return
        password = ask_new_password(self, tr('暗号化のパスワード'))
        if password is None:
            return
        if self._rewrite_catalog(lambda: self._catalog.encrypt(password), tr('暗号化に失敗しました。')):
            QMessageBox.information(self, tr('カタログの暗号化'), tr('カタログを暗号化しました。'))

    def _change_encryption_password(self) -> None:
        password = ask_new_password(self, tr('パスワードの変更'))
        if password is None:
            return
        if self._rewrite_catalog(lambda: self._catalog.set_password(password), tr('パスワードを変更できません。')):
            QMessageBox.information(self, tr('パスワードの変更'), tr('パスワードを変更しました。'))

    def _decrypt(self) -> None:
        answer = QMessageBox.warning(
            self, tr('暗号化の解除'),
            tr('暗号化を解除すると、カタログの中身は誰でも読める状態になります。続けますか?'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )  # fmt: skip
        if answer == QMessageBox.StandardButton.Yes:
            self._rewrite_catalog(self._catalog.decrypt, tr('暗号化を解除できません。'))

    def _change_password(self) -> None:
        password = ask_new_password(self, tr('パスワードの設定'))
        if password is not None:
            self._pending_password = password
            self._update_password_state()

    def _remove_password(self) -> None:
        self._pending_password = None
        self._update_password_state()

    def accept(self) -> None:
        settings = self._catalog.settings
        settings["backup_generations"] = self._generations.value()
        settings["scan"] = {"with_ctime": self._with_ctime.isChecked(), "with_attrs": self._with_attrs.isChecked()}
        settings["ignore_patterns"] = [line.strip() for line in self._ignore.toPlainText().splitlines() if line.strip()]
        if self._extractor_editors:
            settings["context"] = {editor.kind: editor.values() for editor in self._extractor_editors}
        if self._prepare_rewrite is not None:
            self._prepare_rewrite()
        try:
            if self._pending_password is False or self._catalog.encrypted:
                self._catalog.save()
            else:
                self._catalog.set_password(self._pending_password or None)
        except (CatalogError, OSError) as error:
            QMessageBox.critical(self, self.windowTitle(), tr('カタログを保存できません。\n\n{error}').format(error=error))
            return
        super().accept()


class AppSettingsDialog(QDialog):
    def __init__(self, app_settings: AppSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr('アプリ設定'))
        self.resize(560, 240)
        self._settings = app_settings
        layout = QVBoxLayout(self)
        form = QFormLayout()

        row = QHBoxLayout()
        self._es_path = QLineEdit(app_settings.es_path)
        self._es_path.setPlaceholderText(tr('空欄の場合は自動検出 (PATH、アプリのフォルダ、既定のインストール先)'))
        browse = QPushButton(tr('参照…'))
        browse.clicked.connect(self._browse)
        row.addWidget(self._es_path, 1)
        row.addWidget(browse)
        form.addRow(tr('es.exe の場所:'), row)

        self._instance = QLineEdit(app_settings.es_instance)
        self._instance.setPlaceholderText(tr('通常は空欄 (Everything 1.5 アルファ版の既定インスタンスは 1.5a)'))
        form.addRow(tr('Everything インスタンス名:'), self._instance)

        self._limit = QSpinBox()
        self._limit.setRange(1000, 10_000_000)
        self._limit.setSingleStep(10000)
        self._limit.setValue(app_settings.result_limit)
        form.addRow(tr('検索結果の表示上限:'), self._limit)

        self._memory_limit = QSpinBox()
        self._memory_limit.setRange(16, 1_048_576)
        self._memory_limit.setSingleStep(128)
        self._memory_limit.setSuffix(" MB")
        self._memory_limit.setValue(app_settings.memory_limit_mb)
        self._memory_limit.setToolTip(
            tr('暗号化カタログのデータベースは、ディスクに平文を書かないようメモリ上で開きます。\n1 つがこの大きさを超える場合だけ、一時フォルダに復号して開き、閉じるときに削除します。')
        )
        form.addRow(tr('暗号化カタログをメモリで開く上限:'), self._memory_limit)

        self._language = QComboBox()
        for value, text in (
            (LANGUAGE_AUTO, tr('自動 (OS の表示言語が日本語なら日本語、それ以外は英語)')),
            (LANGUAGE_JA, "日本語"),
            (LANGUAGE_EN, "English"),
        ):
            self._language.addItem(text, value)
        self._language.setCurrentIndex(max(0, self._language.findData(app_settings.language)))
        self._language.setToolTip(tr('変更は次回の起動から反映されます'))
        form.addRow(tr('表示言語 (次回起動時から):'), self._language)
        layout.addLayout(form)

        test_row = QHBoxLayout()
        test = QPushButton(tr('接続テスト'))
        test.clicked.connect(self._test)
        self._test_result = QLabel("")
        self._test_result.setWordWrap(True)
        test_row.addWidget(test)
        test_row.addWidget(self._test_result, 1)
        layout.addLayout(test_row)
        layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, tr('es.exe を選択'), self._es_path.text(), tr('es.exe (es.exe);;実行ファイル (*.exe)'))
        if path:
            self._es_path.setText(path.replace("/", "\\"))

    def _test(self) -> None:
        es_path = find_es_exe(self._es_path.text().strip() or None)
        if es_path is None:
            self._test_result.setText(tr('es.exe が見つかりません。'))
            return
        try:
            client = EsClient(es_path, self._instance.text().strip() or None, timeout=10)
            self._test_result.setText(f"OK: {es_path}\nes.exe {client.es_version()} / Everything {client.everything_version()}")
        except EsError as error:
            self._test_result.setText(f"{es_path}\n{error}")

    def accept(self) -> None:
        self._settings.es_path = self._es_path.text().strip()
        self._settings.es_instance = self._instance.text().strip()
        self._settings.result_limit = self._limit.value()
        self._settings.memory_limit_mb = self._memory_limit.value()
        self._settings.language = self._language.currentData()
        self._settings.save()
        super().accept()
