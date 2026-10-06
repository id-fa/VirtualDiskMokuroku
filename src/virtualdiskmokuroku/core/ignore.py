"""カタログに収蔵しないファイル/フォルダ名のルール。

パターンの書式:
  - ``name``        ファイル・フォルダどちらの名前にも一致
  - ``name\\``      フォルダ名のみに一致(配下ごと除外)
  - ``*`` / ``?``   ワイルドカード
  - 途中に区切りを含むもの (``Windows\\Temp\\``) はルートからの相対パスに一致
大文字小文字は区別しない。
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable

DEFAULT_IGNORE = ["System Volume Information\\", "$RECYCLE.BIN\\"]

_WILDCARDS = re.compile(r"[*?\[]")


class IgnoreRules:
    def __init__(self, patterns: Iterable[str] = ()):
        self.patterns = [p.strip() for p in patterns if p and p.strip()]
        self._dir_names: set[str] = set()
        self._any_names: set[str] = set()
        self._dir_name_re: list[re.Pattern[str]] = []
        self._any_name_re: list[re.Pattern[str]] = []
        self._dir_path_re: list[re.Pattern[str]] = []
        self._any_path_re: list[re.Pattern[str]] = []
        for pattern in self.patterns:
            self._add(pattern)
        # 相対パスに一致させるルールがあるか(無ければ呼び出し側は relpath を省略できる)
        self.uses_paths = bool(self._dir_path_re or self._any_path_re)

    def _add(self, pattern: str) -> None:
        normalized = pattern.replace("/", "\\").casefold()
        dir_only = normalized.endswith("\\")
        body = normalized.strip("\\")
        if not body:
            return
        if "\\" in body:
            compiled = re.compile(fnmatch.translate(body))
            (self._dir_path_re if dir_only else self._any_path_re).append(compiled)
        elif _WILDCARDS.search(body):
            compiled = re.compile(fnmatch.translate(body))
            (self._dir_name_re if dir_only else self._any_name_re).append(compiled)
        else:
            (self._dir_names if dir_only else self._any_names).add(body)

    def __bool__(self) -> bool:
        return bool(self.patterns)

    def matches(self, name: str, is_dir: bool, relpath: str | None = None) -> bool:
        """``name`` は要素名、``relpath`` はスキャンルートからの相対パス(区切りは ``\\``)。"""
        folded = name.casefold()
        if folded in self._any_names or any(r.match(folded) for r in self._any_name_re):
            return True
        if is_dir and (folded in self._dir_names or any(r.match(folded) for r in self._dir_name_re)):
            return True
        if self.uses_paths and relpath is not None:
            folded_path = relpath.casefold()
            if any(r.match(folded_path) for r in self._any_path_re):
                return True
            if is_dir and any(r.match(folded_path) for r in self._dir_path_re):
                return True
        return False
