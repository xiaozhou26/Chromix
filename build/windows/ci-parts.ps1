<#
  Snapshot C:\c (the pinned ungoogled tooling, Chromium source, and build output)
  into multi-volume 7z archives distributed round-robin across 4 fixed upload
  slots, so the next CI stage can resume where this one stopped.

  Modes:

    Synced    Source layers are prepared: pack the complete work tree, including
              git metadata for the small pinned tooling checkouts and out\.
    Unsynced  Preparation budget ran out: pack the partial work tree so downloads,
              unpacking, and patch application can resume in the next stage.

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
  $rootItem = Get-Item $Path -ErrorAction SilentlyContinue
  if (-not $rootItem) { return 0 }
  $items = Get-ChildItem $Path -Recurse -Force -File -ErrorAction SilentlyContinue
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
  if (-not (Test-Path "chromix")) { throw "$Root\chromix does not exist" }
  & $SevenZip a -v9g -mx=1 -mtc=on "$PartsDir\stage\tree.7z" chromix
  $rc = $LASTEXITCODE
  if ($rc -gt 1) { throw "7z volume creation failed (exit $rc)" }
  if ($rc -eq 1) {
    Write-Host "==> 7z completed with warnings (some files skipped)"
    $global:LASTEXITCODE = 0
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
