"""GUI のスモークテスト (オフスクリーン)。"""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from virtualdiskmokuroku.core import scanner  # noqa: E402
from virtualdiskmokuroku.core.catalog import Catalog  # noqa: E402
from virtualdiskmokuroku.core.ignore import DEFAULT_IGNORE, IgnoreRules  # noqa: E402
from virtualdiskmokuroku.core.search import SCOPE_ALL, SCOPE_FOLDER, SCOPE_SUBTREE  # noqa: E402
from virtualdiskmokuroku.core.settings import AppSettings  # noqa: E402
from virtualdiskmokuroku.ui.main_window import ROLE_DIR, MainWindow  # noqa: E402
from virtualdiskmokuroku.ui.models import COL_LOCATION, COL_SIZE  # noqa: E402

from test_core import make_tree  # noqa: E402


@pytest.fixture(scope="module")
def app():
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("VIRTUALDISKMOKUROKU_CACHE", str(tmp_path / "cache"))
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "qsettings"))

    catalog = Catalog.create(tmp_path / "ui.vdmoku")
    for name in ("one", "two"):
        tree = tmp_path / name
        tree.mkdir()
        make_tree(tree)
        (tree / f"only_in_{name}.txt").write_bytes(b"abc")
        db_path = tmp_path / f"{name}.db"
        result = scanner.scan_to_db(str(tree), db_path, source=scanner.SOURCE_WALK, ignore=IgnoreRules(DEFAULT_IGNORE))
        catalog.put_drive(db_path, result, name=f"ドライブ{name}")

    main = MainWindow(AppSettings())
    main.show()
    assert main.open_catalog(catalog.path)
    yield main
    main.close()


def wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


def names(window):
    return [row.entry.name for row in window.table_model.rows]


def wait_names(window, expected):
    assert wait_until(lambda: names(window) == expected), names(window)


def test_browse_filter_search_export(window, tmp_path):
    root_names = ["docs", "docs.old", "empty", "music", "a.txt", "only_in_one.txt", "Zeta.bin"]
    wait_names(window, root_names)
    assert window.tree_model.rowCount() == 2
    assert "空き" in window.tree_model.item(0).text()
    assert window.table.isColumnHidden(COL_LOCATION)
    # フォルダ行のサイズは配下の集計値
    assert window.table_model.data(window.table_model.index(0, COL_SIZE)) == "110 B"
    assert "フォルダ 4 / ファイル 3" in window.items_label.text()

    # フォルダへ移動 → ツリーも追従
    window._on_table_double_clicked(window.table_model.index(0, 0))
    wait_names(window, ["sub", "readme.md"])
    assert window.address.text().endswith("one\\docs")
    current = window.tree_model.itemFromIndex(window.tree.currentIndex())
    assert current.text() == "docs" and current.data(ROLE_DIR) == window._location[1]

    # フォルダ内フィルタ / 下位フォルダを含むフィルタ
    window.filter_edit.setText("deep")
    wait_names(window, [])
    window.scope_combo.setCurrentIndex(window.scope_combo.findData(SCOPE_SUBTREE))
    wait_names(window, ["deep.txt"])
    assert not window.table.isColumnHidden(COL_LOCATION)
    assert window.table_model.rows[0].location.endswith("one\\docs\\sub")

    # 全ドライブ検索
    window.scope_combo.setCurrentIndex(window.scope_combo.findData(SCOPE_ALL))
    window.filter_edit.setText("only_in")
    wait_names(window, ["only_in_one.txt", "only_in_two.txt"])
    assert {row.drive_name for row in window.table_model.rows} == {"ドライブone", "ドライブtwo"}

    # コピー
    window.table.selectAll()
    window.copy_names()
    assert QApplication.clipboard().text() == "only_in_one.txt\r\nonly_in_two.txt"
    window.copy_paths()
    assert QApplication.clipboard().text().splitlines()[1].endswith("two\\only_in_two.txt")

    # エクスポート
    from virtualdiskmokuroku.core import export

    csv_path = tmp_path / "out.csv"
    assert export.export_rows(csv_path, window.table_model.rows, export.FORMAT_CSV) == 2
    text = csv_path.read_text(encoding="utf-8-sig")
    assert text.splitlines()[0].startswith("名前,場所,サイズ") and "only_in_two.txt" in text
    txt_path = tmp_path / "out.txt"
    export.export_rows(txt_path, window.table_model.rows, export.FORMAT_TXT)
    assert txt_path.read_text(encoding="utf-8-sig").splitlines()[0].endswith("one\\only_in_one.txt")

    # 場所を開く → 親フォルダへ移動して該当ファイルを選択
    window.table.setCurrentIndex(window.table_model.index(1, 0))
    window.open_location()
    assert wait_until(lambda: "only_in_two.txt" in names(window) and len(names(window)) == 7)
    assert window.filter_edit.text() == ""
    selected = window.table_model.row_at(window.table.currentIndex())
    assert selected.entry.name == "only_in_two.txt"
    assert window._location[0] == window.catalog.drives[1]["id"]

    # 履歴で戻る / 上へ
    window.go_back()
    wait_names(window, ["sub", "readme.md"])
    window.go_up()
    assert wait_until(lambda: len(names(window)) == 7 and "only_in_one.txt" in names(window))
    assert window.table_model.row_at(window.table.currentIndex()).entry.name == "docs"

    # 並べ替え (サイズ降順でもフォルダが先頭)
    window.table.sortByColumn(COL_SIZE, Qt.SortOrder.DescendingOrder)
    assert names(window)[:4] == ["music", "docs", "docs.old", "empty"]
    assert names(window)[4] == "a.txt"

    # アドレスバーからの移動
    window.address.setText(window.catalog.drives[0]["root"] + "\\docs\\sub")
    window._on_address_entered()
    wait_names(window, ["deep.txt", "写真 100%.JPG"])


