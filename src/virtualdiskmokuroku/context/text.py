"""小さなテキストファイルの中身そのもの。

文字コードは BOM → UTF-8(厳密) → charset-normalizer の順で判定する。誤判定に備えて生データも保存し、
後から ``context_db.redecode_text`` で文字コードを指定してデコードし直せるようにする。
"""

from __future__ import annotations

import codecs
import locale

from .base import EntryWriter, Extractor, module_available
from ..i18n import tr

_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),  # UTF-16 LE の BOM と前方一致するので先に調べる
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def _codec_name(encoding: str) -> str:
    try:
        return codecs.lookup(encoding).name
    except LookupError:
        return encoding.lower()


def _system_encoding() -> str:
    """OS の既定 (ANSI) 文字コード。日本語 Windows なら cp932。"""
    try:
        return _codec_name(locale.getpreferredencoding(False))
    except (LookupError, ValueError):
        return ""


def _try_decode(raw: bytes, encoding: str) -> str | None:
    try:
        return raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return None


def _guess_bomless_utf16(raw: bytes) -> str | None:
    """BOM の無い UTF-16 を NUL バイトの偏りから推定する(ASCII 主体のテキスト向け)。"""
    if len(raw) < 4 or len(raw) % 2:
        return None
    half = len(raw) // 2
    if raw[1::2].count(0) > half * 0.6 and raw[0::2].count(0) < half * 0.1:
        return "utf-16-le"
    if raw[0::2].count(0) > half * 0.6 and raw[1::2].count(0) < half * 0.1:
        return "utf-16-be"
    return None


def detect_and_decode(raw: bytes) -> tuple[str, str]:
    """バイト列の文字コードを判定してデコードする。戻り値は (文字コード名, 文字列)。"""
    if not raw:
        return "utf-8", ""
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            text = _try_decode(raw, encoding)
            if text is not None:
                return encoding, text

    # ASCII 主体の BOM 無し UTF-16 は NUL 入りの UTF-8 としても通ってしまうので先に調べる
    encoding = _guess_bomless_utf16(raw)
    if encoding:
        text = _try_decode(raw, encoding)
        if text is not None:
            return encoding, text

    text = _try_decode(raw, "utf-8")
    if text is not None:
        return "utf-8", text

    if module_available("charset_normalizer"):
        import charset_normalizer

        matches = list(charset_normalizer.from_bytes(raw))
        # 候補に OS 既定の文字コードがあればそれを優先する(短い日本語テキストの誤判定を減らす)
        system = _system_encoding()
        if system and _try_decode(raw, system) is not None:
            for match in matches:
                if system in {_codec_name(name) for name in (match.encoding, *match.could_be_from_charset)}:
                    return system, raw.decode(system)
        if matches:
            best = matches[0]
            text = _try_decode(raw, best.encoding)
            if text is not None:
                return _codec_name(best.encoding), text.removeprefix("﻿")

    text = _try_decode(raw, "cp932")
    if text is not None:
        return "cp932", text
    return "latin-1", raw.decode("latin-1")


class TextExtractor(Extractor):
    kind = "text"
    label = "テキスト本文"
    description = "小さなテキストファイル(既定 4KB まで)の中身を保存します"
    default_params = {
        "max_bytes": 4096,
        "extensions": [
            "txt", "md", "log", "ini", "cfg", "conf", "csv", "tsv", "json", "xml", "yml", "yaml", "toml",
            "bat", "cmd", "ps1", "sh", "py", "js", "ts", "html", "htm", "css", "c", "h", "cpp", "hpp",
            "cs", "java", "go", "rs", "sql", "nfo", "diz", "cue", "m3u", "m3u8", "srt", "ass", "url",
            "reg", "inf", "rst", "tex", "description",
        ],  # fmt: skip
    }
    stores = ("ctx", "text")
    packages = "charset-normalizer"  # 無くても動作する(判定精度が下がる)

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self._max_bytes = max(0, int(self.params.get("max_bytes", 4096)))

    def accepts(self, name: str, size: int | None) -> bool:
        return size is not None and size <= self._max_bytes and super().accepts(name, size)

    def extract(self, path: str, writer: EntryWriter) -> None:
        with open(path, "rb") as f:
            raw = f.read(self._max_bytes + 1)
        if len(raw) > self._max_bytes:
            raise ValueError(tr('スキャン後にファイルが大きくなっています'))
        encoding, content = detect_and_decode(raw)
        writer.set_text(encoding, content.removeprefix("﻿"), raw)
