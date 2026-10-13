"""メディアステンシルの記録ファイル (.fsr) の読み取り。

メディアステンシル (工房 和泉家、Version 3.20) は CD/DVD などのフォルダ構成を記録する Windows 用ソフトで、
記録ファイル (.fsr) は MFC の CArchive で書かれたバイナリ。仕様は公開されていないので、以下は実際のファイル
(Version 3.20 で記録したもの) を解析して分かった構造で、意味の分からない部分は読み飛ばしている。整数はリトル
エンディアン、文字列は MFC の CString (Unicode なら FF FE FF に続けて長さ (1 バイト。0xFF なら続く 2 バイト、
それも 0xFFFF なら続く 4 バイト) と UTF-16LE。ANSI (旧バージョンで記録したもの) なら長さと CP932 のバイト列)::

    ヘッダ    "File Struct Record Version 1.00" を 64 バイトに NUL 埋め
    u16       メディア (ルート) の数
    ルートごと (CObject の形式。初回は FF FF, スキーマ u16, 名前長 u16, "CRootEntry"。2 回目以降は 01 80):
      文字列    表示名 (「名前の変更」で付けた名前。空ならラベルを表示する)
      u32 × 2   不明 (常に 0)
      u32       使用量 (バイト) の下位 32 ビット
      u32       記録日時 (time_t)
      文字列    ボリュームラベル。フォルダ単位で記録した場合は "ラベル::\\フォルダ" (ドライブレターは記録されない)
      u32       シリアル
      u16       ドライブの種類 (GetDriveType の値。3 = 固定, 5 = CD-ROM など)
      u16       使用量の上位 16 ビット
      u32       フォルダ数, u32 ファイル数 (書庫の中身は数えない)
      u32 × 2   不明 (常に 0)
      u16       フォルダリストの数
      フォルダリストごと (初回は "CFileArray" のクラス定義、2 回目以降は 03 80):
        文字列  パス ("\\", "\\sub"。書庫の中身なら "\\sub\\x.lzh" のように書庫自身のパス)
        u16     エントリ数
        エントリごと (初回は "CFileEntry" のクラス定義、2 回目以降は 05 80):
          文字列  名前
          u16     属性 (Windows のファイル属性。0x8000 が立っていれば中身を記録した書庫)
          u16     アイコン番号 (0 = 記録なし。1 以上はこのルートのアイコン表の番号)
          u32     フォルダと書庫では中身のフォルダリストの番号。ファイルではサイズの上位 32 ビット (推定)
          u32     サイズの下位 32 ビット
          u32     更新日時 (time_t。0 は不明)
    ルートごとのアイコン表: u32 数, その数だけ (u32 長さ (800), データ)
      データは 16x16 の AND マスク (32 バイト), 32x32 の AND マスク (128 バイト), 16x16 の 16 色 (4 ビット/画素,
      128 バイト), 32x32 の 16 色 (512 バイト) の順。色は Windows の標準 16 色 (DIB の並び)
    ルートごとのコメント: u32 数 (0 か 1), その数だけ (u32 長さ, ANSI (CP932) の文字列。NUL 終端を含む)

書庫 (CAB, LZH) の中身は、書庫のパスを持つフォルダリストに一覧 (ツリーではない) で入る。名前には "dir\\a.txt" や
"docs/manual.txt" のように書庫内のパスが入ることがある。
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field

from .errors import CatalogError, ScanCancelled
from .volume import drive_type_name
from ..i18n import tr

MAGIC = b"File Struct Record Version 1.00"
SUPPORTED_VERSION = "1.00"
DEFAULT_ENCODING = "cp932"
ICON_SIZES = (16, 32)
_ICON_BLOB_SIZE = 800
_ICON_LAYOUT = {16: (0, 160), 32: (32, 288)}  # サイズ -> (AND マスクの位置, 16 色データの位置)
# Windows の標準 16 色 (DIB の並び)
_PALETTE = (
    (0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0), (0, 0, 128), (128, 0, 128), (0, 128, 128), (192, 192, 192),
    (128, 128, 128), (255, 0, 0), (0, 255, 0), (255, 255, 0), (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
)  # fmt: skip

_HEADER_SIZE = 64
_ROOT_HEAD = struct.Struct("<IIII")  # 不明 × 2, 使用量 (下位), 記録日時
_ROOT_INFO = struct.Struct("<IHHIIII")  # シリアル, 種類, 使用量 (上位), フォルダ数, ファイル数, 不明 × 2
_ENTRY = struct.Struct("<HHIII")  # 属性, アイコン, 中身の番号 / サイズ (上位), サイズ (下位), 更新日時
_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
_ROOT_CLASS, _ARRAY_CLASS, _ENTRY_CLASS = b"CRootEntry", b"CFileArray", b"CFileEntry"
_NEW_CLASS_TAG = 0xFFFF
_CLASS_REF_FLAG = 0x8000
_ARCHIVE_FLAG = 0x8000
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_ATTRIBUTE_MASK = 0x7FFF
_MIN_ENTRY_SIZE = 4 + _ENTRY.size
_EPOCH_FILETIME = 116444736000000000  # 1970-01-01 の FILETIME
_MAX_UNIX_TIME = 0xFFFFFFFF
_CANCEL_CHECK_INTERVAL = 20000

_NOT_A_STENCIL = "メディアステンシルの記録ファイル (.fsr) ではありません"


@dataclass(slots=True)
class StencilEntry:
    """ドライブ内のファイルまたはフォルダ 1 件。"""

    path: str  # ルートからの相対パス (区切りは "\\")
    is_dir: bool
    size: int | None
    mtime: int | None  # FILETIME
    attrs: int  # Windows のファイル属性 (書庫を示すビットは除く)
    icon: int = 0  # 記録されたアイコンの番号 (1 から)。0 なら記録なし
    # 中身を記録した書庫の内部エントリ: (パス, サイズ, 更新日時, フォルダか)。パスの区切りは "/"
    inner: list[tuple[str, int | None, int | None, bool]] = field(default_factory=list)

    @property
    def has_context(self) -> bool:
        return bool(self.icon or self.inner)


@dataclass(slots=True)
class StencilRoot:
    """記録したメディア (またはフォルダ) 1 件。"""

    name: str  # 「名前の変更」で付けた表示名。付けていなければ空
    label: str  # ボリュームラベル
    folder: str  # フォルダ単位で記録した場合の "\\フォルダ" (ドライブレターなし)。ドライブ全体なら空
    serial: str  # "1A2B-3C4D"。不明なら空
    drive_type: str  # "fixed" / "cdrom" など (core.volume と同じ名前)
    used_bytes: int
    recorded_at: int  # 記録日時 (time_t)。0 なら不明
    folder_count: int
    file_count: int
    comment: str = ""
    icons: list[bytes] = field(default_factory=list)  # アイコン表 (番号 1 が先頭)
    entries: list[StencilEntry] = field(default_factory=list)

    @property
    def title(self) -> str:
        """メディアステンシルでの表示名。"""
        return self.name or (f"{self.label}::{self.folder}" if self.folder else self.label)


@dataclass(slots=True)
class StencilFile:
    version: str
    roots: list[StencilRoot] = field(default_factory=list)


class _Reader:
    __slots__ = ("data", "pos", "classes", "count")

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.classes: dict[int, bytes] = {}  # CArchive のクラス参照番号 -> クラス名
        self.count = 1  # CArchive が次に振る番号 (クラスとオブジェクトで共用)

    def unpack(self, layout: struct.Struct) -> tuple:
        values = layout.unpack_from(self.data, self.pos)
        self.pos += layout.size
        return values

    def take(self, size: int) -> bytes:
        end = self.pos + size
        if end > len(self.data):
            raise IndexError(tr('データが途中で終わっています'))
        chunk = self.data[self.pos : end]
        self.pos = end
        return chunk

    def string(self, encoding: str) -> str:
        """CString を読む。Unicode (FF FE FF で始まる) と ANSI の両方に対応する。"""
        wide = self.data[self.pos : self.pos + 3] == b"\xff\xfe\xff"
        if wide:
            self.pos += 3
        length = self.data[self.pos]
        self.pos += 1
        if length == 0xFF:
            (length,) = self.unpack(_U16)
            if length == 0xFFFF:
                (length,) = self.unpack(_U32)
        if wide:
            return self.take(length * 2).decode("utf-16-le", errors="replace")
        return self.take(length).decode(encoding, errors="replace")

    def object_class(self, expected: bytes) -> None:
        """CObject の先頭にあるクラスの印を読み、``expected`` と違えば構造の不一致として扱う。"""
        (tag,) = self.unpack(_U16)
        if tag == _NEW_CLASS_TAG:
            (_schema,) = self.unpack(_U16)
            (length,) = self.unpack(_U16)
            name = self.take(length)
            self.classes[self.count] = name
            self.count += 1
        elif tag & _CLASS_REF_FLAG:
            name = self.classes.get(tag & ~_CLASS_REF_FLAG, b"")
        else:
            raise IndexError(tr('オブジェクトの印が不正です'))
        self.count += 1  # オブジェクト自身の番号
        if name != expected:
            raise IndexError(tr('クラスが合いません ({name})').format(name=name.decode("ascii", errors="replace")))


def _filetime(unix_time: int) -> int | None:
    return unix_time * 10_000_000 + _EPOCH_FILETIME if 0 < unix_time <= _MAX_UNIX_TIME else None


def is_stencil(data: bytes) -> bool:
    return data.startswith(MAGIC[: len(b"File Struct Record Version")])


ProgressCallback = Callable[[str, int], None]


def parse_stencil(
    data: bytes,
    *,
    encoding: str = DEFAULT_ENCODING,
    is_cancelled: Callable[[], bool] | None = None,
    progress: ProgressCallback | None = None,
) -> StencilFile:
    """.fsr の中身を読む。``encoding`` は ANSI の文字列 (コメントなど) の文字コード。読めない場合は ``CatalogError``。

    進捗は ``progress("parse", 読み取ったエントリ数)`` で通知する。
    """
    if not is_stencil(data) or len(data) < _HEADER_SIZE + 2:
        raise CatalogError(tr(_NOT_A_STENCIL))
    version = data[len(MAGIC) - len(SUPPORTED_VERSION) : _HEADER_SIZE].split(b"\0", 1)[0].decode("ascii", errors="replace")
    reader = _Reader(data)
    reader.pos = _HEADER_SIZE
    try:
        return _parse(reader, version, encoding, is_cancelled, progress)
    except (struct.error, IndexError, KeyError) as error:
        message = tr('メディアステンシルの記録ファイルを読み取れません (位置 0x{pos:X} 付近で構造が合いません)').format(pos=reader.pos)
        if version != SUPPORTED_VERSION:
            message += tr('。形式バージョン {version} には対応していません (確認済みは {SUPPORTED_VERSION})').format(version=version, SUPPORTED_VERSION=SUPPORTED_VERSION)
        raise CatalogError(message) from error


def read_stencil(
    path,
    *,
    encoding: str = DEFAULT_ENCODING,
    is_cancelled: Callable[[], bool] | None = None,
    progress: ProgressCallback | None = None,
) -> StencilFile:
    with open(path, "rb") as stream:
        data = stream.read()
    return parse_stencil(data, encoding=encoding, is_cancelled=is_cancelled, progress=progress)


def _parse(reader: _Reader, version: str, encoding: str, is_cancelled, progress) -> StencilFile:
    data_size = len(reader.data)
    count = 0

    def check_cancel() -> None:
        nonlocal count
        count += 1
        if count % _CANCEL_CHECK_INTERVAL == 0:
            if is_cancelled is not None and is_cancelled():
                raise ScanCancelled()
            if progress is not None:
                progress("parse", count)

    (root_count,) = reader.unpack(_U16)
    roots: list[StencilRoot] = []
    for _ in range(root_count):
        reader.object_class(_ROOT_CLASS)
        name = reader.string(encoding)
        _unknown1, _unknown2, used_low, recorded_at = reader.unpack(_ROOT_HEAD)
        label = reader.string(encoding)
        serial, drive_type, used_high, folder_count, file_count, _unknown3, _unknown4 = reader.unpack(_ROOT_INFO)
        label, _sep, folder = label.partition("::")
        root = StencilRoot(
            name, label, folder, f"{serial >> 16:04X}-{serial & 0xFFFF:04X}" if serial else "", drive_type_name(drive_type),
            (used_high << 32) | used_low, recorded_at, folder_count, file_count,
        )  # fmt: skip
        roots.append(root)

        # フォルダリストを全部読んでから、番号でつないでパスを組み立てる
        (array_count,) = reader.unpack(_U16)
        arrays: list[tuple[str, list[tuple[str, int, int, int, int, int]]]] = []
        for _ in range(array_count):
            reader.object_class(_ARRAY_CLASS)
            path = reader.string(encoding)
            (entry_count,) = reader.unpack(_U16)
            if entry_count * _MIN_ENTRY_SIZE > data_size - reader.pos:
                raise IndexError(tr('子の数が不正です'))
            items = []
            for _ in range(entry_count):
                reader.object_class(_ENTRY_CLASS)
                entry_name = reader.string(encoding)
                items.append((entry_name, *reader.unpack(_ENTRY)))
                check_cancel()
            arrays.append((path, items))
        _link_arrays(root, arrays)

    for root in roots:
        (icon_count,) = reader.unpack(_U32)
        for _ in range(icon_count):
            (length,) = reader.unpack(_U32)
            root.icons.append(reader.take(length))
    for root in roots:
        (comment_count,) = reader.unpack(_U32)
        for index in range(comment_count):
            (length,) = reader.unpack(_U32)
            text = reader.take(length).split(b"\0", 1)[0].decode(encoding, errors="replace").strip()
            if index == 0:
                root.comment = text
    # 末尾に (検索結果など) 読み方の分からないデータが続くことがあるので、読み残しは許す
    if progress is not None:
        progress("parse", count)
    return StencilFile(version, roots)


def _link_arrays(root: StencilRoot, arrays: list[tuple[str, list[tuple[str, int, int, int, int, int]]]]) -> None:
    """フォルダリスト (番号 0 がルート) をたどり、エントリをルートからの相対パスで ``root.entries`` に入れる。"""
    if not arrays:
        return
    used = [False] * len(arrays)
    stack = [(0, "")]  # (フォルダリストの番号, その中のエントリに付ける前置き)
    while stack:
        index, prefix = stack.pop()
        if index >= len(arrays) or used[index]:
            raise IndexError(tr('フォルダリストの番号が不正です'))
        used[index] = True
        children: list[tuple[int, str]] = []
        for name, raw_attrs, icon, link, size_low, mtime in arrays[index][1]:
            is_dir = bool(raw_attrs & _FILE_ATTRIBUTE_DIRECTORY)
            is_archive = bool(raw_attrs & _ARCHIVE_FLAG) and not is_dir and link != 0
            path = prefix + name
            if is_dir:
                entry = StencilEntry(path, True, None, _filetime(mtime), raw_attrs & _ATTRIBUTE_MASK, icon)
                if link:
                    children.append((link, path + "\\"))
            elif is_archive:
                entry = StencilEntry(path, False, size_low, _filetime(mtime), raw_attrs & _ATTRIBUTE_MASK, icon)
                if link >= len(arrays) or used[link]:
                    raise IndexError(tr('フォルダリストの番号が不正です'))
                used[link] = True
                for inner_name, inner_attrs, _icon, _link, inner_size, inner_mtime in arrays[link][1]:
                    inner_dir = bool(inner_attrs & _FILE_ATTRIBUTE_DIRECTORY)
                    entry.inner.append(
                        (inner_name.replace("\\", "/"), None if inner_dir else inner_size, _filetime(inner_mtime), inner_dir)
                    )
            else:
                size = (link << 32) | size_low
                entry = StencilEntry(path, False, size, _filetime(mtime), raw_attrs & _ATTRIBUTE_MASK, icon)
            root.entries.append(entry)
        stack.extend(reversed(children))


def icon_png(blob: bytes, size: int = 32) -> bytes | None:
    """アイコン表のデータを ``size`` x ``size`` (16 か 32) の PNG (RGBA) にする。形式が違えば None。"""
    if len(blob) != _ICON_BLOB_SIZE or size not in _ICON_LAYOUT:
        return None
    mask_offset, color_offset = _ICON_LAYOUT[size]
    mask_stride, color_stride = size // 8, size // 2
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # フィルタなし
        for x in range(size):
            transparent = blob[mask_offset + y * mask_stride + x // 8] >> (7 - x % 8) & 1
            packed = blob[color_offset + y * color_stride + x // 2]
            red, green, blue = _PALETTE[packed >> 4 if x % 2 == 0 else packed & 0x0F]
            rows += bytes((red, green, blue, 0 if transparent else 255))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8 ビット RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b"")
