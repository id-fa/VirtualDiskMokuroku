import io
import os
import shutil
import tarfile
import wave
import zipfile

import pytest

from virtualdiskmokuroku.context import EXTRACTORS, KEY_LABELS, default_context_settings
from virtualdiskmokuroku.context.archive import ArchiveExtractor, decode_zip_name
from virtualdiskmokuroku.context.audio import AudioExtractor
from virtualdiskmokuroku.context.base import EntryWriter
from virtualdiskmokuroku.context.context_db import ContextDB, redecode_text
from virtualdiskmokuroku.context.iso import IsoExtractor
from virtualdiskmokuroku.context.office import OfficeExtractor
from virtualdiskmokuroku.context.runner import build_context_db
from virtualdiskmokuroku.context.text import TextExtractor, detect_and_decode
from virtualdiskmokuroku.core import scanner
from virtualdiskmokuroku.core.drive_db import DriveDB

Image = pytest.importorskip("PIL.Image")

JAPANESE = "これは日本語のテキストです。文字コードの判定を確認します。\r\n二行目もあります。\r\n"

CORE_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
 <dc:title>四半期レポート</dc:title><dc:creator>山田太郎</dc:creator>
 <cp:lastModifiedBy>鈴木</cp:lastModifiedBy><cp:keywords>売上 予算</cp:keywords>
 <dcterms:created xsi:type="dcterms:W3CDTF">2024-01-02T03:04:05Z</dcterms:created>
 <dcterms:modified xsi:type="dcterms:W3CDTF">2024-02-03T04:05:06Z</dcterms:modified>
</cp:coreProperties>"""
APP_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">
 <Application>Microsoft Office Word</Application><Pages>12</Pages><Company>Example Corp</Company>
</Properties>"""


# ---------------------------------------------------------------------------- テストデータ生成
def make_jpeg(path, with_exif=True):
    image = Image.new("RGB", (400, 300), (200, 30, 30))
    if not with_exif:
        image.save(path, "JPEG")
        return
    from PIL.TiffImagePlugin import IFDRational

    exif = Image.Exif()
    exif[271] = "TestMake"
    exif[272] = "TestModel X100"
    exif[274] = 6  # 右に 90 度回転して表示
    details = exif.get_ifd(0x8769)
    details[36867] = "2024:01:02 03:04:05"
    details[33434] = IFDRational(1, 250)
    details[33437] = IFDRational(28, 10)
    details[34855] = 400
    details[37386] = IFDRational(50, 1)
    details[42036] = "Test Lens 50mm"
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (IFDRational(35), IFDRational(30), IFDRational(0))
    gps[3] = "E"
    gps[4] = (IFDRational(139), IFDRational(45), IFDRational(0))
    image.save(path, "JPEG", exif=exif)


def make_docx(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("docProps/core.xml", CORE_XML)
        archive.writestr("docProps/app.xml", APP_XML)


def make_zip_with_cp932_name(path):
    """UTF-8 フラグ無しで cp932 のファイル名を持つ ZIP(古い Windows 製アーカイバ相当)。"""
    placeholder = b"AAAAAA.txt"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(placeholder.decode(), "content")
        archive.writestr("folder/plain.txt", "12345")
    actual = "日本語.txt".encode("cp932")
    assert len(actual) == len(placeholder)
    path.write_bytes(buffer.getvalue().replace(placeholder, actual))


def make_wav(path, seconds=1):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * 8000 * seconds)


