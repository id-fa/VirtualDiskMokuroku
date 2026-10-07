"""Virtual CD-ROM Case のカタログ (.cas) のインポートのテスト。"""

import os
import tempfile

import pytest

from virtualdiskmokuroku import cli
from virtualdiskmokuroku.context.context_db import ContextDB
from virtualdiskmokuroku.core import vcdcase
from virtualdiskmokuroku.core.catalog import Catalog
from virtualdiskmokuroku.core.drive_db import ROOT_ID
from virtualdiskmokuroku.core.errors import CatalogError, ScanCancelled
from virtualdiskmokuroku.importer import IMPORT_ROOT, SOURCE_VCDCASE, import_vcdcase

import vcdcase_sample as sample


@pytest.fixture
def cas_path(tmp_path):
    path = tmp_path / "sample.cas"
    path.write_bytes(sample.sample_case())
    return path


def test_parse_case():
    case = vcdcase.parse_case(sample.sample_case())
    assert (case.schema, case.name) == (12, "C:\\work\\sample.cas")
    backup, floppy = case.drives

    assert (backup.label, backup.serial, backup.filesystem) == ("BACKUP_2003", "1A2B-3C4D", "CDFS")
    assert (backup.total_bytes, backup.free_bytes, backup.media_type, backup.cluster_size) == (700_000_000, 0, 1, 2048)
    assert backup.comment == "2003/04/05" and backup.category == "バックアップ"
    entries = {entry.path: entry for entry in backup.entries}
    # 分類 (コメントの次の文字列)。付けていなければ空
    assert (entries["写真\\index.html"].category, entries["写真\\海.jpg"].category, entries["readme.txt"].category) == ("旅行", "photo", "")
    assert entries["写真\\海.jpg"].has_context
    assert list(entries) == [
        "readme.txt", "setup.exe", "写真", "写真\\index.html", "写真\\海.jpg", "写真\\空のフォルダ", "data.lzh", "memo.txt", "tv.avi",
    ]  # fmt: skip
    readme = entries["readme.txt"]
    assert (readme.is_dir, readme.size, readme.mtime, readme.ctime) == (False, 120, sample.FILETIME_2021, sample.FILETIME_2003)
    assert entries["写真"].is_dir and entries["写真"].size is None
    assert entries["setup.exe"].ctime is None  # 日時 0 は「不明」
    assert entries["写真\\海.jpg"].ctime is None and entries["写真\\海.jpg"].mtime == sample.FILETIME_2003  # 範囲外の日時も「不明」
    assert entries["memo.txt"].comment == sample.LONG_COMMENT
    assert (readme.crc, entries["setup.exe"].crc) == (sample.README_CRC, None)
    # 中身を展開して登録した書庫はフォルダとして記録されているが、内部リストを持つ 1 つのファイルとして読む
    lzh = entries["data.lzh"]
    assert (lzh.is_dir, lzh.size, lzh.attrs, lzh.crc) == (False, 3000, 0, 0x0BADF00D)
    assert lzh.inner == [
        ("doc", None, None, True),
        ("doc/manual.txt", 1500, sample.FILETIME_2003, False),
        ("tool.exe", 2500, sample.FILETIME_2021, False),
    ]

    assert (floppy.label, floppy.serial, floppy.comment) == ("フロッピー 12", "", "友人から借りたディスク")
    assert (floppy.media_type, floppy.cluster_size) == (8, 512)
    assert [entry.path for entry in floppy.entries] == ["AUTOEXEC.BAT"]

    # ラベルが空なら短いラベルを使う。書庫の中の書庫は、内部リストの中のファイルになる
    nested = sample.archive("outer.zip", sample.ZIP, [
        sample.record("inner.lzh", attrs=sample.KIND_MEMBER | sample.KIND_ARCHIVE | sample.DIRECTORY, archive_type=sample.LZH,
                      size=700, children=[sample.member("a.txt", size=9)]),
    ], size=800)  # fmt: skip
    other = vcdcase.parse_case(sample.case_file([sample.drive("", [nested], short_label="SHORT")])).drives[0]
    assert other.label == "SHORT"
    assert other.entries[0].inner == [
        ("inner.lzh", 700, sample.FILETIME_2021, False), ("inner.lzh/a.txt", 9, sample.FILETIME_2021, False),
    ]  # fmt: skip


