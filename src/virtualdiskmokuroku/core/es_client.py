"""Everything のコマンドラインインターフェース es.exe のラッパー。"""

from __future__ import annotations

import _winapi
import codecs
import csv
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .errors import ScanCancelled
from ..i18n import tr

FILE_ATTRIBUTE_DIRECTORY = 0x10

_PIPE_ACCESS_INBOUND = 0x00000001
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_PIPE_BUFFER = 1 << 20
_ERROR_BROKEN_PIPE = 109
_ERROR_NO_DATA = 232
_ERROR_PIPE_CONNECTED = 535

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

# Everything 本体を起動してから接続できるようになるまで待つ上限 (秒)。起動時に管理者昇格の確認ダイアログが出る
# 設定では、ユーザーがそれに応答するまで接続できないので余裕を持たせる
EVERYTHING_START_TIMEOUT = 180.0


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


def _nearby_dirs() -> tuple[Path, Path]:
    """アプリ近傍として探すフォルダ (実行ファイルまたはスクリプトのフォルダと、カレントフォルダ)。"""
    app_dir = Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] else Path.cwd()
    return app_dir, Path.cwd()


def portable_dir() -> Path:
    """利用者が Everything 本体と es.exe を置くフォルダ (アプリのフォルダの下の ``everything_portable\\``)。"""
    return _nearby_dirs()[0] / "everything_portable"


def find_es_exe(configured: str | os.PathLike[str] | None = None) -> Path | None:
    """es.exe を探す。設定値 → PATH → アプリ近傍 → 既定のインストール先の順。"""
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    on_path = shutil.which("es.exe")
    if on_path:
        candidates.append(Path(on_path))
    for base in _nearby_dirs():
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


