"""メディアステンシルの記録ファイル (.fsr) のインポートのテスト。"""

import os
import struct
import tempfile
import zlib
from pathlib import Path

import pytest

from virtualdiskmokuroku import cli
from virtualdiskmokuroku.context.context_db import ContextDB
from virtualdiskmokuroku.core import stencil
from virtualdiskmokuroku.core.catalog import Catalog
from virtualdiskmokuroku.core.drive_db import ROOT_ID
from virtualdiskmokuroku.core.errors import CatalogError, ScanCancelled
from virtualdiskmokuroku.importer import IMPORT_ROOT, SOURCE_STENCIL, import_catalog

import stencil_sample as sample

REAL_SAMPLE = Path(__file__).with_name("data") / "stencil_sample.fsr"  # メディアステンシル 3.20 で記録した実ファイル


@pytest.fixture
def fsr_path(tmp_path):
    path = tmp_path / "sample.fsr"
    path.write_bytes(sample.sample_record())
    return path


def png_pixels(png: bytes) -> tuple[int, int, list[list[tuple[int, int, int, int]]]]:
    """PNG (RGBA、フィルタなし) を読んで (幅, 高さ, 画素) にする。"""
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    pos, chunks = 8, {}
    while pos < len(png):
        (length,) = struct.unpack_from(">I", png, pos)
        kind = png[pos + 4 : pos + 8]
        chunks[kind] = chunks.get(kind, b"") + png[pos + 8 : pos + 8 + length]
        pos += 12 + length
    width, height, depth, color_type = struct.unpack_from(">IIBB", chunks[b"IHDR"])
    assert (depth, color_type) == (8, 6)
    raw = zlib.decompress(chunks[b"IDAT"])
    stride = 1 + width * 4
    rows = []
    for y in range(height):
        line = raw[y * stride : (y + 1) * stride]
        assert line[0] == 0
        rows.append([tuple(line[1 + x * 4 : 5 + x * 4]) for x in range(width)])
    return width, height, rows


def test_parse_record():
    record = stencil.parse_stencil(sample.sample_record())
    assert record.version == "1.00"
    photos, documents = record.roots

    assert (photos.name, photos.label, photos.folder, photos.title) == ("", "PHOTOS_2021", "", "PHOTOS_2021")
    assert (photos.serial, photos.drive_type, photos.used_bytes, photos.recorded_at) == ("1A2B-3C4D", "cdrom", 700_000_000, sample.UNIX_2026)
    assert (photos.folder_count, photos.file_count, photos.comment, len(photos.icons)) == (2, 5, "旅行の写真\r\n2 行目", 2)
    entries = {entry.path: entry for entry in photos.entries}
    # フォルダリストの番号をたどって、ルートからの相対パスにする (フォルダの中身はその直後に並ぶ)
    assert list(entries) == ["readme.txt", "setup.exe", "写真", "data.lzh", "空のフォルダ", "写真\\海.jpg", "写真\\index.html"]
    readme = entries["readme.txt"]
    assert (readme.is_dir, readme.size, readme.mtime, readme.attrs, readme.icon, readme.inner) == (False, 120, sample.FILETIME_2021, sample.FILE, 0, [])
    assert entries["setup.exe"].size == (1 << 32) + 16 and entries["setup.exe"].icon == 2
    assert entries["写真"].is_dir and entries["写真"].size is None and entries["写真"].attrs == sample.DIRECTORY
    assert entries["写真\\海.jpg"].mtime is None and entries["写真\\海.jpg"].icon == 1  # 日時 0 は「不明」
    assert entries["空のフォルダ"].is_dir and not entries["空のフォルダ"].has_context
    # 中身を記録した書庫は、書庫内リストを持つ 1 つのファイルになる (属性の書庫ビットは外す)
    lzh = entries["data.lzh"]
    assert (lzh.is_dir, lzh.size, lzh.attrs, lzh.has_context) == (False, 3000, sample.FILE, True)
    assert lzh.inner == [
        ("doc/manual.txt", 1500, sample.FILETIME_2021, False),
        ("tool.exe", 2500, sample.UNIX_2026 * 10_000_000 + 116444736000000000, False),
        ("doc", None, sample.FILETIME_2021, True),
    ]

    # フォルダ単位の記録: ラベルは "ラベル::\フォルダ"。「名前の変更」で付けた表示名が入っている
    assert (documents.name, documents.label, documents.folder) == ("仕事の資料", "WINDOWS", "\\Users\\太郎\\docs")
    assert documents.title == "仕事の資料"
    assert (documents.serial, documents.drive_type, documents.used_bytes) == ("D59B-51E0", "fixed", (109 << 32) + 582_344_704)
    assert (documents.recorded_at, documents.comment, documents.icons) == (0, "", [])
    assert [(entry.path, entry.is_dir) for entry in documents.entries] == [("memo.txt", False), ("下書き", True)]