def test_groups(tmp_path):
    """ドライブをグループ (フォルダ) にまとめたカタログ。グループにもドライブと同じ形式の情報が続く。"""
    data = sample.case_file([
        sample.group("BACKUP", [
            sample.drive("PART1", [sample.record("a.txt", size=1)], comment=b""),
            sample.group("内側", [sample.drive("PART2", [sample.folder("d", [sample.record("b.txt", size=2)])])], comment=b"HDS72251"),
        ], comment=b"2005/01/02"),
        sample.group("空のグループ", []),
        sample.drive("TOP", [sample.record("c.txt", size=3)], comment=b""),
    ])  # fmt: skip
    drives = vcdcase.parse_case(data).drives
    assert [(drive.label, drive.group, drive.group_comments) for drive in drives] == [
        ("PART1", ("BACKUP",), ("2005/01/02",)),
        ("PART2", ("BACKUP", "内側"), ("2005/01/02", "HDS72251")),
        ("TOP", (), ()),
    ]
    assert [[entry.path for entry in drive.entries] for drive in drives] == [["a.txt"], ["d", "d\\b.txt"], ["c.txt"]]

    cas_path = tmp_path / "groups.cas"
    cas_path.write_bytes(data)
    catalog = Catalog.create(tmp_path / "groups.vdmoku", cache_root=tmp_path / "cache")
    import_vcdcase(catalog, cas_path)
    part1, part2, top = catalog.drives
    assert [drive["name"] for drive in catalog.drives] == ["PART1", "PART2", "TOP"]
    assert (part1["label"], part1["group"], part1["group_comment"]) == ("PART1", "BACKUP", "2005/01/02")
    assert part1["scanned_at"].startswith("2005-01-0")  # ドライブのコメントが空なら、グループのコメントの日付を使う
    # グループは 1 段だけなので、入れ子になっていたドライブは最も外側のグループに入る
    assert (part2["group"], part2["group_comment"]) == ("BACKUP", "2005/01/02")
    assert "group" not in top and "comment" not in top
    assert catalog.group_names() == ["BACKUP"]
    with catalog.open_drive_db(part2["id"]) as db:
        assert db.find_path("d\\b.txt").size == 2


def test_decode_text_and_properties():
    assert vcdcase.decode_text(b"plain") == ("cp932", "plain")
    assert vcdcase.decode_text("日本語".encode("cp932")) == ("cp932", "日本語")
    assert vcdcase.decode_text("日本語".encode("utf-8")) == ("utf-8", "日本語")
    assert vcdcase.decode_text("日本語".encode("utf-8")[:-1]) == ("utf-8", "日本")  # 長さ制限で末尾が切れた UTF-8

    assert vcdcase.property_items(list(sample.HTML_PROPERTIES)) == [
        ("property_type", "HTML"), ("title", "写真の一覧"), ("subject", "Photo index"), ("keywords", "photo,sea"),
        ("application", "SampleEditor 1.0"),
    ]  # fmt: skip
    assert vcdcase.property_items(list(sample.PDF_PROPERTIES)) == [
        ("property_type", "PDF-P"), ("title", "報告書"), ("author", "山田"), ("creator", "Writer"),
        ("application", "Distiller 5.0"), ("created", "2002/08/08 13:39:04"), ("modified", "2002/08/08 13:46:35"), ("pages", "53"),
    ]  # fmt: skip
    module = dict(vcdcase.property_items(list(sample.MODULE_PROPERTIES)))
    assert module["file_version"] == "1.2.3.4" and module["company"] == "サンプル社" and module["product_name"] == "Sample Suite"
    # プロパティの数に含まれない最後の文字列 (AVI ではソース) も読む。同じ値の繰り返しは 1 つにまとめる
    avi = next(entry for entry in vcdcase.parse_case(sample.sample_case()).drives[0].entries if entry.path == "tv.avi")
    assert len(avi.properties) == len(sample.AUDIO_PROPERTIES) + 1
    assert vcdcase.property_items(avi.properties) == [
        ("property_type", "Audio"), ("title", "番組名"), ("artist", "2003/08/28 TBS"), ("tag_format", "RiffSIF"),
        ("comments", "capt by camn."), ("application", "VirtualDubMod 1.4.13"), ("created", "2003-08-02"),
        ("copyright", "TBS"), ("source", "CATV / MPEG1"),
    ]  # fmt: skip
    # 並びの分からない種類は、値だけを取り込む
    assert vcdcase.property_items([b"MP3", b"", b"Artist"]) == [("property_type", "MP3"), ("property", "Artist")]
    assert vcdcase.property_items([]) == []


