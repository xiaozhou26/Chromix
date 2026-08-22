<#
  One stage of the multi-stage GitHub-hosted Windows x64 build.

  Modeled on ungoogled-chromium-windows' staged CI: each workflow job runs this
  script under a self-imposed time budget. When the budget runs out before ninja
  finishes, the whole build tree (C:\c, mtimes preserved via 7z -mtc=on) is split
  into multi-volume archives that the next job restores and resumes, because a
  single GitHub job cannot exceed 6 hours.

  Layout (fixed absolute path so ninja build files stay valid across stages):
    C:\c\depot_tools            depot_tools checkout
    C:\c\chromium\.gclient      gclient config
    C:\c\chromium\src           chromium source at CHROMIUM_VERSION + Fortress patches
    C:\c\chromium\src\out\Fortress   build output
    C:\parts\p1..p4             7z volumes distributed round-robin for upload
    C:\restore                  artifacts downloaded by the workflow step

  Outputs ($GITHUB_OUTPUT):
    finished      true when ninja returned 0 and the portable bundle was packaged
    upload_parts  true when C:\parts\p1..p4 hold resume volumes to upload

  Usage (from the workflow):
    powershell build\windows\ci-stage.ps1 -StageIndex 3 -FromArtifact
#>
[CmdletBinding()]
param(
  [int]$StageIndex = 1,
  [switch]$FromArtifact
)
$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$ChromiumVersion = (Get-Content "$Repo\CHROMIUM_VERSION").Trim()

$Root = "C:\c"
$Chromium = "$Root\chromium"
$Src = "$Chromium\src"
$OutDir = "$Src\out\Fortress"
$PartsDir = "C:\parts"

# Job-level timeout is 355 min; keep a hard margin inside it.
$Deadline = (Get-Date).AddMinutes(300)
$PackReserveMin = 40   # time kept aside for 7z volume creation at the end

function Write-OutVar($k, $v) {
  if ($env:GITHUB_OUTPUT) { Add-Content -Path $env:GITHUB_OUTPUT -Value "$k=$v" }
  Write-Host "==> outvar $k=$v"
}
function Get-RemainingMin { [int]((New-TimeSpan -Start (Get-Date) -End $Deadline).TotalMinutes) }

# Run a console command as a tracked child process: stream tail of its output
# into the Actions log, kill the whole tree on timeout. Returns exit code
# (124 on timeout). $File/$ArgList must need no quoting beyond -ArgumentList.
function Invoke-Tracked {
  param([string]$File, [string]$ArgList, [string]$Cwd, [int]$TimeoutSec, [switch]$Quiet)
  $log = "$env:TEMP\ci-tracked.log"; $err = "$env:TEMP\ci-tracked.err"
  Remove-Item $log, $err -ErrorAction SilentlyContinue
  $p = Start-Process -FilePath $File -ArgumentList $ArgList -WorkingDirectory $Cwd `
       -PassThru -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $err
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $tick = 0
  while (-not $p.HasExited) {
    if ($sw.Elapsed.TotalSeconds -gt $TimeoutSec) {
      Write-Host "==> timeout after $([int]$sw.Elapsed.TotalMinutes) min; killing process tree"
      & taskkill.exe /PID $p.Id /T /F 2>$null | Out-Null
      $p.WaitForExit(); Start-Sleep -Seconds 2
      return 124
    }
    Start-Sleep -Seconds 10
    $tick++
    if (-not $Quiet -and ($tick % 6) -eq 0 -and (Test-Path $log)) {
      Get-Content $log -Tail 3 | ForEach-Object { Write-Host "    | $_" }
    }
  }
  if (Test-Path $err) { Get-Content $err -Tail 5 | ForEach-Object { Write-Host "  ! | $_" } }
  $code = $p.ExitCode
  if ($null -eq $code) { $code = 1 }
  return $code
}

function Get-FreeGB { [math]::Round((Get-PSDrive C).Free / 1GB, 1) }

function Free-Disk {
  Write-Host "==> disk before cleanup: $(Get-FreeGB) GB free"
  $targets = @(
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
  )
  foreach ($t in $targets) {
    if (Test-Path $t) {
      Write-Host "  removing $t"
      Remove-Item -Recurse -Force $t -ErrorAction SilentlyContinue
    }
  }
  # Drop SDK versions other than the one Chromium pins (10.0.26100.0).
  $kits = "${env:ProgramFiles(x86)}\Windows Kits\10"
  foreach ($sub in "bin", "Include", "Lib", "UnionMetadata", "References") {
    $parent = Join-Path $kits $sub
    if (Test-Path $parent) {
      Get-ChildItem $parent -Directory | Where-Object { $_.Name -match '^10\.0\.' -and $_.Name -ne "10.0.26100.0" } |
        ForEach-Object {
          Write-Host "  removing SDK $($_.FullName)"
          Remove-Item -Recurse -Force $_.FullName -ErrorAction SilentlyContinue
        }
    }
  }
  Write-Host "==> disk after cleanup: $(Get-FreeGB) GB free"
}

# gn gen runs vs_toolchain.py copy_dlls, which requires dbghelp.dll from the
# Windows SDK "Debugging Tools" feature — not preinstalled on GitHub runners.
function Install-Debuggers {
  $dbg = "${env:ProgramFiles(x86)}\Windows Kits\10\Debuggers\x64\dbghelp.dll"
  if (Test-Path $dbg) { Write-Host "==> debugging tools already present"; return }
  Write-Host "==> installing Windows SDK Debugging Tools (feature OptionId.WindowsDesktop.Debuggers)"
  New-Item -ItemType Directory -Force -Path $Root | Out-Null
  $iso = "$Root\winsdk.iso"
  for ($i = 1; $i -le 5; $i++) {
    & curl.exe -sSL -o $iso "https://go.microsoft.com/fwlink/?linkid=2348707"
    if ((Test-Path $iso) -and ((Get-Item $iso).Length -gt 10MB)) { break }
    Write-Host "  iso download attempt $i failed; retrying"; Start-Sleep -Seconds 10
  }
  if (-not (Test-Path $iso) -or ((Get-Item $iso).Length -lt 10MB)) { throw "Windows SDK ISO download failed" }
  $img = Mount-DiskImage -ImagePath $iso -StorageType ISO -PassThru
  $letter = ($img | Get-Volume).DriveLetter
  try {
    $setup = Start-Process -FilePath "$letter`:\WinSDKSetup.exe" `
      -ArgumentList "/features","OptionId.WindowsDesktop.Debuggers","/q","/norestart" -PassThru -Wait
    Write-Host "  WinSDKSetup exit: $($setup.ExitCode)"
  } finally {
    Dismount-DiskImage -ImagePath $iso | Out-Null
    Remove-Item $iso -Force -ErrorAction SilentlyContinue
  }
  if (-not (Test-Path $dbg)) { throw "dbghelp.dll still missing after Debugging Tools install" }
  Write-Host "==> debugging tools installed"
}