def make_tree(base):
    base.mkdir()
    (base / "sub").mkdir()
    make_jpeg(base / "photo.jpg")
    Image.new("RGBA", (64, 32), (0, 0, 255, 128)).save(base / "sub" / "pic.png")
    (base / "broken.jpg").write_bytes(b"this is not a jpeg at all")
    make_docx(base / "report.docx")
    make_wav(base / "tone.wav")
    (base / "notes_utf8.txt").write_bytes("UTF-8 のメモ keyword_alpha\n".encode("utf-8"))
    (base / "notes_sjis.txt").write_bytes(JAPANESE.encode("cp932"))
    (base / "notes_utf16.txt").write_text("UTF-16 の本文", encoding="utf-16")
    (base / "big.txt").write_bytes(b"x" * 5000)  # 上限超えなので対象外
    make_zip_with_cp932_name(base / "data.zip")
    with tarfile.open(base / "sub" / "backup.tar.gz", "w:gz") as archive:
        info = tarfile.TarInfo("etc/config.ini")
        info.size = 3
        info.mtime = 1_700_000_000
        archive.addfile(info, io.BytesIO(b"abc"))
    (base / "other.bin").write_bytes(b"\0" * 10)


def all_enabled(**overrides):
    settings = default_context_settings()
    for config in settings.values():
        config["enabled"] = True
    for kind, params in overrides.items():
        settings[kind].update(params)
    return settings


def scan(tree, db_path):
    scanner.scan_to_db(str(tree), db_path, source=scanner.SOURCE_WALK)
    return db_path


@pytest.fixture
def built(tmp_path):
    tree = tmp_path / "tree"
    make_tree(tree)
    files_db = scan(tree, tmp_path / "files.db")
    context_db = tmp_path / "context.db"
    events = []
    stats = build_context_db(files_db, context_db, str(tree), all_enabled(), progress=lambda *args: events.append(args))
    return tree, files_db, context_db, stats, events


def entry_id(files_db, rel_path):
    with DriveDB(files_db) as db:
        return db.find_path(rel_path).id


# ---------------------------------------------------------------------------- 公開 API の形
def test_registry_and_default_settings():
    assert list(EXTRACTORS) == ["exif", "office", "audio", "text", "thumbnail", "archive", "iso"]
    settings = default_context_settings()
    assert list(settings) == list(EXTRACTORS)
    assert all(config["enabled"] is False for config in settings.values())
    assert settings["text"]["max_bytes"] == 4096
    assert settings["thumbnail"]["size"] == 160
    assert settings["archive"]["max_entries"] == 10000
    settings["text"]["extensions"].append("zzz")  # 既定値を汚さない
    assert "zzz" not in default_context_settings()["text"]["extensions"]
    for kind, cls in EXTRACTORS.items():
        assert cls.kind == kind and cls.label and cls.description
        assert isinstance(cls.available(), bool) and isinstance(cls.requirement(), str)
    assert KEY_LABELS["model"] == "カメラモデル"


