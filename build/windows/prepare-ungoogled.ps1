<#
  Prepare a pinned native-Windows source tree:
    Chromium tarball -> ungoogled core -> Windows overlay -> prune -> Chromix patches.

  The script is resumable. Each completed layer writes a versioned marker under
  the source tree. A mismatched marker stops the build instead of mixing layers.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory)] [string]$Root,
  [Parameter(Mandatory)] [string]$Repo,
  [int]$DeadlineEpoch = 0,
  [int]$ReserveMinutes = 45,
  [ValidateSet("x64", "arm64")]
  [string]$Arch = $(if ($env:CHROMIX_TARGET_ARCH) { $env:CHROMIX_TARGET_ARCH } else { "x64" })
)
$ErrorActionPreference = "Stop"
if ($Arch -cnotin @("x64", "arm64")) { throw "Arch/CHROMIX_TARGET_ARCH must be x64 or arm64" }

$Revisions = & "$PSScriptRoot\read-platform-pins.ps1" -Repo $Repo
$Tooling = Join-Path $Root "tooling"
$Ungoogled = Join-Path $Tooling "ungoogled-chromium"
$Windows = Join-Path $Tooling "ungoogled-chromium-windows"
$DownloadCache = Join-Path $Root "download_cache"
$Src = Join-Path $Root "src"
if ($Arch -eq "arm64" -or (Test-Path (Join-Path $Root ".chromix-target-arch"))) {
  & "$PSScriptRoot\assert-target-arch.ps1" -WorkDir $Root -Arch $Arch -Initialize
}
$PatchExe = ""
$PatchSafetyOptions = @("--fuzz=0", "--binary", "--get=0", "--no-backup-if-mismatch", "--reject-file=-")
$env:PATCH_GET = "0"
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
  # The merge loop is Python, ported verbatim from upstream ungoogled-chromium
  # -windows: this prepare script runs under Windows PowerShell 5.1 on GitHub,
  # and a PowerShell transcription of the copy loop silently dropped
  # bin\cargo.exe on the first CI run while pwsh replications looked perfect.
  Invoke-Checked $Python @(
    (Join-Path $Repo "build\windows\prep_rust_toolchain.py"),
    "--third-party-root", (Join-Path $Src "third_party"), "--arch", $Arch
  )
  foreach ($binary in @("cargo.exe", "rustc.exe")) {
    if (-not (Test-Path (Join-Path $destination "bin\$binary"))) {
      Get-ChildItem (Join-Path $Src "third_party") -Directory |
        Where-Object { $_.Name -like "rust-toolchain*" } | ForEach-Object {
          Write-Host "    toolchain dir: $($_.Name)"
        }
      throw "Rust toolchain merge did not produce bin\$binary"
    }
  }
}