Write-Host "==> Chromix CI stage $StageIndex | Chromium $ChromiumVersion | remaining budget $(Get-RemainingMin) min"
Write-OutVar finished false
Write-OutVar upload_parts false

# ---------------------------------------------------------------- environment
Free-Disk
Install-Debuggers
git config --global core.longpaths true
# depot_tools (after its first self-update) refuses to run when a global
# gitconfig exists unless this flag is set.
git config --global depot-tools.allowGlobalGitConfig true

New-Item -ItemType Directory -Force -Path $Root | Out-Null
if (-not (Test-Path "$Root\depot_tools\gclient.bat")) {
  for ($i = 1; $i -le 5; $i++) {
    & git clone --depth 1 https://chromium.googlesource.com/chromium/tools/depot_tools.git "$Root\depot_tools"
    if ($LASTEXITCODE -eq 0) { break }
    Write-Host "  depot_tools clone attempt $i failed; retry in 30s"; Start-Sleep -Seconds 30
  }
  if ($LASTEXITCODE -ne 0) { throw "depot_tools clone failed" }
}
$env:PATH = "$Root\depot_tools;$env:PATH"
$env:DEPOT_TOOLS_WIN_TOOLCHAIN = "0"
$env:DEPOT_TOOLS_METRICS = "0"
$env:DEPOT_TOOLS_COLLECT_METRICS = "0"
Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue

# ---------------------------------------------------------------- restore tree
if ($FromArtifact -and (Test-Path "C:\restore\tree.7z.001")) {
  Write-Host "==> restoring build tree from previous stage ($(Get-RemainingMin) min left)"
  & 7z.exe x "C:\restore\tree.7z.001" -o"$Root" -y | Select-Object -Last 3
  if ($LASTEXITCODE -ne 0) { throw "7z restore failed (exit $LASTEXITCODE)" }
  Remove-Item C:\restore -Recurse -Force -ErrorAction SilentlyContinue
  Write-Host "==> restore done; disk: $(Get-FreeGB) GB free; remaining $(Get-RemainingMin) min"
}