def test_unicode_strings():
    """Unicode 版の CString (FF FE FF で始まる) も読める。"""
    name = "ユニコード.txt"
    wide = b"\xff\xfe\xff" + bytes([len(name)]) + name.encode("utf-16-le")
    ansi = sample.record("x.txt", size=5)
    data = sample.case_file([sample.drive("DISC", [ansi.replace(sample.cstring(b"x.txt"), wide)])])
    entry = vcdcase.parse_case(data).drives[0].entries[0]
    assert (entry.path, entry.size) == (name, 5)


def test_rejects_broken_files(tmp_path):
    data = sample.sample_case()
    with pytest.raises(CatalogError, match="ではありません"):
        vcdcase.parse_case(b"PK\x03\x04" + bytes(100))
    with pytest.raises(CatalogError, match="ではありません"):
        vcdcase.parse_case(b"")
    with pytest.raises(CatalogError, match="構造が合いません"):
        vcdcase.parse_case(data[:-5])
    with pytest.raises(CatalogError, match="構造が合いません"):
        vcdcase.parse_case(data + b"\x00")
    # 未確認の形式バージョンでも構造が同じなら読めるが、読めなかった場合はバージョンを伝える
    assert len(vcdcase.parse_case(sample.case_file([sample.drive("A", [])], schema=13)).drives) == 1
    with pytest.raises(CatalogError, match="形式バージョン 9 "):
        vcdcase.parse_case(sample.case_file([sample.drive("A", [])], schema=9)[:-3])

    empty = tmp_path / "empty.cas"
    empty.write_bytes(sample.case_file([]))
    catalog = Catalog.create(tmp_path / "c.vdmoku", cache_root=tmp_path / "cache")
    with pytest.raises(CatalogError, match="取り込めるドライブがありません"):
        import_vcdcase(catalog, empty)
    assert catalog.drives == []


