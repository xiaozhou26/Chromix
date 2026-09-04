<#
  One stage of the GitHub-hosted Windows x64 build.

  The source is prepared through the same pinned ungoogled-chromium pipeline as
  build.ps1. Each stage restores C:\c\chromix, resumes ninja, and snapshots the
  tree before the GitHub job deadline.
#>
[CmdletBinding()]
param(
  [int]$StageIndex = 1,
  [int]$MaxStages = 12,
  [switch]$FromArtifact,
  [string]$UpstreamArtifactPath = "",
  [switch]$ValidateOnly
)
$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$Revisions = Import-PowerShellDataFile (Join-Path $Repo "build\ungoogled-revisions.psd1")

$Root = "C:\c"
$WorkDir = "$Root\chromix"
$Src = "$WorkDir\src"
$OutDir = "$Src\out\Chromix"
$PartsDir = "C:\parts"
$Deadline = (Get-Date).AddMinutes(300)
$PackReserveMin = 40

function Write-OutVar($key, $value) {
  if ($env:GITHUB_OUTPUT) { Add-Content -Path $env:GITHUB_OUTPUT -Value "$key=$value" }
  Write-Host "==> outvar $key=$value"
}

function Get-RemainingMin {
  return [int]((New-TimeSpan -Start (Get-Date) -End $Deadline).TotalMinutes)
}

function Test-LastStage { return $StageIndex -ge $MaxStages }

