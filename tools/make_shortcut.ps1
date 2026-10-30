param(
  [Parameter(Mandatory=$true)][string]$Target,
  [Parameter(Mandatory=$true)][string]$LnkPath,
  [string]$WorkDir = "",
  [string]$Icon = ""
)
# 创建/覆盖一个 .lnk 快捷方式。被 打包.bat 调用。
#
# 为什么单独放一个 ps1 而不是在 bat 里内联 powershell -Command：
# cmd 的命令行要按控制台代码页（本机 936/GBK）编码，中文路径拼在 -Command 里
# 极易被拆错引号或转码成乱码，而参数形式由系统做编码转换，稳。
# 本文件刻意保持纯 ASCII，避免 PowerShell 读无 BOM 的 UTF-8 时按 GBK 解码出错。
$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $Target)) {
  Write-Error "target not found: $Target"
  exit 1
}
if (-not $WorkDir) {
  $WorkDir = Split-Path -Parent $Target
}

$sh = New-Object -ComObject WScript.Shell
$sc = $sh.CreateShortcut($LnkPath)
$sc.TargetPath = $Target
$sc.WorkingDirectory = $WorkDir
$sc.Description = 'Mi Home PC'
if ($Icon) {
  $sc.IconLocation = $Icon
} else {
  $sc.IconLocation = "$Target,0"
}
$sc.Save()

if (Test-Path -LiteralPath $LnkPath) {
  Write-Output "shortcut ok: $LnkPath -> $Target"
} else {
  Write-Error "shortcut not created: $LnkPath"
  exit 1
}