def check_imported(catalog):
    """``sample_case`` を取り込んだカタログの中身を確かめる。"""
    backup, floppy = catalog.drives
    assert (backup["name"], backup["label"], backup["serial"], backup["filesystem"]) == ("BACKUP_2003", "BACKUP_2003", "1A2B-3C4D", "CDFS")
    assert (backup["root"], backup["source"], backup["drive_type"]) == (IMPORT_ROOT, SOURCE_VCDCASE, "cdrom")
    assert (backup["total_bytes"], backup["free_bytes"]) == (700_000_000, 0)
    assert (backup["file_count"], backup["dir_count"], backup["total_size"]) == (7, 2, 120 + 4096 + 900 + 50_000 + 3000 + 10 + 7000)
    assert backup["scanned_at"].startswith("2003-04-0") and backup["has_context"] and backup["comment"] == "2003/04/05"
    assert backup["category"] == "バックアップ" and "category" not in floppy
    assert (floppy["name"], floppy["label"], floppy["serial"], floppy["drive_type"]) == ("フロッピー 12", "フロッピー 12", "", "unknown")
    assert floppy["comment"] == "友人から借りたディスク" and not floppy["has_context"]
    assert floppy["scanned_at"] != backup["scanned_at"]  # コメントが日付でなければ .cas の更新日時

    with catalog.open_drive_db(backup["id"]) as db, ContextDB(catalog.context_source(backup["id"])) as context:
        assert db.root == IMPORT_ROOT and db.meta["source"] == SOURCE_VCDCASE
        assert [entry.name for entry in db.children(ROOT_ID)] == ["写真", "data.lzh", "memo.txt", "readme.txt", "setup.exe", "tv.avi"]
        photos = db.find_path("写真")
        assert (photos.is_dir, photos.size, photos.file_count, photos.dir_count) == (1, 50_900, 2, 1)
        sea = db.find_path("写真\\海.jpg")
        assert (sea.size, sea.mtime, sea.ctime, sea.attrs) == (50_000, sample.FILETIME_2003, None, sample.FILE)
        assert db.full_path(sea) == "?:\\写真\\海.jpg"

        # コメントはテキスト内容として入り、保存されていたバイト列も残る (文字コードを変えて読み直せる)
        readme = db.find_path("readme.txt")
        assert context.get_text(readme.id) == ("cp932", "はじめにお読みください。\r\n2 行目")
        assert context.get_meta(readme.id) == [("vcdcase", "crc32", "E2310CA8")]
        assert context.get_text(db.find_path("memo.txt").id)[1] == sample.LONG_COMMENT.decode("cp932")
        assert context.get_text(photos.id) == ("cp932", "2003 年の旅行")  # フォルダのコメント
        html = db.find_path("写真\\index.html")
        assert context.get_text(html.id) == ("utf-8", "写真の一覧")
        assert context.get_meta(html.id) == [
            ("vcdcase", "category", "旅行"),
            ("vcdcase", "property_type", "HTML"), ("vcdcase", "title", "写真の一覧"), ("vcdcase", "subject", "Photo index"),
            ("vcdcase", "keywords", "photo,sea"), ("vcdcase", "application", "SampleEditor 1.0"),
        ]  # fmt: skip
        assert context.get_meta(sea.id) == [("vcdcase", "category", "photo")]  # 分類だけのファイルも拡張コンテキストを持つ
        setup = db.find_path("setup.exe")
        assert ("vcdcase", "company", "サンプル社") in context.get_meta(setup.id)
        assert context.has_context(sea.id) and not context.has_context(db.find_path("写真\\空のフォルダ").id)

        # 中身を展開して登録されていた書庫は、書庫内リストを持つ 1 つのファイルになる (合計サイズにも書庫のサイズで入る)
        archive = db.find_path("data.lzh")
        assert (archive.is_dir, archive.size) == (0, 3000)
        assert context.get_meta(archive.id) == [("vcdcase", "crc32", "0BADF00D"), ("vcdcase", "inner_count", "3")]
        assert [(item.path, item.size, item.is_dir) for item in context.get_inner(archive.id)] == [
            ("doc", None, 1), ("doc/manual.txt", 1500, 0), ("tool.exe", 2500, 0),
        ]  # fmt: skip

        assert context.search_entry_ids(["旅行"]) == sorted([photos.id, html.id])  # フォルダのコメントと、分類
        assert context.search_entry_ids(["Sample", "Suite"]) == [setup.id]
        assert context.search_entry_ids(["manual"]) == [archive.id]
        assert ("vcdcase", "source", "CATV / MPEG1") in context.get_meta(db.find_path("tv.avi").id)
        assert context.summary() == {"vcdcase": 8}

    with catalog.open_drive_db(floppy["id"]) as db:
        assert [entry.name for entry in db.children(ROOT_ID)] == ["AUTOEXEC.BAT"]
    assert catalog.context_source(floppy["id"]) is None


def test_import_into_catalog(cas_path, tmp_path, monkeypatch):
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_dir))
    catalog = Catalog.create(tmp_path / "plain.vdmoku", cache_root=tmp_path / "cache")
    phases: list[tuple[str, int]] = []
    outcome = import_vcdcase(catalog, cas_path, progress=lambda phase, count: phases.append((phase, count)))
    assert phases == [("import_total", 2), ("import", 1), ("import", 2), ("save", 0)]
    assert [drive["name"] for drive in outcome.drives] == ["BACKUP_2003", "フロッピー 12"]
    assert (outcome.file_count, outcome.dir_count, outcome.context_count) == (8, 2, 8)
    assert os.listdir(temp_dir) == []  # 作業フォルダは片付ける

    reopened = Catalog.open(catalog.path, cache_root=tmp_path / "cache")
    check_imported(reopened)

    # もう一度取り込むと、別のドライブとして追加される (既存のドライブは変えない)
    import_vcdcase(reopened, cas_path)
    assert [drive["name"] for drive in reopened.drives] == ["BACKUP_2003", "フロッピー 12"] * 2
    assert len({drive["id"] for drive in reopened.drives}) == 4