function Invoke-Tracked {
  param(
    [string]$File,
    [string]$ArgList,
    [string]$Cwd,
    [int]$TimeoutSec,
    [switch]$Quiet,
    [switch]$FullFailureOutput
  )
  $log = "$env:TEMP\ci-tracked.log"
  $err = "$env:TEMP\ci-tracked.err"
  $wrapperName = "ci-tracked-$PID-$([Guid]::NewGuid().ToString('N'))"
  $wrapper = Join-Path $env:TEMP "$wrapperName.cmd"
  $status = Join-Path $env:TEMP "$wrapperName.exit"
  Remove-Item $log, $err, $wrapper, $status -ErrorAction SilentlyContinue

  $cmdFile = $File.Replace("%", "%%")
  $cmdArgs = $ArgList.Replace("%", "%%")
  $cmdStatus = $status.Replace("%", "%%")
  $wrapperLines = @(
    "@echo off",
    "`"$cmdFile`" $cmdArgs",
    'set "ci_tracked_exit=%ERRORLEVEL%"',
    ">`"$cmdStatus`" echo %ci_tracked_exit%",
    "exit /b %ci_tracked_exit%"
  )

  try {
    [IO.File]::WriteAllLines($wrapper, $wrapperLines, [Text.Encoding]::ASCII)
    $process = Start-Process -FilePath $env:COMSPEC `
      -ArgumentList "/d /s /c `"`"$wrapper`"`"" -WorkingDirectory $Cwd `
      -PassThru -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $err
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    $tick = 0
    while (-not $process.HasExited) {
      if ($stopwatch.Elapsed.TotalSeconds -gt $TimeoutSec) {
        Write-Host "==> timeout after $([int]$stopwatch.Elapsed.TotalMinutes) min; killing process tree"
        try { & taskkill.exe /PID $process.Id /T /F 2>&1 | Out-Null } catch {}
        $process.WaitForExit()
        Start-Sleep -Seconds 2
        return 124
      }
      Start-Sleep -Seconds 10
      $tick++
      if (-not $Quiet -and ($tick % 6) -eq 0 -and (Test-Path $log)) {
        Get-Content $log -Tail 3 | ForEach-Object { Write-Host "    | $_" }
      }
    }
    $process.WaitForExit()

    $code = 1
    if (-not (Test-Path -LiteralPath $status -PathType Leaf)) {
      Write-Host "==> tracked process exit status file is missing: $status; treating as failure"
    } else {
      $statusText = Get-Content -LiteralPath $status -Raw -ErrorAction SilentlyContinue
      $statusValue = if ($null -eq $statusText) { "" } else { $statusText.Trim() }
      $parsedCode = 0
      if ($statusValue -notmatch '^-?\d+$' -or
          -not [int]::TryParse($statusValue, [ref]$parsedCode)) {
        $displayStatus = if ($statusValue) { $statusValue } else { "<empty>" }
        Write-Host "==> tracked process exit status is invalid: '$displayStatus'; treating as failure"
      } else {
        $code = $parsedCode
        Write-Host "==> tracked process exit code: $code"
      }
    }
    if ($code -ne 0) {
      if (Test-Path $log) {
        if ($FullFailureOutput) {
          Write-Host "==> tracked process stdout (complete)"
          Get-Content $log | ForEach-Object { Write-Host "  ! | $_" }
        } else {
          Get-Content $log -Tail 200 | ForEach-Object { Write-Host "  ! | $_" }
        }
      }
      if (Test-Path $err) { Get-Content $err | ForEach-Object { Write-Host "  ! | $_" } }
    }
    return $code
  } finally {
    Remove-Item $wrapper, $status -ErrorAction SilentlyContinue
  }
}

function Get-FreeGB { return [math]::Round((Get-PSDrive C).Free / 1GB, 1) }

function Resolve-7Zip {
  $command = Get-Command 7z.exe -ErrorAction SilentlyContinue
  if ($command) { return $command.Source }
  $installed = "$env:ProgramFiles\7-Zip\7z.exe"
  if (Test-Path $installed) { return $installed }
  throw "7z.exe is not available"
}

function Save-Handoff {
  param([ValidateSet("Synced", "Unsynced")] [string]$Mode)
  if (Test-LastStage) { throw "build did not finish within $MaxStages stages" }
  Write-OutVar upload_parts true
  . "$PSScriptRoot\ci-parts.ps1" -Root $Root -PartsDir $PartsDir -Mode $Mode
}

function Assert-CiScripts {
  foreach ($path in @(
    "$PSScriptRoot\ci-stage.ps1",
    "$PSScriptRoot\ci-parts.ps1",
    "$PSScriptRoot\prepare-ungoogled.ps1",
    "$PSScriptRoot\update-restored-source.ps1",
    "$PSScriptRoot\package-win.ps1"
  )) {
    $tokens = $null
    $errors = $null
    [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors) | Out-Null
    if ($errors.Count -gt 0) { throw "$path failed PowerShell parsing: $($errors[0].Message)" }
  }
  Write-Host "==> CI PowerShell preflight passed"
}

function Free-Disk {
  Write-Host "==> disk before cleanup: $(Get-FreeGB) GB free"
  foreach ($target in @(
    "C:\Android",
    "C:\Program Files\Android",
    "C:\Program Files (x86)\Android",
    "C:\ghcup",
    "C:\Program Files\Haskell",
    "C:\Program Files\MySQL",
    "C:\Program Files\PostgreSQL",
    "C:\Program Files\MongoDB",
    "C:\Miniconda3",
    "C:\Program Files\LLVM",
    "C:\ProgramData\chocolatey\cache",
    "C:\Windows\SoftwareDistribution\Download"
  )) {
    if (Test-Path $target) { Remove-Item -Recurse -Force $target -ErrorAction SilentlyContinue }
  }
  Write-Host "==> disk after cleanup: $(Get-FreeGB) GB free"
}

function Initialize-VisualStudio {
  $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
  if (-not (Test-Path $vswhere)) { throw "vswhere.exe is not available: $vswhere" }
  $installation = (& $vswhere -latest -products * `
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
    -property installationPath).Trim()
  if (-not $installation) { throw "Visual Studio 2022 C++ tools are not installed" }
  $devCmd = Join-Path $installation "Common7\Tools\VsDevCmd.bat"
  if (-not (Test-Path $devCmd)) { throw "VsDevCmd.bat is missing: $devCmd" }

  $command = "`"$devCmd`" -no_logo -arch=x64 -host_arch=x64 >nul && set"
  $environment = & $env:COMSPEC /d /s /c $command
  if ($LASTEXITCODE -ne 0) { throw "VsDevCmd.bat failed with exit $LASTEXITCODE" }
  foreach ($line in $environment) {
    if ($line -match '^([^=]+)=(.*)$') {
      [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
    }
  }
  $compiler = (Get-Command cl.exe -ErrorAction Stop).Source
  Write-Host "==> Visual Studio compiler: $compiler"
}

function Install-Debuggers {
  $dbghelp = "${env:ProgramFiles(x86)}\Windows Kits\10\Debuggers\x64\dbghelp.dll"
  if (Test-Path $dbghelp) { return }
  Write-Host "==> installing Windows SDK Debugging Tools"
  New-Item -ItemType Directory -Force -Path $Root | Out-Null
  $iso = "$Root\winsdk.iso"
  for ($attempt = 1; $attempt -le 5; $attempt++) {
    & curl.exe -sSL -o $iso "https://go.microsoft.com/fwlink/?linkid=2348707"
    if ((Test-Path $iso) -and ((Get-Item $iso).Length -gt 10MB)) { break }
    Start-Sleep -Seconds 10
  }
  if (-not (Test-Path $iso) -or ((Get-Item $iso).Length -lt 10MB)) { throw "Windows SDK ISO download failed" }
  $image = Mount-DiskImage -ImagePath $iso -StorageType ISO -PassThru
  $letter = ($image | Get-Volume).DriveLetter
  try {
    $setup = Start-Process -FilePath "$letter`:\WinSDKSetup.exe" `
      -ArgumentList "/features", "OptionId.WindowsDesktop.Debuggers", "/q", "/norestart" -PassThru -Wait
    if ($setup.ExitCode -ne 0) { throw "WinSDKSetup failed with exit $($setup.ExitCode)" }
  } finally {
    Dismount-DiskImage -ImagePath $iso | Out-Null
    Remove-Item $iso -Force -ErrorAction SilentlyContinue
  }
  if (-not (Test-Path $dbghelp)) { throw "dbghelp.dll is missing after Debugging Tools install" }
}