# ---------------------------------------------------------------------------- 抽出の統合テスト
def test_build_and_read(built):
    _tree, files_db, context_db, stats, events = built
    # 対象: photo.jpg, pic.png, broken.jpg, report.docx, tone.wav, notes×3, data.zip, backup.tar.gz
    assert events[0] == ("context_total", 10)
    assert events[-1] == ("context", 10)
    assert stats.processed == 10 and stats.reused == 0
    assert stats.errors == 2  # broken.jpg の exif と thumbnail
    assert stats.by_kind == {"exif": 1, "office": 1, "audio": 1, "text": 3, "thumbnail": 2, "archive": 2, "iso": 0}

    with ContextDB(context_db) as ctx:
        assert ctx.summary() == {"exif": 1, "office": 1, "audio": 1, "text": 3, "thumbnail": 2, "archive": 2}
        broken = entry_id(files_db, "broken.jpg")
        assert {(row[0], row[1]) for row in ctx.errors()} == {(broken, "exif"), (broken, "thumbnail")}
        assert all(message for _id, _kind, message in ctx.errors())
        assert not ctx.has_context(broken)
        assert not ctx.has_context(entry_id(files_db, "big.txt"))
        assert not ctx.has_context(entry_id(files_db, "other.bin"))

        # EXIF
        photo = entry_id(files_db, "photo.jpg")
        exif = {key: value for kind, key, value in ctx.get_meta(photo) if kind == "exif"}
        assert exif["make"] == "TestMake" and exif["model"] == "TestModel X100"
        assert exif["datetime_original"] == "2024:01:02 03:04:05"
        assert exif["exposure_time"] == "1/250" and exif["f_number"] == "f/2.8"
        assert exif["iso"] == "400" and exif["focal_length"] == "50 mm"
        assert exif["lens_model"] == "Test Lens 50mm"
        assert (exif["width"], exif["height"]) == ("400", "300")
        assert exif["gps_latitude"] == "35.500000" and exif["gps_longitude"] == "139.750000"
        assert set(exif) <= set(KEY_LABELS)

        # サムネイル(EXIF の回転を反映して縦長になる)
        width, height, data = ctx.get_thumb(photo)
        assert (width, height) == (120, 160) and data[:2] == b"\xff\xd8"
        assert Image.open(io.BytesIO(data)).size == (120, 160)
        png_thumb = ctx.get_thumb(entry_id(files_db, "sub\\pic.png"))
        assert png_thumb[:2] == (64, 32)
        r, g, b = Image.open(io.BytesIO(png_thumb[2])).convert("RGB").getpixel((10, 10))
        assert r > 100 and g > 100 and b > 200  # 半透明の青が白背景に合成される

        # Office
        office = {key: value for kind, key, value in ctx.get_meta(entry_id(files_db, "report.docx")) if kind == "office"}
        assert office["title"] == "四半期レポート" and office["author"] == "山田太郎"
        assert office["last_modified_by"] == "鈴木" and office["created"] == "2024-01-02T03:04:05Z"
        assert office["application"] == "Microsoft Office Word" and office["pages"] == "12"

        # 音声
        audio = {key: value for kind, key, value in ctx.get_meta(entry_id(files_db, "tone.wav")) if kind == "audio"}
        assert audio["duration"] == "0:01" and audio["sample_rate"] == "8000 Hz"

        # テキスト
        assert ctx.get_text(entry_id(files_db, "notes_utf8.txt")) == ("utf-8", "UTF-8 のメモ keyword_alpha\n")
        assert ctx.get_text(entry_id(files_db, "notes_utf16.txt")) == ("utf-16", "UTF-16 の本文")
        assert ctx.get_text(entry_id(files_db, "big.txt")) is None

        # 書庫
        inner = ctx.get_inner(entry_id(files_db, "data.zip"))
        assert [(item.path, item.size, item.is_dir) for item in inner] == [("日本語.txt", 7, 0), ("folder/plain.txt", 5, 0)]
        assert inner[0].mtime is not None
        tar_inner = ctx.get_inner(entry_id(files_db, "sub\\backup.tar.gz"))
        assert [(item.path, item.size) for item in tar_inner] == [("etc/config.ini", 3)]
        assert tar_inner[0].mtime == 1_700_000_000 * 10_000_000 + 116444736000000000

        # 検索(メタ情報の値 / テキスト本文 / 書庫内パス、AND 条件)
        assert ctx.search_entry_ids(["testmodel"]) == [photo]
        assert ctx.search_entry_ids(["keyword_alpha"]) == [entry_id(files_db, "notes_utf8.txt")]
        assert ctx.search_entry_ids(["keywordXalpha"]) == []  # _ はワイルドカード扱いしない
        assert ctx.search_entry_ids(["日本語.txt"]) == [entry_id(files_db, "data.zip")]
        assert ctx.search_entry_ids(["山田", "Word"]) == [entry_id(files_db, "report.docx")]
        assert ctx.search_entry_ids(["山田", "testmodel"]) == []
        assert ctx.search_entry_ids([]) == []
        assert len(ctx.search_entry_ids(["t"], limit=2)) == 2
        assert ctx.clone().summary() == ctx.summary()


