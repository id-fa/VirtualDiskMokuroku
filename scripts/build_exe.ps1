# 配布用の実行ファイル (dist\VirtualDiskMokuroku\VirtualDiskMokuroku.exe) を PyInstaller で作成する。
# 事前に: pip install pyinstaller PySide6  (拡張コンテキストを含める場合は README 記載のライブラリも)
# Everything 本体と es.exe は同梱しない。利用者には docs\Everything導入手順.md を案内する。

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$arguments = @(
    "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
    "--name", "VirtualDiskMokuroku",
    "--paths", (Join-Path $root "src"),
    "--collect-submodules", "virtualdiskmokuroku",
    "--distpath", (Join-Path $root "dist"),
    "--workpath", (Join-Path $root "build"),
    "--specpath", (Join-Path $root "build")
)
# 拡張コンテキスト用の任意ライブラリは、導入されているものだけ取り込む
foreach ($module in "PIL", "mutagen", "charset_normalizer", "olefile", "py7zr", "rarfile", "pycdlib") {
    python -c "import $module" 2>$null
    if ($LASTEXITCODE -eq 0) { $arguments += @("--hidden-import", $module) }
}
$arguments += (Join-Path $root "VirtualDiskMokuroku.pyw")

python @arguments
if ($LASTEXITCODE -ne 0) { throw "PyInstaller が失敗しました" }

$docs = Join-Path $root "dist\VirtualDiskMokuroku\docs"
New-Item -ItemType Directory -Force $docs | Out-Null
Copy-Item (Join-Path $root "docs\Everything導入手順.md") $docs
Copy-Item (Join-Path $root "README.md") (Join-Path $root "dist\VirtualDiskMokuroku")
Write-Host "完了: $(Join-Path $root 'dist\VirtualDiskMokuroku\VirtualDiskMokuroku.exe')"
