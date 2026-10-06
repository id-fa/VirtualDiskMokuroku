"""画像の EXIF 情報(メーカー・カメラモデル・撮影条件など)。"""

from __future__ import annotations

from .base import EntryWriter, Extractor, module_available

_IFD_EXIF = 0x8769
_IFD_GPS = 0x8825

# (タグ番号, 保存キー)
_BASE_TAGS = ((271, "make"), (272, "model"), (305, "software"), (306, "datetime"), (274, "orientation"))
_EXIF_TAGS = (
    (36867, "datetime_original"),
    (42035, "lens_make"),
    (42036, "lens_model"),
    (33434, "exposure_time"),
    (33437, "f_number"),
    (34855, "iso"),
    (37380, "exposure_bias"),
    (37386, "focal_length"),
    (41989, "focal_length_35mm"),
)

_heif_registered = False


def register_heif() -> None:
    """pillow-heif が入っていれば HEIC/HEIF を Pillow で開けるようにする。"""
    global _heif_registered
    if _heif_registered:
        return
    _heif_registered = True
    if module_available("pillow_heif"):
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except Exception:  # noqa: BLE001 - 登録できなくても他形式の処理は続ける
            pass


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _trim(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def _format(key: str, value) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    if isinstance(value, tuple) and key != "iso":
        value = value[0] if value else None
    if value is None:
        return None
    if key == "exposure_time":
        seconds = _number(value)
        if not seconds or seconds <= 0:
            return None
        return f"1/{round(1 / seconds)}" if seconds < 1 else _trim(seconds, 1)
    if key == "f_number":
        number = _number(value)
        return f"f/{_trim(number, 1)}" if number else None
    if key in ("focal_length", "focal_length_35mm"):
        number = _number(value)
        return f"{_trim(number, 1)} mm" if number else None
    if key == "exposure_bias":
        number = _number(value)
        return None if number is None else f"{number:+.1f} EV"
    if key == "iso":
        if isinstance(value, tuple):
            value = value[0] if value else None
        return None if value is None else str(value)
    return str(value)


def _degrees(values, reference) -> float | None:
    """度分秒の 3 要素を 10 進の度に変換する。"""
    try:
        degrees, minutes, seconds = (float(part) for part in values)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    result = degrees + minutes / 60 + seconds / 3600
    if isinstance(reference, bytes):
        reference = reference.decode("ascii", "replace")
    if str(reference).strip().upper() in ("S", "W"):
        result = -result
    return result


class ExifExtractor(Extractor):
    kind = "exif"
    label = "EXIF 情報"
    description = "写真のメーカー・カメラモデル・撮影日時・レンズ・露出・GPS などを保存します"
    default_params = {"extensions": ["jpg", "jpeg", "jpe", "tif", "tiff", "heic", "heif"]}
    required_modules = ("PIL",)
    packages = "Pillow"

    def extract(self, path: str, writer: EntryWriter) -> None:
        from PIL import Image

        register_heif()
        with Image.open(path) as image:
            width, height = image.size
            exif = image.getexif()
            for tag, key in _BASE_TAGS:
                writer.add_meta(key, _format(key, exif.get(tag)))
            details = exif.get_ifd(_IFD_EXIF)
            for tag, key in _EXIF_TAGS:
                writer.add_meta(key, _format(key, details.get(tag)))
            writer.add_meta("width", width)
            writer.add_meta("height", height)

            gps = exif.get_ifd(_IFD_GPS)
            if gps:
                latitude = _degrees(gps.get(2), gps.get(1))
                longitude = _degrees(gps.get(4), gps.get(3))
                if latitude is not None and longitude is not None:
                    writer.add_meta("gps_latitude", f"{latitude:.6f}")
                    writer.add_meta("gps_longitude", f"{longitude:.6f}")
                altitude = _number(gps.get(6))
                if altitude is not None:
                    below_sea = gps.get(5) in (1, b"\x01")
                    writer.add_meta("gps_altitude", f"{-altitude if below_sea else altitude:.1f} m")