def test_context_scan_search_and_redecode(window, tmp_path):
    pytest.importorskip("virtualdiskmokuroku.context")
    from virtualdiskmokuroku.ui.scan_dialog import ScanWorker

    tree = tmp_path / "ctx"
    tree.mkdir()
    make_tree(tree)
    (tree / "memo.txt").write_bytes("これは秘密のメモです".encode("cp932"))
    catalog = window.catalog
    catalog.settings["context"] = {"text": {"enabled": True}}
    catalog.save()

    outcome = {}
    worker = ScanWorker(catalog, str(tree), None, "コンテキスト", scanner.SOURCE_WALK, None)
    worker.succeeded.connect(lambda drive, result, stats: outcome.update(drive=drive, stats=stats))
    worker.failed.connect(lambda message: outcome.update(error=message))
    worker.start()
    assert wait_until(lambda: worker.isFinished() and outcome, timeout=20), outcome
    assert "error" not in outcome, outcome
    drive = outcome["drive"]
    assert drive["has_context"] and outcome["stats"].processed >= 1

    window._after_catalog_changed(drive["id"])
    assert wait_until(lambda: "memo.txt" in names(window))
    assert window.context_check.isEnabled()

    # ファイル名には無い語を本文から検索
    window.scope_combo.setCurrentIndex(window.scope_combo.findData(SCOPE_SUBTREE))
    window.filter_edit.setText("秘密")
    wait_names(window, [])
    window.context_check.setChecked(True)
    wait_names(window, ["memo.txt"])

    # プロパティに本文が出る
    window.table.selectRow(0)
    assert wait_until(lambda: window.properties._text_box.isVisible())
    assert window.properties._text.toPlainText() == "これは秘密のメモです"

    # 文字コードを指定して再取込 → カタログにも書き戻される
    entry_id = window.table_model.rows[0].entry.id
    window._redecode_text(drive["id"], entry_id, "latin-1")
    assert wait_until(lambda: names(window) == [] or window.properties._text.toPlainText() != "これは秘密のメモです")
    reopened = Catalog.open(catalog.path)
    from virtualdiskmokuroku.context.context_db import ContextDB

    with ContextDB(reopened.extract_context_db(drive["id"])) as context:
        encoding, content = context.get_text(entry_id)
    assert encoding.lower().replace("_", "-") in ("latin-1", "iso8859-1", "iso-8859-1") and "秘密" not in content

    # 再スキャン (更新) では前回の抽出結果を引き継ぐ
    outcome.clear()
    worker = ScanWorker(catalog, str(tree), drive["id"], "", scanner.SOURCE_WALK, None)
    worker.succeeded.connect(lambda drive, result, stats: outcome.update(drive=drive, stats=stats))
    worker.failed.connect(lambda message: outcome.update(error=message))
    window._close_databases()
    worker.start()
    assert wait_until(lambda: worker.isFinished() and outcome, timeout=20), outcome
    assert "error" not in outcome, outcome
    assert outcome["stats"].reused >= 1 and outcome["stats"].processed == 0
    assert len(outcome["drive"]["backups"]) == 1
    window._after_catalog_changed(drive["id"])


def test_cancel_during_context_registers_partial_results(window, tmp_path):
    pytest.importorskip("virtualdiskmokuroku.context")
    from virtualdiskmokuroku.context.context_db import ContextDB
    from virtualdiskmokuroku.ui.scan_dialog import ScanWorker

    tree = tmp_path / "many"
    tree.mkdir()
    for number in range(200):
        (tree / f"note_{number:03}.txt").write_text(f"memo {number}", encoding="utf-8")
    catalog = window.catalog
    catalog.settings["context"] = {"text": {"enabled": True}}

    def run(drive_id, cancel_at):
        outcome = {}
        worker = ScanWorker(catalog, str(tree), drive_id, "many", scanner.SOURCE_WALK, None)

        def on_progress(phase, count):
            if cancel_at is not None and phase == "context" and count >= cancel_at:
                worker.cancel()

        # 進捗はワーカースレッドから直接受け取り、決まった位置でキャンセルする
        worker.progress.connect(on_progress, Qt.ConnectionType.DirectConnection)
        worker.succeeded.connect(lambda drive, result, stats: outcome.update(drive=drive, stats=stats))
        worker.failed.connect(lambda message: outcome.update(error=message))
        worker.start()
        assert wait_until(lambda: worker.isFinished() and outcome, timeout=20), outcome
        assert "error" not in outcome, outcome
        return outcome["drive"], outcome["stats"]

    drive, stats = run(None, cancel_at=60)
    assert stats.cancelled and 60 <= stats.processed < 200
    assert drive["has_context"] and drive["context_partial"] and drive["file_count"] == 200
    with ContextDB(Catalog.open(catalog.path).extract_context_db(drive["id"])) as context:
        assert context.summary() == {"text": stats.processed}

    # 次の更新では取得済みの分を引き継ぎ、残りだけを読む
    first = stats.processed
    drive, stats = run(drive["id"], cancel_at=None)
    assert not stats.cancelled and stats.reused == first and stats.processed == 200 - first
    assert not drive["context_partial"]
    window._after_catalog_changed(drive["id"])


