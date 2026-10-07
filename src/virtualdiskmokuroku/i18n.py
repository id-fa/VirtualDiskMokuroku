"""UI 文言の翻訳。

ソース上の文言は日本語で書き、表示するときに ``tr()`` を通す。英語は ``locales/en.py`` の対応表
(日本語 → 英語) で引き、対応が無い文言は日本語のまま出す。言語は次の順で決まる:

1. 環境変数 ``VIRTUALDISKMOKUROKU_LANG`` (``ja`` / ``en``。テストや動作確認用)
2. ``set_language()`` で渡された値 (アプリ設定の「言語」)
3. 自動: Windows の表示言語が日本語なら日本語、それ以外は英語

``core`` からも使うので Qt には依存しない。
"""

from __future__ import annotations

import ctypes
import locale
import os
import sys

LANGUAGE_AUTO = "auto"
LANGUAGE_JA = "ja"
LANGUAGE_EN = "en"
LANGUAGES = (LANGUAGE_JA, LANGUAGE_EN)
ENV_VAR = "VIRTUALDISKMOKUROKU_LANG"

_language: str | None = None
_strings: dict[str, str] = {}


def _system_is_japanese() -> bool:
    if sys.platform == "win32":
        try:
            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()  # type: ignore[attr-defined]
            return (lang_id & 0x3FF) == 0x11  # LANG_JAPANESE
        except (AttributeError, OSError):
            pass
    name = (locale.getlocale()[0] or "").lower()
    return name.startswith(("ja", "japanese"))


def detect_language() -> str:
    """環境変数、無ければ OS の表示言語から言語を決める。"""
    override = os.environ.get(ENV_VAR, "").strip().lower()
    if override in LANGUAGES:
        return override
    return LANGUAGE_JA if _system_is_japanese() else LANGUAGE_EN


def set_language(language: str | None) -> str:
    """表示言語を決める。``None`` / ``"auto"`` なら自動判定。環境変数があればそちらを優先する。戻り値は決まった言語。"""
    global _language, _strings
    override = os.environ.get(ENV_VAR, "").strip().lower()
    if override in LANGUAGES:
        language = override
    elif language in (None, "", LANGUAGE_AUTO):
        language = detect_language()
    if language not in LANGUAGES:
        raise ValueError(f"対応していない言語です: {language}")
    if language == LANGUAGE_EN:
        from .locales.en import STRINGS

        _strings = STRINGS
    else:
        _strings = {}
    _language = language
    return language


def current_language() -> str:
    if _language is None:
        set_language(None)
    return _language or LANGUAGE_JA


def tr(text: str) -> str:
    """文言を表示言語に訳す。対応が無ければそのまま返す。"""
    if _language is None:
        set_language(None)
    return _strings.get(text, text)
