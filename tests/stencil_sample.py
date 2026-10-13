"""テスト用にメディアステンシルの記録ファイル (.fsr) を組み立てる。

形式は ``virtualdiskmokuroku.core.stencil`` の説明と同じ(実ファイルの解析から分かった範囲)。
"""

import struct

FILE = 0x20
DIRECTORY = 0x10
ARCHIVE = 0x8000  # 属性に立っていれば、中身を記録した書庫
CDROM, FIXED = 5, 3
UNIX_2021 = 1628237958  # 2021-08-06 08:19:18 UTC
UNIX_2026 = 1791892653  # 2026-10-13 11:57:33 UTC
FILETIME_2021 = UNIX_2021 * 10_000_000 + 116444736000000000
MAGIC = b"File Struct Record Version 1.00"


def cstring(text: str, wide: bool = True) -> bytes:
    """MFC の CString。``wide`` なら Unicode 形式、そうでなければ ANSI (CP932) 形式。"""
    data = text.encode("utf-16-le") if wide else text.encode("cp932")
    length = len(text) if wide else len(data)
    if length < 0xFF:
        head = bytes([length])
    elif length < 0xFFFF:
        head = b"\xff" + struct.pack("<H", length)
    else:
        head = b"\xff\xff\xff" + struct.pack("<I", length)
    return (b"\xff\xfe\xff" if wide else b"") + head + data


def entry(name: str, attrs: int = FILE, *, icon: int = 0, link: int = 0, size: int = 0, mtime: int = UNIX_2021) -> tuple:
    """フォルダリスト内のエントリ 1 件。``link`` はフォルダ・書庫なら中身のフォルダリストの番号、ファイルならサイズの上位。"""
    return (name, attrs, icon, link, size, mtime)


def folder_list(path: str, entries=()) -> tuple:
    return (path, tuple(entries))


def icon_blob(color: int, *, transparent_origin: bool = True) -> bytes:
    """16 色のアイコンデータ (800 バイト)。全面を ``color`` で塗り、``transparent_origin`` なら左上の 1 画素を透明にする。"""
    mask16 = bytearray(32)
    mask32 = bytearray(128)
    if transparent_origin:
        mask16[0] = mask32[0] = 0x80
    packed = (color << 4) | color
    return bytes(mask16) + bytes(mask32) + bytes([packed]) * 128 + bytes([packed]) * 512


def root(
    label: str, folder_lists, *, name: str = "", serial: int = 0x1A2B3C4D, drive_type: int = CDROM,
    used: int = 700_000_000, recorded: int = UNIX_2026, folder_count: int = 0, file_count: int = 0,
    icons=(), comment: bytes | None = None,
) -> dict:  # fmt: skip
    return dict(
        label=label, folder_lists=tuple(folder_lists), name=name, serial=serial, drive_type=drive_type, used=used,
        recorded=recorded, folder_count=folder_count, file_count=file_count, icons=tuple(icons), comment=comment,
    )


class _Archive:
    """CArchive のクラスの印 (初回はクラス定義、2 回目以降は番号の参照) を再現する。"""

    def __init__(self, wide: bool):
        self.wide = wide
        self.classes: dict[bytes, int] = {}
        self.count = 1
        self.out = bytearray()

    def string(self, text: str) -> None:
        self.out += cstring(text, self.wide)

    def tag(self, name: bytes) -> None:
        if name in self.classes:
            self.out += struct.pack("<H", 0x8000 | self.classes[name])
        else:
            self.out += b"\xff\xff" + struct.pack("<HH", 1, len(name)) + name
            self.classes[name] = self.count
            self.count += 1
        self.count += 1  # オブジェクト自身の番号


def record_file(roots: list[dict], *, wide: bool = True, magic: bytes = MAGIC, trailer: bytes = b"") -> bytes:
    archive = _Archive(wide)
    out = archive.out
    out += magic.ljust(64, b"\0")
    out += struct.pack("<H", len(roots))
    for item in roots:
        archive.tag(b"CRootEntry")
        archive.string(item["name"])
        out += struct.pack("<IIII", 0, 0, item["used"] & 0xFFFFFFFF, item["recorded"])
        archive.string(item["label"])
        out += struct.pack(
            "<IHHIIII", item["serial"], item["drive_type"], item["used"] >> 32, item["folder_count"], item["file_count"], 0, 0
        )
        out += struct.pack("<H", len(item["folder_lists"]))
        for path, entries in item["folder_lists"]:
            archive.tag(b"CFileArray")
            archive.string(path)
            out += struct.pack("<H", len(entries))
            for name, attrs, icon, link, size, mtime in entries:
                archive.tag(b"CFileEntry")
                archive.string(name)
                out += struct.pack("<HHIII", attrs, icon, link, size, mtime)
    for item in roots:
        out += struct.pack("<I", len(item["icons"]))
        for blob in item["icons"]:
            out += struct.pack("<I", len(blob)) + blob
    for item in roots:
        comment = item["comment"]
        if comment is None:
            out += struct.pack("<I", 0)
        else:
            out += struct.pack("<II", 1, len(comment) + 1) + comment + b"\0"
    out += trailer
    return bytes(out)


def sample_roots() -> list[dict]:
    photos = root(
        "PHOTOS_2021",
        [
            folder_list("\\", [
                entry("readme.txt", size=120),
                entry("setup.exe", icon=2, link=1, size=16),  # 4 GB を超えるサイズ (上位 32 ビットが 1)
                entry("写真", DIRECTORY, link=1),
                entry("data.lzh", FILE | ARCHIVE, link=3, size=3000),
                entry("空のフォルダ", DIRECTORY, link=2),
            ]),
            folder_list("\\写真", [
                entry("海.jpg", icon=1, size=50_000, mtime=0),  # 日時 0 は「不明」
                entry("index.html", size=900),
            ]),
            folder_list("\\空のフォルダ"),
            folder_list("\\data.lzh", [
                entry("doc/manual.txt", size=1500),
                entry("tool.exe", size=2500, mtime=UNIX_2026),
                entry("doc", DIRECTORY),
            ]),
        ],
        folder_count=2, file_count=5, icons=(icon_blob(6), icon_blob(9, transparent_origin=False)),
        comment="旅行の写真\r\n2 行目".encode("cp932"),
    )  # fmt: skip
    documents = root(
        "WINDOWS::\\Users\\太郎\\docs",
        [folder_list("\\", [entry("memo.txt", size=10), entry("下書き", DIRECTORY)])],
        name="仕事の資料", serial=0xD59B51E0, drive_type=FIXED, used=(109 << 32) + 582_344_704, recorded=0,
        folder_count=1, file_count=1,
    )  # fmt: skip
    return [photos, documents]


def sample_record(**kwargs) -> bytes:
    return record_file(sample_roots(), **kwargs)