def test_open_folder_in_explorer(window, tmp_path, monkeypatch):
    import shutil

    from PySide6.QtWidgets import QMessageBox

    label = "このフォルダをエクスプローラで開く"

    def explorer_action(menu):
        return next((item for item in menu.actions() if item.text().startswith(label)), None)

    opened, warnings = [], []
    monkeypatch.setattr(os, "startfile", opened.append, raising=False)
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, _title, text, *args: warnings.append(text))

    wait_names(window, ["docs", "docs.old", "empty", "music", "a.txt", "only_in_one.txt", "Zeta.bin"])
    drive = window.catalog.drives[0]
    tree_root = str(tmp_path / "one")
    # テスト用ツリーは接続中のボリューム上にあるので「同じドライブが接続されている」と判定される
    assert os.path.samefile(window.catalog.connected_root(drive["id"]), tree_root)

    # 選択なし → 表示中のフォルダ
    window.table.clearSelection()
    explorer_action(window._build_table_menu()).trigger()
    assert os.path.samefile(opened[-1], tree_root)

    # フォルダ行を選択 → そのフォルダ / ファイル行を選択 → 格納フォルダ
    window.table.selectRow(names(window).index("docs"))
    explorer_action(window._build_table_menu()).trigger()
    assert os.path.samefile(opened[-1], os.path.join(tree_root, "docs"))
    window.table.selectRow(names(window).index("a.txt"))
    explorer_action(window._build_table_menu()).trigger()
    assert os.path.samefile(opened[-1], tree_root)

    # 検索結果のファイル行 → そのファイルがあるフォルダ
    window.scope_combo.setCurrentIndex(window.scope_combo.findData(SCOPE_SUBTREE))
    window.filter_edit.setText("deep")
    wait_names(window, ["deep.txt"])
    window.table.selectRow(0)
    explorer_action(window._build_table_menu()).trigger()
    assert os.path.samefile(opened[-1], os.path.join(tree_root, "docs", "sub"))
    window.filter_edit.setText("")
    wait_until(lambda: len(names(window)) == 7)

    # ツリーのフォルダを右クリック
    drive_item = window.tree_model.item(0)
    window.tree.setExpanded(drive_item.index(), True)
    music = next(drive_item.child(row) for row in range(drive_item.rowCount()) if drive_item.child(row).text() == "music")
    explorer_action(window._build_tree_menu(music.index())).trigger()
    assert os.path.samefile(opened[-1], os.path.join(tree_root, "music"))
    assert not warnings

    # カタログにはあるが実際には無くなったフォルダ → 開かずにエラー表示
    shutil.rmtree(os.path.join(tree_root, "music"))
    count = len(opened)
    explorer_action(window._build_tree_menu(music.index())).trigger()
    assert len(opened) == count and len(warnings) == 1 and "見つかりません" in warnings[0]

    # 同じドライブが接続されていない (別のボリューム) → メニューに出さない
    drive["serial"] = "0000-0000"
    assert window.catalog.connected_root(drive["id"]) is None
    assert explorer_action(window._build_tree_menu(drive_item.index())) is None
    window.table.clearSelection()
    assert explorer_action(window._build_table_menu()) is None


def test_limit_and_drive_management(window):
    window.app_settings.result_limit = 3
    window.scope_combo.setCurrentIndex(window.scope_combo.findData(SCOPE_SUBTREE))
    window.filter_edit.setText("t")
    assert wait_until(lambda: len(names(window)) == 3 and window._truncated)
    assert "上限" in window.items_label.text()

    window.app_settings.result_limit = 1000
    window.scope_combo.setCurrentIndex(window.scope_combo.findData(SCOPE_FOLDER))
    window.filter_edit.setText("")
    assert wait_until(lambda: len(names(window)) == 7 and not window._truncated)

    # ドライブ削除 → 残りのドライブが表示される
    first = window.catalog.drives[0]["id"]
    window._close_databases()
    window.catalog.remove_drive(first)
    window._after_catalog_changed()
    assert window.tree_model.rowCount() == 1
    assert wait_until(lambda: "only_in_two.txt" in names(window))


