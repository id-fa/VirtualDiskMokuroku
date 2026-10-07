"""Virtual CD-ROM Case のカタログ (.cas) の読み取り (アルファ版。形式は調査中)。

対象は圧縮なしで保存した .cas だけ。Zlib で圧縮した .cas や .caz (LHA) は読めないので、Virtual CD-ROM Case の
オプションで圧縮をオフにして保存し直してもらう (README の「Virtual CD-ROM Case のカタログのインポート」を参照)。

.cas は MFC の CArchive で書かれたバイナリで、仕様は公開されていない。以下は実際のファイル (スキーマ 12) を
CSV エクスポートと突き合わせて分かった構造で、意味の分からない部分は読み飛ばしている。整数はリトルエンディアン、
日時は FILETIME (UTC)、文字列は MFC の CString 形式 (長さ 1 バイト。0xFF なら続く 2 バイト、それも 0xFFFF なら
続く 4 バイトが長さ) で、文字コードは ANSI (日本語環境では CP932)::

    ヘッダ    FF FF, スキーマ (u16), クラス名の長さ (u16), "CCDCase", 不明 12 バイト
    レコード  属性 (u32), サイズ (u64), 属性 (u32。同じ値), 書庫の種類 (u16), アクセス・更新・作成日時 (u64 × 3), 名前,
              文字列の数 (u8。常に 2) とその文字列 (コメント, 分類),
              プロパティの数 (u8) とその文字列 (先頭は "HTML" "MODULE" などの種類。1 つ以上あれば、数に含まれない
              文字列がもう 1 つ続く),
              不明 7 バイト, CRC の有無 (u16), CRC32 (u32), 子の数 (u32), 子レコード…,
              書庫内のエントリなら 圧縮後のサイズ (u32), 不明 1 バイト,
              ドライブかグループなら ラベル, 不明の文字列, シリアル (u32), ファイルシステム, 容量 (u64), 空き (u64),
                           短いラベル, クラスタあたりのセクタ数 (u32), セクタサイズ (u32)

属性の下位は Windows のファイル属性で、上位バイトが種別を表す::

    0x20  カタログ自身 (先頭のレコード。その子がドライブかグループ)
    0x10  ドライブ。下位 4 ビットはメディアの種類 (1 = CD/DVD、8 = ディスク)。名前は空
    0x40  中身を展開して登録した書庫 (フォルダ属性が立ち、子に中身を持つ)。書庫の種類は 1 = LZH, 2 = ZIP, 6 = RAR など
    0x80  書庫内のエントリ (書庫内のフォルダは 0xC0)

ドライブをまとめるグループ (フォルダ) は、属性に 0x00200000 が立つ名前付きのレコードで、子にドライブを持つ。
ドライブと同じ形式の情報が後ろに続くが、中身はグループ名だけで、容量と空きは全ビット 1 になっている。

ドライブレターは記録されていない。日時は 0 のほか、1970 年より前や範囲外の値が入っていることがある (いずれも「不明」)。
コメントやプロパティは、Virtual CD-ROM Case がファイルから読んだバイト列のまま入っている
(UTF-8 の HTML のタイトルは UTF-8 のまま) ので、``decode_text`` で文字コードを推定して読む。
"""

from __future__ import annotations

import re
import struct
from collections.abc import Callable
from dataclasses import dataclass, field

from .errors import CatalogError, ScanCancelled
from ..i18n import tr

SUPPORTED_SCHEMA = 12
DEFAULT_ENCODING = "cp932"

_NOT_A_CASE = (
    "圧縮なしで保存した Virtual CD-ROM Case のカタログ (.cas) ではありません。"
    "圧縮して保存した .cas や .caz は、Virtual CD-ROM Case で圧縮をオフにして .cas 形式で保存し直してください"
)
_CLASS_NAME = b"CCDCase"
_CLASS_HEAD = struct.Struct("<HHH")
_HEADER_UNKNOWN = 12
_RECORD_HEAD = struct.Struct("<IQIH3Q")
_RECORD_UNKNOWN = 7
_RECORD_TAIL = struct.Struct("<HII")
_MEMBER_TAIL = struct.Struct("<IB")
_DRIVE_VOLUME = struct.Struct("<QQ")
_DRIVE_TAIL = struct.Struct("<II")
_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
_MIN_RECORD_SIZE = _RECORD_HEAD.size + 3 + _RECORD_UNKNOWN + _RECORD_TAIL.size

_KIND_MEMBER = 0x80000000
_KIND_ARCHIVE = 0x40000000
_KIND_DRIVE = 0x10000000
_KIND_GROUP = 0x00200000
_MEDIA_SHIFT, _MEDIA_MASK = 24, 0x0F
_ATTRIBUTE_MASK = 0x00FFFFFF & ~_KIND_GROUP
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_MIN_FILETIME = 116444736000000000  # 1970-01-01。これより前は Virtual CD-ROM Case でも「不明」と表示される
_MAX_FILETIME = 2650467743999999999  # 9999-12-31
_CANCEL_CHECK_INTERVAL = 20000

