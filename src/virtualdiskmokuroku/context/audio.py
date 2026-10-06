"""音声/動画ファイルのメタタグ情報 (MP3/MP4/AAC/FLAC など)。mutagen を使う。"""

from __future__ import annotations

from .base import EntryWriter, Extractor

# 保存キー → 形式ごとのタグ名候補 (EasyID3/EasyMP4/Vorbis, ID3 フレーム, ASF, APEv2)
_TAG_CANDIDATES = (
    ("title", ("title", "TIT2", "Title")),
    ("artist", ("artist", "TPE1", "Author", "Artist")),
    ("album", ("album", "TALB", "WM/AlbumTitle", "Album")),
    ("album_artist", ("albumartist", "TPE2", "WM/AlbumArtist", "Album Artist")),
    ("year", ("date", "TDRC", "TYER", "WM/Year", "Year")),
    ("genre", ("genre", "TCON", "WM/Genre", "Genre")),
    ("track", ("tracknumber", "TRCK", "WM/TrackNumber", "Track")),
    ("disc", ("discnumber", "TPOS", "WM/PartOfSet", "Disc")),
    ("composer", ("composer", "TCOM", "WM/Composer", "Composer")),
)


def _text(value) -> str:
    if isinstance(value, (list, tuple)):
        return "; ".join(part for part in (_text(item) for item in value) if part)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value).strip()


def _duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


class AudioExtractor(Extractor):
    kind = "audio"
    label = "音声/動画タグ"
    description = "MP3 / MP4 / AAC / FLAC などのタイトル・アーティスト・アルバム・長さを保存します"
    default_params = {
        "extensions": [
            "mp3", "m4a", "m4b", "mp4", "m4v", "aac", "flac", "ogg", "oga", "opus",
            "wma", "wav", "aiff", "aif", "ape", "wv", "mpc",
        ]  # fmt: skip
    }
    required_modules = ("mutagen",)
    packages = "mutagen"

    def extract(self, path: str, writer: EntryWriter) -> None:
        import mutagen

        media = mutagen.File(path, easy=True)
        if media is None:
            raise ValueError("未対応の形式、またはタグを読み取れません")

        tags = media.tags
        if tags is not None:
            for key, candidates in _TAG_CANDIDATES:
                for name in candidates:
                    try:
                        value = tags.get(name)
                    except (KeyError, ValueError, TypeError):
                        value = None
                    if value:
                        writer.add_meta(key, _text(value))
                        break

        info = media.info
        length = getattr(info, "length", None)
        if length:
            writer.add_meta("duration", _duration(length))
        bitrate = getattr(info, "bitrate", None)
        if bitrate:
            writer.add_meta("bitrate", f"{round(bitrate / 1000)} kbps")
        sample_rate = getattr(info, "sample_rate", None)
        if sample_rate:
            writer.add_meta("sample_rate", f"{sample_rate} Hz")
        writer.add_meta("channels", getattr(info, "channels", None))
