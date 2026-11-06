# 图纸上传/识别两个入口的 Windows 构建脚本（在工作设备上运行）
#
# 用法示例（在工作设备、已安装 CAXA 2011 客户端与 Python 依赖后）：
#   powershell -ExecutionPolicy Bypass -File .\build_all.ps1 -All
#   powershell -ExecutionPolicy Bypass -File .\build_all.ps1 -Uploader -Recognizer
#   powershell -ExecutionPolicy Bypass -File .\build_all.ps1 -Helper -Force
#   powershell -ExecutionPolicy Bypass -File .\build_all.ps1 -All -PlanOnly   # 只检查环境/路径
#
# 说明：
# - Helper（caxa_helper.exe）必须引用现场 CAXA 2011 的 DLL，只有工作设备能编译；
#   本脚本检测不到 DLL 时会明确失败，不会拿旧 exe 冒充新版本。
# - 上传端（upload_main.py）与识别端（recognize_main.py）分别用 PyInstaller 打包，
#   输出目录默认为 dist\uploader 与 dist\recognizer。
# - 本脚本不访问网络、不启动 CAXA、不探测服务器。

[CmdletBinding()]
param(
    [switch]$All,
    [switch]$Helper,
    [switch]$Uploader,
    [switch]$Recognizer,
    [switch]$Force,
    [switch]$PlanOnly,
    [string]$OutRoot = ""
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
if (-not $OutRoot) { $OutRoot = Join-Path $Root "dist" }
$AppDir = Join-Path $Root "app"
$HelperDir = Join-Path $AppDir "caxa\caxa_helper"

if ($All) { $Helper = $true; $Uploader = $true; $Recognizer = $true }
if (-not ($Helper -or $Uploader -or $Recognizer)) {
    Write-Host "未指定构建目标。使用 -All 或 -Helper/-Uploader/-Recognizer。" -ForegroundColor Yellow
    exit 2
}

function Get-Sha256([string]$Path) {
    if (-not (Test-Path $Path)) { return "(missing)" }
    return (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLowerInvariant()
}

function Assert-File([string]$Path, [string]$What) {
    if (-not (Test-Path $Path)) { throw "缺少$What：$Path" }
}

Write-Host "== 构建环境 =="
Write-Host "工作区：$Root"
$python = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $python) { throw "找不到 python，请先安装 Python 3.11+ 并加入 PATH。" }
Write-Host ("Python：{0} ({1})" -f (& python --version 2>&1), $python.Source)

if ($Uploader -or $Recognizer) {
    $pyinstaller = ""
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $pyinstaller = (& python -c "import PyInstaller, sys; print(PyInstaller.__version__)" 2>$null)
    } catch {
        $pyinstaller = ""
    }
    $ErrorActionPreference = $previousPreference
    if (-not $pyinstaller -or $LASTEXITCODE -ne 0) {
        if ($PlanOnly) {
            Write-Host "PyInstaller 未安装（PlanOnly：仅提示，实际构建前需 pip install pyinstaller）。" -ForegroundColor Yellow
        } else {
            throw "未安装 PyInstaller。请先运行：python -m pip install pyinstaller"
        }
    } else {
        Write-Host "PyInstaller：$pyinstaller"
    }
}

if ($Helper) {
    Write-Host "`n== C# helper =="
    $helperScript = Join-Path $HelperDir "build_helper.ps1"
    Assert-File $helperScript "helper 构建脚本"
    if ($PlanOnly) {
        & powershell -ExecutionPolicy Bypass -File $helperScript -PlanOnly
    } else {
        $helperArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $helperScript)
        if ($Force) { $helperArgs += "-Force" }
        & powershell @helperArgs
        if ($LASTEXITCODE -ne 0) { throw "helper 编译失败（退出码 $LASTEXITCODE）" }
    }
}

function Invoke-PyInstaller(
    [string]$Entry,
    [string]$Name,
    [string]$DistName,
    [string[]]$ExtraArguments = @()
) {
    $target = Join-Path $OutRoot $DistName
    if ((Test-Path $target) -and -not $Force) {
        throw "输出目录已存在：$target（加 -Force 覆盖，避免误覆盖已验收产物）"
    }
    $arguments = @(
        "-m", "PyInstaller",
        "--noconfirm", "--clean", "--windowed", "--onefile",
        "--name", $Name,
        "--paths", $AppDir,
        "--distpath", $OutRoot,
        "--workpath", (Join-Path $Root "build\$DistName"),
        "--specpath", (Join-Path $Root "build\spec"),
        "--collect-all", "tkinterdnd2"
    )
    $arguments += $ExtraArguments
    $arguments += $Entry
    Write-Host ("PyInstaller：{0} -> {1}" -f $Entry, $target)
    if ($PlanOnly) { return }
    & python @arguments
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败（退出码 $LASTEXITCODE）：$Entry" }
    if (-not (Test-Path $target)) { throw "未生成输出：$target" }
    $exe = Get-ChildItem -Path $target -Filter *.exe -Recurse | Select-Object -First 1
    if ($exe) { Write-Host ("产物：{0}  SHA256={1}" -f $exe.FullName, (Get-Sha256 $exe.FullName)) }
}

if ($Uploader) {
    Write-Host "`n== 上传端（upload_main.py） =="
    Assert-File (Join-Path $AppDir "upload_main.py") "上传端入口"
    $helperExe = Join-Path $HelperDir "caxa_helper.exe"
    if (-not $PlanOnly) {
        Assert-File $helperExe "已按当前源码编译的 CAXA helper"
    }
    $uploaderExtra = @()
    if (Test-Path $helperExe) {
        $uploaderExtra += "--add-binary"
        $uploaderExtra += "$helperExe;caxa/caxa_helper"
    }
    Invoke-PyInstaller `
        (Join-Path $AppDir "upload_main.py") `
        "图纸上传助手" `
        "uploader" `
        $uploaderExtra
}

if ($Recognizer) {
    Write-Host "`n== 识别端（recognize_main.py） =="
    Assert-File (Join-Path $AppDir "recognize_main.py") "识别端入口"
    Invoke-PyInstaller (Join-Path $AppDir "recognize_main.py") "图纸识别助手" "recognizer"
}

Write-Host "`n完成。产物在 $OutRoot；发布源码请用 pack_source.ps1（默认排除旧 exe）。"