Write-Host "==> Chromix CI stage $StageIndex | Chromium $($Revisions.ChromiumVersion) | remaining $(Get-RemainingMin) min"
Write-OutVar finished false
Write-OutVar upload_parts false
Assert-CiScripts
Free-Disk
Initialize-VisualStudio
Install-Debuggers
git config --global core.longpaths true

Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
$env:DEPOT_TOOLS_WIN_TOOLCHAIN = "0"
$env:DEPOT_TOOLS_METRICS = "0"
$env:DEPOT_TOOLS_COLLECT_METRICS = "0"

if ($UpstreamArtifactPath) {
  if ($StageIndex -ne 1) { throw "upstream artifact import must start at stage 1" }
  $upstreamExtract = "C:\upstream-build-artifact"
  Remove-Item $upstreamExtract -Recurse -Force -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force -Path $upstreamExtract | Out-Null
  Write-Host "==> importing ungoogled-chromium-windows artifact from $UpstreamArtifactPath"
  $innerArchive = Join-Path $UpstreamArtifactPath "artifacts.zip"
  if (-not (Test-Path $innerArchive)) { throw "upstream artifact does not contain artifacts.zip" }
  Remove-Item $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
  $sevenZip = Resolve-7Zip
  & $sevenZip x $innerArchive -o"$WorkDir" -y | Select-Object -Last 3
  if ($LASTEXITCODE -ne 0) { throw "upstream build tree extraction failed" }
  $upstreamSrc = Join-Path $WorkDir "build\src"
  if (-not (Test-Path (Join-Path $upstreamSrc "BUILD.gn"))) {
    throw "upstream artifact is missing build/src/BUILD.gn"
  }
  $versionFile = Join-Path $upstreamSrc "chrome\VERSION"
  if (-not (Test-Path $versionFile)) { throw "upstream artifact is missing chrome/VERSION" }
  $versionParts = @{}
  foreach ($line in Get-Content $versionFile) {
    if ($line -match '^([A-Z]+)=(\d+)$') { $versionParts[$Matches[1]] = $Matches[2] }
  }
  $upstreamVersion = @("MAJOR", "MINOR", "BUILD", "PATCH") |
    ForEach-Object { $versionParts[$_] }
  $upstreamVersion = $upstreamVersion -join "."
  if ($upstreamVersion -ne $Revisions.ChromiumVersion) {
    throw "upstream artifact targets Chromium $upstreamVersion, expected $($Revisions.ChromiumVersion)"
  }
  Move-Item $upstreamSrc $Src
  Remove-Item (Join-Path $WorkDir "build") -Recurse -Force -ErrorAction SilentlyContinue
  $upstreamOut = Join-Path $Src "out\Default"
  if (Test-Path $upstreamOut) {
    New-Item -ItemType Directory -Force -Path (Split-Path $OutDir) | Out-Null
    Move-Item $upstreamOut $OutDir
  }
  Set-Content -Path (Join-Path $Src ".chromix-source-unpacked") `
    -Value $Revisions.ChromiumVersion -Encoding ASCII
  Set-Content -Path (Join-Path $Src ".chromix-ungoogled-core") `
    -Value $Revisions.UngoogledCommit -Encoding ASCII
  Set-Content -Path (Join-Path $Src ".chromix-ungoogled-windows") `
    -Value $Revisions.UngoogledWindowsCommit -Encoding ASCII
  Remove-Item $upstreamExtract -Recurse -Force -ErrorAction SilentlyContinue
  Write-Host "==> upstream Chromium source/object tree imported; Chromix patches remain to apply"
}

if ($FromArtifact -and -not (Test-Path "C:\restore\tree.7z.001")) {
  throw "resume artifact missing: C:\restore\tree.7z.001"
}
if (-not $FromArtifact -and $StageIndex -gt 1) {
  throw "stage $StageIndex requires -FromArtifact"
}
if ($FromArtifact) {
  $sevenZip = Resolve-7Zip
  & $sevenZip t "C:\restore\tree.7z.001" | Select-Object -Last 3
  if ($LASTEXITCODE -ne 0) { throw "7z archive test failed" }
  & $sevenZip x "C:\restore\tree.7z.001" -o"$Root" -y | Select-Object -Last 3
  if ($LASTEXITCODE -ne 0) { throw "7z restore failed" }
  Remove-Item C:\restore -Recurse -Force -ErrorAction SilentlyContinue

  $unpackedMarker = Join-Path $Src ".chromix-source-unpacked"
  $readyMarker = Join-Path $Src ".chromix-source-ready"
  $restoredVersion = ""
  if (Test-Path $unpackedMarker) {
    $restoredVersion = (Get-Content $unpackedMarker -Raw).Trim()
  } elseif (Test-Path $readyMarker) {
    $restoredVersion = ((Get-Content $readyMarker -Raw).Trim() -split '\|', 2)[0]
  }
  if ($restoredVersion -and $restoredVersion -ne $Revisions.ChromiumVersion) {
    Write-Host "==> restored tree targets Chromium $restoredVersion; preserving tooling/download_cache and removing incompatible src/out"
    Remove-Item $Src -Recurse -Force
  } else {
    & "$PSScriptRoot\update-restored-source.ps1" -Src $Src -OutDir $OutDir
  }
}

if (-not (Test-Path (Join-Path $Src ".chromix-source-ready"))) {
  if ((Get-RemainingMin) -lt ($PackReserveMin + 30)) {
    Save-Handoff -Mode Unsynced
    return
  }
  $prepareDeadline = [DateTimeOffset]::new($Deadline).ToUnixTimeSeconds()
  try {
    & "$PSScriptRoot\prepare-ungoogled.ps1" -Root $WorkDir -Repo $Repo `
      -DeadlineEpoch $prepareDeadline -ReserveMinutes $PackReserveMin
  } catch {
    if ($_.Exception.Message -like "PREPARE_BUDGET_EXHAUSTED:*") {
      Save-Handoff -Mode Unsynced
      return
    }
    throw
  }
}

