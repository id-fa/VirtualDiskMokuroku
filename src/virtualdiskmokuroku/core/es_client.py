"""Everything のコマンドラインインターフェース es.exe のラッパー。"""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .errors import ScanCancelled

FILE_ATTRIBUTE_DIRECTORY = 0x10

# es.exe の終了コード
_ES_EXIT_MESSAGES = {
    1: "ウィンドウクラスの登録に失敗しました",
    2: "待受ウィンドウの作成に失敗しました",
    3: "メモリ不足です",
    4: "コマンドラインオプションの引数が不足しています",
    5: "エクスポート先ファイルを作成できません",
    6: "不明なオプションです",
    7: "Everything への問い合わせ送信に失敗しました",
    8: "Everything が起動していません(IPC ウィンドウが見つかりません)",
}
_ES_EXIT_NOT_RUNNING = 8

_CREATE_NO_WINDOW = 0x08000000


class EsError(Exception):
    """es.exe の実行に失敗した。"""


class EsNotFoundError(EsError):
    """es.exe が見つからない。"""


class EverythingNotRunningError(EsError):
    """Everything 本体が起動していない、または IPC に接続できない。"""


@dataclass(slots=True)
class RawEntry:
    """スキャン元から得た 1 件分のファイル情報。``path`` は末尾区切りなしのフルパス。"""

    path: str
    is_dir: bool
    size: int | None = None
    mtime: int | None = None  # FILETIME (100ns, 1601-01-01 UTC 起点)
    ctime: int | None = None
    attrs: int | None = None


def find_es_exe(configured: str | os.PathLike[str] | None = None) -> Path | None:
    """es.exe を探す。設定値 → PATH → アプリ近傍 → 既定のインストール先の順。"""
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    on_path = shutil.which("es.exe")
    if on_path:
        candidates.append(Path(on_path))
    app_dir = Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] else Path.cwd()
    for base in (app_dir, Path.cwd()):
        candidates.append(base / "es.exe")
        candidates.append(base / "everything_portable" / "es.exe")
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(env)
        if root:
            candidates.append(Path(root) / "Everything" / "es.exe")
            candidates.append(Path(root) / "Everything 1.5a" / "es.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _to_int(value: str) -> int | None:
    if not value:
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    # 未取得の値は -1 / 0xFFFFFFFFFFFFFFFF で返ることがある
    if number < 0 or number >= 1 << 63:
        return None
    return number


def parse_export_csv(csv_path: str | os.PathLike[str]) -> Iterator[RawEntry]:
    """``-export-csv`` の出力 (UTF-8) を読み、RawEntry を順に返す。"""
    with open(csv_path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return
        columns = {name.strip().casefold(): index for index, name in enumerate(header)}
        i_name = columns.get("filename", 0)
        i_size = columns.get("size")
        i_mtime = columns.get("date modified")
        i_ctime = columns.get("date created")
        i_attrs = columns.get("attributes")

        def field(row: list[str], index: int | None) -> int | None:
            if index is None or index >= len(row):
                return None
            return _to_int(row[index])

        for row in reader:
            if not row:
                continue
            path = row[i_name]
            if not path:
                continue
            attrs = field(row, i_attrs)
            is_dir = path.endswith("\\") or bool(attrs is not None and attrs & FILE_ATTRIBUTE_DIRECTORY)
            yield RawEntry(
                path=path.rstrip("\\"),
                is_dir=is_dir,
                size=field(row, i_size),
                mtime=field(row, i_mtime),
                ctime=field(row, i_ctime),
                attrs=attrs,
            )


def _search_root(root: str) -> str:
    """es.exe の ``-path`` に渡す形へ正規化する(ドライブ直下のみ末尾 ``\\`` を残す)。"""
    root = os.path.abspath(root)
    stripped = root.rstrip("\\")
    if len(stripped) == 2 and stripped[1] == ":":
        return stripped + "\\"
    return stripped


class EsClient:
    def __init__(self, es_path: str | os.PathLike[str], instance: str | None = None, timeout: float | None = None):
        self.es_path = Path(es_path)
        self.instance = instance or None
        self.timeout = timeout
        if not self.es_path.is_file():
            raise EsNotFoundError(f"es.exe が見つかりません: {self.es_path}")

    def _run(self, args: list[str], is_cancelled: Callable[[], bool] | None = None) -> str:
        command = [str(self.es_path)]
        if self.instance:
            command += ["-instance", self.instance]
        command += args
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
        waited = 0.0
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                waited += 0.2
                cancelled = is_cancelled is not None and is_cancelled()
                if cancelled or (self.timeout is not None and waited >= self.timeout):
                    process.kill()
                    process.communicate()
                    if cancelled:
                        raise ScanCancelled() from None
                    raise EsError("es.exe がタイムアウトしました") from None
        if process.returncode != 0:
            detail = _ES_EXIT_MESSAGES.get(process.returncode) or stderr.decode("mbcs", "replace").strip()
            message = f"es.exe がエラー終了しました (code {process.returncode}): {detail}"
            if process.returncode == _ES_EXIT_NOT_RUNNING:
                raise EverythingNotRunningError(message)
            raise EsError(message)
        return stdout.decode("mbcs", "replace").strip()

    def everything_version(self) -> str:
        return self._run(["-get-everything-version"])

    def es_version(self) -> str:
        return self._run(["-version"])

    def result_count(self, root: str) -> int:
        """``root`` 以下の Everything 上の件数。インデックス対象外なら 0。"""
        output = self._run(["-path", _search_root(root), "-get-result-count"])
        try:
            return int(output.replace(",", ""))
        except ValueError:
            raise EsError(f"件数を解釈できません: {output!r}") from None

    def export(
        self,
        root: str,
        csv_path: str | os.PathLike[str],
        *,
        with_ctime: bool = True,
        with_attrs: bool = True,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        args = ["-path", _search_root(root), "-full-path-and-name", "-size", "-date-modified"]
        if with_ctime:
            args.append("-date-created")
        if with_attrs:
            args.append("-attributes")
        args += ["-size-format", "1", "-date-format", "2", "-no-digit-grouping", "-export-csv", str(csv_path)]
        self._run(args, is_cancelled)

    def iter_entries(
        self,
        root: str,
        *,
        with_ctime: bool = True,
        with_attrs: bool = True,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[RawEntry]:
        """``root`` 以下の全エントリを返す。一時 CSV に書き出してから読み込む。"""
        fd, csv_path = tempfile.mkstemp(prefix="vdmoku_es_", suffix=".csv")
        os.close(fd)
        try:
            self.export(root, csv_path, with_ctime=with_ctime, with_attrs=with_attrs, is_cancelled=is_cancelled)
            yield from parse_export_csv(csv_path)
        finally:
            try:
                os.remove(csv_path)
            except OSError:
                pass