def test_encrypted_catalog_leaves_no_plaintext_on_disk(window, tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    pytest.importorskip("virtualdiskmokuroku.context")
    Image = pytest.importorskip("PIL.Image")
    import tempfile
    import zipfile

    from PySide6.QtWidgets import QMessageBox

    from virtualdiskmokuroku.core.search import SCOPE_ALL
    from virtualdiskmokuroku.ui.scan_dialog import ScanWorker
    from virtualdiskmokuroku.ui.settings_dialogs import CatalogSettingsDialog
    from virtualdiskmokuroku.ui.thumbnail_view import VIEW_DETAILS, VIEW_TILES

    sqlite_magic = b"SQLite format 3"
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_dir))  # 一時ファイルの置き場をここに向けて監視する
    cache_dir = tmp_path / "cache"

    def scan_disk():
        """一時フォルダとキャッシュにある、平文の SQLite ファイルや一覧の CSV。"""
        found = []
        for folder in (temp_dir, cache_dir):
            for base, _dirs, file_names in os.walk(folder):
                for file_name in file_names:
                    path = os.path.join(base, file_name)
                    with open(path, "rb") as f:
                        head = f.read(len(sqlite_magic))
                    if head == sqlite_magic or file_name.lower().endswith(".csv"):
                        found.append(path)
        return found

    # それまで開いていた通常のカタログのキャッシュは対象外。暗号化カタログを扱い始めてから増えた分だけを見る
    window._close_databases()
    baseline = set(scan_disk())

    def plaintext_left():
        return [path for path in scan_disk() if path not in baseline]

    tree = tmp_path / "secret_tree"
    (tree / "書類").mkdir(parents=True)
    (tree / "書類" / "極秘メモ.txt").write_bytes("これは誰にも見せない内容です".encode("cp932"))
    for number in range(5):
        Image.new("RGB", (64, 32), (200, number * 40, 90)).save(tree / f"photo_{number}.png")

    # --- 暗号化カタログを作ってスキャン (拡張コンテキストあり)
    from virtualdiskmokuroku.core.catalog import Catalog as CatalogClass

    catalog = CatalogClass.create(tmp_path / "secret.vdmoku", "合言葉", encrypt=True)
    catalog.settings["context"] = {"text": {"enabled": True}, "thumbnail": {"enabled": True, "size": 48}}
    catalog.save()
    window._set_catalog(catalog)
    assert "[暗号化]" in window.windowTitle() and window.act_export_decrypted.isEnabled()

    def scan(drive_id):
        outcome = {}
        worker = ScanWorker(catalog, str(tree), drive_id, "秘密", scanner.SOURCE_WALK, None)
        worker.succeeded.connect(lambda drive, result, stats: outcome.update(drive=drive, stats=stats, result=result))
        worker.failed.connect(lambda message: outcome.update(error=message))
        worker.start()
        assert wait_until(lambda: worker.isFinished() and outcome, timeout=30), outcome
        assert "error" not in outcome, outcome
        return outcome

    outcome = scan(None)
    drive = outcome["drive"]
    assert outcome["stats"].processed == 6 and outcome["result"].database is None
    assert plaintext_left() == []

    # --- 閲覧・検索・サムネイル表示
    window._after_catalog_changed(drive["id"])
    assert wait_until(lambda: len(names(window)) == 6)
    window.scope_combo.setCurrentIndex(window.scope_combo.findData(SCOPE_ALL))
    window.context_check.setChecked(True)
    window.filter_edit.setText("誰にも見せない")
    wait_names(window, ["極秘メモ.txt"])
    window.table.selectRow(0)
    assert wait_until(lambda: window.properties._text.toPlainText() == "これは誰にも見せない内容です")
    window.filter_edit.setText("")
    assert wait_until(lambda: len(names(window)) == 6)
    window.set_view_mode(VIEW_TILES)
    photo = window.table_model.rows[names(window).index("photo_3.png")]
    info = window.thumb_provider.get(photo)
    assert info.pixmap is not None and info.resolution == "64 x 32"
    assert not window.thumb_view.grab().isNull()
    window.set_view_mode(VIEW_DETAILS)
    assert plaintext_left() == []

    # --- 文字コード再取込 (メモリ上で書き換えてカタログへ戻す)
    memo_id = window._db(drive["id"]).find_path("書類\\極秘メモ.txt").id
    window._redecode_text(drive["id"], memo_id, "latin-1")
    assert wait_until(lambda: len(names(window)) == 6)
    assert "誰にも" not in window._context_db(drive["id"]).get_text(memo_id)[1]
    window._redecode_text(drive["id"], memo_id, "cp932")
    assert wait_until(lambda: len(names(window)) == 6)
    assert window._context_db(drive["id"]).get_text(memo_id)[1] == "これは誰にも見せない内容です"

    # --- 更新スキャン: 前回の結果 (メモリ上の DB) を引き継ぐ
    outcome = scan(drive["id"])
    assert outcome["stats"].reused == 6 and outcome["stats"].processed == 0
    assert len(outcome["drive"]["backups"]) == 1
    window._after_catalog_changed(drive["id"])
    assert wait_until(lambda: len(names(window)) == 6)
    assert plaintext_left() == []

    # --- カタログのどこにも平文は無い
    raw = catalog.path.read_bytes()
    for needle in (sqlite_magic, "極秘メモ".encode("utf-8"), "秘密".encode("utf-8"), b"photo_3.png", b"\xff\xd8\xff\xe0"):
        assert needle not in raw

    # --- 設定画面からのパスワード変更と暗号化の解除 (その場で実行される)
    dialog = CatalogSettingsDialog(catalog, window, prepare_rewrite=window._close_databases)
    assert not dialog._change_key_button.isHidden() and dialog._encrypt_button.isHidden()
    assert dialog._rewrite_catalog(lambda: catalog.set_password("新しい合言葉"), "失敗")
    with pytest.raises(Exception):
        CatalogClass.open(catalog.path, "合言葉")
    reopened = CatalogClass.open(catalog.path, "新しい合言葉")
    assert reopened.drive(drive["id"])["has_context"]
    reopened.close()

    # 復号して書き出したカタログは、暗号化なしで同じ内容を持つ
    exported = tmp_path / "exported" / "plain.vdmoku"
    exported.parent.mkdir()
    catalog.export_decrypted(exported)
    with zipfile.ZipFile(exported) as archive:
        assert archive.read(f"drives/{drive['id']}/files.db").startswith(sqlite_magic)
    assert plaintext_left() == []  # 書き出し先以外には平文を作らない

    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    dialog._decrypt()
    assert dialog.catalog_rewritten and not catalog.encrypted
    assert not dialog._encrypt_button.isHidden() and dialog._change_key_button.isHidden()
    dialog.close()
    window._update_ui_state()
    assert "[暗号化]" not in window.windowTitle() and not window.act_export_decrypted.isEnabled()
    window._run_query()
    assert wait_until(lambda: len(names(window)) == 6)


