import os

# テストの文言の比較は日本語で行う (英語 OS 上でも同じ結果になるように)
os.environ.setdefault("VIRTUALDISKMOKUROKU_LANG", "ja")
