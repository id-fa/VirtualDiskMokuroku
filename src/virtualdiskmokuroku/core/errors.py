class ScanCancelled(Exception):
    """ユーザー操作によりスキャンが中断された。"""


class CatalogError(Exception):
    """カタログファイルの読み書きに失敗した。"""


class PasswordError(CatalogError):
    """パスワードが必要、または一致しない。"""