def test_text_redecode(built):
    _tree, files_db, context_db, _stats, _events = built
    sjis = entry_id(files_db, "notes_sjis.txt")
    with ContextDB(context_db) as ctx:
        encoding, content = ctx.get_text(sjis)
    if encoding == "cp932":  # 日本語 Windows では自動判定で正しく読める
        assert content == JAPANESE

    wrong = redecode_text(context_db, sjis, "latin-1")
    assert wrong != JAPANESE
    assert redecode_text(context_db, sjis, "cp932") == JAPANESE
    with ContextDB(context_db) as ctx:
        assert ctx.get_text(sjis) == ("cp932", JAPANESE)
    with pytest.raises(LookupError):
        redecode_text(context_db, sjis, "no-such-encoding")
    with pytest.raises(KeyError):
        redecode_text(context_db, entry_id(files_db, "photo.jpg"), "utf-8")


def test_reuse_previous_results(built, tmp_path, monkeypatch):
    tree, files_db, context_db, _stats, _events = built
    # 1 件変更・1 件追加して再スキャン
    (tree / "notes_utf8.txt").write_text("書き換えた内容", encoding="utf-8")
    os.utime(tree / "notes_utf8.txt", (2_000_000_000, 2_000_000_000))
    (tree / "sub" / "added.txt").write_text("new file", encoding="utf-8")
    files_db2 = scan(tree, tmp_path / "files2.db")
    context_db2 = tmp_path / "context2.db"

    read_paths = []
    original = TextExtractor.extract
    monkeypatch.setattr(TextExtractor, "extract", lambda self, path, writer: (read_paths.append(path), original(self, path, writer))[1])
    stats = build_context_db(files_db2, context_db2, str(tree), all_enabled(), previous=(files_db, context_db))

    # broken.jpg は前回エラーなので読み直す。変更した notes_utf8.txt と追加した added.txt も読む
    assert stats.processed == 3 and stats.reused == 8 and stats.errors == 2
    assert stats.by_kind == {"exif": 1, "office": 1, "audio": 1, "text": 4, "thumbnail": 2, "archive": 2, "iso": 0}
    assert sorted(os.path.basename(path) for path in read_paths) == ["added.txt", "notes_utf8.txt"]

    with ContextDB(context_db) as old, ContextDB(context_db2) as new:
        assert new.get_text(entry_id(files_db2, "notes_utf8.txt")) == ("utf-8", "書き換えた内容")
        assert new.get_text(entry_id(files_db2, "sub\\added.txt")) == ("utf-8", "new file")
        for rel in ("photo.jpg", "report.docx", "data.zip", "sub\\backup.tar.gz", "sub\\pic.png", "notes_utf16.txt"):
            before, after = entry_id(files_db, rel), entry_id(files_db2, rel)
            assert new.get_meta(after) == old.get_meta(before)
            assert new.get_text(after) == old.get_text(before)
            assert new.get_thumb(after) == old.get_thumb(before)
            assert new.get_inner(after) == old.get_inner(before)
        # 生データも引き継ぐので、引き継いだテキストも再デコードできる
    assert redecode_text(context_db2, entry_id(files_db2, "notes_sjis.txt"), "cp932") == JAPANESE

    # パラメータを変えた種別は引き継がず作り直す
    context_db3 = tmp_path / "context3.db"
    stats = build_context_db(
        files_db2, context_db3, str(tree), all_enabled(thumbnail={"size": 64}), previous=(files_db2, context_db2)
    )
    assert stats.processed == 3  # photo.jpg, pic.png, broken.jpg
    with ContextDB(context_db3) as ctx:
        assert ctx.get_thumb(entry_id(files_db2, "photo.jpg"))[:2] == (48, 64)
        assert ctx.get_meta(entry_id(files_db2, "photo.jpg"))  # exif は引き継がれている


