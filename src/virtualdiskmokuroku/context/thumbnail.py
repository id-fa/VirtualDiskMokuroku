"""画像ファイルのサムネイル (JPEG)。"""

from __future__ import annotations

import io

from .base import EntryWriter, Extractor
from .exif import register_heif

_EXIF_ORIENTATION = 0x0112
_ROTATED_ORIENTATIONS = (5, 6, 7, 8)  # 縦横が入れ替わる向き


class ThumbnailExtractor(Extractor):
    kind = "thumbnail"
    label = "画像サムネイル"
    description = "画像の縮小版(長辺のサイズを指定可)を保存します"
    default_params = {
        "size": 160,
        "quality": 80,
        "extensions": ["jpg", "jpeg", "jpe", "png", "gif", "bmp", "webp", "tif", "tiff", "heic", "heif"],
    }
    stores = ("ctx", "thumb")
    revision = 2  # 2: 元画像の解像度 (width / height) も保存する
    required_modules = ("PIL",)
    packages = "Pillow"

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self._size = min(2048, max(16, int(self.params.get("size", 160))))
        self._quality = min(95, max(10, int(self.params.get("quality", 80))))

    def extract(self, path: str, writer: EntryWriter) -> None:
        from PIL import Image, ImageOps

        register_heif()
        with Image.open(path) as image:
            # 元画像の解像度。EXIF の回転指定があれば、サムネイルと同じく表示時の向きに合わせる
            width, height = image.size
            if image.getexif().get(_EXIF_ORIENTATION) in _ROTATED_ORIENTATIONS:
                width, height = height, width
            writer.add_meta("width", width)
            writer.add_meta("height", height)

            # JPEG はデコード時に縮小して読み込みを速くする
            image.draft("RGB", (self._size, self._size))
            image = ImageOps.exif_transpose(image)
            image.thumbnail((self._size, self._size), Image.Resampling.LANCZOS)
            if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=self._quality, optimize=True)
            writer.set_thumb(image.width, image.height, buffer.getvalue())
