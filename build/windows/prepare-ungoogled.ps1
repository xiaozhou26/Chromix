<#
  Prepare a pinned native-Windows source tree:
    Chromium tarball -> ungoogled core -> Windows overlay -> Chromix patches.

  The script is resumable. Each completed layer writes a versioned marker under
  the source tree. A mismatched marker stops the build instead of mixing layers.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory)] [string]$Root,
  [Parameter(Mandatory)] [string]$Repo,
  [int]$DeadlineEpoch = 0,
  [int]$ReserveMinutes = 45
)
$ErrorActionPreference = "Stop"

$Revisions = Import-PowerShellDataFile (Join-Path $Repo "build\ungoogled-revisions.psd1")
$Tooling = Join-Path $Root "tooling"
$Ungoogled = Join-Path $Tooling "ungoogled-chromium"
$Windows = Join-Path $Tooling "ungoogled-chromium-windows"
$DownloadCache = Join-Path $Root "download_cache"
$Src = Join-Path $Root "src"
$PatchExe = Join-Path $Src "third_party\git\usr\bin\patch.exe"
$Python = (Get-Command python.exe -ErrorAction Stop).Source

function Get-RemainingMinutes {
  if (-not $DeadlineEpoch) { return 2147483647 }
  return [int](($DeadlineEpoch - [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) / 60)
}

function Assert-Budget([string]$Step) {
  $left = Get-RemainingMinutes
  if ($left -le $ReserveMinutes) {
    throw "PREPARE_BUDGET_EXHAUSTED: $Step needs time reserved for the handoff ($left min left)"
  }
}

function Invoke-Checked {
  param([string]$File, [string[]]$Arguments, [string]$WorkingDirectory = $Repo)
  Assert-Budget "$File $($Arguments -join ' ')"
  Push-Location $WorkingDirectory
  try {
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
      throw "$File failed with exit $LASTEXITCODE`: $($Arguments -join ' ')"
    }
  } finally {
    Pop-Location
  }
}

function Ensure-Checkout {
  param([string]$Url, [string]$Path, [string]$Commit)
  if (-not (Test-Path (Join-Path $Path ".git"))) {
    New-Item -ItemType Directory -Force -Path (Split-Path $Path) | Out-Null
    Invoke-Checked git @("clone", "--filter=blob:none", "--no-checkout", $Url, $Path)
  }
  Invoke-Checked git @("fetch", "--depth", "1", "origin", $Commit) $Path
  Invoke-Checked git @("checkout", "--detach", "--force", $Commit) $Path
  $head = (& git -C $Path rev-parse HEAD).Trim()
  if ($head -ne $Commit) { throw "$Path is at $head, expected $Commit" }
}