def test_scan_root_can_differ_and_only_enabled_kinds_run(built, tmp_path):
    tree, files_db, _context_db, _stats, _events = built
    moved = tmp_path / "moved"
    shutil.copytree(tree, moved)
    settings = default_context_settings()
    settings["text"]["enabled"] = True
    context_db = tmp_path / "moved.db"
    stats = build_context_db(files_db, context_db, str(moved), settings)
    assert stats.by_kind == {"text": 3} and stats.errors == 0
    with ContextDB(context_db) as ctx:
        assert ctx.summary() == {"text": 3}
        assert ctx.meta["kinds"] == ["text"]

    # 何も有効でなければ空の DB ができる
    empty_db = tmp_path / "empty.db"
    stats = build_context_db(files_db, empty_db, str(moved), default_context_settings())
    assert (stats.processed, stats.reused, stats.errors, stats.by_kind) == (0, 0, 0, {})
    with ContextDB(empty_db) as ctx:
        assert ctx.summary() == {}


def test_missing_files_are_recorded_as_errors(built, tmp_path):
    _tree, files_db, _context_db, _stats, _events = built
    context_db = tmp_path / "offline.db"
    settings = default_context_settings()
    settings["text"]["enabled"] = True
    stats = build_context_db(files_db, context_db, str(tmp_path / "does_not_exist"), settings)
    assert stats.errors == 3 and stats.by_kind == {"text": 0}
    with ContextDB(context_db) as ctx:
        assert len(ctx.errors()) == 3 and len(ctx.errors(limit=1)) == 1


def test_cancel_keeps_results_obtained_so_far(built, tmp_path):
    tree, files_db, full_context_db, full_stats, _events = built
    total = full_stats.processed

    # 3 ファイル処理したところでキャンセル → 例外にならず、そこまでの分で DB が完成する
    calls = []
    partial_db = tmp_path / "partial.db"
    stats = build_context_db(
        files_db, partial_db, str(tree), all_enabled(), is_cancelled=lambda: (calls.append(1), len(calls) > 3)[1]
    )
    assert stats.cancelled and stats.processed == 3 and stats.reused == 0
    with ContextDB(partial_db) as ctx:
        assert ctx.meta["partial"] is True
        assert sum(ctx.summary().values()) + len(ctx.errors()) > 0

    # 続きから: 取得済みの分は引き継ぎ、残りだけ読む(前回エラーだったものは読み直す)
    resumed_db = tmp_path / "resumed.db"
    resumed = build_context_db(files_db, resumed_db, str(tree), all_enabled(), previous=(files_db, partial_db))
    assert not resumed.cancelled
    assert resumed.processed + resumed.reused == total and 0 < resumed.reused <= 3
    assert resumed.by_kind == full_stats.by_kind
    with ContextDB(resumed_db) as ctx:
        assert ctx.meta["partial"] is False

    # 更新中にすぐキャンセルした場合も、前回から変わっていないファイルの結果は失わない
    kept_db = tmp_path / "kept.db"
    kept = build_context_db(
        files_db, kept_db, str(tree), all_enabled(), previous=(files_db, full_context_db), is_cancelled=lambda: True
    )
    assert kept.cancelled and kept.processed == 0 and kept.reused > 0
    with ContextDB(kept_db) as new, ContextDB(full_context_db) as old:
        assert new.summary() == old.summary()

    # 前回結果が無い状態ですぐキャンセル → 空の DB
    empty_db = tmp_path / "empty_cancel.db"
    empty = build_context_db(files_db, empty_db, str(tree), all_enabled(), is_cancelled=lambda: True)
    assert empty.cancelled and empty.processed == 0 and empty_db.exists()


# ---------------------------------------------------------------------------- 個別の抽出器
def test_detect_and_decode():
    assert detect_and_decode(b"") == ("utf-8", "")
    assert detect_and_decode(b"plain ascii") == ("utf-8", "plain ascii")
    assert detect_and_decode("﻿BOM 付き".encode("utf-8")) == ("utf-8-sig", "BOM 付き")
    assert detect_and_decode("日本語".encode("utf-16")) == ("utf-16", "日本語")
    assert detect_and_decode("abc def".encode("utf-16-le")) == ("utf-16-le", "abc def")
    encoding, text = detect_and_decode(JAPANESE.encode("euc_jp") * 3)
    assert text  # どの文字コードに判定されても例外にはならない
    encoding, text = detect_and_decode(bytes(range(128, 256)))
    assert encoding and len(text) > 0


