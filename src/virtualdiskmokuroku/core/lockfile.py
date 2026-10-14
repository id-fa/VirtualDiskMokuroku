"""カタログの排他ロック (同じカタログを別のプロセスで同時に開かないようにする)。

ロックはカタログと同じフォルダの ``<名前>.vdmoku.lock``。中身は JSON で、持ち主のプロセス ID・そのプロセスの
開始時刻 (FILETIME)・コンピューター名。同じコンピューターなら「その PID のプロセスが同じ開始時刻で生きているか」で
有効性を判定する (PID は再利用されるので開始時刻も見る)。別のコンピューターのロックは確認できないので有効とみなす
(残骸ならユーザーが削除する)。

同じプロセスで同じカタログを複数回開くのは許す (参照数を数え、最後に閉じたときにロックファイルを消す)。
ロックファイルを作れない場所 (読み取り専用のメディアなど) ではロック無しで開く。
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import sys
import threading
from pathlib import Path

from .errors import CatalogLockedError
from .workdb import remove_quietly
from ..i18n import tr

LOCK_SUFFIX = ".lock"
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259

_held: dict[str, tuple[Path, int]] = {}  # 正規化したカタログのパス → (ロックファイル, 参照数)
_held_guard = threading.Lock()


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]


def process_start_time(pid: int) -> int | None:
    """``pid`` のプロセスが生きていればその開始時刻 (FILETIME)、終了していれば None。"""
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(_FILETIME)] * 4
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        code = ctypes.c_ulong(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
            return None
        times = [_FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
            return None
        return (times[0].high << 32) | times[0].low
    finally:
        kernel32.CloseHandle(handle)


def _this_process() -> dict:
    return {"pid": os.getpid(), "started": process_start_time(os.getpid()), "host": platform.node()}


def _read_owner(lock_path: Path) -> dict | None:
    try:
        owner = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return owner if isinstance(owner, dict) and isinstance(owner.get("pid"), int) else None


def _is_stale(owner: dict, me: dict) -> bool:
    """ロックの持ち主がもういないか。別のコンピューターのロックは確認できないので有効とみなす。"""
    if owner.get("host") != me["host"]:
        return False
    if owner["pid"] == me["pid"]:
        return True  # 自分のプロセスの残骸 (参照数で管理しているので、ここに来るのは異常終了後の再起動など)
    return process_start_time(owner["pid"]) != owner.get("started")


def lock_path_for(catalog_path: Path) -> Path:
    return catalog_path.with_name(catalog_path.name + LOCK_SUFFIX)


def acquire(catalog_path: Path) -> Path | None:
    """``catalog_path`` のロックを取る。別のプロセスが持っていれば ``CatalogLockedError``。

    戻り値はロックファイルのパス。ロックファイルを作れない場所なら None (ロック無しで続ける)。
    """
    key = os.path.normcase(os.path.abspath(catalog_path))
    lock_path = lock_path_for(catalog_path)
    with _held_guard:
        held = _held.get(key)
        if held is not None:
            _held[key] = (held[0], held[1] + 1)
            return held[0]
        me = _this_process()
        for _attempt in range(2):
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                owner = _read_owner(lock_path)
                if owner is not None and not _is_stale(owner, me):
                    raise CatalogLockedError(
                        tr('このカタログは別のインスタンスで開かれています (プロセス ID {pid}、{host})。そちらを閉じてから開き直してください。異常終了で残ったロックなら、このファイルを削除してください: {path}').format(pid=owner["pid"], host=owner.get("host", "?"), path=lock_path)
                    ) from None
                remove_quietly(lock_path)  # 持ち主がいない古いロック (または壊れたファイル)。消して取り直す
                continue
            except OSError:
                return None
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(me, stream)
            _held[key] = (lock_path, 1)
            return lock_path
        raise CatalogLockedError(tr('ロックファイルを取得できません: {path}').format(path=lock_path))


def release(catalog_path: Path) -> None:
    """``acquire`` と対で呼ぶ。このプロセスで最後に閉じたときにロックファイルを消す。"""
    key = os.path.normcase(os.path.abspath(catalog_path))
    with _held_guard:
        held = _held.get(key)
        if held is None:
            return
        lock_path, count = held
        if count > 1:
            _held[key] = (lock_path, count - 1)
            return
        del _held[key]
        remove_quietly(lock_path)
