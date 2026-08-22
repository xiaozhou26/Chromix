<#
  Chromix native Windows build.

  Prereqs:
    - Visual Studio 2022 with "Desktop development with C++" + Windows 11 SDK (with Debugging Tools)
    - depot_tools on PATH, set DEPOT_TOOLS_WIN_TOOLCHAIN=0
    - ~100 GB free disk, long build time
    - git, python3

  Usage (from a Developer PowerShell):
    pwsh build\windows\build.ps1 -WorkDir D:\chromix-build
#>
[CmdletBinding()]
param(
  [string]$WorkDir = "$PSScriptRoot\..\..\.chromix-build-win",
  [string]$ChromiumVersion = "",
  [switch]$Resume,
  [int]$Jobs = 8
)
$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path
if (-not $ChromiumVersion) { $ChromiumVersion = (Get-Content "$Repo\CHROMIUM_VERSION").Trim() }
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

Write-Host "==> Chromix Windows build | Chromium $ChromiumVersion | $WorkDir"
if ($Resume) { Write-Host "==> Resume mode: skipping fetch/checkout/sync/patches (tree already prepared)" }

# 1. depot_tools
if (-not (Test-Path "$WorkDir\depot_tools")) {
  git clone --depth 1 https://chromium.googlesource.com/chromium/tools/depot_tools.git "$WorkDir\depot_tools"
}
$env:PATH = "$WorkDir\depot_tools;$env:PATH"
$env:DEPOT_TOOLS_WIN_TOOLCHAIN = "0"
# On non-English Windows, global PYTHONUTF8/PYTHONIOENCODING=utf-8 makes build
# scripts crash decoding native-tool output (e.g. icacls GBK text) in UTF-8.
# Drop them so Python uses the system codepage for subprocesses.
Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
# vs_toolchain.py only probes %ProgramFiles%\Microsoft Visual Studio\2022\<edition>;
# if VS/BuildTools lives under Program Files (x86), point it there explicitly.
if (-not $env:vs2022_install) {
  $bt = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2022\BuildTools"
  if (Test-Path $bt) { $env:vs2022_install = $bt }
}
$env:DEPOT_TOOLS_METRICS = "0"
$env:DEPOT_TOOLS_COLLECT_METRICS = "0"

if (-not $Resume) {
  # 2. fetch + sync to the pinned tag
  if (-not (Test-Path "$WorkDir\chromium\src")) {
    New-Item -ItemType Directory -Force -Path "$WorkDir\chromium" | Out-Null
    Push-Location "$WorkDir\chromium"; fetch --nohooks --no-history chromium
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw "fetch chromium failed (exit $LASTEXITCODE)" }
    Pop-Location
  }
  Push-Location "$WorkDir\chromium\src"
  git fetch --depth 1 origin "refs/tags/${ChromiumVersion}:refs/tags/${ChromiumVersion}"
  if ($LASTEXITCODE -ne 0) { throw "git fetch tag failed (exit $LASTEXITCODE)" }
  git checkout -f "tags/$ChromiumVersion"
  if ($LASTEXITCODE -ne 0) { throw "git checkout tag failed (exit $LASTEXITCODE)" }

  # gclient sync downloads third_party deps; googlesource transiently rate-limits (HTTP 429),
  # so retry a few times with a backoff before giving up.
  $maxSyncAttempts = 5
  for ($attempt = 1; $attempt -le $maxSyncAttempts; $attempt++) {
    Write-Host "  gclient sync attempt $attempt/$maxSyncAttempts"
    gclient sync -D --no-history --reset --jobs $Jobs
    if ($LASTEXITCODE -eq 0) { break }
    if ($attempt -lt $maxSyncAttempts) {
      Write-Host "  gclient sync failed (exit $LASTEXITCODE); retrying in 30s..."
      Start-Sleep -Seconds 30
    }
  }
  if ($LASTEXITCODE -ne 0) { throw "gclient sync failed after $maxSyncAttempts attempts (exit $LASTEXITCODE)" }

  # 3. apply Chromix patches (git apply works cross-platform)
  foreach ($rel in Get-Content "$Repo\patches\series") {
    if (-not $rel.Trim()) { continue }
    $p = Join-Path $Repo $rel
    Write-Host "  applying $rel"
    git apply --3way --whitespace=nowarn $p
    if ($LASTEXITCODE -ne 0) { throw "patch failed (needs re-anchoring on $ChromiumVersion): $rel" }
  }
}
else {
  Push-Location "$WorkDir\chromium\src"
  $currentTag = git describe --tags 2>$null
  if ($LASTEXITCODE -ne 0 -or $currentTag.Trim() -ne $ChromiumVersion) {
    Pop-Location
    throw "Resume requested but $WorkDir\chromium\src is not at tag $ChromiumVersion (got: $currentTag)"
  }
  Write-Host "  resume: tree already at $ChromiumVersion with patches applied; skipping to build"
}

# 4. configure + build
$gnArgs = (Get-Content "$Repo\build\args.windows.gn") -join "`n"
gn gen out\Chromix --args="$gnArgs"
# Cap parallel jobs: an explicit -j overrides autoninja's all-cores default,
# which saturates CPU and can OOM the linker on RAM-constrained machines.
autoninja -C out\Chromix -j $Jobs chrome
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "autoninja failed (exit $LASTEXITCODE)" }

Pop-Location
Write-Host "==> Done: $WorkDir\chromium\src\out\Chromix\chrome.exe"
& "$WorkDir\chromium\src\out\Chromix\chrome.exe" --version
