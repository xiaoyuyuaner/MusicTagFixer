$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

python -m py_compile music_tag_fixer.py
python -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --name "音乐标签与云盘修复工具" `
  --collect-data opencc `
  --hidden-import opencc `
  music_tag_fixer.py

Write-Host "构建完成：$ProjectRoot\dist\音乐标签与云盘修复工具"
