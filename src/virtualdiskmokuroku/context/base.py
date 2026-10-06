"""拡張コンテキスト抽出器の共通インターフェース。

抽出器はファイル 1 件を読み、結果を ``EntryWriter`` に積む。DB への書き込みと例外処理は runner が行う。
依存ライブラリは ``extract`` の中で遅延 import し、未導入の環境でもパッケージ全体は import できるようにする。
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterable
from datetime import datetime

_FILETIME_EPOCH_OFFSET = 116444736000000000  # 1601-01-01 から 1970-01-01 までの 100ns 単位


def unix_to_filetime(seconds: float | None) -> int | None:
    """UNIX 時刻(秒)を FILETIME に変換する。"""
    if seconds is None:
        return None
    try:
        return int(seconds * 10_000_000) + _FILETIME_EPOCH_OFFSET
    except (OverflowError, ValueError):
        return None


def datetime_to_filetime(moment: datetime | None) -> int | None:
    """datetime を FILETIME に変換する(タイムゾーン無しはローカル時刻とみなす)。"""
    if moment is None:
        return None
    try:
        return unix_to_filetime(moment.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


def module_available(*modules: str) -> bool:
    try:
        return all(importlib.util.find_spec(module) is not None for module in modules)
    except (ImportError, ValueError):
        return False


def normalize_extensions(extensions: Iterable[str]) -> tuple[str, ...]:
    """拡張子リストを ``str.endswith`` に渡せる形 (".ext" の小文字タプル) にする。"""
    return tuple(sorted({"." + ext.strip().lower().lstrip(".") for ext in extensions if ext and ext.strip(". ")}))


class EntryWriter:
    """抽出器 1 回分(1 ファイル × 1 種別)の結果を受け取るバッファ。"""

    __slots__ = ("entry_id", "kind", "meta", "text", "thumb", "inner")

    def __init__(self, entry_id: int, kind: str):
        self.entry_id = entry_id
        self.kind = kind
        self.meta: list[tuple[str, str]] = []
        self.text: tuple[str, str, bytes | None] | None = None  # (encoding, content, raw)
        self.thumb: tuple[int, int, bytes] | None = None  # (width, height, JPEG)
        self.inner: list[tuple[str, int | None, int | None, int]] = []  # (path, size, mtime, is_dir)

    def add_meta(self, key: str, value: object) -> None:
        """メタ情報を 1 項目追加する。空の値は無視する。"""
        if value is None:
            return
        text = str(value).replace("\0", "").strip()
        if text:
            self.meta.append((key, text))

    def set_text(self, encoding: str, content: str, raw: bytes | None = None) -> None:
        self.text = (encoding, content, raw)

    def set_thumb(self, width: int, height: int, data: bytes) -> None:
        self.thumb = (width, height, data)

    def add_inner(self, path: str, size: int | None, mtime: int | None, is_dir: bool) -> None:
        """書庫/ISO 内のエントリを 1 件追加する。``path`` の区切りは ``/``、``mtime`` は FILETIME。"""
        self.inner.append((path, size, mtime, int(bool(is_dir))))

    @property
    def is_empty(self) -> bool:
        return not (self.meta or self.text or self.thumb or self.inner)


class Extractor:
    kind: str = ""
    label: str = ""  # 設定画面に出す日本語名
    description: str = ""
    default_params: dict = {}
    # 抽出結果の保存先 ("ctx" / "text" / "thumb" / "inner")。前回結果の引き継ぎで読むテーブルを決める
    stores: tuple[str, ...] = ("ctx",)
    # 保存する内容を変えたら上げる。前回と違う場合は引き継がずに読み直す(パラメータ変更時と同じ扱い)
    revision: int = 1
    # 必須の依存 (import 名) と、不足時に案内する pip パッケージ名
    required_modules: tuple[str, ...] = ()
    packages: str = ""

    def __init__(self, params: dict | None = None):
        self.params = {**self.default_params, **(params or {})}
        self.params.pop("enabled", None)
        self._suffixes = normalize_extensions(self.active_extensions())

    def active_extensions(self) -> Iterable[str]:
        """実際に対象とする拡張子(依存が無くて扱えないものは除く)。"""
        return self.params.get("extensions", ())

    @classmethod
    def available(cls) -> bool:
        """依存ライブラリが揃っていて利用できるか。"""
        return module_available(*cls.required_modules)

    @classmethod
    def requirement(cls) -> str:
        """不足時に表示する pip パッケージ名。"""
        return cls.packages

    def accepts(self, name: str, size: int | None) -> bool:
        return bool(self._suffixes) and name.lower().endswith(self._suffixes)

    def extract(self, path: str, writer: EntryWriter) -> None:
        """``path`` を読んで結果を ``writer`` に積む。失敗は例外で知らせる(runner が記録する)。"""
        raise NotImplementedError