# プロパティの並び (種類ごと)。CSV エクスポートの列と突き合わせて分かったものと、そこから類推できるものだけ
_PROPERTY_KEYS: dict[str, dict[int, str]] = {
    "HTML": {1: "title", 2: "subject", 3: "keywords", 5: "application"},
    "MODULE": {
        1: "file_version", 2: "product_version", 3: "target_os", 4: "module_type", 5: "comments",
        6: "company", 7: "description", 8: "copyright", 9: "trademarks", 10: "product_name",
    },
    # AVI などの RIFF の INFO チャンク。14 は 5 と同じ値が入っている
    "Audio": {
        1: "title", 3: "artist", 4: "tag_format", 5: "comments", 7: "application", 10: "created",
        14: "comments", 17: "copyright", 22: "source",
    },
    "PDF-P": {
        1: "title", 2: "subject", 3: "keywords", 4: "author", 5: "creator", 6: "application",
        7: "created", 8: "modified", 9: "pages",
    },
}  # fmt: skip
_PDF_DATE = re.compile(r"(?:D:)?(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})")


@dataclass(slots=True)
class CaseEntry:
    """ドライブ内のファイルまたはフォルダ 1 件。"""

    path: str  # ドライブのルートからの相対パス (区切りは "\\")
    is_dir: bool
    size: int | None
    mtime: int | None  # FILETIME
    ctime: int | None
    attrs: int
    comment: bytes = b""  # 保存されていたままのバイト列
    properties: list[bytes] = field(default_factory=list)  # 先頭は種類 ("HTML" など)
    crc: int | None = None  # CRC32 (登録時に計算していた場合)
    # 中身を展開して登録した書庫の内部エントリ: (パス, サイズ, 更新日時, フォルダか)。パスの区切りは "/"
    inner: list[tuple[str, int | None, int | None, bool]] = field(default_factory=list)
    category: str = ""  # Virtual CD-ROM Case で付けた分類

    @property
    def has_context(self) -> bool:
        return bool(self.comment or self.properties or self.inner or self.category) or self.crc is not None


@dataclass(slots=True)
class CaseDrive:
    label: str  # Virtual CD-ROM Case での表示名でもある
    serial: str  # "1A2B-3C4D"。不明なら空
    filesystem: str
    total_bytes: int
    free_bytes: int
    media_type: int  # 1 = CD/DVD、8 = ディスク (ほかの値は未確認)
    cluster_size: int
    comment: str  # 既定では登録した日付 ("2026/10/06") が入っている
    category: str = ""  # Virtual CD-ROM Case で付けた分類
    group: tuple[str, ...] = ()  # ドライブがグループ (フォルダ) に入っていた場合の、上位のグループ名 (外側から順)
    group_comments: tuple[str, ...] = ()  # 各グループのコメント (group と同じ並び)
    entries: list[CaseEntry] = field(default_factory=list)


@dataclass(slots=True)
class CaseFile:
    schema: int
    name: str  # 保存時のファイルパス
    drives: list[CaseDrive] = field(default_factory=list)


@dataclass(slots=True)
class _Frame:
    """読みかけの親レコード。"""

    remaining: int  # まだ読んでいない子の数
    has_info: bool = False  # 子の後ろにドライブ情報が続く (ドライブとグループ)
    is_member: bool = False  # 子の後ろに書庫内エントリの情報が続く
    media_type: int = 0
    new_drive: CaseDrive | None = None  # このレコードで始まったドライブ (ドライブ情報の書き込み先)
    drive: CaseDrive | None = None  # 子が属するドライブ
    comment: bytes = b""
    category: bytes = b""
    group: tuple[str, ...] = ()
    group_comments: tuple[str, ...] = ()
    prefix: str = ""  # 子のパスの前に付ける文字列
    inner_owner: CaseEntry | None = None  # 書庫の中を読んでいる場合、その書庫のエントリ


class _Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def unpack(self, layout: struct.Struct) -> tuple:
        values = layout.unpack_from(self.data, self.pos)
        self.pos += layout.size
        return values

    def byte(self) -> int:
        value = self.data[self.pos]
        self.pos += 1
        return value

    def take(self, size: int) -> bytes:
        end = self.pos + size
        if end > len(self.data):
            raise IndexError(tr('データが途中で終わっています'))
        chunk = self.data[self.pos : end]
        self.pos = end
        return chunk

    def string(self) -> bytes | str:
        """CString を読む。ANSI ならバイト列のまま、Unicode (FF FE FF で始まる) なら文字列で返す。"""
        length = self.byte()
        wide = False
        if length == 0xFF:
            (length,) = self.unpack(_U16)
            if length == 0xFFFE:
                wide = True
                length = self.byte()
                if length == 0xFF:
                    (length,) = self.unpack(_U16)
            if length == 0xFFFF:
                (length,) = self.unpack(_U32)
        if wide:
            return self.take(length * 2).decode("utf-16-le", errors="replace")
        return self.take(length)