def test_ansi_strings_and_trailing_data():
    record = stencil.parse_stencil(sample.sample_record(wide=False, trailer=b"\0" * 8))
    assert [root.title for root in record.roots] == ["PHOTOS_2021", "仕事の資料"]
    assert [entry.path for entry in record.roots[0].entries][:3] == ["readme.txt", "setup.exe", "写真"]


def test_icon_png():
    width, height, rows = png_pixels(stencil.icon_png(sample.icon_blob(6)))
    assert (width, height) == (32, 32)
    assert rows[0][0] == (0, 128, 128, 0)  # 左上だけ透明
    assert rows[0][1] == rows[31][31] == (0, 128, 128, 255)
    width, height, rows = png_pixels(stencil.icon_png(sample.icon_blob(9, transparent_origin=False), 16))
    assert (width, height) == (16, 16) and rows[0][0] == (255, 0, 0, 255)
    assert stencil.icon_png(b"\0" * 10) is None and stencil.icon_png(sample.icon_blob(1), 48) is None


def test_rejects_broken_files():
    with pytest.raises(CatalogError, match="ではありません"):
        stencil.parse_stencil(b"\xff\xff\x0c\x00\x07\x00CCDCase" + bytes(40))
    with pytest.raises(CatalogError, match="ではありません"):
        stencil.parse_stencil(sample.MAGIC)
    data = sample.sample_record()
    with pytest.raises(CatalogError, match=r"位置 0x[0-9A-F]+ 付近"):
        stencil.parse_stencil(data[: len(data) // 2])
    with pytest.raises(CatalogError, match=r"形式バージョン 2\.00 には対応していません"):
        stencil.parse_stencil(sample.sample_record(magic=b"File Struct Record Version 2.00")[:200])
    # フォルダリストの番号が範囲外
    broken = sample.record_file([sample.root("X", [sample.folder_list("\\", [sample.entry("a", sample.DIRECTORY, link=5)])])])
    with pytest.raises(CatalogError, match="構造が合いません"):
        stencil.parse_stencil(broken)
    # クラスの印が想定と違う
    with pytest.raises(CatalogError, match="構造が合いません"):
        stencil.parse_stencil(data.replace(b"CRootEntry", b"CRootEntrz"))


def test_real_sample_recorded_by_media_stencil():
    """メディアステンシル 3.20 で実際に記録したファイル (アイコン・CAB・LZH 入り、コメント付き)。"""
    record = stencil.read_stencil(REAL_SAMPLE)
    (root,) = record.roots
    assert (root.label, root.serial, root.drive_type, root.name, root.folder) == ("WINDOWS", "D59B-51E0", "fixed", "", "")
    assert (root.folder_count, root.file_count, len(root.icons), root.comment) == (2, 6, 1, "テストのコメント\r\n2 行目")
    entries = {entry.path: entry for entry in root.entries}
    assert list(entries) == [
        "readme.txt", "sample.cab", "sub", "sub\\deep", "sub\\icon.ico", "sub\\sample.lzh", "sub\\tool.exe", "sub\\deep\\d.txt"
    ]  # fmt: skip
    assert (entries["readme.txt"].size, entries["sub\\tool.exe"].size, entries["sub"].is_dir) == (11, 69632, True)
    assert [(path, size) for path, size, _mtime, _is_dir in entries["sample.cab"].inner] == [("one.txt", 14), ("two.bin", 3000), ("dir/three.txt", 14)]
    assert [(path, size) for path, size, _mtime, _is_dir in entries["sub\\sample.lzh"].inner] == [
        ("inlzh.txt", 19), ("docs/manual.txt", 700), ("docs/img/pic.bin", 1024)
    ]  # fmt: skip
    assert entries["sub\\icon.ico"].icon == 1 and entries["sub\\tool.exe"].icon == 0
    width, height, rows = png_pixels(stencil.icon_png(root.icons[0]))
    assert (width, height) == (32, 32) and any(pixel[3] == 255 for row in rows for pixel in row)


def check_imported(catalog):
    """``sample_record`` を取り込んだカタログの中身を確かめる。"""
    photos, documents = catalog.drives
    assert (photos["name"], photos["label"], photos["serial"], photos["drive_type"]) == ("PHOTOS_2021", "PHOTOS_2021", "1A2B-3C4D", "cdrom")
    assert (photos["root"], photos["source"], photos["has_context"]) == (IMPORT_ROOT, SOURCE_STENCIL, True)
    assert photos["scanned_at"] == "2026-10-13T11:57:33+00:00" and photos["comment"] == "旅行の写真\r\n2 行目"
    assert (photos["file_count"], photos["dir_count"], photos["total_size"]) == (5, 2, 120 + (1 << 32) + 16 + 3000 + 50_000 + 900)
    assert "total_bytes" not in photos or photos["total_bytes"] is None
    # フォルダ単位の記録は "?:\フォルダ" をルートにし、「名前の変更」で付けた名前で登録される
    assert (documents["name"], documents["label"], documents["root"]) == ("仕事の資料", "WINDOWS", "?:\\Users\\太郎\\docs")
    assert (documents["drive_type"], documents["has_context"]) == ("fixed", False) and "comment" not in documents
    assert documents["scanned_at"] != photos["scanned_at"]  # 記録日時が無ければ .fsr の更新日時

    with catalog.open_drive_db(photos["id"]) as db, ContextDB(catalog.context_source(photos["id"])) as context:
        assert db.root == IMPORT_ROOT and db.meta["source"] == SOURCE_STENCIL and db.meta["stencil_used_bytes"] == 700_000_000
        assert [entry.name for entry in db.children(ROOT_ID)] == ["写真", "空のフォルダ", "data.lzh", "readme.txt", "setup.exe"]
        sea = db.find_path("写真\\海.jpg")
        assert (sea.size, sea.mtime, sea.ctime, sea.attrs) == (50_000, None, None, sample.FILE)
        assert db.full_path(sea) == "?:\\写真\\海.jpg"
        assert db.find_path("setup.exe").size == (1 << 32) + 16
        assert db.find_path("空のフォルダ").is_dir

        # 記録されていたアイコンはサムネイルになる
        width, height, png = context.get_thumb(sea.id)
        assert (width, height) == (32, 32) and png_pixels(png)[2][1][1] == (0, 128, 128, 255)
        assert png_pixels(context.get_thumb(db.find_path("setup.exe").id)[2])[2][0][0] == (255, 0, 0, 255)
        assert context.get_thumb(db.find_path("readme.txt").id) is None
        # 書庫の中身
        archive = db.find_path("data.lzh")
        assert (archive.is_dir, archive.size) == (0, 3000)
        assert context.get_meta(archive.id) == [("stencil", "inner_count", "3")]
        assert [(item.path, item.size, item.is_dir) for item in context.get_inner(archive.id)] == [
            ("doc/manual.txt", 1500, 0), ("tool.exe", 2500, 0), ("doc", None, 1),
        ]  # fmt: skip
        assert context.search_entry_ids(["manual"]) == [archive.id]
        assert context.has_context(sea.id) and not context.has_context(db.find_path("写真\\index.html").id)
        assert context.summary() == {"stencil": 3} and context.meta["kinds"] == ["stencil"]

    with catalog.open_drive_db(documents["id"]) as db:
        assert db.root == "?:\\Users\\太郎\\docs"
        assert [(entry.name, entry.is_dir) for entry in db.children(ROOT_ID)] == [("下書き", 1), ("memo.txt", 0)]
        assert db.full_path(db.find_path("memo.txt")) == "?:\\Users\\太郎\\docs\\memo.txt"
    assert catalog.context_source(documents["id"]) is None


def test_import_into_catalog(fsr_path, tmp_path, monkeypatch):
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_dir))
    catalog = Catalog.create(tmp_path / "plain.vdmoku", cache_root=tmp_path / "cache")
    phases: list[tuple[str, int]] = []
    outcome = import_catalog(catalog, fsr_path, progress=lambda phase, count: phases.append((phase, count)))
    assert phases == [
        ("parse", 12), ("import_total", 2),
        ("drive:PHOTOS_2021", 7), ("read", 7), ("build", 7), ("context", 3), ("import", 1),
        ("drive:仕事の資料", 2), ("read", 2), ("build", 2), ("import", 2),
        ("save", 0),
    ]  # fmt: skip
    assert [drive["name"] for drive in outcome.drives] == ["PHOTOS_2021", "仕事の資料"]
    assert (outcome.file_count, outcome.dir_count, outcome.context_count) == (6, 3, 3)
    assert os.listdir(temp_dir) == []  # 作業フォルダは片付ける
    check_imported(Catalog.open(catalog.path, cache_root=tmp_path / "cache"))

    # 実ファイルも同じ入口で取り込める (拡張子ではなく中身で形式を判定する)
    copied = tmp_path / "real.bin"
    copied.write_bytes(REAL_SAMPLE.read_bytes())
    outcome = import_catalog(catalog, copied)
    assert (outcome.file_count, outcome.dir_count, outcome.context_count) == (6, 2, 3)
    assert [drive["name"] for drive in outcome.drives] == ["WINDOWS"] and outcome.drives[0]["comment"] == "テストのコメント\r\n2 行目"

    # 中身がメディアステンシルの形式でない .fsr
    bogus = tmp_path / "bogus.fsr"
    bogus.write_bytes(b"not a record")
    with pytest.raises(CatalogError, match="ではありません"):
        import_catalog(catalog, bogus)
    empty = tmp_path / "empty.fsr"
    empty.write_bytes(sample.record_file([]))
    with pytest.raises(CatalogError, match="取り込めるドライブがありません"):
        import_catalog(catalog, empty)
    assert [drive["name"] for drive in catalog.drives] == ["PHOTOS_2021", "仕事の資料", "WINDOWS"]


