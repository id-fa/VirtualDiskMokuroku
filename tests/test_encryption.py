"""暗号化カタログのテスト。"""

import json
import os
import threading
import zipfile

import pytest

pytest.importorskip("cryptography")

from virtualdiskmokuroku.core import scanner  # noqa: E402
from virtualdiskmokuroku.core.catalog import (  # noqa: E402
    CONTEXT_DB,
    FILES_DB,
    MANIFEST_ENC,
    MANIFEST_NAME,
    Catalog,
    cleanup_stale_sessions,
)
from virtualdiskmokuroku.core.drive_db import ROOT_ID, DriveDB  # noqa: E402
from virtualdiskmokuroku.core.errors import CatalogError, PasswordError  # noqa: E402
from virtualdiskmokuroku.core.ignore import DEFAULT_IGNORE, IgnoreRules  # noqa: E402

from test_core import make_tree  # noqa: E402

FAST_KDF = {"n": 1 << 10, "r": 8, "p": 1}
PASSWORD = "ひみつの合言葉"
SQLITE_MAGIC = b"SQLite format 3"


def files_on_disk(folder):
    return [os.path.join(base, name) for base, _dirs, names in os.walk(folder) for name in names]


def plaintext_files(folder):
    """フォルダ内にある平文の SQLite ファイル。"""
    found = []
    for path in files_on_disk(folder):
        with open(path, "rb") as f:
            if f.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC:
                found.append(path)
    return found


