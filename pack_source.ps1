# 打包“源码交付”压缩包：默认排除旧 helper exe、__pycache__、本机 config.json、
# 构建产物和测试临时目录，并生成 SHA256SUMS.txt 便于核对。
#
#   powershell -ExecutionPolicy Bypass -File .\pack_source.ps1
#   powershell -ExecutionPolicy Bypass -File .\pack_source.ps1 -IncludeHelperExe   # 仅当 exe 确为新编译
#
# 说明：默认不包含 caxa_helper.exe，避免把旧二进制冒充新版本；工作设备请用
# build_all.ps1 -Helper 重新编译并核对 SHA-256。

[CmdletBinding()]
param(
    [string]$Output = (Join-Path (Split-Path $PSScriptRoot -Parent) ("dsh_fix_source_{0}.zip" -f (Get-Date -Format "yyyyMMdd_HHmmss"))),
    [switch]$IncludeHelperExe
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

$excludeDirs = @(
    "__pycache__", ".pytest_cache", "build", "dist", "tests\_tmp", ".git"
)
$excludeFiles = @(
    "config.json", "*.pyc", "*.pyo", "STATUS.json.tmp"
)
if (-not $IncludeHelperExe) {
    $excludeFiles += "caxa_helper.exe"
}

$staging = Join-Path ([System.IO.Path]::GetTempPath()) ("dsh_fix_pack_" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $staging | Out-Null

try {
    Push-Location $Root
    $files = Get-ChildItem -Recurse -File -ErrorAction SilentlyContinue | Where-Object {
        $relative = ($_.FullName.Substring($Root.Length).TrimStart('\', '/') -replace '\\', '/')
        foreach ($dir in @("__pycache__", "build", "dist", ".git", ".pytest_cache")) {
            if (("/" + $relative + "/") -like "*/$dir/*") { return $false }
        }
        if ($relative -like "tests/_tmp/*") { return $false }
        if ($relative -like "tests/probe_*" -or $relative -like "tests/tmp*") { return $false }
        foreach ($pattern in $excludeFiles) {
            if ($_.Name -like $pattern) { return $false }
        }
        return $true
    }
    Pop-Location

    $manifest = @()
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($Root.Length).TrimStart('\', '/')
        $destination = Join-Path $staging $relative
        New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force | Out-Null
        Copy-Item $file.FullName $destination -Force
        $manifest += ("{0}  {1}" -f (Get-FileHash -Algorithm SHA256 -Path $file.FullName).Hash.ToLowerInvariant(), $relative.Replace('\', '/'))
    }
    $manifest | Set-Content -Path (Join-Path $staging "SHA256SUMS.txt") -Encoding utf8

    if (Test-Path $Output) { Remove-Item $Output -Force }
    Compress-Archive -Path (Join-Path $staging "*") -DestinationPath $Output -CompressionLevel Optimal
    Write-Host ("已生成：{0}" -f $Output)
    Write-Host ("文件数：{0}；SHA256：{1}" -f $files.Count, (Get-FileHash -Algorithm SHA256 -Path $Output).Hash.ToLowerInvariant())
    if (-not $IncludeHelperExe) {
        Write-Host "未包含 caxa_helper.exe（默认排除旧二进制）。请在工作设备用 build_all.ps1 -Helper 编译。" -ForegroundColor Yellow
    }
} finally {
    Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
}
