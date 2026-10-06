# サードパーティ ライセンス

VirtualDiskMokuroku 自体は [MIT License](LICENSE) です。
配布用の実行ファイル (`scripts\build_exe.ps1` で作成したフォルダ) には、以下の第三者ソフトウェアが同梱されます。
各ライセンスの本文は `licenses\` フォルダにあります。

ここに挙げているのは主要なものです。同梱物の全体は実行ファイルの `_internal` フォルダで確認できます。

## Qt / PySide6 (Qt for Python)

| 項目 | 内容 |
|---|---|
| 対象 | PySide6, shiboken6, Qt 6 ライブラリ |
| 著作権 | Copyright (C) The Qt Company Ltd. and other contributors |
| ライセンス | GNU Lesser General Public License v3 (`LGPL-3.0-only`) の条件で利用しています |
| 本文 | [licenses/LGPL-3.0.txt](licenses/LGPL-3.0.txt)、LGPL が参照する [licenses/GPL-3.0.txt](licenses/GPL-3.0.txt) |
| ソースコード | <https://code.qt.io/cgit/pyside/pyside-setup.git/> / <https://download.qt.io/official_releases/QtForPython/> / <https://download.qt.io/official_releases/qt/> |

- VirtualDiskMokuroku は Qt / PySide6 を改変せず、動的にリンクして利用しています。
- 実行ファイルはフォルダ形式で、Qt / PySide6 は `_internal\PySide6` と `_internal\shiboken6` に独立したファイルとして置かれています。
  利用者は、これらを互換性のある別のバージョン(自分で改変・ビルドしたものを含む)に差し替えて実行できます。
- 差し替えたライブラリと組み合わせて動かすための改変や、そのデバッグを目的としたリバースエンジニアリングを禁止しません。

## 拡張コンテキスト用ライブラリ

ビルドした環境に導入されている場合のみ同梱されます。

| ライブラリ | ライセンス | 入手元 |
|---|---|---|
| Pillow | MIT-CMU | <https://github.com/python-pillow/Pillow> |
| TinyTag | MIT | <https://github.com/tinytag/tinytag> |
| charset-normalizer | MIT | <https://github.com/jawah/charset_normalizer> |
| olefile | BSD-2-Clause | <https://github.com/decalage2/olefile> |
| rarfile | ISC | <https://github.com/markokr/rarfile> |
| py7zr | LGPL-2.1-or-later | <https://github.com/miurahr/py7zr> |
| pyppmd / pybcj / inflate64 / multivolumefile (py7zr の依存) | LGPL-2.1-or-later | <https://github.com/miurahr> |
| pycdlib | LGPL-2.1-only | <https://github.com/clalancette/pycdlib> |
| pycryptodomex (py7zr の依存) | BSD-2-Clause / Public Domain | <https://github.com/Legrandin/pycryptodome> |
| brotli, texttable (py7zr の依存) | MIT | <https://github.com/google/brotli> / <https://github.com/foutaise/texttable> |
| psutil (py7zr の依存) | BSD-3-Clause | <https://github.com/giampaolo/psutil> |
| backports.zstd (py7zr の依存) | PSF-2.0 | <https://github.com/Rogdham/backports.zstd> |

LGPL-2.1 の本文は [licenses/LGPL-2.1.txt](licenses/LGPL-2.1.txt) です。これらのライブラリも改変せずに利用しており、
`_internal` フォルダ内の該当ファイルを差し替えることができます。

GPL のライブラリは同梱していません (`licenses/GPL-3.0.txt` は LGPL-3.0 が参照する本文として添付しているものです)。

## その他

| ソフトウェア | ライセンス |
|---|---|
| Python 3 ランタイムと標準ライブラリ | PSF-2.0 |
| PyInstaller ブートローダ | GPL-2.0-or-later (ビルドした実行ファイルを任意のライセンスで配布できる例外条項付き) |
| NumPy | BSD-3-Clause |
| PyYAML, setuptools, cffi | MIT |
| pycryptodome | BSD-2-Clause / Public Domain |
| pywin32 | PSF-2.0 |

Everything 本体と es.exe (voidtools) は同梱していません。