def test_import_in_batches_and_cancel(cas_path, tmp_path, monkeypatch):
    from virtualdiskmokuroku import importer

    monkeypatch.setattr(importer, "_FLUSH_BYTES", 1)  # ドライブ 1 台ごとにカタログへ書き込む
    catalog = Catalog.create(tmp_path / "batch.vdmoku", cache_root=tmp_path / "cache")
    import_vcdcase(catalog, cas_path)
    check_imported(Catalog.open(catalog.path, cache_root=tmp_path / "cache"))

    # 2 台目の前でキャンセル: 書き込み済みの 1 台目は残り、カタログは壊れない
    catalog = Catalog.create(tmp_path / "cancel.vdmoku", cache_root=tmp_path / "cache")
    seen: list[str] = []
    with pytest.raises(ScanCancelled):
        import_vcdcase(catalog, cas_path, progress=lambda phase, _count: seen.append(phase), is_cancelled=lambda: "import" in seen)
    assert [drive["name"] for drive in Catalog.open(catalog.path, cache_root=tmp_path / "cache").drives] == ["BACKUP_2003"]

    # 書き込み前のキャンセルでは何も登録されない
    monkeypatch.setattr(importer, "_FLUSH_BYTES", 1 << 30)
    catalog = Catalog.create(tmp_path / "cancel2.vdmoku", cache_root=tmp_path / "cache")
    seen.clear()
    with pytest.raises(ScanCancelled):
        import_vcdcase(catalog, cas_path, progress=lambda phase, _count: seen.append(phase), is_cancelled=lambda: "import" in seen)
    assert catalog.drives == [] and Catalog.open(catalog.path, cache_root=tmp_path / "cache").drives == []


def test_import_into_encrypted_catalog_writes_no_plaintext(cas_path, tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    from test_encryption import FAST_KDF, PASSWORD, SQLITE_MAGIC, files_on_disk

    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_dir))
    cache = tmp_path / "cache"
    catalog = Catalog.create(tmp_path / "secret.vdmoku", PASSWORD, cache, encrypt=True, kdf=FAST_KDF)

    seen_files: list[str] = []
    import_vcdcase(
        catalog, cas_path, progress=lambda _phase, _count: seen_files.extend(files_on_disk(cache) + files_on_disk(temp_dir))
    )
    assert seen_files == [] and files_on_disk(temp_dir) == [] and files_on_disk(cache) == []
    assert SQLITE_MAGIC not in catalog.path.read_bytes()
    assert "BACKUP_2003".encode() not in catalog.path.read_bytes()
    catalog.close()

    reopened = Catalog.open(catalog.path, PASSWORD, cache)
    check_imported(reopened)
    reopened.close()
    assert files_on_disk(cache) == []

    # メモリの上限を超える場合だけセッション用の一時フォルダを使い、登録が終われば残さない
    small = Catalog.create(tmp_path / "small.vdmoku", PASSWORD, cache, encrypt=True, kdf=FAST_KDF)
    small.memory_limit = 1024
    spilled: set[str] = set()
    import_vcdcase(
        small, cas_path, progress=lambda _phase, _count: spilled.update(os.path.basename(path) for path in files_on_disk(cache))
    )
    assert any(name.startswith("files-") for name in spilled) and any(name.startswith("context-") for name in spilled)
    assert files_on_disk(temp_dir) == []
    small.release_sources()
    assert files_on_disk(cache) == []
    small.memory_limit = 64 * 1024 * 1024
    check_imported(small)
    small.close()


def test_cli_import(cas_path, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VIRTUALDISKMOKUROKU_CACHE", str(tmp_path / "cache"))
    target = tmp_path / "cli.vdmoku"
    assert cli.main(["import", str(cas_path), str(target)]) == 0
    output = capsys.readouterr().out
    assert "追加: BACKUP_2003" in output and "2 ドライブを取り込みました" in output
    assert cli.main(["find", str(target), "海"]) == 0
    assert "[BACKUP_2003] ?:\\写真\\海.jpg" in capsys.readouterr().out
    assert cli.main(["import", str(tmp_path / "missing.cas"), str(target)]) == 1
