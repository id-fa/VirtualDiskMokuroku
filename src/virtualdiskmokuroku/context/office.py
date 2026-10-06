"""Office ファイルのメタ情報(タイトル・作成者・更新日時など)。

docx/xlsx/pptx 系は ZIP 内の docProps を標準ライブラリだけで読む。doc/xls/ppt 系は olefile を使う。
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterable
from datetime import datetime
from xml.etree import ElementTree

from .base import EntryWriter, Extractor, module_available

_OOXML_EXTENSIONS = ("docx", "docm", "dotx", "xlsx", "xlsm", "xltx", "pptx", "pptm", "ppsx", "potx", "vsdx")
_OLE_EXTENSIONS = ("doc", "dot", "xls", "xlt", "ppt", "pps", "pot", "msg", "vsd")
_MAX_XML_BYTES = 4 * 1024 * 1024

# docProps/core.xml と app.xml の要素名(名前空間を除いたもの) → 保存キー
_CORE_KEYS = {
    "title": "title",
    "subject": "subject",
    "creator": "author",
    "lastModifiedBy": "last_modified_by",
    "created": "created",
    "modified": "modified",
    "keywords": "keywords",
    "description": "comments",
    "category": "category",
    "revision": "revision",
}
_APP_KEYS = {
    "Application": "application",
    "Company": "company",
    "Manager": "manager",
    "Pages": "pages",
    "Words": "words",
    "Slides": "slides",
}
# olefile のメタデータ属性 → 保存キー
_OLE_KEYS = (
    ("title", "title"),
    ("subject", "subject"),
    ("author", "author"),
    ("last_saved_by", "last_modified_by"),
    ("create_time", "created"),
    ("last_saved_time", "modified"),
    ("keywords", "keywords"),
    ("comments", "comments"),
    ("category", "category"),
    ("revision_number", "revision"),
    ("creating_application", "application"),
    ("company", "company"),
    ("manager", "manager"),
    ("num_pages", "pages"),
    ("num_words", "words"),
    ("slides", "slides"),
)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_props(archive: zipfile.ZipFile, member: str, keys: dict[str, str], writer: EntryWriter) -> None:
    try:
        info = archive.getinfo(member)
    except KeyError:
        return
    if info.file_size > _MAX_XML_BYTES:
        return
    root = ElementTree.fromstring(archive.read(info))
    for element in root:
        key = keys.get(_local_name(element.tag))
        if key and element.text:
            writer.add_meta(key, element.text)


class OfficeExtractor(Extractor):
    kind = "office"
    label = "Office メタ情報"
    description = "Word / Excel / PowerPoint のタイトル・作成者・更新日時などを保存します"
    default_params = {"extensions": [*_OOXML_EXTENSIONS, *_OLE_EXTENSIONS]}
    packages = "olefile"  # 旧形式 (doc/xls/ppt) にのみ必要

    def active_extensions(self) -> Iterable[str]:
        extensions = self.params.get("extensions", ())
        if module_available("olefile"):
            return extensions
        return [ext for ext in extensions if ext.lower().lstrip(".") not in _OLE_EXTENSIONS]

    def extract(self, path: str, writer: EntryWriter) -> None:
        with open(path, "rb") as f:
            signature = f.read(4)
        if signature[:2] == b"PK":
            self._extract_ooxml(path, writer)
        else:
            self._extract_ole(path, writer)

    def _extract_ooxml(self, path: str, writer: EntryWriter) -> None:
        with zipfile.ZipFile(path) as archive:
            _read_props(archive, "docProps/core.xml", _CORE_KEYS, writer)
            _read_props(archive, "docProps/app.xml", _APP_KEYS, writer)

    def _extract_ole(self, path: str, writer: EntryWriter) -> None:
        import olefile

        with olefile.OleFileIO(path) as ole:
            metadata = ole.get_metadata()
            codepage = getattr(metadata, "codepage", None)
            for attribute, key in _OLE_KEYS:
                value = getattr(metadata, attribute, None)
                if isinstance(value, bytes):
                    value = _decode(value, codepage)
                elif isinstance(value, datetime):
                    value = value.isoformat(sep=" ", timespec="seconds")
                if value not in (None, "", 0):
                    writer.add_meta(key, value)


def _decode(value: bytes, codepage: int | None) -> str:
    candidates = []
    if codepage == 65001:
        candidates.append("utf-8")
    elif codepage and codepage > 0:
        candidates.append(f"cp{codepage}")
    candidates += ["cp932", "latin-1"]
    for encoding in candidates:
        try:
            return value.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return value.decode("latin-1", "replace")
