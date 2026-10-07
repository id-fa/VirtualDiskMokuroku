"""拡張コンテキスト(ファイルの中身から得る追加情報)の抽出・保存・検索。

抽出器は種別 (kind) ごとに 1 クラスで、カタログ設定 ``settings["context"][kind]`` で個別に有効/無効とパラメータを持つ。
"""

from __future__ import annotations

import copy

from .archive import ArchiveExtractor
from .audio import AudioExtractor
from .base import EntryWriter, Extractor
from .exif import ExifExtractor
from .iso import IsoExtractor
from .office import OfficeExtractor
from .text import TextExtractor
from .thumbnail import ThumbnailExtractor

# kind -> 抽出器クラス。並び順が設定画面などでの表示順
EXTRACTORS: dict[str, type[Extractor]] = {
    cls.kind: cls
    for cls in (
        ExifExtractor,
        OfficeExtractor,
        AudioExtractor,
        TextExtractor,
        ThumbnailExtractor,
        ArchiveExtractor,
        IsoExtractor,
    )
}

# ctx テーブルのキー -> 表示名
KEY_LABELS: dict[str, str] = {
    # exif
    "make": "メーカー",
    "model": "カメラモデル",
    "lens_make": "レンズメーカー",
    "lens_model": "レンズ",
    "datetime_original": "撮影日時",
    "datetime": "更新日時 (EXIF)",
    "exposure_time": "露出時間",
    "f_number": "F値",
    "iso": "ISO感度",
    "exposure_bias": "露出補正",
    "focal_length": "焦点距離",
    "focal_length_35mm": "焦点距離 (35mm換算)",
    "software": "ソフトウェア",
    "orientation": "画像の向き",
    "width": "幅",
    "height": "高さ",
    "gps_latitude": "緯度",
    "gps_longitude": "経度",
    "gps_altitude": "高度",
    # office
    "title": "タイトル",
    "subject": "件名",
    "author": "作成者",
    "last_modified_by": "最終更新者",
    "created": "作成日時",
    "modified": "更新日時",
    "keywords": "キーワード",
    "comments": "コメント",
    "category": "分類",
    "revision": "改訂番号",
    "application": "アプリケーション",
    "company": "会社",
    "manager": "管理者",
    "pages": "ページ数",
    "words": "単語数",
    "slides": "スライド数",
    # audio
    "artist": "アーティスト",
    "album": "アルバム",
    "album_artist": "アルバムアーティスト",
    "year": "年",
    "genre": "ジャンル",
    "track": "トラック番号",
    "disc": "ディスク番号",
    "composer": "作曲者",
    "duration": "長さ",
    "bitrate": "ビットレート",
    "sample_rate": "サンプリング周波数",
    "channels": "チャンネル数",
    # archive / iso
    "inner_count": "内部エントリ数",
    "inner_truncated": "一覧の打ち切り",
    "iso_filesystem": "ISO のファイルシステム",
    "volume_label": "ボリュームラベル",
    # Virtual CD-ROM Case からのインポート
    "property_type": "プロパティの種類",
    "property": "その他のプロパティ",
    "crc32": "CRC32",
    "tag_format": "タグの形式",
    "source": "ソース",
    "trademarks": "商標",
    "creator": "作成元アプリケーション",
    "file_version": "ファイルバージョン",
    "product_version": "製品バージョン",
    "product_name": "製品名",
    "description": "説明",
    "copyright": "著作権",
    "target_os": "対象 OS",
    "module_type": "モジュールの種類",
}

# 抽出器以外が作る種別 (kind) -> 表示名
KIND_LABELS: dict[str, str] = {"vcdcase": "Virtual CD-ROM Case からのインポート"}


def default_context_settings() -> dict:
    """カタログ設定 ``settings["context"]`` の既定値(すべて無効)。"""
    return {kind: {"enabled": False, **copy.deepcopy(cls.default_params)} for kind, cls in EXTRACTORS.items()}


__all__ = ["EXTRACTORS", "KEY_LABELS", "KIND_LABELS", "EntryWriter", "Extractor", "default_context_settings"]
