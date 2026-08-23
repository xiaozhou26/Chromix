<#
  Snapshot C:\c (depot_tools + chromium src) into multi-volume 7z archives
  distributed round-robin across 4 fixed upload slots, so the next CI stage
  can resume where this one stopped.

  Modes (a full gclient tree is ~80 GB with ~600 dependency .git dirs, which
  does not fit the runner disk next to its own archive, nor the 4x9 GB
  artifact slots - so .git state and working tree are never packed together):

    Synced    Source is synced + patched: pack the working tree (incl. out\)
              WITHOUT .git - later stages only run gn/ninja, which need no git.
    Unsynced  Sync budget ran out: pack ONLY .gclient* + every .git dir - the
              next stage's `gclient sync` rebuilds working trees from the
              local git objects instead of re-downloading them.

  -mtc=on keeps file modification times, which ninja relies on for its
  incremental state. -mx=1 trades compression ratio for speed.
  Each volume stays under the ~10 GB per-artifact cap.
#>
param(
  [Parameter(Mandatory)] [string]$Root,
  [Parameter(Mandatory)] [string]$PartsDir,
  [ValidateSet("Synced", "Unsynced")]
  [string]$Mode = "Synced"
)
$ErrorActionPreference = "Stop"
$MaxSlots = 4

Remove-Item $PartsDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path "$PartsDir\stage" | Out-Null
foreach ($i in 1..$MaxSlots) { New-Item -ItemType Directory -Force -Path "$PartsDir\p$i" | Out-Null }

function Get-FreeGB { [math]::Round((Get-PSDrive C).Free / 1GB, 1) }

function Resolve-7Zip {
  $cmd = Get-Command 7z.exe -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $installed = "$env:ProgramFiles\7-Zip\7z.exe"
  if (Test-Path $installed) { return $installed }
  throw "7z.exe is not available"
}

function Get-TreeGB {
  param([string]$Path, [string]$ArchiveMode)
  if ($ArchiveMode -eq "Unsynced") {
    $items = @(
      Get-Item (Join-Path $Path "chromium\.gclient") -ErrorAction SilentlyContinue
      Get-Item (Join-Path $Path "chromium\.gclient_entries") -ErrorAction SilentlyContinue
      Get-ChildItem $Path -Recurse -Force -Filter ".git" -ErrorAction SilentlyContinue |
        ForEach-Object {
          if ($_.PSIsContainer) {
            Get-ChildItem $_.FullName -Recurse -Force -File -ErrorAction SilentlyContinue
          }
          else { $_ }
        }
    )
  }
  else {
    $items = Get-ChildItem $Path -Recurse -Force -File -ErrorAction SilentlyContinue |
      Where-Object { $_.FullName -notmatch "\\\.git(\\|$)" }
  }
  [math]::Round(($items | Measure-Object Length -Sum).Sum / 1GB, 1)
}

$SevenZip = Resolve-7Zip
$freeBefore = Get-FreeGB
$inputGB = Get-TreeGB -Path $Root -ArchiveMode $Mode
$minFreeGB = if ($Mode -eq "Unsynced") {
  [math]::Max(12, [math]::Ceiling($inputGB + 5))
} else {
  [math]::Max(24, [math]::Ceiling(($inputGB * 0.25) + 5))
}
Write-Host "==> handoff input: $inputGB GB; minimum free space: $minFreeGB GB"
if ($freeBefore -lt $minFreeGB) {
  throw "only $freeBefore GB free - need at least $minFreeGB GB to create the $Mode handoff"
}

Write-Host "==> packing $Root into 7z volumes at $PartsDir (mode: $Mode; disk: $freeBefore GB free)"
$sw = [Diagnostics.Stopwatch]::StartNew()
Push-Location $Root
try {
  if ($Mode -eq "Synced") {
    # Relative paths so the archive stores chromium\... and depot_tools\...
    # and later extracts 1:1 with `7z x tree.7z.001 -oC:\c`.
    & $SevenZip a -v9g -mx=1 -mtc=on "$PartsDir\stage\tree.7z" chromium depot_tools "-xr!.git"
    if ($LASTEXITCODE -ne 0) { throw "7z volume creation failed (exit $LASTEXITCODE)" }
  }
  else {
    $list = "$PartsDir\stage\list.txt"
    $entries = @("chromium\.gclient", "chromium\.gclient_entries") |
      Where-Object { Test-Path (Join-Path $Root $_) }
    Write-Host "==> enumerating .git dirs and files (this walks the whole tree once)"
    $gitDirs = Get-ChildItem -Recurse -Force -Filter ".git" -ErrorAction SilentlyContinue |
      ForEach-Object { (Resolve-Path -Relative $_.FullName) -replace '^\.[\\/]', '' }
    Write-Host "==> found $($gitDirs.Count) git entries"
    ($entries + $gitDirs) | Set-Content -Path $list -Encoding ASCII
    & $SevenZip a -spf2 -v9g -mx=1 -mtc=on "$PartsDir\stage\tree.7z" "@$list"
    if ($LASTEXITCODE -ne 0) { throw "7z volume creation failed (exit $LASTEXITCODE)" }
  }
} finally { Pop-Location }
Write-Host "==> packed in $([int]$sw.Elapsed.TotalMinutes) min; disk now: $(Get-FreeGB) GB free"

$vols = @(Get-ChildItem "$PartsDir\stage\tree.7z.*" | Sort-Object Name)
if ($vols.Count -gt $MaxSlots) {
  throw "$($vols.Count) volumes exceed the $MaxSlots upload slots (~$([math]::Round(($vols | Measure-Object Length -Sum).Sum/1GB,1)) GB total) - tree too large to hand off"
}
Write-Host "==> $($vols.Count) volumes, distributing round-robin into p1..p$MaxSlots"
for ($i = 0; $i -lt $vols.Count; $i++) {
  $slot = ($i % $MaxSlots) + 1
  Move-Item $vols[$i].FullName "$PartsDir\p$slot\"
}
Get-ChildItem $PartsDir\p1, $PartsDir\p2, $PartsDir\p3, $PartsDir\p4 |
  ForEach-Object { Write-Host "    $($_.FullName)  $([math]::Round($_.Length/1GB, 2)) GB" }
Remove-Item "$PartsDir\stage" -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "==> disk after packing: $(Get-FreeGB) GB free"