$UngoogledTooling = Join-Path $WorkDir "tooling\ungoogled-chromium"
$WindowsTooling = Join-Path $WorkDir "tooling\ungoogled-chromium-windows"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
python (Join-Path $Repo "tools\merge_gn_args.py") (Join-Path $OutDir "args.gn") `
  (Join-Path $UngoogledTooling "flags.gn") `
  (Join-Path $WindowsTooling "flags.windows.gn") `
  (Join-Path $Repo "build\args.windows.gn")
if ($LASTEXITCODE -ne 0) { throw "GN argument merge failed" }

$env:PATH = "$(Join-Path $Src 'third_party\ninja');$(Join-Path $Src 'third_party\node\win');$env:PATH"
Push-Location $Src
try {
  $gn = Join-Path $OutDir "gn.exe"
  if (-not (Test-Path $gn)) {
    python tools\gn\bootstrap\bootstrap.py -o $gn --skip-generate-buildfiles
    if ($LASTEXITCODE -ne 0) { throw "GN bootstrap failed" }
  }
  if (-not (Test-Path "third_party\rust-toolchain\bin\bindgen.exe")) {
    python tools\rust\build_bindgen.py --skip-test
    if ($LASTEXITCODE -ne 0) { throw "bindgen build failed" }
  }
  & $gn gen $OutDir --fail-on-unused-args
  if ($LASTEXITCODE -ne 0) { throw "gn gen failed" }
} finally {
  Pop-Location
}

if ($ValidateOnly) {
  Write-Host "==> validate-only: building V8 Torque generation target"
  $validationBudget = [Math]::Max(60, (Get-RemainingMin) - 10)
  $validationRc = Invoke-Tracked -File (Join-Path $Src "third_party\ninja\ninja.exe") `
    -ArgList "-C `"$OutDir`" -j 1 -v gen/v8/torque-generated/bit-field-asserts.cc" `
    -Cwd $Src -TimeoutSec ($validationBudget * 60) -FullFailureOutput
  if ($validationRc -ne 0) { throw "V8 Torque validation failed (exit $validationRc)" }
  Write-Host "==> validate-only: gn gen and V8 Torque generation passed"
  Write-OutVar finished true
  return
}

$ninjaBudget = (Get-RemainingMin) - $PackReserveMin
if ($ninjaBudget -lt 20) {
  Save-Handoff -Mode Synced
  return
}
$rc = Invoke-Tracked -File (Join-Path $Src "third_party\ninja\ninja.exe") `
  -ArgList "-C `"$OutDir`" -j 4 chrome" -Cwd $Src -TimeoutSec ($ninjaBudget * 60)

if ($rc -eq 0) {
  New-Item -ItemType Directory -Force -Path "$Root\dist" | Out-Null
  & "$PSScriptRoot\package-win.ps1" -Out $OutDir -Dest "$Root\dist"
  Write-OutVar finished true
  return
}
if ($rc -eq 124) {
  Save-Handoff -Mode Synced
  return
}
throw "ninja failed (exit $rc)"
