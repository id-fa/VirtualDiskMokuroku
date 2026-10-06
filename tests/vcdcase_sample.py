"""テスト用に Virtual CD-ROM Case のカタログ (.cas) を組み立てる。

形式は ``virtualdiskmokuroku.core.vcdcase`` の説明と同じ(実ファイルの解析から分かった範囲)。
"""

import struct

FILE = 0x20
DIRECTORY = 0x10
KIND_ARCHIVE = 0x40000000
KIND_MEMBER = 0x80000000
LZH, ZIP = 1, 2
FILETIME_2021 = 132726424220000000  # 2021-08-05 14:07:02 UTC
FILETIME_2003 = 127000000000000000
FILETIME_BROKEN = 0xFFFFFFFFFFFFFFFF  # 範囲外の日時 (「不明」として扱われる)


def cstring(data: bytes) -> bytes:
    if len(data) < 0xFF:
        return bytes([len(data)]) + data
    if len(data) < 0xFFFE:
        return b"\xff" + struct.pack("<H", len(data)) + data
    return b"\xff\xff\xff" + struct.pack("<I", len(data)) + data


def record(name, *, attrs=FILE, size=0, mtime=FILETIME_2021, ctime=0, comment=b"", properties=(), children=(),
           crc=None, archive_type=0, packed=None, lead=None):  # fmt: skip
    if isinstance(name, str):
        name = name.encode("cp932")
    data = struct.pack("<IQIH3Q", attrs if lead is None else lead, size, attrs, archive_type, 0, mtime, ctime)
    data += cstring(name) + b"\x02" + cstring(comment) + b"\x00"
    data += bytes([len(properties)]) + b"".join(cstring(item) for item in properties)
    if properties:
        data += b"\x00"
    data += bytes(7) + struct.pack("<HII", crc is not None, crc or 0, len(children)) + b"".join(children)
    if attrs & KIND_MEMBER:
        data += struct.pack("<IB", size // 2 if packed is None else packed, 0)
    return data


def folder(name, children=(), **kwargs):
    return record(name, attrs=DIRECTORY, children=children, **kwargs)


def archive(name, archive_type, children, **kwargs):
    """中身を展開して登録した書庫 (フォルダ属性が付き、サイズと日時は書庫ファイルのもの)。"""
    return record(name, attrs=KIND_ARCHIVE | DIRECTORY, archive_type=archive_type, children=children, **kwargs)


def member(name, **kwargs):
    return record(name, attrs=KIND_MEMBER | 0xA0, **kwargs)


def member_folder(name, children=()):
    return record(name, attrs=KIND_MEMBER | KIND_ARCHIVE | DIRECTORY, mtime=0, children=children)


def drive(label, children, *, serial=0x1A2B3C4D, filesystem="CDFS", total=700_000_000, free=0, media=1,
          cluster_sectors=1, sector=2048, comment=b"2003/04/05", short_label=None):  # fmt: skip
    data = record(b"", attrs=0x10000000 | (media << 24) | DIRECTORY, mtime=0, comment=comment, children=children)
    data += cstring(label.encode("cp932")) + b"\x00" + struct.pack("<I", serial) + cstring(filesystem.encode("ascii"))
    data += struct.pack("<QQ", total, free) + cstring((label[:16] if short_label is None else short_label).encode("cp932"))
    data += struct.pack("<II", cluster_sectors, sector)
    return data


def case_file(drives, schema=12):
    top = record("C:\\work\\sample.cas", attrs=0x20000020, lead=0xFFFFFFFF, children=drives)
    return b"\xff\xff" + struct.pack("<HH", schema, 7) + b"CCDCase" + b"\xff" * 12 + top


HTML_PROPERTIES = (
    b"HTML", "写真の一覧".encode("utf-8"), b"Photo index", b"photo,sea", b"", b"SampleEditor 1.0", b"", b"", b"", b"", b"",
)  # fmt: skip
MODULE_PROPERTIES = (
    b"MODULE", b"1.2.3.4", b"1.2.0.0", b"NT_WINDOWS32", b"APP", b"", "サンプル社".encode("cp932"),
    "セットアップ".encode("cp932"), b"(C) 2003 Sample", b"", b"Sample Suite ", b"", b"", b"", b"", b"", b"", b"",
)  # fmt: skip
PDF_PROPERTIES = (
    b"PDF-P", "報告書".encode("cp932"), b"", b"", "山田".encode("cp932"), b"Writer", b"Distiller 5.0",
    b"20020808133904+09'00'", b"20020808134635+09'00'", b"53", b"", b"", b"",
)  # fmt: skip
LONG_COMMENT = ("とても長いコメント。" * 40).encode("cp932")  # 255 バイトを超える文字列
README_CRC = 0xE2310CA8


def sample_case() -> bytes:
    """2 台のドライブを持つカタログ。"""
    backup = drive(
        "BACKUP_2003",
        [
            record(
                "readme.txt", size=120, ctime=FILETIME_2003, crc=README_CRC,
                comment="はじめにお読みください。\r\n2 行目".encode("cp932"),
            ),  # fmt: skip
            record("setup.exe", size=4096, comment="セットアップ".encode("cp932"), properties=MODULE_PROPERTIES),
            folder(
                "写真",
                [
                    record("index.html", size=900, comment="写真の一覧".encode("utf-8"), properties=HTML_PROPERTIES),
                    record("海.jpg", size=50_000, mtime=FILETIME_2003, ctime=FILETIME_BROKEN),
                    folder("空のフォルダ"),
                ],
                comment="2003 年の旅行".encode("cp932"),
            ),
            archive(
                "data.lzh", LZH,
                [
                    member_folder("doc", [member("manual.txt", size=1500, mtime=FILETIME_2003, crc=0x12345678)]),
                    member("tool.exe", size=2500),
                ],
                size=3000, crc=0x0BADF00D,
            ),  # fmt: skip
            record("memo.txt", size=10, comment=LONG_COMMENT),
        ],
    )
    floppy = drive(
        "フロッピー 12",
        [record("AUTOEXEC.BAT", size=64, mtime=FILETIME_2003)],
        serial=0, filesystem="FAT", total=1_457_664, free=1_000_000, media=8, cluster_sectors=1, sector=512,
        comment="友人から借りたディスク".encode("cp932"),
    )  # fmt: skip
    return case_file([backup, floppy])