# ---------------------------------------------------------------- fetch + sync
if (-not (Test-Path "$Src\.chromix-synced")) {
  # Hand-written .gclient: clone src from the GitHub mirror, which is fast and
  # reliable from Azure runner IPs (googlesource rate-limits them hard; it
  # killed the initial 2 GB src clone ~10 min in on the first CI attempt).
  # DEPS dependencies still come from googlesource via the retry loop below.
  New-Item -ItemType Directory -Force -Path $Chromium | Out-Null
  if (-not (Test-Path "$Chromium\.gclient")) {
    @'
solutions = [
  {
    "name": "src",
    "url": "https://github.com/chromium/chromium.git",
    "custom_deps": {},
    "custom_vars": {},
  },
]
'@ | Set-Content -Path "$Chromium\.gclient" -Encoding ASCII
  }

  # gclient sync pinned to the tag: googlesource rate-limits (HTTP 429)
  # transiently, so retry within the remaining budget; already-cloned deps are
  # skipped on re-run, so partial syncs resume where they left off.
  $syncOk = $false
  $attempt = 0
  while ((Get-RemainingMin) -gt ($PackReserveMin + 60)) {
    $attempt++
    $budgetSec = ((Get-RemainingMin) - ($PackReserveMin + 45)) * 60
    if ($budgetSec -lt 300) { break }
    Write-Host "==> gclient sync attempt $attempt (budget $([int]($budgetSec/60)) min)"
    $rc = Invoke-Tracked -File "cmd.exe" -ArgList "/c gclient sync --revision src@$ChromiumVersion -D --no-history --reset --jobs 8" -Cwd $Chromium -TimeoutSec $budgetSec
    if ($rc -eq 0) { $syncOk = $true; break }
    Write-Host "  sync attempt failed (exit $rc); retrying in 60s"
    Start-Sleep -Seconds 60
  }
  if (-not $syncOk) {
    Write-Host "==> sync budget exhausted; handing partial tree to next stage"
    Write-OutVar upload_parts true
    . "$PSScriptRoot\ci-parts.ps1" -Root $Root -PartsDir $PartsDir
    return
  }
  # gclient leaves src detached at the pinned revision; verify before marking.
  Push-Location $Src
  try {
    $head = & git rev-parse HEAD
    $want = & git rev-parse "refs/tags/$ChromiumVersion^{commit}"
    if ($head -ne $want) { throw "src is at $head, expected tag $ChromiumVersion ($want)" }
  } finally { Pop-Location }
  Set-Content -Path "$Src\.chromix-synced" -Value $ChromiumVersion

  # Apply Fortress patches (only after a fully synced tree at the pinned tag).
  Push-Location $Src
  try {
    foreach ($rel in Get-Content "$Repo\patches\series") {
      if (-not $rel.Trim()) { continue }
      Write-Host "  applying $rel"
      & git apply --3way --whitespace=nowarn (Join-Path $Repo $rel)
      if ($LASTEXITCODE -ne 0) { throw "patch failed: $rel" }
    }
  } finally { Pop-Location }
  Set-Content -Path "$Src\.chromix-patched" -Value $ChromiumVersion
  Write-Host "==> source ready (synced + patched); disk: $(Get-FreeGB) GB free; remaining $(Get-RemainingMin) min"
}
else {
  Write-Host "==> source tree already synced + patched (stage $StageIndex resume)"
}

# ---------------------------------------------------------------- build
if (-not (Test-Path "$OutDir\build.ninja")) {
  Write-Host "==> gn gen"
  $gnArgs = (Get-Content "$Repo\build\args.windows.gn") -join "`n"
  Push-Location $Src
  try {
    & gn gen out\Fortress --args="$gnArgs"
    if ($LASTEXITCODE -ne 0) { throw "gn gen failed (exit $LASTEXITCODE)" }
  } finally { Pop-Location }
}

$ninjaBudget = (Get-RemainingMin) - $PackReserveMin
if ($ninjaBudget -lt 20) {
  Write-Host "==> not enough budget left to build (<20 min); handing tree to next stage"
  Write-OutVar upload_parts true
  . "$PSScriptRoot\ci-parts.ps1" -Root $Root -PartsDir $PartsDir
  return
}
Write-Host "==> autoninja budget: $ninjaBudget min"
$rc = Invoke-Tracked -File "cmd.exe" -ArgList "/c autoninja -C out\Fortress -j 4 chrome" -Cwd $Src -TimeoutSec ($ninjaBudget * 60)

if ($rc -eq 0) {
  Write-Host "==> BUILD COMPLETE; packaging portable bundle"
  New-Item -ItemType Directory -Force -Path "$Root\dist" | Out-Null
  & "$Repo\build\windows\package-win.ps1" -Out $OutDir -Dest "$Root\dist"
  Write-OutVar finished true
  return
}

Write-Host "==> ninja stopped (exit $rc, budget or mid-build failure); handing tree to next stage"
Write-OutVar upload_parts true
. "$PSScriptRoot\ci-parts.ps1" -Root $Root -PartsDir $PartsDir