function Assert-Arm64RustToolchain {
  if ($Arch -ne "arm64") { return }
  Invoke-Checked $Python @(
    (Join-Path $Repo "build\windows\prep_rust_toolchain.py"),
    "--third-party-root", (Join-Path $Src "third_party"), "--arch", $Arch, "--verify-only"
  )
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

function Invoke-ChromixPatches(
    [string]$ResumePatch = "",
    [bool]$ResumePatchIsClean = $false,
    [string]$ResumePatchHash = "") {
  $resuming = [bool]$ResumePatch
  foreach ($line in Get-Content (Join-Path $Repo "patches\series")) {
    $rel = ($line -split "#", 2)[0].Trim()
    if (-not $rel) { continue }
    if ($resuming -and $rel -ne $ResumePatch) {
      Write-Host "    skipping completed $rel"
      continue
    }

    $patch = Join-Path $Repo $rel
    $patchHash = (Get-FileHash -Algorithm SHA256 $patch).Hash.ToLowerInvariant()
    Write-Host "    $rel"
    Set-Marker ".chromix-patch-in-progress" "$rel|$patchHash"
    Push-Location $Src
    try {
      if ($resuming) {
        $patchChanged = $ResumePatchHash -and $ResumePatchHash -ne $patchHash
        if ($patchChanged) {
          Write-Host "      patch content changed since the interrupted attempt"
        }
        if ($ResumePatchIsClean) {
          Assert-Budget "$PatchExe -p1 --batch --forward -i $patch"
          & $PatchExe -p1 --batch --forward -i $patch @PatchSafetyOptions
          if ($LASTEXITCODE -ne 0) {
            throw "$PatchExe failed to apply rolled-back patch $rel"
          }
          Write-Host "      rolled-back patch applied"
        } else {
          Assert-Budget "$PatchExe -p1 --batch --forward --dry-run -i $patch"
          & $PatchExe -p1 --batch --forward --dry-run -i $patch @PatchSafetyOptions | Out-Null
          if ($LASTEXITCODE -eq 0) {
            & $PatchExe -p1 --batch --forward -i $patch @PatchSafetyOptions
            if ($LASTEXITCODE -ne 0) {
              throw "$PatchExe failed to apply interrupted patch $rel"
            }
            Write-Host "      interrupted patch had not changed the source"
          } else {
            if ($patchChanged) {
              throw "interrupted patch content changed and the new patch cannot apply cleanly: $rel"
            }
            Assert-Budget "$PatchExe -p1 --batch --reverse --dry-run -i $patch"
            & $PatchExe -p1 --batch --reverse --dry-run -i $patch @PatchSafetyOptions | Out-Null
            if ($LASTEXITCODE -eq 0) {
              Write-Host "      interrupted patch was already complete"
            } else {
              Assert-Budget "$PatchExe -p1 --batch --reverse --force -i $patch"
              & $PatchExe -p1 --batch --reverse --force -i $patch @PatchSafetyOptions | Out-Null
              & $PatchExe -p1 --batch --forward --dry-run -i $patch @PatchSafetyOptions | Out-Null
              if ($LASTEXITCODE -ne 0) {
                throw "$PatchExe could not roll back interrupted patch $rel"
              }
              & $PatchExe -p1 --batch --forward -i $patch @PatchSafetyOptions
              if ($LASTEXITCODE -ne 0) {
                throw "$PatchExe failed to reapply interrupted patch $rel"
              }
              Write-Host "      interrupted patch rolled back and reapplied"
            }
          }
        }
        $resuming = $false
      } else {
        Assert-Budget "$PatchExe -p1 --batch --forward -i $patch"
        & $PatchExe -p1 --batch --forward -i $patch @PatchSafetyOptions
        if ($LASTEXITCODE -ne 0) {
          throw "$PatchExe failed to apply $rel"
        }
      }
    } finally {
      Pop-Location
    }
  }
  if ($resuming) { throw "resume patch is not present in patches\series: $ResumePatch" }
  Remove-Item (Join-Path $Src ".chromix-patch-in-progress") -Force -ErrorAction SilentlyContinue
  Get-ChildItem $Src -Filter "*.rej" -Recurse -File -ErrorAction SilentlyContinue |
    Remove-Item -Force
}

function Resolve-HostPatch {
  $candidates = [Collections.Generic.List[string]]::new()
  foreach ($gitCommand in @(Get-Command git.exe -All -ErrorAction SilentlyContinue)) {
    if ([string]::IsNullOrWhiteSpace($gitCommand.Source)) { continue }
    $directory = Split-Path $gitCommand.Source
    foreach ($root in @($directory, (Split-Path $directory), (Split-Path (Split-Path $directory)))) {
      if (-not [string]::IsNullOrWhiteSpace($root)) {
        $candidates.Add((Join-Path $root "usr\bin\patch.exe"))
      }
    }
  }
  foreach ($base in @($env:ProgramW6432, $env:ProgramFiles, ${env:ProgramFiles(x86)})) {
    if (-not [string]::IsNullOrWhiteSpace($base)) {
      $candidates.Add((Join-Path $base "Git\usr\bin\patch.exe"))
    }
  }
  foreach ($command in @(Get-Command patch.exe -ErrorAction SilentlyContinue -All)) {
    if (-not [string]::IsNullOrWhiteSpace($command.Source)) { $candidates.Add($command.Source) }
  }
  $candidates = @($candidates | Select-Object -Unique | Where-Object {
    Test-Path -LiteralPath $_ -PathType Leaf
  })
  if ($candidates.Count -eq 0) { throw "no host patch.exe candidates; install Git for Windows" }
  $probeArgs = @(
    (Join-Path $Repo "tools\apply_restored_patches.py"), "--select-patch-bin",
    "--src", $Src, "--repo", $Repo, "--core", $Ungoogled,
    "--platform-tooling", $Windows, "--platform", "windows", "--patch-bin", $candidates[0]
  )
  foreach ($candidate in $candidates | Select-Object -Skip 1) {
    $probeArgs += @("--patch-candidate", $candidate)
  }
  Assert-Budget "host patch capability probe"
  $selected = @(& $Python @probeArgs)
  if ($LASTEXITCODE -ne 0 -or $selected.Count -ne 1 -or [string]::IsNullOrWhiteSpace($selected[0])) {
    throw "no compatible host patch.exe passed the capability probe"
  }
  Write-Host "==> verified host patch: $($selected[0])"
  return $selected[0]
}

function Assert-RestoredToolchain {
  foreach ($relative in @(
    "third_party\git\usr\bin\patch.exe",
    "third_party\ninja\ninja.exe",
    "third_party\node\win\node.exe",
    "third_party\llvm-build\Release+Asserts\bin\clang-cl.exe",
    "third_party\llvm-build\Release+Asserts\bin\lld-link.exe",
    "third_party\llvm-build\Release+Asserts\bin\llvm-lib.exe",
    "third_party\llvm-build\Release+Asserts\bin\llvm-ml.exe",
    "third_party\rust-toolchain\bin\cargo.exe",
    "third_party\rust-toolchain\bin\rustc.exe",
    "third_party\rust-toolchain\INSTALLED_VERSION",
    "third_party\dawn\tools\golang\windows-amd64\bin\go.exe",
    "third_party\devtools-frontend\src\third_party\esbuild\esbuild.exe"
  )) {
    $path = Join-Path $Src $relative
    if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-Item -LiteralPath $path).Length -eq 0) {
      throw "restored Windows build dependency is missing or empty: $relative"
    }
  }
  $libraries = @{
    "third_party\rust-toolchain\lib\rustlib\x86_64-pc-windows-msvc\lib" = @(
      "libstd-*.rlib", "libcore-*.rlib", "liballoc-*.rlib", "libcompiler_builtins-*.rlib"
    )
    "third_party\rust-toolchain\bin" = @("rustc_driver*.dll")
    "third_party\llvm-build\Release+Asserts\lib\clang" = @(
      "stddef.h", "stdarg.h", "clang_rt.builtins-x86_64.lib"
    )
  }
  foreach ($relative in $libraries.Keys) {
    foreach ($pattern in $libraries[$relative]) {
      $files = @(Get-ChildItem -LiteralPath (Join-Path $Src $relative) -Filter $pattern -File -Recurse -ErrorAction SilentlyContinue)
      if ($files.Count -eq 0 -or ($files | Where-Object { $_.Length -eq 0 })) {
        throw "restored Windows toolchain libraries are missing or empty: $relative\$pattern"
      }
    }
  }
  if ((Test-Path (Join-Path $Src "third_party\rust-toolchain\bin\bindgen.exe")) -and
      -not (Test-Path -LiteralPath (Join-Path $Src "third_party\rust-toolchain\bin\libclang.dll") -PathType Leaf)) {
    throw "restored bindgen is missing its libclang.dll runtime"
  }
}