def test_text_extractor_accepts():
    extractor = TextExtractor({"max_bytes": 100, "extensions": ["txt", ".MD"], "enabled": True})
    assert extractor.accepts("README.TXT", 100)
    assert extractor.accepts("notes.md", 0)
    assert not extractor.accepts("a.txt", 101)
    assert not extractor.accepts("a.txt", None)
    assert not extractor.accepts("a.bin", 10)
    assert "enabled" not in extractor.params


def test_decode_zip_name_variants(tmp_path):
    path = tmp_path / "names.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ascii.txt", "")
        archive.writestr("ユニコード.txt", "")  # UTF-8 フラグ付き
    with zipfile.ZipFile(path) as archive:
        assert [decode_zip_name(info) for info in archive.infolist()] == ["ascii.txt", "ユニコード.txt"]


def test_archive_limits_and_7z(tmp_path):
    path = tmp_path / "many.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("dir/", "")
        for index in range(20):
            archive.writestr(f"dir/file{index:02d}.txt", "x" * index)
    writer = EntryWriter(1, "archive")
    ArchiveExtractor({"max_entries": 5}).extract(str(path), writer)
    assert len(writer.inner) == 5
    assert writer.inner[0] == ("dir", None, writer.inner[0][2], 1)
    assert dict(writer.meta)["inner_count"] == "5" and "inner_truncated" in dict(writer.meta)

    limited = ArchiveExtractor({"max_size_mb": 1})
    assert limited.accepts("a.zip", 1024 * 1024) and not limited.accepts("a.zip", 1024 * 1024 + 1)
    assert limited.accepts("a.TAR.GZ", 10) and not limited.accepts("a.gz", 10)

    py7zr = pytest.importorskip("py7zr")
    seven = tmp_path / "data.7z"
    with py7zr.SevenZipFile(seven, "w") as archive:
        archive.writestr(b"hello", "inner/hello.txt")
    writer = EntryWriter(2, "archive")
    ArchiveExtractor().extract(str(seven), writer)
    assert ("inner/hello.txt", 5) in [(row[0], row[1]) for row in writer.inner]


def test_office_ole_extensions_follow_dependency():
    extractor = OfficeExtractor()
    assert extractor.accepts("a.DOCX", 1)
    from virtualdiskmokuroku.context.base import module_available

    assert extractor.accepts("a.doc", 1) == module_available("olefile")