def test_thumbnail_view_modes(window, tmp_path):
    pytest.importorskip("virtualdiskmokuroku.context")
    Image = pytest.importorskip("PIL.Image")
    from PySide6.QtTest import QTest

    from virtualdiskmokuroku.pipeline import scan_into_catalog
    from virtualdiskmokuroku.ui.thumbnail_view import VIEW_DETAILS, VIEW_THUMB_LIST, VIEW_TILES

    # サムネイルを持たないカタログでは選べず、設定されていても詳細表示のまま
    wait_until(lambda: len(names(window)) == 7)
    assert not window._mode_actions[VIEW_TILES].isEnabled()
    window.set_view_mode(VIEW_TILES)
    assert window.effective_view_mode() == VIEW_DETAILS and window._view() is window.table
    window.set_view_mode(VIEW_DETAILS)

    tree = tmp_path / "photos"
    (tree / "album").mkdir(parents=True)
    for number in range(12):
        Image.new("RGB", (64, 32), (number * 20, 100, 200)).save(tree / f"img_{number:02}.png")
    (tree / "note.txt").write_text("memo", encoding="utf-8")
    catalog = window.catalog
    catalog.settings["context"] = {"thumbnail": {"enabled": True, "size": 48}}
    catalog.save()
    window._close_databases()
    outcome = scan_into_catalog(catalog, str(tree), name="写真", source=scanner.SOURCE_WALK)
    window._after_catalog_changed(outcome.drive["id"])
    assert wait_until(lambda: len(names(window)) == 14)
    window.resize(1200, 700)

    # --- 敷き詰め
    assert window._mode_actions[VIEW_TILES].isEnabled()
    window.set_view_mode(VIEW_TILES)
    view = window.thumb_view
    assert window._view() is view and window.view_stack.currentWidget() is view
    assert AppSettings.load().view_mode == VIEW_TILES
    delegate = view.thumb_delegate
    assert (delegate.base_size, delegate.zoom, delegate.box_size) == (48, 1, 48)
    model = window.table_model
    wait_until(lambda: view.visualRect(model.index(13, 0)).isValid())
    first, second = view.visualRect(model.index(0, 0)), view.visualRect(model.index(1, 0))
    assert first.top() == second.top() and second.left() > first.left()  # 横に並ぶ
    assert first.width() < 100

    image_row = model.rows[names(window).index("img_03.png")]
    info = window.thumb_provider.get(image_row)
    assert info.pixmap is not None and (info.pixmap.width(), info.pixmap.height()) == (48, 24)
    assert info.resolution == "64 x 32"
    assert window.thumb_provider.get(model.rows[names(window).index("note.txt")]).pixmap is None
    assert not view.grab().isNull()  # 描画でエラーにならない

    # サムネイルの下の項目を減らすと低くなり、2 倍拡大で大きくなる
    full_height = first.height()
    window._caption_actions["size"].setChecked(False)
    window._caption_actions["mtime"].setChecked(False)
    assert delegate.captions == ("name", "resolution")
    assert wait_until(lambda: view.visualRect(model.index(0, 0)).height() < full_height)
    assert AppSettings.load().thumb_captions == ["name", "resolution"]
    window.act_thumb_zoom.setChecked(True)
    assert delegate.box_size == 96
    assert wait_until(lambda: view.visualRect(model.index(0, 0)).width() > 96)
    assert not view.grab().isNull()

    # クリックで選択 → コピーやステータス表示は詳細表示と同じように働く
    position = names(window).index("img_03.png")
    wait_until(lambda: view.visualRect(model.index(position, 0)).isValid())
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(model.index(position, 0)).center())
    assert [row.entry.name for row in window._selected_rows()] == ["img_03.png"]
    window.copy_names()
    assert QApplication.clipboard().text() == "img_03.png"
    assert "1 個選択" in window.items_label.text()
    assert window.properties._thumb.isVisible()

    # --- 情報付き: 1 項目が横幅いっぱいになる
    window.set_view_mode(VIEW_THUMB_LIST)
    assert wait_until(lambda: view.visualRect(model.index(0, 0)).width() > view.viewport().width() * 0.8)
    assert view.visualRect(model.index(1, 0)).top() > view.visualRect(model.index(0, 0)).top()
    assert not view.grab().isNull()

    # --- 詳細に戻すと、選択は行全体の選択として引き継がれる
    window.set_view_mode(VIEW_DETAILS)
    assert window._view() is window.table
    selected = window.table.selectionModel().selectedRows()
    assert [model.rows[index.row()].entry.name for index in selected] == ["img_03.png"]

    # メニューからの並べ替え (同じ項目をもう一度選ぶと逆順)
    window.set_view_mode(VIEW_TILES)
    window.sort_by(0)
    assert names(window)[:2] == ["album", "note.txt"]
    window.sort_by(0)
    assert names(window)[:2] == ["album", "img_00.png"]

    # フォルダをダブルクリックすると、サムネイル表示のまま中へ移動する
    window._on_table_double_clicked(model.index(0, 0))
    assert wait_until(lambda: window.address.text().endswith("album"))
    assert window._view() is view
    window.act_thumb_zoom.setChecked(False)
    window.set_view_mode(VIEW_DETAILS)


def test_import_vcdcase(window, tmp_path, monkeypatch):
    import vcdcase_sample

    from virtualdiskmokuroku.ui.import_dialog import ImportDialog

    cas_path = tmp_path / "sample.cas"
    cas_path.write_bytes(vcdcase_sample.sample_case())
    dialogs = []

    def run_dialog(dialog):
        dialogs.append(dialog)
        assert wait_until(lambda: dialog._worker is None, timeout=20)
        dialog.reject()

    monkeypatch.setattr(ImportDialog, "exec", run_dialog)
    assert window.act_import_vcdcase.isEnabled()
    window.import_vcdcase(str(cas_path))
    assert dialogs[0].outcome is not None and "2 台のドライブを追加しました" in dialogs[0]._status.text()

    # 追加されたドライブへ移動している
    assert window.tree_model.rowCount() == 4
    wait_names(window, ["写真", "data.lzh", "memo.txt", "readme.txt", "setup.exe", "tv.avi"])
    assert window.address.text() == "?:\\"
    assert window._current_drive()["name"] == "BACKUP_2003"

    # コメントはテキスト内容として、プロパティはメタ情報として出る (フォルダのコメントも)
    window.table.selectRow(names(window).index("readme.txt"))
    assert wait_until(lambda: window.properties._text.toPlainText().startswith("はじめにお読みください。"))
    window.table.selectRow(names(window).index("setup.exe"))
    assert wait_until(lambda: window.properties._meta.isVisible())
    meta = window.properties._meta
    shown = {meta.item(row, 0).text(): meta.item(row, 1).text() for row in range(meta.rowCount())}
    assert shown["会社"] == "サンプル社" and shown["ファイルバージョン"] == "1.2.3.4"
    assert meta.item(0, 0).toolTip() == "Virtual CD-ROM Case からのインポート"
    window.table.selectRow(names(window).index("data.lzh"))
    assert wait_until(lambda: window.properties._inner.isVisible() and window.properties._inner.rowCount() == 3)
    window.table.selectRow(names(window).index("写真"))
    assert wait_until(lambda: window.properties._text.toPlainText() == "2003 年の旅行")

    # コメントも検索できる
    window.scope_combo.setCurrentIndex(window.scope_combo.findData(SCOPE_ALL))
    window.context_check.setChecked(True)
    window.filter_edit.setText("お読みください")
    wait_names(window, ["readme.txt"])

    # 読めないファイルはエラーを表示し、カタログは変えない
    broken = tmp_path / "broken.cas"
    broken.write_bytes(cas_path.read_bytes()[:-7])
    shown_errors = []
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "critical", lambda _parent, _title, text: shown_errors.append(text))
    window.import_vcdcase(str(broken))
    assert dialogs[1].outcome is None and "構造が合いません" in shown_errors[0]
    assert window.tree_model.rowCount() == 4