function Get-PatchSetKey {
  $hasher = [Security.Cryptography.SHA256]::Create()
  try {
    $series = Join-Path $Repo "patches\series"
    $bytes = [Collections.Generic.List[byte]]::new()
    foreach ($line in Get-Content $series) {
      $rel = ($line -split "#", 2)[0].Trim()
      if (-not $rel) { continue }
      $path = Join-Path $Repo $rel
      if (-not (Test-Path $path)) { throw "patch listed in series is missing: $rel" }
      $bytes.AddRange([IO.File]::ReadAllBytes($path))
    }
    $payloadRoot = Join-Path $Repo "build\windows\lite-tarball-files"
    if (Test-Path $payloadRoot) {
      foreach ($path in Get-ChildItem $payloadRoot -Recurse -File | Sort-Object FullName) {
        $relative = $path.FullName.Substring($payloadRoot.Length).TrimStart('\')
        $bytes.AddRange([Text.Encoding]::UTF8.GetBytes($relative.Replace('\', '/')))
        $bytes.AddRange([IO.File]::ReadAllBytes($path.FullName))
      }
    }
    return ([BitConverter]::ToString($hasher.ComputeHash($bytes.ToArray())) -replace "-", "").ToLowerInvariant()
  } finally {
    $hasher.Dispose()
  }
}

function Test-Marker([string]$Name, [string]$Value) {
  $path = Join-Path $Src $Name
  return (Test-Path $path) -and ((Get-Content $path -Raw).Trim() -eq $Value)
}

function Set-Marker([string]$Name, [string]$Value) {
  Set-Content -Path (Join-Path $Src $Name) -Value $Value -Encoding ASCII
}

function Prepare-RustToolchain {
  $source = Join-Path $Src "third_party\rust-toolchain-x64"
  $destination = Join-Path $Src "third_party\rust-toolchain"
  $rustc = Join-Path $source "rustc\bin\rustc.exe"
  if (-not (Test-Path $rustc)) { throw "downloaded x64 rustc is missing: $rustc" }
  Remove-Item $destination -Recurse -Force -ErrorAction SilentlyContinue
  foreach ($part in @("bin", "lib")) {
    $target = Join-Path $destination $part
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Get-ChildItem $source -Directory | ForEach-Object {
      $payload = Join-Path $_.FullName $part
      if (Test-Path $payload) { Copy-Item (Join-Path $payload "*") $target -Recurse -Force }
    }
  }
  & $rustc --version | Set-Content -Encoding ASCII (Join-Path $destination "INSTALLED_VERSION")
  if ($LASTEXITCODE -ne 0) { throw "rustc version check failed" }
}

function Restore-LiteTarballFiles {
  $payloadRoot = Join-Path $Repo "build\windows\lite-tarball-files"
  if (-not (Test-Path $payloadRoot)) { return }

  Get-ChildItem $payloadRoot -Recurse -File | ForEach-Object {
    $relative = $_.FullName.Substring($payloadRoot.Length).TrimStart('\')
    $destination = Join-Path $Src $relative
    New-Item -ItemType Directory -Force -Path (Split-Path $destination) | Out-Null
    Copy-Item $_.FullName $destination -Force
    Write-Host "    restored lite-tarball file: $relative"
  }
}

function Invoke-PatchDirectory([string]$Directory) {
  Invoke-Checked $Python @(
    (Join-Path $Ungoogled "utils\patches.py"),
    "apply", "--patch-bin", $PatchExe, $Src, $Directory
  )
}

function Invoke-ChromixPatches {
  foreach ($line in Get-Content (Join-Path $Repo "patches\series")) {
    $rel = ($line -split "#", 2)[0].Trim()
    if (-not $rel) { continue }
    $patch = Join-Path $Repo $rel
    Write-Host "    $rel"
    Assert-Budget "$PatchExe -p1 --batch --forward --force -i $patch"
    Push-Location $Src
    try {
      & $PatchExe -p1 --batch --forward --force -i $patch
      if ($LASTEXITCODE -ne 0) {
        & $PatchExe -p1 --batch --reverse --dry-run -i $patch | Out-Null
        if ($LASTEXITCODE -ne 0) {
          throw "$PatchExe could neither finish nor verify $rel"
        }
        Write-Host "      patch content already present after recovery"
      }
    } finally {
      Pop-Location
    }
  }
  Get-ChildItem $Src -Filter "*.rej" -Recurse -File -ErrorAction SilentlyContinue |
    Remove-Item -Force
}

$patchSetKey = Get-PatchSetKey
$versionKey = "$($Revisions.ChromiumVersion)|$($Revisions.UngoogledCommit)|$($Revisions.UngoogledWindowsCommit)|$patchSetKey"
if (Test-Marker ".chromix-source-ready" $versionKey) {
  Write-Host "==> source layers already prepared"
  return
}
if (Test-Path (Join-Path $Src ".chromix-source-ready")) {
  $preparedKey = (Get-Content (Join-Path $Src ".chromix-source-ready") -Raw).Trim()
  throw "prepared source key is $preparedKey, expected $versionKey; use a clean work directory"
}
if (Test-Path (Join-Path $Src ".chromix-layer-in-progress")) {
  $interruptedLayer = (Get-Content (Join-Path $Src ".chromix-layer-in-progress") -Raw).Trim()
  if ($interruptedLayer -eq "chromix") {
    Write-Host "==> retrying interrupted Chromix patch application in place"
    Get-ChildItem $Src -Filter "*.rej" -Recurse -File -ErrorAction SilentlyContinue |
      Remove-Item -Force
    Remove-Item (Join-Path $Src ".chromix-layer-in-progress") -Force
  } else {
    Write-Host "==> discarding a source tree interrupted during $interruptedLayer application"
    Remove-Item $Src -Recurse -Force
  }
}

New-Item -ItemType Directory -Force -Path $Root, $Tooling, $DownloadCache | Out-Null
Ensure-Checkout "https://github.com/ungoogled-software/ungoogled-chromium.git" $Ungoogled $Revisions.UngoogledCommit
Ensure-Checkout "https://github.com/ungoogled-software/ungoogled-chromium-windows.git" $Windows $Revisions.UngoogledWindowsCommit

$actualChromiumVersion = (Get-Content (Join-Path $Ungoogled "chromium_version.txt") -Raw).Trim()
if ($actualChromiumVersion -ne $Revisions.ChromiumVersion) {
  throw "ungoogled-chromium targets Chromium $actualChromiumVersion, expected $($Revisions.ChromiumVersion)"
}
$actualUngoogledVersion = "$actualChromiumVersion-$((Get-Content (Join-Path $Ungoogled 'revision.txt') -Raw).Trim())"
if ($actualUngoogledVersion -ne $Revisions.UngoogledVersion) {
  throw "ungoogled-chromium version is $actualUngoogledVersion, expected $($Revisions.UngoogledVersion)"
}
$actualWindowsVersion = "$actualUngoogledVersion.$((Get-Content (Join-Path $Windows 'revision.txt') -Raw).Trim())"
if ($actualWindowsVersion -ne $Revisions.UngoogledWindowsVersion) {
  throw "ungoogled-chromium-windows version is $actualWindowsVersion, expected $($Revisions.UngoogledWindowsVersion)"
}

if (-not (Test-Marker ".chromix-source-unpacked" $Revisions.ChromiumVersion)) {
  if (Test-Path $Src) { Remove-Item $Src -Recurse -Force }
  New-Item -ItemType Directory -Force -Path $Src | Out-Null
  Write-Host "==> retrieving Chromium $($Revisions.ChromiumVersion) tarball"
  Invoke-Checked $Python @(
    (Join-Path $Ungoogled "utils\downloads.py"), "retrieve",
    "-c", $DownloadCache, "-i", (Join-Path $Ungoogled "downloads.ini")
  )
  Write-Host "==> unpacking Chromium source"
  Invoke-Checked $Python @(
    (Join-Path $Ungoogled "utils\downloads.py"), "unpack",
    "-c", $DownloadCache, "-i", (Join-Path $Ungoogled "downloads.ini"),
    "--7z-path", "_use_registry", $Src
  )
  Write-Host "==> retrieving native Windows toolchain dependencies"
  Invoke-Checked $Python @(
    (Join-Path $Ungoogled "utils\downloads.py"), "retrieve",
    "-c", $DownloadCache, "-i", (Join-Path $Windows "downloads.ini")
  )
  Invoke-Checked $Python @(
    (Join-Path $Ungoogled "utils\prune_binaries.py"),
    $Src, (Join-Path $Ungoogled "pruning.list")
  )
  foreach ($directory in @(
    (Join-Path $Src "third_party\microsoft_dxheaders\src"),
    (Join-Path $Src "third_party\microsoft_webauthn\src"),
    (Join-Path $Src "third_party\devtools-frontend\src\third_party\esbuild")
  )) {
    if (Test-Path $directory) { Remove-Item $directory -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
  }
  Invoke-Checked $Python @(
    (Join-Path $Ungoogled "utils\downloads.py"), "unpack",
    "-c", $DownloadCache, "-i", (Join-Path $Windows "downloads.ini"),
    "--7z-path", "_use_registry", $Src
  )
  if (-not (Test-Path $PatchExe)) { throw "GNU patch.exe missing after Windows downloads: $PatchExe" }
  foreach ($tool in @(
    (Join-Path $Src "third_party\ninja\ninja.exe"),
    (Join-Path $Src "third_party\node\win\node.exe"),
    (Join-Path $Src "third_party\llvm-build\Release+Asserts\bin\clang-cl.exe"),
    (Join-Path $Src "third_party\rust-toolchain-x64\rustc\bin\rustc.exe")
  )) {
    if (-not (Test-Path $tool)) { throw "Windows build dependency is missing after unpack: $tool" }
  }
  Prepare-RustToolchain
  Set-Marker ".chromix-source-unpacked" $Revisions.ChromiumVersion
}
if (-not (Test-Path (Join-Path $Src "third_party\rust-toolchain\bin\rustc.exe"))) {
  Prepare-RustToolchain
}
Restore-LiteTarballFiles

if (-not (Test-Marker ".chromix-ungoogled-core" $Revisions.UngoogledCommit)) {
  Write-Host "==> applying ungoogled-chromium core patches"
  Set-Marker ".chromix-layer-in-progress" "ungoogled-core"
  Invoke-PatchDirectory (Join-Path $Ungoogled "patches")
  Set-Marker ".chromix-ungoogled-core" $Revisions.UngoogledCommit
  Remove-Item (Join-Path $Src ".chromix-layer-in-progress") -Force
}

if (-not (Test-Marker ".chromix-ungoogled-windows" $Revisions.UngoogledWindowsCommit)) {
  Write-Host "==> applying ungoogled-chromium-windows overlay"
  Set-Marker ".chromix-layer-in-progress" "ungoogled-windows"
  Invoke-PatchDirectory (Join-Path $Windows "patches")
  Set-Marker ".chromix-ungoogled-windows" $Revisions.UngoogledWindowsCommit
  Remove-Item (Join-Path $Src ".chromix-layer-in-progress") -Force
}

if (-not (Test-Marker ".chromix-patches" $patchSetKey)) {
  Write-Host "==> applying Chromix patches"
  Set-Marker ".chromix-layer-in-progress" "chromix"
  Invoke-ChromixPatches
  Set-Marker ".chromix-patches" $patchSetKey
  Remove-Item (Join-Path $Src ".chromix-layer-in-progress") -Force
}

# Domain substitution is deferred until after build-time downloads. Applying it
# here rewrites toolchain URLs used by Chromium and the Windows overlay.
Set-Marker ".chromix-source-ready" $versionKey
Write-Host "==> source ready: $versionKey"
