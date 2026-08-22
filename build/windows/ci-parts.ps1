<#
  Snapshot C:\c (depot_tools + chromium src incl. out dir) into multi-volume
  7z archives distributed round-robin across 4 fixed upload slots, so the next
  CI stage can resume ninja incrementally.

  -mtc=on keeps file modification times, which ninja relies on for its
  incremental state. -mx=1 trades compression ratio for speed (~20 GB tree).
  Each volume stays under 9 GB to fit the per-artifact size cap; the workflow
  uploads p1..p4 as artifacts tree-s<stage>-part1..4 and the next stage
  downloads + extracts them with `7z x tree.7z.001`.
#>
param(
  [Parameter(Mandatory)] [string]$Root,
  [Parameter(Mandatory)] [string]$PartsDir
)
$ErrorActionPreference = "Stop"

Remove-Item $PartsDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path "$PartsDir\stage" | Out-Null
foreach ($i in 1..4) { New-Item -ItemType Directory -Force -Path "$PartsDir\p$i" | Out-Null }

Write-Host "==> packing $Root into 7z volumes at $PartsDir (this takes a while)"
$sw = [Diagnostics.Stopwatch]::StartNew()
# Relative paths from $Root so the archive stores chromium\... and depot_tools\...
# and later extracts 1:1 with `7z x tree.7z.001 -oC:\c`.
Push-Location $Root
try {
  & 7z.exe a -v9g -mx=1 -mtc=on "$PartsDir\stage\tree.7z" chromium depot_tools
  if ($LASTEXITCODE -ne 0) { throw "7z volume creation failed (exit $LASTEXITCODE)" }
} finally { Pop-Location }
Write-Host "==> packed in $([int]$sw.Elapsed.TotalMinutes) min"

$vols = @(Get-ChildItem "$PartsDir\stage\tree.7z.*" | Sort-Object Name)
Write-Host "==> $($vols.Count) volumes, distributing round-robin into p1..p4"
for ($i = 0; $i -lt $vols.Count; $i++) {
  $slot = ($i % 4) + 1
  Move-Item $vols[$i].FullName "$PartsDir\p$slot\"
}
Get-ChildItem $PartsDir\p1, $PartsDir\p2, $PartsDir\p3, $PartsDir\p4 |
  ForEach-Object { Write-Host "    $($_.FullName)  $([math]::Round($_.Length/1GB, 2)) GB" }
Remove-Item "$PartsDir\stage" -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "==> disk after packing: $([math]::Round((Get-PSDrive C).Free/1GB, 1)) GB free"