@pytest.fixture
def scanned(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    make_tree(tree)
    db_path = tmp_path / "files.db"
    result = scanner.scan_to_db(str(tree), db_path, source=scanner.SOURCE_WALK, ignore=IgnoreRules(DEFAULT_IGNORE))
    return tree, db_path, result


@pytest.fixture
def encrypted(tmp_path, scanned):
    _tree, db_path, result = scanned
    cache = tmp_path / "cache"
    catalog = Catalog.create(tmp_path / "secret.vdmoku", PASSWORD, cache, encrypt=True, kdf=FAST_KDF)
    drive = catalog.put_drive(db_path, result, name="秘密のドライブ")
    yield catalog, drive, cache
    catalog.close()


def test_encrypted_catalog_roundtrip(encrypted, tmp_path):
    catalog, drive, cache = encrypted
    assert catalog.encrypted

    # ファイルのどこにも平文は無い(ドライブ名・ファイル名・SQLite のヘッダ)
    raw = catalog.path.read_bytes()
    for needle in (SQLITE_MAGIC, "秘密のドライブ".encode("utf-8"), b"readme.md", b"deep.txt", drive["serial"].encode()):
        assert needle not in raw
    with zipfile.ZipFile(catalog.path) as archive:
        assert archive.testzip() is None
        public = json.loads(archive.read(MANIFEST_NAME))
        assert set(public) == {"format", "format_version", "catalog_id", "encryption"}
        assert public["format_version"] == 2  # 古い版のアプリは「新しい版で作成された」と表示する
        assert MANIFEST_ENC in archive.namelist()

    assert Catalog.is_password_protected(catalog.path) and Catalog.is_encrypted_file(catalog.path)
    with pytest.raises(PasswordError):
        Catalog.open(catalog.path, cache_root=cache)
    with pytest.raises(PasswordError):
        Catalog.open(catalog.path, "違うパスワード", cache)

    reopened = Catalog.open(catalog.path, PASSWORD, cache)
    try:
        assert reopened.encrypted and reopened.drive(drive["id"])["name"] == "秘密のドライブ"
        source = reopened.db_source(drive["id"])
        assert "vfs=memdb" in source and reopened.db_source(drive["id"]) == source  # 2 回目は同じものを返す
        with DriveDB(source) as db:
            assert [e.name for e in db.children(ROOT_ID)] == ["docs", "docs.old", "empty", "music", "a.txt", "Zeta.bin"]
            assert db.find_path("docs\\sub\\deep.txt") is not None

        # 別スレッド (検索ワーカー) の接続からも同じ内容を読める
        seen = {}

        def worker():
            with DriveDB(source) as other:
                seen["names"] = [e.name for e in other.subtree(ROOT_ID, ["deep"])]

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        assert seen["names"] == ["deep.txt"]

        with pytest.raises(CatalogError):
            reopened.extract_db(drive["id"])  # 暗号化カタログはディスクに展開しない
        assert reopened.context_source(drive["id"]) is None
        assert reopened.read_db_bytes(drive["id"]).startswith(SQLITE_MAGIC)
    finally:
        reopened.close()
    assert files_on_disk(cache) == []  # キャッシュにも一時フォルダにも何も残さない


def test_update_keeps_backup_and_password_change_does_not_reencrypt(encrypted, scanned, tmp_path):
    catalog, drive, cache = encrypted
    tree, _db_path, _result = scanned
    (tree / "new.txt").write_bytes(b"12345")
    db2 = tmp_path / "files2.db"
    result2 = scanner.scan_to_db(str(tree), db2, source=scanner.SOURCE_WALK, ignore=IgnoreRules(DEFAULT_IGNORE))
    catalog.put_drive(db2.read_bytes(), result2, drive_id=drive["id"])  # メモリ上で作った DB (バイト列) も渡せる
    stamp = catalog.drive(drive["id"])["backups"][0]["stamp"]
    with catalog.open_drive_db(drive["id"]) as db:
        assert db.find_path("new.txt") is not None
    with catalog.open_drive_db(drive["id"], backup=stamp) as db:  # 世代を回したメンバーも復号できる
        assert db.find_path("new.txt") is None
    catalog.release_sources()

    member = f"drives/{drive['id']}/{FILES_DB}"
    with zipfile.ZipFile(catalog.path) as archive:
        before = archive.read(member)
    catalog.set_password("新しいパスワード", FAST_KDF)
    with zipfile.ZipFile(catalog.path) as archive:
        assert archive.read(member) == before  # 中身は再暗号化されていない(鍵を包み直しただけ)
    with pytest.raises(PasswordError):
        Catalog.open(catalog.path, PASSWORD, cache)
    reopened = Catalog.open(catalog.path, "新しいパスワード", cache)
    with reopened.open_drive_db(drive["id"]) as db:
        assert db.find_path("new.txt") is not None
    reopened.close()
    with pytest.raises(CatalogError):
        catalog.set_password(None)

    catalog.restore_backup(drive["id"], stamp)
    with catalog.open_drive_db(drive["id"]) as db:
        assert db.find_path("new.txt") is None
    catalog.release_sources()
    catalog.remove_drive(drive["id"])
    with zipfile.ZipFile(catalog.path) as archive:
        assert sorted(archive.namelist()) == [MANIFEST_ENC, MANIFEST_NAME]


def test_tampering_and_member_swap_are_detected(encrypted, scanned, tmp_path):
    catalog, drive, _cache = encrypted
    _tree, db_path, result = scanned
    other = catalog.put_drive(db_path, result, name="もう 1 台")

    def rewrite_members(change):
        with zipfile.ZipFile(catalog.path) as archive:
            members = {info.filename: archive.read(info) for info in archive.infolist()}
        change(members)
        with zipfile.ZipFile(catalog.path, "w", zipfile.ZIP_STORED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)

    first = f"drives/{drive['id']}/{FILES_DB}"
    second = f"drives/{other['id']}/{FILES_DB}"

    # 別のドライブの DB と入れ替える (どちらも正しく暗号化されているが、置き場所が違う)
    def swap(members):
        members[first], members[second] = members[second], members[first]

    rewrite_members(swap)
    with pytest.raises(CatalogError):
        catalog.db_source(drive["id"])
    rewrite_members(swap)
    with catalog.open_drive_db(drive["id"]) as db:
        assert db.find_path("a.txt") is not None
    catalog.release_sources()

    # 1 バイト書き換える
    def flip(members):
        data = bytearray(members[first])
        data[len(data) // 2] ^= 0x01
        members[first] = bytes(data)

    rewrite_members(flip)
    with pytest.raises(CatalogError):
        catalog.db_source(drive["id"])
    with pytest.raises(CatalogError):
        catalog.read_db_bytes(drive["id"])

    # manifest を別のカタログのものに差し替える
    stranger = Catalog.create(tmp_path / "other.vdmoku", PASSWORD, encrypt=True, kdf=FAST_KDF)
    with zipfile.ZipFile(stranger.path) as archive:
        foreign = archive.read(MANIFEST_ENC)
    rewrite_members(lambda members: members.__setitem__(MANIFEST_ENC, foreign))
    with pytest.raises(CatalogError):
        Catalog.open(catalog.path, PASSWORD)


def test_convert_plain_to_encrypted_and_back(tmp_path, scanned):
    _tree, db_path, result = scanned
    cache = tmp_path / "cache"
    catalog = Catalog.create(tmp_path / "plain.vdmoku", "照合だけ", cache)
    fake_context = tmp_path / "context.db"
    fake_context.write_bytes(SQLITE_MAGIC + b"\0" + b"context" * 100)
    drive = catalog.put_drive(db_path, result, name="変換", context_db_path=fake_context)
    catalog.extract_db(drive["id"])
    assert plaintext_files(cache)  # 通常のカタログはキャッシュに平文を展開する

    with pytest.raises(PasswordError):
        catalog.encrypt("", FAST_KDF)
    catalog.encrypt(PASSWORD, FAST_KDF)
    assert catalog.encrypted and catalog.settings["password"] is None
    assert plaintext_files(cache) == []  # 暗号化したら平文のキャッシュは消す
    raw = catalog.path.read_bytes()
    assert SQLITE_MAGIC not in raw and "変換".encode() not in raw
    with pytest.raises(CatalogError):
        catalog.encrypt(PASSWORD, FAST_KDF)

    reopened = Catalog.open(catalog.path, PASSWORD, cache)
    with reopened.open_drive_db(drive["id"]) as db:
        assert db.find_path("docs\\readme.md") is not None
    assert reopened.read_db_bytes(drive["id"], CONTEXT_DB) == fake_context.read_bytes()

    # 復号して別ファイルへ書き出す(元は暗号化されたまま)
    exported = tmp_path / "exported.vdmoku"
    reopened.export_decrypted(exported)
    plain = Catalog.open(exported, cache_root=cache)
    assert not plain.encrypted and plain.drive(drive["id"])["name"] == "変換"
    with plain.open_drive_db(drive["id"]) as db:
        assert db.find_path("docs\\readme.md") is not None
    assert Catalog.is_encrypted_file(catalog.path)
    with pytest.raises(CatalogError):
        reopened.export_decrypted(reopened.path)

    # 暗号化を解除
    reopened.decrypt()
    assert not reopened.encrypted and not Catalog.is_password_protected(reopened.path)
    with zipfile.ZipFile(reopened.path) as archive:
        assert archive.testzip() is None and MANIFEST_ENC not in archive.namelist()
        assert archive.read(f"drives/{drive['id']}/{FILES_DB}").startswith(SQLITE_MAGIC)
    with pytest.raises(CatalogError):
        reopened.decrypt()
    assert Catalog.open(reopened.path, cache_root=cache).drive(drive["id"])["has_context"]


def test_scan_pipeline_in_memory_and_spill(tmp_path, monkeypatch):
    """暗号化カタログへのスキャンは一時ファイルを作らない。上限を超える場合だけ一時フォルダを使い、後で消す。"""
    import tempfile

    from virtualdiskmokuroku.pipeline import scan_into_catalog

    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_dir))
    cache = tmp_path / "cache"
    tree = tmp_path / "tree"
    tree.mkdir()
    make_tree(tree)
    for number in range(300):
        (tree / "docs" / f"note_{number:03}.txt").write_text(f"memo {number} " * 20, encoding="utf-8")

    catalog = Catalog.create(tmp_path / "secret.vdmoku", PASSWORD, cache, encrypt=True, kdf=FAST_KDF)
    catalog.settings["context"] = {"text": {"enabled": True}}
    catalog.save()

    # 上限内: すべてメモリ上で完結する
    seen_files: list[str] = []
    outcome = scan_into_catalog(
        catalog, str(tree), name="memory", source=scanner.SOURCE_WALK,
        progress=lambda _phase, _count: seen_files.extend(files_on_disk(cache) + files_on_disk(temp_dir)),
    )  # fmt: skip
    assert seen_files == [] and files_on_disk(temp_dir) == [] and files_on_disk(cache) == []
    assert outcome.context_stats.processed == 304 and outcome.result.database is None
    first = outcome.drive["id"]

    # 上限超過: 構築中だけセッション用の一時フォルダを使い、登録が終われば残さない
    catalog.memory_limit = 16 * 1024
    spilled: set[str] = set()
    outcome = scan_into_catalog(
        catalog, str(tree), drive_id=first, source=scanner.SOURCE_WALK,
        progress=lambda _phase, _count: spilled.update(os.path.basename(path) for path in files_on_disk(cache)),
    )  # fmt: skip
    assert any(name.startswith("context-") for name in spilled)  # 実際に退避が起きた
    assert outcome.context_stats.reused == 304  # 前回の結果 (こちらも上限超過で一時フォルダに復号) を引き継いだ
    assert files_on_disk(temp_dir) == []
    catalog.release_sources()
    assert files_on_disk(cache) == []

    reopened = Catalog.open(catalog.path, PASSWORD, cache)
    reopened.memory_limit = 64 * 1024 * 1024
    with reopened.open_drive_db(first) as db:
        assert db.meta["file_count"] == outcome.result.stats.file_count
        assert db.find_path("docs\\note_299.txt") is not None
    assert len(reopened.drive(first)["backups"]) == 1
    reopened.close()
    assert SQLITE_MAGIC not in catalog.path.read_bytes()


