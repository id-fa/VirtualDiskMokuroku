"""画像ファイルのサムネイル (JPEG)。"""

from __future__ import annotations

import io

from .base import EntryWriter, Extractor
from .exif import register_heif


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
