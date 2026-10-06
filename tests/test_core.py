import os
import zipfile

import pytest

from virtualdiskmokuroku.core import scanner
from virtualdiskmokuroku.core.catalog import FILES_DB, Catalog
from virtualdiskmokuroku.core.drive_db import ROOT_ID, DriveDB, build_drive_db
from virtualdiskmokuroku.core.errors import CatalogError, PasswordError
from virtualdiskmokuroku.core.es_client import RawEntry, parse_export_csv
from virtualdiskmokuroku.core.ignore import DEFAULT_IGNORE, IgnoreRules


def make_tree(base):
    """テスト用ツリーを作り、{相対パス: サイズ} を返す。"""
    files = {
        "a.txt": 10,
        "Zeta.bin": 5,
        "docs\\readme.md": 100,
        "docs\\sub\\deep.txt": 7,
        "docs\\sub\\写真 100%.JPG": 3,
        "docs.old\\x.txt": 1,
        "$RECYCLE.BIN\\junk.dat": 999,
        "System Volume Information\\tracking.log": 50,
        "music\\song_a.mp3": 2000,
    }
    for rel, size in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    (base / "empty").mkdir()
    return files


# ---------------------------------------------------------------------------- ignore
def test_ignore_rules():
    rules = IgnoreRules(DEFAULT_IGNORE + ["*.tmp", "Thumbs.db", "Windows\\Temp\\"])
    assert rules.matches("$Recycle.Bin", True)
    assert not rules.matches("$RECYCLE.BIN", False)  # フォルダ専用ルールはファイルに一致しない
    assert rules.matches("a.TMP", False)
    assert rules.matches("thumbs.db", False)
    assert rules.matches("Temp", True, "windows\\temp")
    assert not rules.matches("Temp", True, "users\\temp")
    assert not IgnoreRules([])


# ---------------------------------------------------------------------------- es.exe CSV
def test_parse_export_csv(tmp_path):
    csv_path = tmp_path / "out.csv"
    csv_path.write_text(
        "Filename,Size,Date Modified,Date Created,Attributes\n"
        '"D:\\dir\\",6839496,134357452924816989,134357449092324976,16\n'
        '"D:\\dir\\名前, with comma.txt",70,134357451540635023,,32\n'
        '"D:\\dir\\unknown.bin",,18446744073709551615,,\n',
        encoding="utf-8",
    )
    entries = list(parse_export_csv(csv_path))
    assert entries[0] == RawEntry("D:\\dir", True, 6839496, 134357452924816989, 134357449092324976, 16)
    assert entries[1].path == "D:\\dir\\名前, with comma.txt"
    assert (entries[1].is_dir, entries[1].size, entries[1].ctime) == (False, 70, None)
    assert (entries[2].size, entries[2].mtime, entries[2].attrs) == (None, None, None)