function Assert-PreparedLayers {
  $markers = @{
    ".chromix-source-unpacked" = $Revisions.ChromiumVersion
    ".chromix-ungoogled-core" = $Revisions.UngoogledCommit
    ".chromix-ungoogled-windows" = $Revisions.UngoogledWindowsCommit
    ".chromix-binaries-pruned" = $Revisions.UngoogledCommit
    ".chromix-patches" = $patchSetKey
  }
  if ($RestoredUpstream) {
    $markers[".chromix-domain-substituted"] = $Revisions.UngoogledCommit
    $markers[".chromix-toolchain-ready"] = $toolchainKey
  }
  foreach ($name in $markers.Keys) {
    if (-not (Test-Marker $name $markers[$name])) {
      throw "prepared source layer marker is missing or mismatched: $name"
    }
  }
}

$patchSetKey = Get-PatchSetKey
$versionKey = "$($Revisions.ChromiumVersion)|$($Revisions.UngoogledCommit)|$($Revisions.UngoogledWindowsCommit)|$patchSetKey"
$toolchainKey = "$($Revisions.UngoogledCommit)|$($Revisions.UngoogledWindowsCommit)"
$readyMarker = Join-Path $Src ".chromix-source-ready"
$RestoredUpstream = Test-Path (Join-Path $Src ".chromix-upstream-restored.json")
if (Get-ChildItem -LiteralPath $Root -Directory -Filter ".chromix-upstream-restore-*" -ErrorAction SilentlyContinue) {
  throw "upstream restore transaction was interrupted; use a clean work directory"
}
if (-not $RestoredUpstream -and (Test-Path (Join-Path $Src ".chromix-restored-patches.json"))) {
  throw "restored Chromix patches exist without an upstream receipt"
}
foreach ($name in @(".chromix-domain-substitution-in-progress", ".chromix-restored-patches-in-progress")) {
  if (Test-Path (Join-Path $Src $name)) { throw "source preparation was interrupted: $name; use a clean work directory" }
}
foreach ($name in @(".chromix-layer-in-progress", ".chromix-patch-in-progress")) {
  if ((Test-Path (Join-Path $Src $name)) -and ((Test-Path $readyMarker) -or $RestoredUpstream)) {
    throw "source layer is still in progress: $name; refusing to trust a ready marker or restored source"
  }
}
if ((Test-Path (Join-Path $Src ".chromix-patch-in-progress")) -and
    -not (Test-Path (Join-Path $Src ".chromix-layer-in-progress"))) {
  throw "orphaned Chromix patch progress marker; use a clean work directory"
}
if ($RestoredUpstream) {
  Invoke-Checked $Python @(
    (Join-Path $Repo "tools\restore_upstream_cache.py"), "--phase", "verify",
    "--platform", "windows", "--arch", $Arch, "--workdir", $Root
  )
}
if ((Test-Path $readyMarker) -and -not (Test-Marker ".chromix-source-ready" $versionKey)) {
  $preparedKey = (Get-Content $readyMarker -Raw).Trim()
  throw "prepared source key is $preparedKey, expected $versionKey; use a clean work directory"
}
if (Test-Path (Join-Path $Src ".chromix-domain-substituted")) {
  if (-not (Test-Marker ".chromix-domain-substituted" $Revisions.UngoogledCommit)) {
    throw "domain substitution marker does not match the pinned core commit"
  }
}
if (Test-Path $readyMarker) {
  Assert-PreparedLayers
  Assert-Arm64RustToolchain
  if (-not $RestoredUpstream) {
    Write-Host "==> source layers already prepared and verified: $versionKey"
    return
  }
}
if (-not $RestoredUpstream) { $PatchExe = Resolve-HostPatch }
$resumeChromixPatch = ""
$resumeChromixPatchHash = ""
$resumeChromixPatchIsClean = $false
if (Test-Path (Join-Path $Src ".chromix-layer-in-progress")) {
  $interruptedLayer = (Get-Content (Join-Path $Src ".chromix-layer-in-progress") -Raw).Trim()
  if ($interruptedLayer -eq "chromix") {
    Write-Host "==> retrying interrupted Chromix patch application in place"
    Get-ChildItem $Src -Filter "*.rej" -Recurse -File -ErrorAction SilentlyContinue |
      Remove-Item -Force
    $patchProgress = Join-Path $Src ".chromix-patch-in-progress"
    if (Test-Path $patchProgress) {
      $patchProgressValue = (Get-Content $patchProgress -Raw).Trim()
      $patchProgressParts = $patchProgressValue -split "\|", 2
      $resumeChromixPatch = $patchProgressParts[0]
      if ($patchProgressParts.Count -eq 2) {
        $resumeChromixPatchHash = $patchProgressParts[1]
      }
    } else {
      $legacyWebGL = Join-Path $Src "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc"
      if ((Test-Path $legacyWebGL) -and
          (Select-String -Path $legacyWebGL -SimpleMatch "void RecordWebGLCreate(" -Quiet) -and
          -not (Select-String -Path $legacyWebGL -SimpleMatch "RecordWebGLOp(63u" -Quiet)) {
        $resumeChromixPatch = "patches/0082-third_party-blink-renderer-modules-webgl-webgl_rendering_context_base-cc.patch"
        $legacyRollback = Join-Path $Repo "build\windows\recovery\chromium-152-webgl-0082-partial.patch"
        Write-Host "==> inferred legacy interrupted patch: $resumeChromixPatch"
        Assert-Budget "$PatchExe -p1 --batch --forward -i $legacyRollback"
        Push-Location $Src
        try {
          & $PatchExe -p1 --batch --forward -i $legacyRollback @PatchSafetyOptions
          if ($LASTEXITCODE -ne 0) {
            & $PatchExe -p1 --batch --reverse --dry-run -i $legacyRollback @PatchSafetyOptions | Out-Null
            if ($LASTEXITCODE -ne 0) {
              throw "$PatchExe failed to roll back the legacy interrupted WebGL patch"
            }
            Write-Host "      legacy WebGL rollback target already present"
          }
        } finally {
          Pop-Location
        }
        $resumeChromixPatchIsClean = $true
      } else {
        throw "interrupted Chromix source has no patch progress marker and could not be identified"
      }
    }
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

if ($RestoredUpstream) {
  Invoke-Checked $Python @(
    (Join-Path $Repo "tools\prepare_restored_build.py"), "--phase", "inspect",
    "--platform", "windows", "--arch", $Arch, "--workdir", $Root
  )
  Assert-RestoredToolchain
  Assert-Arm64RustToolchain
  $RestoredPatchExe = Resolve-HostPatch
  Write-Host "==> verified upstream core/Windows overlay/prune/domain layers; appending Chromix patches"
  $applyArgs = @(
    (Join-Path $Repo "tools\apply_restored_patches.py"),
    "--src", $Src, "--repo", $Repo, "--core", $Ungoogled,
    "--platform-tooling", $Windows, "--platform", "windows", "--patch-bin", $RestoredPatchExe
  )
  if (Test-Path $readyMarker) { $applyArgs += "--check" }
  Invoke-Checked $Python $applyArgs
  if (-not (Test-Path $readyMarker)) {
    Set-Marker ".chromix-source-unpacked" $Revisions.ChromiumVersion
    Set-Marker ".chromix-ungoogled-core" $Revisions.UngoogledCommit
    Set-Marker ".chromix-ungoogled-windows" $Revisions.UngoogledWindowsCommit
    Set-Marker ".chromix-binaries-pruned" $Revisions.UngoogledCommit
    Set-Marker ".chromix-patches" $patchSetKey
    Set-Marker ".chromix-domain-substituted" $Revisions.UngoogledCommit
    Set-Marker ".chromix-toolchain-ready" $toolchainKey
    Set-Marker ".chromix-source-ready" $versionKey
  }
  Write-Host "==> restored source ready and verified: $versionKey"
  return
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
  if (-not (Test-Path (Join-Path $Src "third_party\git\usr\bin\patch.exe"))) {
    throw "GNU patch.exe missing after Windows downloads"
  }
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
Assert-Arm64RustToolchain
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

if (-not (Test-Marker ".chromix-binaries-pruned" $Revisions.UngoogledCommit)) {
  Write-Host "==> pruning unneeded binaries"
  # Contingent paths include the Windows toolchains already installed above.
  Invoke-Checked $Python @(
    (Join-Path $Ungoogled "utils\prune_binaries.py"),
    "--keep-contingent-paths",
    $Src, (Join-Path $Ungoogled "pruning.list")
  )
  Set-Marker ".chromix-binaries-pruned" $Revisions.UngoogledCommit
}

if (-not (Test-Marker ".chromix-patches" $patchSetKey)) {
  Write-Host "==> applying Chromix patches"
  Set-Marker ".chromix-layer-in-progress" "chromix"
  Invoke-ChromixPatches $resumeChromixPatch $resumeChromixPatchIsClean $resumeChromixPatchHash
  Set-Marker ".chromix-patches" $patchSetKey
  Remove-Item (Join-Path $Src ".chromix-layer-in-progress") -Force
}

# Domain substitution is deferred until after build-time downloads. Applying it
# here rewrites toolchain URLs used by Chromium and the Windows overlay.
Set-Marker ".chromix-source-ready" $versionKey
Write-Host "==> source ready: $versionKey"