def find_everything_exe(es_path: str | os.PathLike[str] | None = None) -> Path | None:
    """Everything 本体 (Everything.exe) を探す。es.exe と同じフォルダ → アプリ近傍の ``everything_portable\\`` の順。

    見つかったものを起動すれば、その es.exe から接続できる見込みが高い場所だけを探す (既定のインストール先は
    探さない。そこにあるものは普通スタートアップで起動しているか、ユーザーが意図して止めている)。
    """
    candidates: list[Path] = []
    if es_path:
        candidates.append(Path(es_path).parent / "Everything.exe")
    for base in _nearby_dirs():
        candidates.append(base / "everything_portable" / "Everything.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def start_everything(exe: str | os.PathLike[str], instance: str | None = None) -> subprocess.Popen:
    """Everything 本体をバックグラウンド (タスクトレイ常駐) で起動する。

    起動しただけで戻るので、接続できるようになるまでは ``wait_for_everything`` で待つ。``instance`` を
    指定すると同じ名前のインスタンスとして起動する (es.exe 側の ``-instance`` と揃える)。
    「管理者として実行」が必要な設定のときなど、起動できなければ OSError になる。
    """
    exe = Path(exe)
    command = [str(exe), "-startup"]
    if instance:
        command += ["-instance", instance]
    return subprocess.Popen(
        command,
        cwd=str(exe.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )


def wait_for_everything(
    es: EsClient, timeout: float = EVERYTHING_START_TIMEOUT, *,
    should_stop: Callable[[], bool] | None = None, interval: float = 1.0,
) -> bool:  # fmt: skip
    """Everything に接続できるようになるまで待つ。``timeout`` 秒以内に応答しなければ False。

    ``interval`` 秒ごとに es.exe を実行して確かめる。``should_stop`` は待っている間に繰り返し呼ばれ、True を
    返すと待つのをやめる (GUI ではここでイベントを処理し、キャンセルを見る)。
    """
    deadline = time.monotonic() + timeout
    while True:
        if es.is_everything_running():
            return True
        if (should_stop is not None and should_stop()) or time.monotonic() >= deadline:
            return False
        time.sleep(interval)


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


def parse_csv_lines(lines: Iterable[str]) -> Iterator[RawEntry]:
    """``-export-csv`` 形式の行 (先頭はヘッダ) を読み、RawEntry を順に返す。"""
    reader = csv.reader(lines)
    header = next(reader, None)
    if header is None:
        return
    columns = {name.strip().lstrip("﻿").casefold(): index for index, name in enumerate(header)}
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


def parse_export_csv(csv_path: str | os.PathLike[str]) -> Iterator[RawEntry]:
    """``-export-csv`` で書き出したファイル (UTF-8) を読む。"""
    with open(csv_path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        yield from parse_csv_lines(f)


def _decode_lines(chunks: Iterable[bytes]) -> Iterator[str]:
    """UTF-8 のバイト列の並びを行に分ける(チャンクの境界が文字や行の途中にあってもよい)。"""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    for chunk in chunks:
        pending += decoder.decode(chunk)
        *complete, pending = pending.split("\n")
        for line in complete:
            yield line + "\n"
    pending += decoder.decode(b"", final=True)
    if pending:
        yield pending


def _read_pipe(handle: int, chunks: queue.Queue) -> None:
    """名前付きパイプへの接続を待ち、届いたデータをキューに積む。最後に None (正常終了) か例外を積む。"""
    try:
        try:
            _winapi.ConnectNamedPipe(handle, False)
        except OSError as error:
            if error.winerror != _ERROR_PIPE_CONNECTED:
                raise
        while True:
            try:
                data, _result = _winapi.ReadFile(handle, _PIPE_BUFFER)
            except OSError as error:
                if error.winerror in (_ERROR_BROKEN_PIPE, _ERROR_NO_DATA):
                    break  # 書き込み側が閉じた
                raise
            if not data:
                break
            chunks.put(data)
        chunks.put(None)
    except OSError as error:
        chunks.put(error)


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
            raise EsNotFoundError(tr('es.exe が見つかりません: {es_path}').format(es_path=self.es_path))

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
                    raise EsError(tr('es.exe がタイムアウトしました')) from None
        if process.returncode != 0:
            detail = tr(_ES_EXIT_MESSAGES.get(process.returncode, "")) or stderr.decode("mbcs", "replace").strip()
            message = tr('es.exe がエラー終了しました (code {returncode}): {detail}').format(returncode=process.returncode, detail=detail)
            if process.returncode == _ES_EXIT_NOT_RUNNING:
                raise EverythingNotRunningError(message)
            raise EsError(message)
        return stdout.decode("mbcs", "replace").strip()

    def everything_version(self) -> str:
        return self._run(["-get-everything-version"])

    def is_everything_running(self) -> bool:
        """Everything 本体に接続できるか。接続できない以外のエラー (es.exe の不具合など) はそのまま送出する。"""
        try:
            self.everything_version()
        except EverythingNotRunningError:
            return False
        return True

    def es_version(self) -> str:
        return self._run(["-version"])

    def result_count(self, root: str) -> int:
        """``root`` 以下の Everything 上の件数。インデックス対象外なら 0。"""
        output = self._run(["-path", _search_root(root), "-get-result-count"])
        try:
            return int(output.replace(",", ""))
        except ValueError:
            raise EsError(tr('件数を解釈できません: {output!r}').format(output=output)) from None

    def export(
        self,
        root: str,
        csv_path: str | os.PathLike[str],
        *,
        with_ctime: bool = True,
        with_attrs: bool = True,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        """一覧を CSV ファイルに書き出す(調査用。通常のスキャンは ``iter_entries`` がファイルを介さずに行う)。"""
        self._run(self._export_args(root, str(csv_path), with_ctime, with_attrs), is_cancelled)

    @staticmethod
    def _export_args(root: str, destination: str, with_ctime: bool, with_attrs: bool) -> list[str]:
        args = ["-path", _search_root(root), "-full-path-and-name", "-size", "-date-modified"]
        if with_ctime:
            args.append("-date-created")
        if with_attrs:
            args.append("-attributes")
        return args + ["-size-format", "1", "-date-format", "2", "-no-digit-grouping", "-export-csv", destination]

    def iter_entries(
        self,
        root: str,
        *,
        with_ctime: bool = True,
        with_attrs: bool = True,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[RawEntry]:
        """``root`` 以下の全エントリを返す。

        es.exe の書き出し先を名前付きパイプにして直接受け取るので、一覧がディスク上のファイルになることはない
        (標準出力は ANSI コードページになり文字が欠けるため使わない)。
        """
        pipe_name = f"\\\\.\\pipe\\vdmoku_es_{uuid.uuid4().hex}"
        handle = _winapi.CreateNamedPipe(
            pipe_name, _PIPE_ACCESS_INBOUND | _FILE_FLAG_FIRST_PIPE_INSTANCE, 0, 1, _PIPE_BUFFER, _PIPE_BUFFER, 0, 0
        )
        chunks: queue.Queue = queue.Queue(maxsize=32)
        reader = threading.Thread(target=_read_pipe, args=(handle, chunks), daemon=True)
        reader.start()
        command = [str(self.es_path)]
        if self.instance:
            command += ["-instance", self.instance]
        command += self._export_args(root, pipe_name, with_ctime, with_attrs)
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW
        )
        poked = False

        def poke() -> None:
            """es.exe がパイプを開かずに終わった場合に、接続待ちの読み取りスレッドを解放する。"""
            nonlocal poked
            if not poked:
                poked = True
                try:
                    open(pipe_name, "wb").close()
                except OSError:
                    pass

        def received() -> Iterator[bytes]:
            waited = 0.0
            while True:
                try:
                    item = chunks.get(timeout=0.2)
                except queue.Empty:
                    waited += 0.2
                    if is_cancelled is not None and is_cancelled():
                        raise ScanCancelled() from None
                    if process.poll() is not None:
                        poke()
                    elif self.timeout is not None and waited >= self.timeout:
                        raise EsError(tr('es.exe がタイムアウトしました')) from None
                    continue
                waited = 0.0
                if item is None:
                    return
                if isinstance(item, BaseException):
                    raise EsError(tr('es.exe の出力を受け取れません ({item})').format(item=item)) from item
                if is_cancelled is not None and is_cancelled():
                    raise ScanCancelled()
                yield item

        try:
            yield from parse_csv_lines(_decode_lines(received()))
            returncode = process.wait()
            if returncode != 0:
                stderr = process.stderr.read() if process.stderr else b""
                detail = tr(_ES_EXIT_MESSAGES.get(returncode, "")) or stderr.decode("mbcs", "replace").strip()
                message = tr('es.exe がエラー終了しました (code {returncode}): {detail}').format(returncode=returncode, detail=detail)
                if returncode == _ES_EXIT_NOT_RUNNING:
                    raise EverythingNotRunningError(message)
                raise EsError(message)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stderr:
                process.stderr.close()
            poke()
            reader.join(timeout=5)
            if not reader.is_alive():
                _winapi.CloseHandle(handle)
