"""音声/動画ファイルのメタタグ情報 (MP3/MP4/AAC/FLAC など)。TinyTag (MIT ライセンス) を使う。"""

from __future__ import annotations

from collections.abc import Iterable

from .base import EntryWriter, Extractor

# 保存キー → TinyTag の属性名
_TEXT_FIELDS = (
    ("title", "title"),
    ("artist", "artist"),
    ("album", "album"),
    ("album_artist", "albumartist"),
    ("year", "year"),
    ("genre", "genre"),
    ("composer", "composer"),
)


def _supported_suffixes() -> frozenset[str]:
    """TinyTag が読める拡張子 (".mp3" 形式)。TinyTag が無ければ空。"""
    try:
        from tinytag import TinyTag
    except ImportError:
        return frozenset()
    return frozenset(suffix.lower() for suffix in TinyTag.SUPPORTED_FILE_EXTENSIONS)


def _duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _numbered(number, total) -> str:
    """トラック番号などを "3/12" (総数が無ければ "3") の形にする。"""
    if number is None:
        return ""
    return f"{number}/{total}" if total else str(number)


class AudioExtractor(Extractor):
    kind = "audio"
    label = "音声/動画タグ"
    description = "MP3 / MP4 / AAC (M4A) / FLAC などのタイトル・アーティスト・アルバム・長さを保存します"
    default_params = {
        "extensions": [
            "mp3", "m4a", "m4b", "mp4", "m4v", "flac", "ogg", "oga", "opus",
            "wma", "wmv", "asf", "wav", "aiff", "aif",
        ]  # fmt: skip
    }
    required_modules = ("tinytag",)
    packages = "tinytag"

    def active_extensions(self) -> Iterable[str]:
        # 設定に TinyTag が読めない拡張子が残っていても、エラーにせず対象外にする
        supported = _supported_suffixes()
        extensions = super().active_extensions()
        if not supported:
            return extensions
        return [ext for ext in extensions if "." + ext.strip().lower().lstrip(".") in supported]

    def extract(self, path: str, writer: EntryWriter) -> None:
        from tinytag import TinyTag

        tag = TinyTag.get(path)

        for key, attribute in _TEXT_FIELDS:
            value = getattr(tag, attribute, None)
            if value:
                writer.add_meta(key, str(value).strip())
        track = _numbered(tag.track, tag.track_total)
        if track:
            writer.add_meta("track", track)
        disc = _numbered(tag.disc, tag.disc_total)
        if disc:
            writer.add_meta("disc", disc)

        if tag.duration:
            writer.add_meta("duration", _duration(tag.duration))
        if tag.bitrate:
            writer.add_meta("bitrate", f"{round(tag.bitrate)} kbps")
        if tag.samplerate:
            writer.add_meta("sample_rate", f"{tag.samplerate} Hz")
        if tag.channels:
            writer.add_meta("channels", tag.channels)