def test_audio_tags_mp3(tmp_path):
    pytest.importorskip("tinytag")

    def text_frame(frame_id: bytes, text: str) -> bytes:
        data = b"\x01" + text.encode("utf-16")  # 文字コード 1 = BOM 付き UTF-16
        return frame_id + len(data).to_bytes(4, "big") + b"\0\0" + data

    frames = b"".join(
        text_frame(frame_id, text)
        for frame_id, text in (
            (b"TIT2", "テスト曲"), (b"TPE1", "テストアーティスト"), (b"TALB", "テストアルバム"),
            (b"TYER", "2024"), (b"TCON", "Rock"), (b"TRCK", "3/12"),
        )  # fmt: skip
    )
    size = len(frames)
    synchsafe = bytes(((size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F))
    id3 = b"ID3\x03\x00\x00" + synchsafe + frames  # ID3v2.3
    frame = b"\xff\xfb\x90\x64" + b"\0" * 413  # MPEG1 Layer3 128kbps 44.1kHz の無音フレーム
    path = tmp_path / "song.mp3"
    path.write_bytes(id3 + frame * 40)

    extractor = AudioExtractor({"extensions": ["mp3", "ape", "m4a"]})
    assert extractor.accepts("a.MP3", 1) and extractor.accepts("a.m4a", 1)
    assert not extractor.accepts("a.ape", 1)  # TinyTag が読めない形式は設定にあっても対象外

    writer = EntryWriter(1, "audio")
    AudioExtractor().extract(str(path), writer)
    meta = dict(writer.meta)
    assert meta["title"] == "テスト曲" and meta["artist"] == "テストアーティスト"
    assert meta["album"] == "テストアルバム" and meta["year"] == "2024"
    assert meta["genre"] == "Rock" and meta["track"] == "3/12"
    assert meta["bitrate"] == "128 kbps" and meta["sample_rate"] == "44100 Hz"
    assert set(meta) <= set(KEY_LABELS)


def test_iso_listing(tmp_path):
    pycdlib = pytest.importorskip("pycdlib")

    joliet = tmp_path / "joliet.iso"
    iso = pycdlib.PyCdlib()
    iso.new(joliet=3, vol_ident="TESTVOL")
    iso.add_directory("/DIR1", joliet_path="/dir1")
    iso.add_fp(io.BytesIO(b"hello iso"), 9, "/DIR1/HELLO.TXT;1", joliet_path="/dir1/hello world.txt")
    iso.write(str(joliet))
    iso.close()
    writer = EntryWriter(1, "iso")
    IsoExtractor().extract(str(joliet), writer)
    meta = dict(writer.meta)
    assert meta["iso_filesystem"] == "Joliet" and meta["volume_label"] == "TESTVOL" and meta["inner_count"] == "2"
    assert [(row[0], row[1], row[3]) for row in writer.inner] == [("dir1", None, 1), ("dir1/hello world.txt", 9, 0)]
    assert writer.inner[1][2] is not None

    plain = tmp_path / "plain.iso"
    iso = pycdlib.PyCdlib()
    iso.new()
    iso.add_fp(io.BytesIO(b"abc"), 3, "/FOO.TXT;1")
    iso.write(str(plain))
    iso.close()
    writer = EntryWriter(2, "iso")
    IsoExtractor().extract(str(plain), writer)
    assert dict(writer.meta)["iso_filesystem"] == "ISO 9660"
    assert [(row[0], row[1]) for row in writer.inner] == [("FOO.TXT", 3)]


def test_thumbnail_stores_original_resolution(built, tmp_path):
    import sqlite3

    from virtualdiskmokuroku.context.thumbnail import ThumbnailExtractor

    tree, files_db, context_db, _stats, _events = built
    pic = entry_id(files_db, "sub\\pic.png")
    with ContextDB(context_db) as ctx:
        assert ctx.get_image_size(pic) == (64, 32)
        assert ("thumbnail", "width", "64") in ctx.get_meta(pic)
        assert ctx.get_image_size(entry_id(files_db, "notes_utf8.txt")) is None

    # EXIF の回転指定がある画像は、サムネイルと同じく表示時の向きで保存する
    rotated = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6
    Image.new("RGB", (40, 20), "red").save(rotated, exif=exif)
    writer = EntryWriter(1, "thumbnail")
    ThumbnailExtractor().extract(str(rotated), writer)
    assert dict(writer.meta) == {"width": "20", "height": "40"}
    assert writer.thumb[:2] == (20, 40)

    # 解像度を保存していなかった旧版の結果は引き継がず、サムネイルだけ読み直す
    old_db = tmp_path / "old_context.db"
    shutil.copyfile(context_db, old_db)
    conn = sqlite3.connect(old_db)
    conn.execute("DELETE FROM meta WHERE key = 'revision:thumbnail'")
    conn.execute("DELETE FROM ctx WHERE kind = 'thumbnail'")
    conn.commit()
    conn.close()
    with ContextDB(old_db) as ctx:
        assert ctx.get_image_size(pic) is None and ctx.get_thumb(pic) is not None
    new_db = tmp_path / "new_context.db"
    stats = build_context_db(files_db, new_db, str(tree), all_enabled(), previous=(files_db, old_db))
    assert stats.processed == 3  # photo.jpg, pic.png, broken.jpg
    with ContextDB(new_db) as ctx:
        assert ctx.get_image_size(pic) == (64, 32)
        assert ctx.get_text(entry_id(files_db, "notes_utf8.txt")) is not None  # 他の種別は引き継がれている
