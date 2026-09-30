<# Validate VS2022 ARM64 libraries while keeping all build tools x64-hosted. #>
param(
  [string]$Installation = "",
  [string]$ChromiumVersion = ""
)
$ErrorActionPreference = "Stop"
if (-not $Installation) {
  $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
  if (-not (Test-Path -LiteralPath $vswhere)) { throw "vswhere.exe is not available: $vswhere" }
  $installations = @(& $vswhere -latest -products * -version '[17.0,18.0)' `
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 Microsoft.VisualStudio.Component.VC.Tools.ARM64 `
    -property installationPath)
  $discoveryExit = $LASTEXITCODE
  if ($discoveryExit -ne 0) { throw "VS2022 discovery failed (vswhere exit $discoveryExit)" }
  $Installation = $installations | Select-Object -First 1
  if (-not $Installation) {
    throw "Install VS2022 components Microsoft.VisualStudio.Component.VC.Tools.x86.x64 and Microsoft.VisualStudio.Component.VC.Tools.ARM64 using the existing VS installer --add"
  }
}
$Installation = $Installation.Trim()
$toolsetFile = Join-Path $Installation "VC\Auxiliary\Build\Microsoft.VCToolsVersion.default.txt"
if (-not (Test-Path -LiteralPath $toolsetFile)) { throw "VS2022 default toolset version is missing: $toolsetFile" }
$toolset = (Get-Content -LiteralPath $toolsetFile -Raw).Trim()
$vc = Join-Path $Installation "VC\Tools\MSVC\$toolset"
& "$PSScriptRoot\ensure-windows-sdk.ps1" -Arch arm64 -ChromiumVersion $ChromiumVersion
foreach ($path in @(
  (Join-Path $vc "bin\Hostx64\x64\cl.exe"),
  (Join-Path $vc "bin\Hostx64\arm64\cl.exe"),
  (Join-Path $vc "lib\x64\libcmt.lib"),
  (Join-Path $vc "lib\arm64\libcmt.lib"),
  (Join-Path $vc "lib\arm64\msvcrt.lib")
)) {
  if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-Item -LiteralPath $path).Length -eq 0) {
    throw "Windows ARM64 prerequisite is missing or empty: $path; install VS2022 ARM64 C++ tools"
  }
}
$redistRoot = Join-Path $Installation "VC\Redist\MSVC"
$redist = Get-ChildItem -LiteralPath $redistRoot -Directory |
  Where-Object { $_.Name -match '^14\.\d+\.\d+$' } |
  Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1
if (-not $redist) { throw "VS2022 VC143 redistributable directory is missing: $redistRoot" }
foreach ($name in @("msvcp140.dll", "msvcp140_atomic_wait.dll", "vccorlib140.dll", "vcruntime140.dll")) {
  $path = Join-Path $redist.FullName "arm64\Microsoft.VC143.CRT\$name"
  if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-Item -LiteralPath $path).Length -eq 0) {
    throw "VS2022 ARM64 VC143 runtime is missing or empty: $path"
  }
}
$env:GYP_MSVS_OVERRIDE_PATH = $Installation
$env:vs2022_install = $Installation
Write-Host "==> VS2022 ARM64 target libraries and x64 host tools verified"