def test_cancel_and_encrypted_catalog(fsr_path, tmp_path):
    catalog = Catalog.create(tmp_path / "cancel.vdmoku", cache_root=tmp_path / "cache")
    seen: list[str] = []
    with pytest.raises(ScanCancelled):
        import_catalog(catalog, fsr_path, progress=lambda phase, _count: seen.append(phase), is_cancelled=lambda: "import" in seen)
    assert catalog.drives == []

    pytest.importorskip("cryptography")
    from test_encryption import FAST_KDF, PASSWORD, SQLITE_MAGIC, files_on_disk

    cache = tmp_path / "secure_cache"
    secret = Catalog.create(tmp_path / "secret.vdmoku", PASSWORD, cache, encrypt=True, kdf=FAST_KDF)
    seen_files: list[str] = []
    import_catalog(secret, fsr_path, progress=lambda _phase, _count: seen_files.extend(files_on_disk(cache)))
    assert seen_files == [] and files_on_disk(cache) == []
    assert SQLITE_MAGIC not in secret.path.read_bytes() and b"PHOTOS_2021" not in secret.path.read_bytes()
    secret.close()
    reopened = Catalog.open(secret.path, PASSWORD, cache)
    check_imported(reopened)
    reopened.close()


def test_cli_import(fsr_path, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VIRTUALDISKMOKUROKU_CACHE", str(tmp_path / "cache"))
    target = tmp_path / "cli.vdmoku"
    assert cli.main(["import", str(fsr_path), str(target)]) == 0
    output = capsys.readouterr().out
    assert "追加: PHOTOS_2021" in output and "追加: 仕事の資料" in output and "2 ドライブを取り込みました" in output
    assert cli.main(["find", str(target), "海"]) == 0
    assert "[PHOTOS_2021] ?:\\写真\\海.jpg" in capsys.readouterr().out
