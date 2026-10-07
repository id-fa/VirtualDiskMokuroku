"""ボリューム・ディスクの識別情報と容量の取得 (Win32 API)。

外付けドライブはドライブレターが変わるため、ラベル・シリアル・デバイス名などを記録して同定に使う。
いずれも管理者権限なしで取得できる。
"""

from __future__ import annotations

import ctypes
import os
import struct
from ctypes import wintypes
from dataclasses import asdict, dataclass
from ..i18n import tr

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_DRIVE_TYPES = {0: "unknown", 1: "no_root", 2: "removable", 3: "fixed", 4: "remote", 5: "cdrom", 6: "ramdisk"}
_BUS_TYPES = {
    1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "IEEE1394", 5: "SSA", 6: "FibreChannel", 7: "USB", 8: "RAID",
    9: "iSCSI", 10: "SAS", 11: "SATA", 12: "SD", 13: "MMC", 14: "Virtual", 15: "FileBackedVirtual",
    16: "StorageSpaces", 17: "NVMe", 18: "SCM", 19: "UFS",
}  # fmt: skip

_SEM_FAILCRITICALERRORS = 0x0001
_FILE_SHARE_READ_WRITE = 0x00000003
_OPEN_EXISTING = 3
_IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_kernel32.GetLogicalDriveStringsW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
_kernel32.GetLogicalDriveStringsW.restype = wintypes.DWORD
_kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetDriveTypeW.restype = wintypes.UINT
_kernel32.GetVolumeInformationW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
]  # fmt: skip
_kernel32.GetVolumeInformationW.restype = wintypes.BOOL
_kernel32.GetVolumeNameForVolumeMountPointW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
_kernel32.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL
_kernel32.GetDiskFreeSpaceExW.argtypes = [
    wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_ulonglong),
    ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong),
]  # fmt: skip
_kernel32.GetDiskFreeSpaceExW.restype = wintypes.BOOL
_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]  # fmt: skip
_kernel32.CreateFileW.restype = ctypes.c_void_p
_kernel32.DeviceIoControl.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]  # fmt: skip
_kernel32.DeviceIoControl.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.SetErrorMode.argtypes = [wintypes.UINT]
_kernel32.SetErrorMode.restype = wintypes.UINT


@dataclass(slots=True)
class VolumeInfo:
    root: str  # "X:\\"
    drive_type: str = "unknown"
    ready: bool = False  # メディアが入っていて読める状態か
    label: str = ""
    serial: str = ""  # "1A2B-3C4D"
    filesystem: str = ""
    volume_guid: str = ""  # "\\\\?\\Volume{...}\\"
    total_bytes: int | None = None
    free_bytes: int | None = None
    device_vendor: str = ""
    device_model: str = ""
    device_serial: str = ""
    bus_type: str = ""
    removable_media: bool | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def display_name(self) -> str:
        label = self.label or {"cdrom": "CD/DVD", "removable": tr('リムーバブル')}.get(self.drive_type, tr('ローカル ディスク'))
        return f"{label} ({self.root[:2]})"


def drive_root(path: str) -> str:
    """パスが属するドライブのルート ("X:\\") を返す。"""
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    return drive + "\\"


def list_drive_roots() -> list[str]:
    size = _kernel32.GetLogicalDriveStringsW(0, None)
    buffer = ctypes.create_unicode_buffer(size + 1)
    _kernel32.GetLogicalDriveStringsW(size, buffer)
    return [root for root in ctypes.wstring_at(buffer, size).split("\0") if root]


def _device_descriptor(root: str) -> dict:
    """IOCTL_STORAGE_QUERY_PROPERTY でデバイスのモデル名・シリアル・バス種別を得る。"""
    handle = _kernel32.CreateFileW(f"\\\\.\\{root[:2]}", 0, _FILE_SHARE_READ_WRITE, None, _OPEN_EXISTING, 0, None)
    if handle is None or handle == _INVALID_HANDLE_VALUE:
        return {}
    try:
        query = struct.pack("<II4x", 0, 0)  # StorageDeviceProperty, PropertyStandardQuery
        out = ctypes.create_string_buffer(4096)
        returned = wintypes.DWORD(0)
        ok = _kernel32.DeviceIoControl(
            handle, _IOCTL_STORAGE_QUERY_PROPERTY, query, len(query), out, len(out), ctypes.byref(returned), None
        )
        if not ok or returned.value < 36:
            return {}
        data = out.raw[: returned.value]
        # STORAGE_DEVICE_DESCRIPTOR
        (_version, _size, _dev_type, _dev_mod, removable, _queueing, vendor_off, product_off, _revision_off,
         serial_off, bus_type, _raw_len) = struct.unpack_from("<IIBBBBIIIIII", data)  # fmt: skip

        def text(offset: int) -> str:
            if not offset or offset >= len(data):
                return ""
            end = data.find(b"\0", offset)
            return data[offset : end if end >= 0 else len(data)].decode("ascii", "replace").strip()

        return {
            "device_vendor": text(vendor_off),
            "device_model": text(product_off),
            "device_serial": text(serial_off),
            "bus_type": _BUS_TYPES.get(bus_type, str(bus_type) if bus_type else ""),
            "removable_media": bool(removable),
        }
    finally:
        _kernel32.CloseHandle(handle)


def get_volume_info(root: str, include_device: bool = True) -> VolumeInfo:
    """ドライブルート (または配下のパス) のボリューム情報を取得する。

    ``include_device=False`` ならデバイス情報(モデル名など)の問い合わせを省く(同定だけしたいとき用)。
    """
    root = drive_root(root)
    info = VolumeInfo(root=root)
    # メディア未挿入のドライブでシステムのエラーダイアログを出さない
    old_mode = _kernel32.SetErrorMode(_SEM_FAILCRITICALERRORS)
    try:
        info.drive_type = _DRIVE_TYPES.get(_kernel32.GetDriveTypeW(root), "unknown")

        label = ctypes.create_unicode_buffer(261)
        filesystem = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD(0)
        max_component = wintypes.DWORD(0)
        flags = wintypes.DWORD(0)
        if _kernel32.GetVolumeInformationW(
            root, label, len(label), ctypes.byref(serial), ctypes.byref(max_component), ctypes.byref(flags),
            filesystem, len(filesystem),
        ):  # fmt: skip
            info.ready = True
            info.label = label.value
            info.filesystem = filesystem.value
            info.serial = f"{serial.value >> 16:04X}-{serial.value & 0xFFFF:04X}"

        guid = ctypes.create_unicode_buffer(64)
        if _kernel32.GetVolumeNameForVolumeMountPointW(root, guid, len(guid)):
            info.volume_guid = guid.value

        free_to_caller = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        total_free = ctypes.c_ulonglong(0)
        if _kernel32.GetDiskFreeSpaceExW(
            root, ctypes.byref(free_to_caller), ctypes.byref(total), ctypes.byref(total_free)
        ):
            info.total_bytes = total.value
            info.free_bytes = total_free.value

        if include_device and info.drive_type != "remote":
            for key, value in _device_descriptor(root).items():
                setattr(info, key, value)
    finally:
        _kernel32.SetErrorMode(old_mode)
    return info


def list_volumes(include_device: bool = True) -> list[VolumeInfo]:
    return [get_volume_info(root, include_device) for root in list_drive_roots()]
