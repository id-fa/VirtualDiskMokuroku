"""表示言語 (i18n) のテスト。"""

import ast
import os
import re
from pathlib import Path

import pytest

from virtualdiskmokuroku import i18n
from virtualdiskmokuroku.locales.en import STRINGS

SRC = Path(__file__).resolve().parents[1] / "src" / "virtualdiskmokuroku"
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_UNTRANSLATED = {"日本語"}  # 言語の選択肢はその言語の名前で出す (訳さない)


def _is_japanese(text: str) -> bool:
    return any("぀" <= ch <= "ヿ" or "一" <= ch <= "鿿" for ch in text)


def _source_keys() -> tuple[dict[str, str], list[str]]:
    """ソース中の tr() の引数と、tr() の外にある日本語リテラル (関数の中のもの = 訳し忘れ)。"""
    keys: dict[str, str] = {}
    unwrapped: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "locales" in path.parts or path.name == "i18n.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = set()
        parents: dict[int, ast.AST] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                first = node.body[0] if node.body else None
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docstrings.add(id(first.value))
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tr" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    keys[arg.value] = f"{path.name}:{node.lineno}"
                continue
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str) and _is_japanese(node.value)):
                continue
            if id(node) in docstrings or node.value in _UNTRANSLATED:
                continue
            parent = parents.get(id(node))
            if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and parent.func.id == "tr":
                continue
            current = node
            import_time = False
            while (parent := parents.get(id(current))) is not None:
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    break
                if isinstance(parent, (ast.Module, ast.ClassDef)):
                    import_time = True
                    break
                current = parent
            if import_time:
                keys[node.value] = f"{path.name}:{node.lineno} (定数表。表示時に tr() を通す)"
            else:
                unwrapped.append(f"{path.name}:{node.lineno}: {node.value!r}")
    return keys, unwrapped


def test_every_japanese_string_is_translatable():
    keys, unwrapped = _source_keys()
    assert unwrapped == [], "tr() で包まれていない日本語の文言があります"
    missing = [f"{where}: {key!r}" for key, where in keys.items() if key not in STRINGS]
    assert missing == [], "locales/en.py に英語が無い文言があります"


def test_english_strings_keep_placeholders_and_have_no_stale_keys():
    keys, _unwrapped = _source_keys()
    stale = [key for key in STRINGS if key not in keys]
    assert stale == [], "ソースに無くなった文言が locales/en.py に残っています"
    for key, english in STRINGS.items():
        assert english and not _is_japanese(english), key
        expected = sorted(_PLACEHOLDER.findall(key))
        assert sorted(_PLACEHOLDER.findall(english)) == expected, f"プレースホルダが一致しません: {key!r}"
        # メニューのアクセスキーはどちらにも 1 つ (無いなら無い)
        assert ("&" in key) == ("&" in english), f"アクセスキーの有無が違います: {key!r}"


def test_language_detection_and_switching(monkeypatch):
    monkeypatch.delenv(i18n.ENV_VAR, raising=False)
    monkeypatch.setattr(i18n, "_system_is_japanese", lambda: True)
    assert i18n.detect_language() == "ja"
    monkeypatch.setattr(i18n, "_system_is_japanese", lambda: False)
    assert i18n.detect_language() == "en"
    monkeypatch.setenv(i18n.ENV_VAR, "ja")
    assert i18n.detect_language() == "ja"

    # 環境変数は設定より優先。無ければ設定 (auto は自動判定)
    assert i18n.set_language("en") == "ja"
    monkeypatch.delenv(i18n.ENV_VAR)
    assert i18n.set_language("en") == "en" and i18n.current_language() == "en"
    assert i18n.tr("キャンセル") == "Cancel"
    assert i18n.tr("対応表に無い文言") == "対応表に無い文言"
    assert i18n.set_language("auto") == "en"
    assert i18n.set_language("ja") == "ja" and i18n.tr("キャンセル") == "キャンセル"
    with pytest.raises(ValueError):
        i18n.set_language("fr")
    # 後続のテストのために日本語へ戻す (conftest の環境変数と同じ)
    monkeypatch.setenv(i18n.ENV_VAR, os.environ.get(i18n.ENV_VAR, "ja"))
    i18n.set_language(None)