def test_sqlite_temp_files_are_kept_in_memory(encrypted, tmp_path):
    """SQLite が並べ替え用に %TEMP% へ一時ファイル (平文) を作らないよう、一時領域をメモリに固定している。"""
    import sqlite3

    from virtualdiskmokuroku.core.workdb import WorkDb, open_shared_memory_db

    memory_only = 2  # PRAGMA temp_store の値

    def temp_store(conn):
        return conn.execute("PRAGMA temp_store").fetchone()[0]

    catalog, drive, _cache = encrypted
    with catalog.open_drive_db(drive["id"]) as db:
        assert temp_store(db._conn) == memory_only

    work = WorkDb.in_memory(1024, lambda: tmp_path, "work")
    assert temp_store(work.conn) == memory_only
    work.conn.execute("CREATE TABLE t(x)")
    work.conn.executemany("INSERT INTO t VALUES (?)", [("x" * 500,) for _ in range(100)])
    assert work.checkpoint() and work.on_disk
    assert temp_store(work.conn) == memory_only  # 一時ファイルへ退避した後も変わらない
    work.discard()

    plain = sqlite3.connect(":memory:")
    plain.execute("CREATE TABLE t(x)")
    uri, owner = open_shared_memory_db(plain.serialize())
    assert temp_store(owner) == memory_only
    owner.close()
    plain.close()


