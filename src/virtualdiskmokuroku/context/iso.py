"""ISO イメージ内のファイルリスト。pycdlib を使う。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .base import EntryWriter, Extractor, datetime_to_filetime


def _record_size(record) -> int | None:
    for attribute in ("data_length", "info_len"):
        value = getattr(record, attribute, None)
        if isinstance(value, int):
            return value
    return None


def _record_mtime(record) -> int | None:
    """ISO9660 のディレクトリレコード、または UDF のファイルエントリから更新日時を得る。"""
    try:
        date = getattr(record, "date", None)
        if date is not None:  # ISO9660 / Joliet / Rock Ridge
            offset = timezone(timedelta(minutes=15 * (date.gmtoffset or 0)))
            moment = datetime(1900 + date.years_since_1900, date.month, date.day_of_month,
                              date.hour, date.minute, date.second, tzinfo=offset)  # fmt: skip
            return datetime_to_filetime(moment)
        stamp = getattr(record, "mod_time", None)
        if stamp is not None:  # UDF
            tz_minutes = getattr(stamp, "tz", None)
            tzinfo = timezone(timedelta(minutes=tz_minutes)) if tz_minutes is not None and abs(tz_minutes) <= 1440 else None
            moment = datetime(stamp.year, stamp.month, stamp.day, stamp.hour, stamp.minute, stamp.second, tzinfo=tzinfo)
            return datetime_to_filetime(moment)
    except (ValueError, TypeError, AttributeError, OverflowError):
        pass
    return None


def _display_name(name: str, path_key: str) -> str:
    """ISO9660 の名前からバージョン番号 (;1) と末尾の ``.`` を除く。"""
    if path_key == "iso_path":
        name = name.split(";", 1)[0]
        if name.endswith("."):
            name = name[:-1]
    return name


class IsoExtractor(Extractor):
    kind = "iso"
    label = "ISO 内ファイルリスト"
    description = "ISO イメージの中にあるファイルの一覧を保存します"
    default_params = {"max_entries": 10000, "extensions": ["iso"]}
    stores = ("ctx", "inner")
    required_modules = ("pycdlib",)
    packages = "pycdlib"

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self._max_entries = max(0, int(self.params.get("max_entries", 10000)))

    def extract(self, path: str, writer: EntryWriter) -> None:
        import pycdlib

        iso = pycdlib.PyCdlib()
        iso.open(path)
        try:
            # 長いファイル名を保持できる順に採用する
            if iso.has_udf():
                path_key, system = "udf_path", "UDF"
            elif iso.has_joliet():
                path_key, system = "joliet_path", "Joliet"
            elif iso.has_rock_ridge():
                path_key, system = "rr_path", "Rock Ridge"
            else:
                path_key, system = "iso_path", "ISO 9660"
            writer.add_meta("iso_filesystem", system)
            try:
                writer.add_meta("volume_label", iso.pvd.volume_identifier.decode("ascii", "replace"))
            except AttributeError:
                pass

            count = 0
            truncated = False
            for directory, dir_names, file_names in iso.walk(**{path_key: "/"}):
                base = directory.rstrip("/")
                shown_base = "/".join(_display_name(part, path_key) for part in base.split("/") if part)
                for names, is_dir in ((dir_names, True), (file_names, False)):
                    for name in names:
                        if self._max_entries and count >= self._max_entries:
                            truncated = True
                            break
                        try:
                            record = iso.get_record(**{path_key: f"{base}/{name}"})
                        except Exception:  # noqa: BLE001 - レコードが読めなくても名前だけは残す
                            record = None
                        shown = _display_name(name, path_key)
                        writer.add_inner(
                            f"{shown_base}/{shown}" if shown_base else shown,
                            None if is_dir else _record_size(record),
                            _record_mtime(record),
                            is_dir,
                        )
                        count += 1
                if truncated:
                    break
            writer.add_meta("inner_count", count)
            if truncated:
                writer.add_meta("inner_truncated", f"先頭 {count} 件のみ")
        finally:
            iso.close()
