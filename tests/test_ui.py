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