def test_drive_groups(window, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    from virtualdiskmokuroku.ui.main_window import ROLE_DRIVE, ROLE_GROUP

    assert wait_until(lambda: len(names(window)) == 7)
    one, two = [drive["id"] for drive in window.catalog.drives]
    assert window._current_drive()["id"] == one

    # 表示中のドライブを新しいグループに入れる → ツリーにグループの行ができ、その下にドライブが入る
    monkeypatch.setattr(QInputDialog, "getItem", lambda *args, **kwargs: ("バックアップ", True))
    window.set_current_drive_group()
    assert window.catalog.drive(one)["group"] == "バックアップ"
    assert window.tree_model.rowCount() == 2
    group_item = window.tree_model.item(0)
    assert (group_item.text(), group_item.data(ROLE_GROUP), group_item.data(ROLE_DRIVE)) == ("バックアップ", "バックアップ", None)
    assert group_item.rowCount() == 1 and group_item.child(0).data(ROLE_DRIVE) == one
    assert window.tree.isExpanded(group_item.index())
    assert window.tree_model.item(1).data(ROLE_DRIVE) == two
    assert wait_until(lambda: len(names(window)) == 7)
    assert window.tree_model.itemFromIndex(window.tree.currentIndex()).data(ROLE_DRIVE) == one

    # グループの行を選んでも一覧は変わらない。右クリックメニューはグループ名の変更
    location = window._location
    window.tree.setCurrentIndex(group_item.index())
    assert window._location == location and not window.tree.selectionModel().isSelected(group_item.index())
    group_menu = [item.text() for item in window._build_tree_menu(group_item.index()).actions() if item.text()]
    assert group_menu[0].startswith("グループ名を変更") and window.act_rename.text() not in group_menu
    assert window.act_set_group in window._build_tree_menu(group_item.child(0).index()).actions()

    # 一覧でフォルダへ移動すると、グループの下のツリーも追従する
    window._on_table_double_clicked(window.table_model.index(0, 0))
    wait_names(window, ["sub", "readme.md"])
    current = window.tree_model.itemFromIndex(window.tree.currentIndex())
    assert current.text() == "docs" and current.parent().parent().data(ROLE_GROUP) == "バックアップ"

    # グループ名を変更。折りたたんだ状態は、ツリーを作り直しても保たれる
    window.navigate(two, 0)
    window.tree.collapse(window.tree_model.item(0).index())
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("保管", True))
    window.rename_group("バックアップ")
    group_item = window.tree_model.item(0)
    assert group_item.text() == "保管" and not window.tree.isExpanded(group_item.index())
    assert window._current_drive()["id"] == two

    # もう 1 台も同じグループへ (既存のグループを選ぶ) → 最上位はグループだけになり、選択中のドライブが見えるよう展開される
    monkeypatch.setattr(QInputDialog, "getItem", lambda *args, **kwargs: ("保管", True))
    window.set_current_drive_group()
    group_item = window.tree_model.item(0)
    assert window.tree_model.rowCount() == 1 and group_item.rowCount() == 2
    assert window.tree.isExpanded(group_item.index())
    assert window.tree_model.itemFromIndex(window.tree.currentIndex()).data(ROLE_DRIVE) == two

    # 空欄にするとグループから外れる。開き直しても同じ構成になる
    monkeypatch.setattr(QInputDialog, "getItem", lambda *args, **kwargs: ("", True))
    window.set_current_drive_group()
    assert "group" not in window.catalog.drive(two)
    assert [window.tree_model.item(row).data(ROLE_GROUP) for row in range(window.tree_model.rowCount())] == ["保管", None]
    path = window.catalog.path
    assert window.open_catalog(path)
    assert [window.tree_model.item(row).data(ROLE_GROUP) for row in range(window.tree_model.rowCount())] == ["保管", None]
    assert window.tree_model.item(0).child(0).data(ROLE_DRIVE) == one


