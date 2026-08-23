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
    C:\c\chromium\src           chromium source at CHROMIUM_VERSION + Chromix patches
    C:\c\chromium\src\out\Chromix   build output
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
  [int]$MaxStages = 12,
  [switch]$FromArtifact
)
$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$ChromiumVersion = (Get-Content "$Repo\CHROMIUM_VERSION").Trim()

$Root = "C:\c"
$Chromium = "$Root\chromium"
$Src = "$Chromium\src"
$OutDir = "$Src\out\Chromix"
$PartsDir = "C:\parts"

# Job-level timeout is 355 min; keep a hard margin inside it.
$Deadline = (Get-Date).AddMinutes(300)
$PackReserveMin = 40   # time kept aside for 7z volume creation at the end

function Write-OutVar($k, $v) {
  if ($env:GITHUB_OUTPUT) { Add-Content -Path $env:GITHUB_OUTPUT -Value "$k=$v" }
  Write-Host "==> outvar $k=$v"
}
function Get-RemainingMin { [int]((New-TimeSpan -Start (Get-Date) -End $Deadline).TotalMinutes) }
function Test-LastStage { $StageIndex -ge $MaxStages }

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
  $code = $p.ExitCode
  if ($null -eq $code) { $code = 1 }
  if ($code -ne 0) {
    # gclient prints most errors to stdout; surface the tail on failure so the
    # Actions log shows WHY a step died instead of just the exit code.
    if (Test-Path $log) { Get-Content $log -Tail 20 | ForEach-Object { Write-Host "  ! | $_" } }
    if (Test-Path $err) { Get-Content $err -Tail 10 | ForEach-Object { Write-Host "  ! | $_" } }
  }
  return $code
}

function Get-FreeGB { [math]::Round((Get-PSDrive C).Free / 1GB, 1) }

function Resolve-7Zip {
  $cmd = Get-Command 7z.exe -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $installed = "$env:ProgramFiles\7-Zip\7z.exe"
  if (Test-Path $installed) { return $installed }
  throw "7z.exe is not available"
}

function Save-Handoff {
  param([ValidateSet("Synced", "Unsynced")] [string]$Mode)
  if (Test-LastStage) {
    throw "build did not finish within $MaxStages stages"
  }
  Write-OutVar upload_parts true
  . "$PSScriptRoot\ci-parts.ps1" -Root $Root -PartsDir $PartsDir -Mode $Mode
}

function Assert-CiScripts {
  foreach ($path in @(
    "$PSScriptRoot\ci-stage.ps1",
    "$PSScriptRoot\ci-parts.ps1",
    "$PSScriptRoot\package-win.ps1"
  )) {
    $bytes = [IO.File]::ReadAllBytes($path)
    if ($bytes | Where-Object { $_ -gt 127 } | Select-Object -First 1) {
      throw "$path contains non-ASCII bytes; Windows PowerShell 5.1 may misparse UTF-8 without a BOM"
    }
    $tokens = $null
    $errors = $null
    [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors) | Out-Null
    if ($errors.Count -gt 0) {
      throw "$path failed PowerShell parsing: $($errors[0].Message)"
    }
  }
  Write-Host "==> CI PowerShell preflight passed"
}

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
# Windows SDK "Debugging Tools" feature - not preinstalled on GitHub runners.
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
Assert-CiScripts
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
if ($FromArtifact -and -not (Test-Path "C:\restore\tree.7z.001")) {
  throw "resume artifact missing: C:\restore\tree.7z.001"
}
if (-not $FromArtifact -and $StageIndex -gt 1) {
  throw "stage $StageIndex requires -FromArtifact"
}
if ($FromArtifact -and (Test-Path "C:\restore\tree.7z.001")) {
  Write-Host "==> validating build tree from previous stage"
  $sevenZip = Resolve-7Zip
  & $sevenZip t "C:\restore\tree.7z.001" | Select-Object -Last 3
  if ($LASTEXITCODE -ne 0) { throw "7z archive test failed (exit $LASTEXITCODE)" }
  Write-Host "==> restoring build tree from previous stage ($(Get-RemainingMin) min left)"
  & $sevenZip x "C:\restore\tree.7z.001" -o"$Root" -y | Select-Object -Last 3
  if ($LASTEXITCODE -ne 0) { throw "7z restore failed (exit $LASTEXITCODE)" }
  Remove-Item C:\restore -Recurse -Force -ErrorAction SilentlyContinue
  Write-Host "==> restore done; disk: $(Get-FreeGB) GB free; remaining $(Get-RemainingMin) min"
}

# ---------------------------------------------------------------- fetch + sync
$syncedMarker = "$Src\.chromix-synced"
$patchedMarker = "$Src\.chromix-patched"
$sourceReady = (Test-Path $syncedMarker) -and (Test-Path $patchedMarker) -and
  ((Get-Content $syncedMarker -Raw).Trim() -eq $ChromiumVersion) -and
  ((Get-Content $patchedMarker -Raw).Trim() -eq $ChromiumVersion)
