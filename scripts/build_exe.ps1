# 配布用の実行ファイル (dist\VirtualDiskMokuroku\VirtualDiskMokuroku.exe) を PyInstaller で作成する。
# 事前に: pip install pyinstaller PySide6  (拡張コンテキストを含める場合は README 記載のライブラリも)
# Everything 本体と es.exe は同梱しない。利用者には docs\Everything導入手順.md を案内する。
#
# -Exclude で、導入済みでも同梱しないライブラリを指定できる。
#   例: pwsh scripts\build_exe.ps1 -Exclude py7zr,pycdlib

param([string[]]$Exclude = @())

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
# 暗号化と拡張コンテキスト用のライブラリは、導入されているものだけ取り込む
foreach ($module in "cryptography", "PIL", "tinytag", "charset_normalizer", "olefile", "py7zr", "rarfile", "pycdlib") {
    if ($Exclude -contains $module) {
        $arguments += @("--exclude-module", $module)
        continue
    }
    python -c "import $module" 2>$null
    if ($LASTEXITCODE -eq 0) { $arguments += @("--hidden-import", $module) }
}
$arguments += (Join-Path $root "VirtualDiskMokuroku.pyw")

python @arguments
if ($LASTEXITCODE -ne 0) { throw "PyInstaller が失敗しました" }

$docs = Join-Path $root "dist\VirtualDiskMokuroku\docs"
New-Item -ItemType Directory -Force $docs | Out-Null
Copy-Item (Join-Path $root "docs\Everything導入手順.md") $docs
# README と、同梱ライブラリのライセンス一式 (LGPL などは本文の添付が必要)
$dist = Join-Path $root "dist\VirtualDiskMokuroku"
Copy-Item (Join-Path $root "README.md"), (Join-Path $root "LICENSE"), (Join-Path $root "THIRD-PARTY-NOTICES.md") $dist
Copy-Item (Join-Path $root "licenses") $dist -Recurse -Force
Write-Host "完了: $(Join-Path $root 'dist\VirtualDiskMokuroku\VirtualDiskMokuroku.exe')"