def _text(value: bytes | str, encoding: str) -> str:
    return value if isinstance(value, str) else value.decode(encoding, errors="replace")


def _raw(value: bytes | str) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else value


def _filetime(value: int) -> int | None:
    return value if _MIN_FILETIME <= value <= _MAX_FILETIME else None


def decode_text(raw: bytes) -> tuple[str, str]:
    """コメントやプロパティのバイト列を文字列にする。戻り値は (文字コード名, 文字列)。

    UTF-8 として読めれば UTF-8 (長さ制限で末尾の文字が切れている場合を含む)、そうでなければ CP932 とみなす。
    """
    if raw.isascii():
        return DEFAULT_ENCODING, raw.decode("ascii")
    try:
        return "utf-8", raw.decode("utf-8")
    except UnicodeDecodeError as error:
        if error.start > 0 and error.start >= len(raw) - 3 and error.end == len(raw):
            try:
                return "utf-8", raw[: error.start].decode("utf-8")
            except UnicodeDecodeError:
                pass
    return DEFAULT_ENCODING, raw.decode(DEFAULT_ENCODING, errors="replace")


def property_items(properties: list[bytes]) -> list[tuple[str, str]]:
    """プロパティを (キー, 値) の並びにする。位置の意味が分からない値は ``property`` というキーで返す。"""
    if not properties:
        return []
    kind = properties[0].decode("ascii", errors="replace")
    keys = _PROPERTY_KEYS.get(kind, {})
    items = [("property_type", kind)] if kind else []
    for index, raw in enumerate(properties[1:], 1):
        value = decode_text(raw)[1].strip()
        if not value:
            continue
        key = keys.get(index, "property")
        if key in ("created", "modified"):
            match = _PDF_DATE.match(value)  # PDF の日付 "20020808133904+09'00'"
            if match is not None:
                value = "{}/{}/{} {}:{}:{}".format(*match.groups())
        if (key, value) not in items:
            items.append((key, value))
    return items


def parse_case(
    data: bytes, *, encoding: str = DEFAULT_ENCODING, is_cancelled: Callable[[], bool] | None = None
) -> CaseFile:
    """.cas の中身を読む。``encoding`` はファイル名などの文字コード。読めない場合は ``CatalogError``。"""
    reader = _Reader(data)
    header = data[: _CLASS_HEAD.size + len(_CLASS_NAME)]
    if len(header) < _CLASS_HEAD.size or not header.endswith(_CLASS_NAME):
        raise CatalogError(tr(_NOT_A_CASE))
    tag, schema, name_length = reader.unpack(_CLASS_HEAD)
    if tag != 0xFFFF or name_length != len(_CLASS_NAME):
        raise CatalogError(tr(_NOT_A_CASE))
    try:
        reader.take(name_length + _HEADER_UNKNOWN)
        case = _parse_records(reader, schema, encoding, is_cancelled)
        if reader.pos != len(data):
            raise IndexError(tr('末尾に読み残しがあります'))
        return case
    except (struct.error, IndexError) as error:
        message = tr('Virtual CD-ROM Case のカタログを読み取れません (位置 0x{pos:X} 付近で構造が合いません)').format(pos=reader.pos)
        if schema != SUPPORTED_SCHEMA:
            message += tr('。形式バージョン {schema} には対応していません (確認済みは {SUPPORTED_SCHEMA})').format(schema=schema, SUPPORTED_SCHEMA=SUPPORTED_SCHEMA)
        raise CatalogError(message) from error


def read_case(path, *, encoding: str = DEFAULT_ENCODING, is_cancelled: Callable[[], bool] | None = None) -> CaseFile:
    with open(path, "rb") as stream:
        data = stream.read()
    return parse_case(data, encoding=encoding, is_cancelled=is_cancelled)