def test_english_ui(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication as _QApplication

    from virtualdiskmokuroku import i18n
    from virtualdiskmokuroku.ui.app import install_qt_translator
    from virtualdiskmokuroku.ui.organize_dialog import OrganizeDialog
    from virtualdiskmokuroku.ui.settings_dialogs import AppSettingsDialog

    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "qsettings"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.delenv(i18n.ENV_VAR, raising=False)
    assert i18n.set_language("en") == "en"
    try:
        assert not install_qt_translator(_QApplication.instance())  # 英語は Qt の既定なので翻訳は入れない
        main = MainWindow(AppSettings())
        assert [item.text() for item in main.menuBar().actions()] == ["&File", "&Edit", "&View", "&Drive", "&Settings", "&Help"]
        main.close_catalog()
        assert main.items_label.text() == "No catalog is open"
        assert main.table_model.headerData(0, Qt.Orientation.Horizontal) == "Name"
        assert main.act_organize.text() == "&Organize drives…"
        assert main.windowTitle().startswith("VirtualDiskMokuroku ")
        catalog = Catalog.create(tmp_path / "en.vdmoku")
        assert main.open_catalog(catalog.path)
        assert "No drives are registered" in main.items_label.text()
        dialog = OrganizeDialog(catalog, main)
        assert dialog.windowTitle() == "Organize drives"
        assert [dialog.model.headerData(column, Qt.Orientation.Horizontal) for column in range(2)] == ["Name", "Files"]
        settings = AppSettings()
        settings_dialog = AppSettingsDialog(settings, main)
        assert settings_dialog.windowTitle() == "Application settings"
        settings_dialog._language.setCurrentIndex(settings_dialog._language.findData("ja"))
        settings_dialog.accept()
        assert settings.language == "ja" and AppSettings.load().language == "ja"
        main.close()
        # エラーメッセージ (core) も英語になる
        from virtualdiskmokuroku.core.errors import PasswordError

        with pytest.raises(PasswordError, match="Wrong password"):
            Catalog.open(Catalog.create(tmp_path / "pw.vdmoku", password="x").path, "y")
    finally:
        monkeypatch.setenv(i18n.ENV_VAR, "ja")
        i18n.set_language(None)
    assert i18n.tr("キャンセル") == "キャンセル"
    # 日本語では Qt 自身の文言 (ボタンなど) の翻訳も入れる
    assert install_qt_translator(_QApplication.instance())


def test_organize_dialog(window, tmp_path, monkeypatch):
    from PySide6.QtCore import QModelIndex
    from PySide6.QtWidgets import QMessageBox

    from virtualdiskmokuroku.ui.main_window import ROLE_GROUP as MAIN_ROLE_GROUP
    from virtualdiskmokuroku.ui.organize_dialog import COL_CONTEXT, ROLE_DRIVE, ROLE_GROUP, OrganizeDialog

    move = Qt.DropAction.MoveAction
    assert wait_until(lambda: len(names(window)) == 7)
    catalog = window.catalog
    one, two = [drive["id"] for drive in catalog.drives]
    # 3 台目は拡張コンテキスト付き
    db_path = tmp_path / "three.db"
    result = scanner.scan_to_db(str(tmp_path / "one"), db_path, source=scanner.SOURCE_WALK, ignore=IgnoreRules(DEFAULT_IGNORE))
    window._close_databases()
    three = catalog.put_drive(db_path, result, name="ドライブthree", context_db_path=b"context")["id"]
    window._after_catalog_changed(one)
    assert wait_until(lambda: len(names(window)) == 7)
    assert window.act_organize.isEnabled()

    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args, **_kwargs: QMessageBox.StandardButton.Ok)
    dialog = OrganizeDialog(catalog, window, prepare_rewrite=window._close_databases)
    model = dialog.model

    def item(drive_id):
        found = model.match(model.index(0, 0), ROLE_DRIVE, drive_id, 1, Qt.MatchFlag.MatchExactly | Qt.MatchFlag.MatchRecursive)
        return model.itemFromIndex(found[0])

    assert model.rowCount() == 3 and dialog.layout_of_tree() == [(one, None), (two, None), (three, None)]
    assert model.item(2, COL_CONTEXT).text() == "あり"
    # サイズ列は元データの集計ではなく、カタログ内で DB が占めるサイズ
    from virtualdiskmokuroku.core.formatting import format_size
    from virtualdiskmokuroku.ui.organize_dialog import COL_SIZE

    storage = catalog.storage_sizes()
    assert model.item(2, COL_SIZE).text() == format_size(storage[three][0]) != format_size(catalog.drive(three)["total_size"])
    assert "カタログ内" in model.item(2, COL_SIZE).toolTip()
    # 日時列はスキャン日時ではなく、ドライブ内で最も新しいファイルの更新日時
    from virtualdiskmokuroku.core.formatting import format_filetime
    from virtualdiskmokuroku.ui.organize_dialog import COL_LATEST

    assert model.item(2, COL_LATEST).text() == format_filetime(catalog.drive(three)["latest_mtime"]) != ""
    assert "スキャン" in model.item(2, COL_LATEST).toolTip()
    assert not dialog.copy_button.isEnabled()  # 選択なし

    # 新しいグループに、選択中のドライブを入れる
    dialog._select_items([item(one)])
    dialog.new_group("保管")
    group = model.item(0)
    assert group.data(ROLE_GROUP) == "保管" and group.rowCount() == 1
    assert dialog.layout_of_tree() == [(one, "保管"), (two, None), (three, None)]

    # ドラッグ & ドロップ (モデルの操作): ドライブはグループの中へ入れられるが、ドライブの中へは入れられない
    mime = model.mimeData([item(two).index()])
    assert not model.canDropMimeData(mime, move, -1, -1, item(one).index())
    assert model.canDropMimeData(mime, move, -1, -1, group.index())
    assert model.dropMimeData(mime, move, -1, -1, group.index()) is False  # 自分で動かすのでビューには消させない
    assert dialog.layout_of_tree() == [(one, "保管"), (two, "保管"), (three, None)]

    # グループは別のグループの中へは入れられない。最上位では並び替えられる
    dialog._select_items([item(three)])
    dialog.new_group("写真")
    photos = model.item(1)
    assert photos.data(ROLE_GROUP) == "写真" and dialog.layout_of_tree() == [(one, "保管"), (two, "保管"), (three, "写真")]
    mime = model.mimeData([group.index()])
    assert not model.canDropMimeData(mime, move, -1, -1, photos.index())
    assert not model.canDropMimeData(mime, move, 0, 0, photos.index())
    assert model.canDropMimeData(mime, move, 2, 0, QModelIndex())
    model.dropMimeData(mime, move, 2, 0, QModelIndex())
    assert dialog.layout_of_tree() == [(three, "写真"), (one, "保管"), (two, "保管")]
    # 自分自身の位置に落としても壊れない
    mime = model.mimeData([item(one).index()])
    model.dropMimeData(mime, move, 0, 0, group.index())
    assert dialog.layout_of_tree() == [(three, "写真"), (one, "保管"), (two, "保管")]
    # ビュー経由のドロップ (ドロップイベントを合成): two をグループ「写真」の末尾へ
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QDropEvent

    dialog.show()
    dialog.tree.expandAll()
    assert wait_until(lambda: dialog.tree.visualRect(item(three).index()).isValid())
    mime = model.mimeData([item(two).index()])
    rect = dialog.tree.visualRect(photos.index())
    event = QDropEvent(QPointF(rect.center()), move, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    # InternalMove では自分から始まったドラッグしか受けないが、合成したイベントには送り元が無いので一時的に緩める
    from PySide6.QtWidgets import QAbstractItemView

    dialog.tree.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
    dialog.tree.dropEvent(event)
    dialog.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
    assert not event.isAccepted()  # 自分で動かした (ビューに元の行を消させない)
    assert dialog.layout_of_tree() == [(three, "写真"), (two, "写真"), (one, "保管")]
    model.dropMimeData(model.mimeData([item(two).index()]), move, -1, -1, group.index())  # 戻す
    assert dialog.layout_of_tree() == [(three, "写真"), (one, "保管"), (two, "保管")]

    # 上へ / 下へ、グループから外す
    dialog._select_items([group])
    dialog.move_up()
    assert dialog.layout_of_tree() == [(one, "保管"), (two, "保管"), (three, "写真")]
    dialog._select_items([item(two)])
    dialog.move_up()
    dialog.move_up()  # 端ではそのまま
    assert dialog.layout_of_tree() == [(two, "保管"), (one, "保管"), (three, "写真")]
    dialog._select_items([item(one)])
    assert dialog.ungroup_button.isEnabled()
    dialog.ungroup()
    assert dialog.layout_of_tree() == [(two, "保管"), (one, None), (three, "写真")]
    assert [row.data(ROLE_DRIVE) for row in dialog._selected_items()] == [one]

    # グループ名の変更 (既にある名前にすると 1 つにまとまる)
    dialog._select_items([photos])
    assert dialog.rename_group_button.isEnabled()
    dialog.rename_group("アルバム")
    assert dialog.layout_of_tree() == [(two, "保管"), (one, None), (three, "アルバム")]
    dialog.rename_group("保管")
    assert dialog.layout_of_tree() == [(two, "保管"), (three, "保管"), (one, None)]
    assert model.rowCount() == 2

    # 拡張コンテキストの削除予定 (もう一度押すと取りやめ)
    dialog._select_items([item(one)])
    assert not dialog.context_button.isEnabled()
    dialog._select_items([group])  # グループを選ぶと中のドライブが対象
    assert dialog.context_button.isEnabled()
    dialog.toggle_context_removal()
    assert dialog._context_drop == {three} and group.child(1, COL_CONTEXT).text() == "削除予定"
    assert "取りやめ" in dialog.context_button.text()
    dialog.toggle_context_removal()
    assert dialog._context_drop == set() and group.child(1, COL_CONTEXT).text() == "あり"
    dialog.toggle_context_removal()

    # 他のカタログへコピー (グループを選ぶと中の全ドライブ)。自分自身へはコピーできない
    dialog._select_items([group, item(one)])
    assert dialog.copy_button.isEnabled()
    target = tmp_path / "copy.vdmoku"
    copied = dialog.copy_to_catalog(str(target))
    assert [drive["id"] for drive in copied] == [two, three, one]
    copied_catalog = Catalog.open(target)
    assert [(drive["name"], drive.get("group")) for drive in copied_catalog.drives] == [
        ("ドライブtwo", "保管"), ("ドライブthree", "保管"), ("ドライブone", None),
    ]  # fmt: skip
    assert copied_catalog.extract_context_db(three).read_bytes() == b"context"
    with copied_catalog.open_drive_db(one) as db:
        assert db.find_path("only_in_one.txt") is not None
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", lambda _parent, _title, text: errors.append(text))
    assert dialog.copy_to_catalog(str(catalog.path)) is None and "自身" in errors[0]
    # 既存のカタログを選ぶと追加になる (同じ ID があれば振り直す)
    dialog._select_items([item(one)])
    assert dialog.copy_to_catalog(str(target))[0]["id"] != one
    assert len(Catalog.open(target).drives) == 4

    # 削除予定にするとツリーから消える。OK でまとめてカタログへ書き込む
    dialog._select_items([item(one)])
    dialog.remove_selected()
    assert one in dialog._removed and dialog.layout_of_tree() == [(two, "保管"), (three, "保管")]
    assert "1 台のドライブを削除" in dialog.summary.text() and "拡張コンテキスト" in dialog.summary.text()
    dialog.accept()
    assert dialog.changed and dialog.reload_needed
    reopened = Catalog.open(catalog.path)
    assert [(drive["id"], drive.get("group")) for drive in reopened.drives] == [(two, "保管"), (three, "保管")]
    assert not reopened.drive(three)["has_context"] and reopened.extract_context_db(three) is None
    assert [(drive["id"], drive.get("group")) for drive in catalog.drives] == [(two, "保管"), (three, "保管")]
    window._after_catalog_changed(two)
    assert wait_until(lambda: "only_in_two.txt" in names(window))

    # メインウィンドウから: 何も変えずに OK を押すとカタログは書き換えない
    updated_at = Catalog.read_manifest(catalog.path)["updated_at"]
    monkeypatch.setattr(OrganizeDialog, "exec", lambda self: self.accept())
    window.organize_drives()
    assert Catalog.read_manifest(catalog.path)["updated_at"] == updated_at

    # 並び替えて OK → ツリーが作り直され、表示中のドライブはそのまま
    def reorder(self):
        self._select_items([self.model.item(0).child(1)])
        self.ungroup()
        self.move_up()
        self.accept()

    monkeypatch.setattr(OrganizeDialog, "exec", reorder)
    window.organize_drives()
    assert [(drive["id"], drive.get("group")) for drive in window.catalog.drives] == [(three, None), (two, "保管")]
    assert window.tree_model.rowCount() == 2 and window.tree_model.item(1).data(MAIN_ROLE_GROUP) == "保管"
    assert window._current_drive()["id"] == two
    assert wait_until(lambda: "only_in_two.txt" in names(window))