# ---------------------------------------------------------------------------- drive DB
@pytest.fixture
def scanned(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    files = make_tree(tree)
    db_path = tmp_path / "files.db"
    result = scanner.scan_to_db(str(tree), db_path, source=scanner.SOURCE_WALK, ignore=IgnoreRules(DEFAULT_IGNORE))
    return tree, files, db_path, result


def test_build_aggregates_and_preorder(scanned):
    _tree, files, db_path, result = scanned
    kept = {rel: size for rel, size in files.items() if not rel.startswith(("$RECYCLE", "System Volume"))}
    assert result.stats.file_count == len(kept)
    assert result.stats.total_size == sum(kept.values())
    assert result.stats.dir_count == 5  # docs, docs\sub, docs.old, music, empty

    with DriveDB(db_path) as db:
        assert db.meta["file_count"] == len(kept)
        names = [entry.name for entry in db.children(ROOT_ID)]
        assert names == ["docs", "docs.old", "empty", "music", "a.txt", "Zeta.bin"]  # フォルダ優先・大文字小文字無視

        docs = db.find_path("DOCS")
        assert docs is not None and docs.is_dir
        assert (docs.size, docs.file_count, docs.dir_count) == (110, 3, 1)
        subtree = db.subtree(docs.id)
        assert {db.rel_path(entry) for entry in subtree} == {
            "docs\\readme.md", "docs\\sub", "docs\\sub\\deep.txt", "docs\\sub\\写真 100%.JPG",
        }  # fmt: skip
        assert all(docs.id < entry.id <= docs.last_desc_id for entry in subtree)
        # "docs.old" は "docs" のサブツリーに混ざらない
        assert db.find_path("docs.old\\x.txt").id > docs.last_desc_id

        empty = db.find_path("empty")
        assert (empty.size, empty.file_count, empty.dir_count, empty.last_desc_id) == (0, 0, 0, empty.id)


def test_filters(scanned):
    _tree, _files, db_path, _result = scanned
    with DriveDB(db_path) as db:
        assert [e.name for e in db.children(ROOT_ID, ["zeta"])] == ["Zeta.bin"]
        assert [e.name for e in db.subtree(ROOT_ID, ["100%"])] == ["写真 100%.JPG"]  # % はワイルドカード扱いしない
        assert db.subtree(ROOT_ID, ["song_a"])[0].name == "song_a.mp3"
        assert db.subtree(ROOT_ID, ["songXa"]) == []  # _ もワイルドカード扱いしない
        assert {e.name for e in db.subtree(ROOT_ID, ["d", "txt"])} == {"deep.txt"}  # AND 条件
        docs = db.find_path("docs")
        assert {e.name for e in db.subtree(docs.id, [".txt"])} == {"deep.txt"}
        hit = db.subtree(ROOT_ID, ["deep"])[0]
        assert db.full_path(hit).endswith("tree\\docs\\sub\\deep.txt")


def test_build_synthesizes_missing_parents_and_prunes_ignored(tmp_path):
    entries = [
        RawEntry("X:\\b\\c\\file.txt", False, 5, 1, 1, 32),  # 親フォルダの行が無い
        RawEntry("X:\\$RECYCLE.BIN\\S-1\\deleted.txt", False, 9, 1, 1, 32),  # 無視フォルダの行が無い
        RawEntry("X:\\a", True, 123456, 1, 1, 16),
        RawEntry("X:\\a\\one.txt", False, 1, 1, 1, 32),
        RawEntry("X:\\a\\one.txt", False, 1, 1, 1, 32),  # 重複
        RawEntry("Y:\\other.txt", False, 1, 1, 1, 32),  # ルート外
    ]
    db_path = tmp_path / "files.db"
    stats = build_drive_db(db_path, "X:\\", entries, ignore=IgnoreRules(DEFAULT_IGNORE))
    assert (stats.file_count, stats.dir_count, stats.total_size, stats.ignored_count) == (2, 3, 6, 1)
    with DriveDB(db_path) as db:
        assert db.root == "X:\\"
        assert [e.name for e in db.children(ROOT_ID)] == ["a", "b"]
        assert db.find_path("a").size == 1  # Everything が返すフォルダサイズではなく自前集計
        assert db.full_path(db.find_path("b\\c\\file.txt")) == "X:\\b\\c\\file.txt"


# ---------------------------------------------------------------------------- catalog
def test_catalog_roundtrip_backup_and_restore(tmp_path, scanned):
    tree, files, db_path, result = scanned
    cache = tmp_path / "cache"
    catalog_path = tmp_path / "test.vdmoku"
    catalog = Catalog.create(catalog_path, cache_root=cache)
    drive = catalog.put_drive(db_path, result, name="テスト")
    drive_id = drive["id"]
    assert catalog.find_matching_drives(result.volume, result.root) == [drive]

    # 再スキャンして更新 → 旧世代が 1 つ残る
    (tree / "new.txt").write_bytes(b"12345")
    db2 = tmp_path / "files2.db"
    result2 = scanner.scan_to_db(str(tree), db2, source=scanner.SOURCE_WALK, ignore=IgnoreRules(DEFAULT_IGNORE))
    catalog.put_drive(db2, result2, drive_id=drive_id)

    reopened = Catalog.open(catalog_path, cache_root=cache)
    drive = reopened.drive(drive_id)
    assert drive["name"] == "テスト"
    assert drive["file_count"] == result.stats.file_count + 1
    assert len(drive["backups"]) == 1
    stamp = drive["backups"][0]["stamp"]
    assert drive["backups"][0]["file_count"] == result.stats.file_count
    with zipfile.ZipFile(catalog_path) as archive:
        assert archive.testzip() is None
        assert f"drives/{drive_id}/backup/{stamp}/{FILES_DB}" in archive.namelist()
    with reopened.open_drive_db(drive_id) as db:
        assert db.find_path("new.txt") is not None
    with reopened.open_drive_db(drive_id, backup=stamp) as db:
        assert db.find_path("new.txt") is None

    # 3 回目の更新 → 世代数 1 なので最初の世代は消える
    db3 = tmp_path / "files3.db"
    result3 = scanner.scan_to_db(str(tree), db3, source=scanner.SOURCE_WALK)
    reopened.put_drive(db3, result3, drive_id=drive_id)
    drive = reopened.drive(drive_id)
    assert len(drive["backups"]) == 1 and drive["backups"][0]["file_count"] == result.stats.file_count + 1
    with zipfile.ZipFile(catalog_path) as archive:
        assert archive.testzip() is None
        assert len([name for name in archive.namelist() if name.endswith(FILES_DB)]) == 2

    # バックアップから復元 → 現行と入れ替わる
    current_count = drive["file_count"]
    reopened.restore_backup(drive_id, drive["backups"][0]["stamp"])
    drive = Catalog.open(catalog_path, cache_root=cache).drive(drive_id)
    assert drive["file_count"] == result.stats.file_count + 1
    assert drive["backups"][0]["file_count"] == current_count

    reopened.remove_drive(drive_id)
    with zipfile.ZipFile(catalog_path) as archive:
        assert archive.namelist() == ["manifest.json"]


def test_catalog_password_and_settings(tmp_path):
    path = tmp_path / "secret.vdmoku"
    catalog = Catalog.create(path, password="合言葉", cache_root=tmp_path / "cache")
    catalog.settings["backup_generations"] = 3
    catalog.settings["ignore_patterns"].append("*.tmp")
    catalog.save()

    assert Catalog.is_password_protected(path)
    with pytest.raises(PasswordError):
        Catalog.open(path)
    with pytest.raises(PasswordError):
        Catalog.open(path, "違う")
    reopened = Catalog.open(path, "合言葉")
    assert reopened.settings["backup_generations"] == 3
    assert "*.tmp" in reopened.settings["ignore_patterns"]
    reopened.set_password(None)
    assert not Catalog.is_password_protected(path)

    with pytest.raises(CatalogError):
        Catalog.create(path)


def test_update_without_backup_drops_stale_context(tmp_path, scanned):
    _tree, _files, db_path, result = scanned
    catalog_path = tmp_path / "test.vdmoku"
    catalog = Catalog.create(catalog_path, cache_root=tmp_path / "cache")
    catalog.settings["backup_generations"] = 0
    fake_context = tmp_path / "context.db"
    fake_context.write_bytes(b"context")
    drive = catalog.put_drive(db_path, result, context_db_path=fake_context)
    assert drive["has_context"] and catalog.extract_context_db(drive["id"]) is not None

    drive = catalog.put_drive(db_path, result, drive_id=drive["id"])
    assert not drive["has_context"] and drive["backups"] == []
    with zipfile.ZipFile(catalog_path) as archive:
        assert sorted(archive.namelist()) == ["drives/" + drive["id"] + "/" + FILES_DB, "manifest.json"]


def test_opens_catalog_created_under_old_name(tmp_path):
    import json

    path = tmp_path / "old.pmcat"
    Catalog.create(path, cache_root=tmp_path / "cache")
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    manifest["format"] = "pymediacatalogue"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
    catalog = Catalog.open(path, cache_root=tmp_path / "cache")
    catalog.save()
    assert Catalog.read_manifest(path)["catalog_id"] == manifest["catalog_id"]


def test_failed_update_keeps_original(tmp_path, scanned):
    _tree, _files, db_path, result = scanned
    catalog_path = tmp_path / "test.vdmoku"
    catalog = Catalog.create(catalog_path, cache_root=tmp_path / "cache")
    drive = catalog.put_drive(db_path, result)
    before = catalog_path.read_bytes()
    with pytest.raises(OSError):
        catalog.put_drive(tmp_path / "missing.db", result, drive_id=drive["id"])
    assert catalog_path.read_bytes() == before
    assert not os.path.exists(str(catalog_path) + ".tmp")