def test_large_database_uses_session_folder_and_is_removed(encrypted):
    catalog, drive, cache = encrypted
    catalog.memory_limit = 1024  # どの DB も「大きすぎる」扱いにする
    source = catalog.db_source(drive["id"])
    assert "vfs=memdb" not in source
    session = catalog.session_dir()
    assert session.parent == cache and len(plaintext_files(session)) == 1
    with DriveDB(source) as db:
        assert db.find_path("a.txt") is not None
    catalog.release_sources()
    assert not session.exists() and files_on_disk(cache) == []

    # 異常終了で残った一時フォルダは、次回起動時に片付ける(動いているプロセスのものは残す)
    stale = cache / "session-999999-deadbeef"
    stale.mkdir()
    (stale / "left.db").write_bytes(SQLITE_MAGIC)
    mine = cache / f"session-{os.getpid()}-00000000"
    mine.mkdir()
    cleanup_stale_sessions(cache)
    assert not stale.exists() and mine.exists()


def test_copy_drives_between_encrypted_and_plain_catalogs(encrypted, scanned, tmp_path):
    catalog, drive, cache = encrypted
    _tree, db_path, result = scanned
    fake_context = tmp_path / "context.db"
    fake_context.write_bytes(b"context data")
    other = catalog.put_drive(db_path, result, name="コンテキスト付き", context_db_path=fake_context)
    catalog.set_drive_group(other["id"], "秘密")

    # 暗号化 → 通常: 復号して写す (平文の一時ファイルは作らない)
    plain = Catalog.create(tmp_path / "plain.vdmoku", cache_root=cache)
    copied = catalog.copy_drives_to(plain, [drive["id"], other["id"]])
    assert [item.get("group") for item in copied] == [None, "秘密"]
    assert files_on_disk(cache) == []  # 復号は直接コピー先へ書くので、平文の一時ファイルは作らない
    plain = Catalog.open(plain.path, cache_root=cache)
    with plain.open_drive_db(drive["id"]) as db:
        assert db.find_path("a.txt") is not None
    assert plain.extract_context_db(other["id"]).read_bytes() == b"context data"
    with zipfile.ZipFile(plain.path) as archive:
        assert archive.testzip() is None
        assert archive.read(f"drives/{drive['id']}/{FILES_DB}").startswith(SQLITE_MAGIC)
    assert files_on_disk(cache / catalog.catalog_id) == []  # 暗号化カタログ側のキャッシュに平文は無い

    # 通常 → 暗号化、暗号化 → 暗号化 (別の鍵): どちらもコピー先の鍵で暗号化される
    for source in (plain, catalog):
        target = Catalog.create(tmp_path / f"target-{source.encrypted}.vdmoku", "別の合言葉", cache, encrypt=True, kdf=FAST_KDF)
        source.copy_drives_to(target, [other["id"]])
        raw = target.path.read_bytes()
        assert SQLITE_MAGIC not in raw and b"context data" not in raw and "コンテキスト付き".encode() not in raw
        reopened = Catalog.open(target.path, "別の合言葉", cache)
        assert reopened.drive(other["id"])["has_context"]
        with reopened.open_drive_db(other["id"]) as db:
            assert db.find_path("docs\\readme.md") is not None
        assert reopened.read_db_bytes(other["id"], CONTEXT_DB) == b"context data"
        reopened.close()
    catalog.release_sources()

    def plaintext_outside_plain_cache():
        # 通常のカタログ plain.vdmoku のキャッシュ (展開した DB) 以外に平文が無いこと
        return [path for path in plaintext_files(cache) if str(plain.cache_dir) not in path]

    assert plaintext_outside_plain_cache() == []

    # 大きな DB 扱い (メモリ上限超過) ではセッション用フォルダに復号するが、書き終えたら消す
    catalog.memory_limit = 16
    target = Catalog.create(tmp_path / "big.vdmoku", "別の合言葉", cache, encrypt=True, kdf=FAST_KDF)
    catalog.copy_drives_to(target, [drive["id"]])
    assert plaintext_outside_plain_cache() == []
    with Catalog.open(target.path, "別の合言葉", cache).open_drive_db(drive["id"]) as db:
        assert db.find_path("a.txt") is not None
    catalog.release_sources()

    # 拡張コンテキストだけの削除と一括削除
    catalog.remove_context([other["id"]])
    assert not catalog.drive(other["id"])["has_context"]
    with zipfile.ZipFile(catalog.path) as archive:
        assert f"drives/{other['id']}/{CONTEXT_DB}" not in archive.namelist()
    catalog.remove_drives([drive["id"], other["id"]])
    with zipfile.ZipFile(catalog.path) as archive:
        assert sorted(archive.namelist()) == [MANIFEST_ENC, MANIFEST_NAME]