if (-not $sourceReady) {
  Remove-Item $syncedMarker, $patchedMarker -Force -ErrorAction SilentlyContinue
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

  # gclient sync pinned to the tag, deps only: googlesource rate-limits (HTTP
  # 429) transiently, so retry within the remaining budget; already-cloned deps
  # are skipped on re-run, so partial syncs resume where they left off. Hooks
  # run manually below -- gclient's parallel hook phase failed silently and
  # repeatably on runners (exact ~60s death, no error text in either stream),
  # and at 151 the clang/rust toolchains are GCS *deps*, not hooks, so only a
  # handful of small hooks are actually needed to build.
  $syncOk = $false
  $attempt = 0
  $fastFails = 0
  while ((Get-RemainingMin) -gt ($PackReserveMin + 60)) {
    $attempt++
    $budgetSec = ((Get-RemainingMin) - ($PackReserveMin + 45)) * 60
    if ($budgetSec -lt 300) { break }
    Write-Host "==> gclient sync attempt $attempt (budget $([int]($budgetSec/60)) min)"
    $swSync = [Diagnostics.Stopwatch]::StartNew()
    $rc = Invoke-Tracked -File "cmd.exe" -ArgList "/c gclient sync --nohooks --revision src@$ChromiumVersion -D --no-history --reset --jobs 8" -Cwd $Chromium -TimeoutSec $budgetSec
    if ($rc -eq 0) { $syncOk = $true; break }
    # A sync interrupted mid-clone leaves stale git lock files; every retry then
    # dies at the same dep within ~a minute. Sweep the locks and carry on.
    if ($swSync.Elapsed.TotalSeconds -lt 150) { $fastFails++ } else { $fastFails = 0 }
    if ($fastFails -ge 4) {
      Write-Host "==> $fastFails consecutive fast failures; sweeping stale git locks"
      Get-ChildItem "$Chromium" -Recurse -Force -Include index.lock, shallow.lock, config.lock, HEAD.lock, packed-refs.lock -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match "\\\.git\\" } |
        ForEach-Object { Write-Host "    removing $($_.FullName)"; Remove-Item -Force $_.FullName -ErrorAction SilentlyContinue }
      $fastFails = 0
    }
    Write-Host "  sync attempt failed (exit $rc); retrying in 60s"
    Start-Sleep -Seconds 60
  }
  if (-not $syncOk) {
    Write-Host "==> sync budget exhausted; handing git state to next stage"
    Save-Handoff -Mode Unsynced
    return
  }
  # gclient leaves src detached at the pinned revision; verify before marking.
  Push-Location $Src
  try {
    $head = & git rev-parse HEAD
    $want = & git rev-parse "refs/tags/$ChromiumVersion^{commit}"
    if ($head -ne $want) { throw "src is at $head, expected tag $ChromiumVersion ($want)" }
  } finally { Pop-Location }

  # Run the unconditional and Windows hooks observed in Chromium 151's DEPS.
  # The recursive DevTools hook is last because gclient previously returned 1
  # immediately after it; running hooks one by one makes the exact failure clear.
  $hooks = @(
    @{ Desc = "vpython bootstrap"; Cwd = $Chromium; Cmd = "vpython3.bat -vpython-spec src/.vpython3 -vpython-tool install" },
    @{ Desc = "landmines"; Cwd = $Chromium; Cmd = "python3 src/build/landmines.py" },
    @{ Desc = "disable depot_tools self-update"; Cwd = $Chromium; Cmd = "python3 src/third_party/depot_tools/update_depot_tools_toggle.py --disable" },
    @{ Desc = "remove stale files"; Cwd = $Chromium; Cmd = "python3 src/tools/remove_stale_files.py src/third_party/test_fonts/test_fonts.tar.gz src/third_party/node/node_modules.tar.gz src/third_party/tfhub_models src/tools/clang/crashreports" },
    @{ Desc = "remove stale pyc files"; Cwd = $Chromium; Cmd = "python3 src/tools/remove_stale_pyc_files.py src/android_webview/tools src/build/android src/gpu/gles2_conform_support src/infra src/ppapi src/printing src/third_party/blink/renderer/build/scripts src/third_party/blink/tools src/third_party/catapult src/third_party/mako src/tools" },
    @{ Desc = "vs_toolchain update"; Cwd = $Chromium; Cmd = "python3 src/build/vs_toolchain.py update --force" },
    @{ Desc = "lastchange"; Cwd = $Chromium; Cmd = "python3 src/build/util/lastchange.py -o src/build/util/LASTCHANGE" },
    @{ Desc = "gpu lists version"; Cwd = $Chromium; Cmd = "python3 src/build/util/lastchange.py -m GPU_LISTS_VERSION --revision-id-only --header src/gpu/config/gpu_lists_version.h" },
    @{ Desc = "skia commit hash"; Cwd = $Chromium; Cmd = "python3 src/build/util/lastchange.py -m SKIA_COMMIT_HASH -s src/third_party/skia --header src/skia/ext/skia_commit_hash.h" },
    @{ Desc = "dawn commit hash"; Cwd = $Chromium; Cmd = "python3 src/build/util/lastchange.py -m DAWN_COMMIT_HASH -s src/third_party/dawn --revision src/gpu/webgpu/DAWN_VERSION --header src/gpu/webgpu/dawn_commit_hash.h" },
    @{ Desc = "rc.exe resource compiler"; Cwd = $Chromium; Cmd = "python3 src/third_party/depot_tools/download_from_google_storage.py --no_resume --bucket chromium-browser-clang/rc -s src/build/toolchain/win/rc/win/rc.exe.sha1" },
    @{ Desc = "Apache Win32 binaries"; Cwd = $Chromium; Cmd = "python3 src/third_party/depot_tools/download_from_google_storage.py --no_resume --directory --recursive --num_threads=16 --bucket chromium-apache-win32 src/third_party/apache-win32" },
    @{ Desc = "location tags"; Cwd = $Chromium; Cmd = "python3 src/testing/generate_location_tags.py --out src/testing/location_tags.json" },
    @{ Desc = "reclient config"; Cwd = $Chromium; Cmd = "python3 src/buildtools/reclient_cfgs/configure_reclient_cfgs.py --rbe_instance projects/rbe-chrome-untrusted/instances/default_instance --reproxy_cfg_template reproxy.cfg.template --rewrapper_cfg_project `"`" --skip_remoteexec_cfg_fetch --quiet" },
    @{ Desc = "siso config"; Cwd = $Chromium; Cmd = "python3 src/build/config/siso/configure_siso.py --rbe_instance projects/rbe-chrome-untrusted/instances/default_instance --reapi_instance `"`" --reapi_address `"`" --reapi_backend_config_path `"`" --credential-helper `"`"" },
    @{ Desc = "tast control"; Cwd = $Chromium; Cmd = "python3 src/build/util/tast_control.py -o src/chromeos/tast_control.gni -t src/chromeos/tast_control.gni.template -i src/chromeos/tast_control_disabled_tests.txt --input-public src/chromeos/tast_control_disabled_tests_public_builders.txt -f src/chromeos/tast_control_flaky_tests.txt" },
    @{ Desc = "devtools rollup libs"; Cwd = "$Src\third_party\devtools-frontend\src"; Cmd = "vpython3.bat scripts/deps/sync_rollup_libs.py" }
  )
  foreach ($h in $hooks) {
    if ((Get-RemainingMin) -lt ($PackReserveMin + 25)) {
      Write-Host "==> hook budget exhausted before '$($h.Desc)'"
      Save-Handoff -Mode Unsynced
      return
    }
    $hookRc = 1
    for ($try = 1; $try -le 3; $try++) {
      Write-Host "==> hook [$($h.Desc)] attempt $try"
      $hookRc = Invoke-Tracked -File "cmd.exe" -ArgList "/c $($h.Cmd)" -Cwd $h.Cwd -TimeoutSec 600
      if ($hookRc -eq 0) { break }
      Start-Sleep -Seconds 20
    }
    if ($hookRc -ne 0) {
      if ($h.Optional) {
        Write-Host "==> optional hook failed (continuing): $($h.Desc)"
      } else {
        throw "hook failed after retries: $($h.Desc)"
      }
    }
  }
  # Apply Chromix patches only after sync and all required hooks succeeded.
  Push-Location $Src
  try {
    foreach ($rel in Get-Content "$Repo\patches\series") {
      if (-not $rel.Trim()) { continue }
      Write-Host "  applying $rel"
      & git apply --3way --whitespace=nowarn (Join-Path $Repo $rel)
      if ($LASTEXITCODE -ne 0) { throw "patch failed: $rel" }
    }
  } finally { Pop-Location }
  Set-Content -Path $syncedMarker -Value $ChromiumVersion
  Set-Content -Path $patchedMarker -Value $ChromiumVersion
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
    & gn gen out\Chromix --args="$gnArgs"
    if ($LASTEXITCODE -ne 0) { throw "gn gen failed (exit $LASTEXITCODE)" }
  } finally { Pop-Location }
}

$ninjaBudget = (Get-RemainingMin) - $PackReserveMin
if ($ninjaBudget -lt 20) {
  Write-Host "==> not enough budget left to build (<20 min); handing tree to next stage"
  Save-Handoff -Mode Synced
  return
}
Write-Host "==> autoninja budget: $ninjaBudget min"
$rc = Invoke-Tracked -File "cmd.exe" -ArgList "/c autoninja -C out\Chromix -j 4 chrome" -Cwd $Src -TimeoutSec ($ninjaBudget * 60)

if ($rc -eq 0) {
  Write-Host "==> BUILD COMPLETE; packaging portable bundle"
  New-Item -ItemType Directory -Force -Path "$Root\dist" | Out-Null
  & "$Repo\build\windows\package-win.ps1" -Out $OutDir -Dest "$Root\dist"
  Write-OutVar finished true
  return
}

if ($rc -eq 124) {
  Write-Host "==> ninja budget exhausted; handing tree to next stage"
  Save-Handoff -Mode Synced
  return
}
throw "autoninja failed (exit $rc)"