def _parse_records(reader: _Reader, schema: int, encoding: str, is_cancelled) -> CaseFile:
    data_size = len(reader.data)
    count = 0

    def read_record() -> tuple:
        nonlocal count
        count += 1
        if is_cancelled is not None and count % _CANCEL_CHECK_INTERVAL == 0 and is_cancelled():
            raise ScanCancelled()
        _same, size, kind, archive_type, _atime, mtime, ctime = reader.unpack(_RECORD_HEAD)
        name = reader.string()
        strings = [reader.string() for _ in range(reader.byte())]
        properties = [_raw(reader.string()) for _ in range(reader.byte())]
        if properties:
            properties.append(_raw(reader.string()))
        reader.take(_RECORD_UNKNOWN)
        has_crc, crc, children = reader.unpack(_RECORD_TAIL)
        if children * _MIN_RECORD_SIZE > data_size - reader.pos:
            raise IndexError(tr('子の数が不正です'))
        comment = _raw(strings[0]) if strings else b""
        category = _raw(strings[1]) if len(strings) > 1 else b""
        return (
            size, kind, archive_type, _filetime(mtime), _filetime(ctime), name, comment, category, properties,
            crc if has_crc else None, children,
        )  # fmt: skip

    top = read_record()
    case = CaseFile(schema, _text(top[5], encoding))
    stack = [_Frame(top[10])]
    while stack:
        frame = stack[-1]
        if frame.remaining == 0:
            stack.pop()
            if frame.is_member:
                reader.unpack(_MEMBER_TAIL)
            if frame.has_info:
                _read_drive_info(reader, frame, encoding)
                if frame.new_drive is not None:
                    case.drives.append(frame.new_drive)
            continue
        frame.remaining -= 1
        size, kind, archive_type, mtime, ctime, raw_name, comment, category, properties, crc, children = read_record()
        name = _text(raw_name, encoding)
        is_drive = bool(kind & _KIND_DRIVE)
        has_info = bool(kind & (_KIND_DRIVE | _KIND_GROUP))
        attrs = kind & _ATTRIBUTE_MASK
        is_dir = bool(attrs & _FILE_ATTRIBUTE_DIRECTORY)
        child = _Frame(children, has_info, bool(kind & _KIND_MEMBER))

        if frame.inner_owner is not None:
            # 中身を展開して登録した書庫の中。書庫のエントリに内部リストとしてぶら下げる
            if archive_type:
                is_dir = False  # 書庫の中の書庫
            path = frame.prefix + name
            frame.inner_owner.inner.append((path, None if is_dir else size, mtime, is_dir))
            child.inner_owner, child.prefix = frame.inner_owner, path + "/"
        elif frame.drive is None:
            if is_drive:
                child.drive = child.new_drive = CaseDrive(
                    "", "", "", 0, 0, 0, 0, "", group=frame.group, group_comments=frame.group_comments
                )
                child.comment, child.category = comment, category
                child.media_type = (kind >> _MEDIA_SHIFT) & _MEDIA_MASK
            else:  # ドライブをまとめるグループ
                child.group, child.group_comments = frame.group, frame.group_comments
                if name:
                    child.group = (*frame.group, name)
                    child.group_comments = (*frame.group_comments, decode_text(comment)[1].strip())
        elif name:
            if kind & _KIND_ARCHIVE:
                # 書庫はフォルダとして記録されているが、サイズ・日時は書庫ファイルのもの。1 つのファイルとして扱う
                is_dir = False
                attrs &= ~_FILE_ATTRIBUTE_DIRECTORY
            entry = CaseEntry(
                frame.prefix + name, is_dir, None if is_dir else size, mtime, ctime, attrs, comment, properties, crc,
                category=decode_text(category)[1].strip(),
            )  # fmt: skip
            frame.drive.entries.append(entry)
            child.drive = frame.drive
            if is_dir:
                child.prefix = entry.path + "\\"
            else:
                child.inner_owner = entry
        else:
            child.drive, child.prefix = frame.drive, frame.prefix
        if children or has_info:
            stack.append(child)
        elif child.is_member:
            reader.unpack(_MEMBER_TAIL)
    return case


def _read_drive_info(reader: _Reader, frame: _Frame, encoding: str) -> None:
    label = _text(reader.string(), encoding)
    reader.string()
    (serial,) = reader.unpack(_U32)
    filesystem = _text(reader.string(), encoding)
    total_bytes, free_bytes = reader.unpack(_DRIVE_VOLUME)
    short_label = _text(reader.string(), encoding)  # 長いラベルは 16 文字で切られている
    cluster_sectors, sector_size = reader.unpack(_DRIVE_TAIL)
    drive = frame.new_drive
    if drive is None:  # グループの情報 (中身はグループ名だけ)。読み飛ばす
        return
    drive.label = label or short_label
    drive.serial = f"{serial >> 16:04X}-{serial & 0xFFFF:04X}" if serial else ""
    drive.filesystem = filesystem
    drive.total_bytes, drive.free_bytes = total_bytes, free_bytes
    drive.media_type, drive.cluster_size = frame.media_type, cluster_sectors * sector_size
    drive.comment = decode_text(frame.comment)[1].strip()
    drive.category = decode_text(frame.category)[1].strip()
