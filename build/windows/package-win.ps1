<#
  Package the native Windows Chromix build into a portable chromix bundle,
  zip archive, and SHA256 manifest.
#>
param(
  [Parameter(Mandatory)] [string]$Out,
  [Parameter(Mandatory)] [string]$Dest
)
$ErrorActionPreference = "Stop"
$Bundle = Join-Path $Dest "chromix"
Remove-Item -Recurse -Force $Bundle -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Bundle | Out-Null

$required = @(
  "chrome.exe", "chrome.dll", "chrome_elf.dll",
  "chrome_100_percent.pak", "chrome_200_percent.pak", "resources.pak",
  "icudtl.dat", "libEGL.dll", "libGLESv2.dll"
)
foreach ($name in $required) {
  $source = Join-Path $Out $name
  if (-not (Test-Path $source)) { throw "required runtime file is missing: $source" }
  Copy-Item $source (Join-Path $Bundle $name)
}

$snapshot = @("v8_context_snapshot.bin", "snapshot_blob.bin") |
  Where-Object { Test-Path (Join-Path $Out $_) } |
  Select-Object -First 1
if (-not $snapshot) { throw "no V8 snapshot blob found in $Out" }
Copy-Item (Join-Path $Out $snapshot) (Join-Path $Bundle $snapshot)

$locales = Join-Path $Out "locales"
if (-not (Test-Path $locales)) { throw "required locales directory is missing: $locales" }
Copy-Item $locales (Join-Path $Bundle "locales") -Recurse

foreach ($name in @(
  "chrome_proxy.exe", "chrome_wer.dll", "chrome_crashpad_handler.exe",
  "d3dcompiler_47.dll", "dxcompiler.dll", "dxil.dll",
  "vk_swiftshader.dll", "vk_swiftshader_icd.json", "vulkan-1.dll"
)) {
  $source = Join-Path $Out $name
  if (Test-Path $source) { Copy-Item $source (Join-Path $Bundle $name) }
}

$manifests = @(Get-ChildItem $Out -File -Filter "*.manifest" -ErrorAction SilentlyContinue)
if ($manifests.Count -eq 0) { throw "required side-by-side manifest is missing from $Out" }
foreach ($manifest in $manifests) { Copy-Item $manifest.FullName (Join-Path $Bundle $manifest.Name) }
Get-ChildItem $Out -Directory | Where-Object { $_.Name -match '^\d+\.\d+\.\d+\.\d+$' } |
  ForEach-Object { Copy-Item $_.FullName (Join-Path $Bundle $_.Name) -Recurse }

$runtimeCandidates = @(
  (Join-Path $Out "msvcp140.dll"),
  (Join-Path $Out "vcruntime140.dll"),
  (Join-Path $Out "vcruntime140_1.dll"),
  (Join-Path $Out "concrt140.dll")
)
foreach ($source in $runtimeCandidates) {
  if (Test-Path $source) { Copy-Item $source (Join-Path $Bundle (Split-Path $source -Leaf)) }
}

@'
@echo off
"%~dp0chrome.exe" %*
'@ | Set-Content -Encoding ASCII (Join-Path $Bundle "chromix.cmd")

$dll = Join-Path $Bundle "chrome.dll"
$bytes = [IO.File]::ReadAllBytes($dll)
$ascii = [Text.Encoding]::ASCII.GetString($bytes)
$utf16 = [Text.Encoding]::Unicode.GetString($bytes)
$forbiddenMarkers = @(
  "NVIDIA GeForce RTX 3060",
  "Google Inc. (NVIDIA Corporation)"
)
foreach ($marker in $forbiddenMarkers) {
  if ($ascii.Contains($marker) -or $utf16.Contains($marker)) {
    throw "chrome.dll contains forbidden WebGL identity marker: $marker"
  }
}
$requiredMarkers = @(
  "Google Inc. (Intel)",
  "ANGLE (Intel, Intel(R) UHD Graphics 770",
  "uxr-webgl-vendor",
  "uxr-webgl-renderer"
)
foreach ($marker in $requiredMarkers) {
  if (-not ($ascii.Contains($marker) -or $utf16.Contains($marker))) {
    throw "chrome.dll is missing required WebGL persona marker: $marker"
  }
}
Write-Host "==> chrome.dll WebGL persona marker scan passed"

$asset = Join-Path $Dest "chromix-win-x64.zip"
Remove-Item $asset -ErrorAction SilentlyContinue
Compress-Archive -Path $Bundle -DestinationPath $asset
$hash = (Get-FileHash $asset -Algorithm SHA256).Hash.ToLowerInvariant()
"$hash  chromix-win-x64.zip" | Set-Content -Encoding ASCII (Join-Path $Dest "SHA256SUMS")
Write-Host "==> $asset  sha256=$hash"
