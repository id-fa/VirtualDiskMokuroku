"""アプリ全体の設定(カタログ毎の設定はカタログの manifest に持つ)。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

_MAX_RECENT = 10


_LEGACY_DIR_NAME = "PyMediaCatalogue"  # 旧名。設定ファイルが新しい場所に無いときだけ読みに行く


def app_data_dir(name: str = "VirtualDiskMokuroku") -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / name


@dataclass
class AppSettings:
    es_path: str = ""  # 空なら自動検出
    es_instance: str = ""  # Everything のインスタンス名 (1.5 アルファ版の既定は "1.5a")
    result_limit: int = 100000  # フィルタ/検索結果の表示上限
    memory_limit_mb: int = 512  # 暗号化カタログの DB をメモリ上で開く上限。超える分だけ一時フォルダに復号する
    recent_catalogs: list[str] = field(default_factory=list)
    view_mode: str = "details"  # 一覧の表示形式: details / tiles / thumb_list
    thumb_zoom: bool = False  # サムネイルを 2 倍に拡大して並べる
    # 敷き詰め表示でサムネイルの下に出す項目 (name / resolution / size / mtime)
    thumb_captions: list[str] = field(default_factory=lambda: ["name", "resolution", "size", "mtime"])

    @staticmethod
    def file_path() -> Path:
        return app_data_dir() / "settings.json"

    @classmethod
    def load(cls) -> AppSettings:
        settings = cls()
        path = cls.file_path()
        if not path.exists():
            path = app_data_dir(_LEGACY_DIR_NAME) / path.name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return settings
        for key, value in data.items():
            if hasattr(settings, key) and isinstance(value, type(getattr(settings, key))):
                setattr(settings, key, value)
        return settings

    def save(self) -> None:
        path = self.file_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    def add_recent(self, catalog_path: str | os.PathLike[str]) -> None:
        text = str(Path(catalog_path).resolve())
        self.recent_catalogs = [text] + [item for item in self.recent_catalogs if item.casefold() != text.casefold()]
        del self.recent_catalogs[_MAX_RECENT:]
