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

$freeBefore = Get-FreeGB
if ($freeBefore -lt 12) {
  throw "only $freeBefore GB free - not enough to create volumes alongside the tree"
}

Write-Host "==> packing $Root into 7z volumes at $PartsDir (mode: $Mode; disk: $freeBefore GB free)"
$sw = [Diagnostics.Stopwatch]::StartNew()
Push-Location $Root
try {
  if ($Mode -eq "Synced") {
    # Relative paths so the archive stores chromium\... and depot_tools\...
    # and later extracts 1:1 with `7z x tree.7z.001 -oC:\c`.
    & 7z.exe a -v9g -mx=1 -mtc=on "$PartsDir\stage\tree.7z" chromium depot_tools "-xr!.git"
    if ($LASTEXITCODE -ne 0) { throw "7z volume creation failed (exit $LASTEXITCODE)" }
  }
  else {
    $list = "$PartsDir\stage\list.txt"
    $entries = @("chromium\.gclient", "chromium\.gclient_entries") |
      Where-Object { Test-Path (Join-Path $Root $_) }
    Write-Host "==> enumerating .git dirs (this walks the whole tree once)"
    $gitDirs = Get-ChildItem -Recurse -Force -Directory -Filter ".git" -ErrorAction SilentlyContinue |
      ForEach-Object { Resolve-Path -Relative $_.FullName }
    Write-Host "==> found $($gitDirs.Count) git repositories"
    ($entries + $gitDirs) | Set-Content -Path $list -Encoding ASCII
    & 7z.exe a -v9g -mx=1 -mtc=on "$PartsDir\stage\tree.7z" "@$list"
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
