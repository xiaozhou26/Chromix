<# Validate target identity before preparing or resuming Windows build state. #>
param(
  [Parameter(Mandatory)] [string]$WorkDir,
  [ValidateSet("x64", "arm64")] [string]$Arch = "x64",
  [switch]$Initialize,
  [switch]$RequireMarker
)
$ErrorActionPreference = "Stop"
$marker = Join-Path $WorkDir ".chromix-target-arch"
$src = Join-Path $WorkDir "src"
$receiptPath = Join-Path $src ".chromix-upstream-restored.json"
$hasMarker = Test-Path -LiteralPath $marker
if ($hasMarker) {
  $saved = (Get-Content -LiteralPath $marker -Raw).Trim()
  if ($saved -cne $Arch) { throw "Windows target architecture marker mismatch: $saved, expected $Arch" }
} elseif ($RequireMarker) {
  throw "Windows $Arch snapshot has no target architecture marker; use a clean work directory"
}
if (Test-Path -LiteralPath $receiptPath) {
  $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
  if ($receipt -isnot [PSCustomObject] -or $receipt.identity -isnot [PSCustomObject] -or
      $receipt.platform -cne "windows" -or $receipt.arch -cne $Arch -or
      $receipt.identity.platform -cne "windows" -or $receipt.identity.arch -cne $Arch) {
    throw "Windows upstream receipt platform/architecture mismatch: expected windows/$Arch"
  }
}
if (-not $hasMarker -and $Arch -eq "arm64" -and (Test-Path -LiteralPath $src)) {
  if (-not $Initialize -or -not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) {
    throw "Windows $Arch snapshot has no target architecture marker; use a clean work directory"
  }
  # Only a fully verified initial restore may initialize an existing source tree.
  $repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
  & python (Join-Path $repo "tools\restore_upstream_cache.py") --phase verify `
    --platform windows --arch $Arch --workdir $WorkDir
  if ($LASTEXITCODE -ne 0) { throw "restored upstream source verification failed before target marker initialization" }
}
foreach ($name in @("Chromix", "Default")) {
  $out = Join-Path $src "out\$name"
  $gnArgs = Join-Path $out "args.gn"
  if (Test-Path -LiteralPath $gnArgs) {
    $text = Get-Content -LiteralPath $gnArgs -Raw
    $assignments = [regex]::Matches($text, '(?m)^\s*target_cpu\s*=.*$')
    $expected = '^\s*target_cpu\s*=\s*"' + $Arch + '"\s*(?:#.*)?$'
    if ($Arch -eq "x64" -and $assignments.Count -eq 0) { continue }
    if ($assignments.Count -eq 0 -or
        ($Arch -eq "arm64" -and $assignments.Count -ne 1) -or
        @($assignments | Where-Object { $_.Value -cnotmatch $expected }).Count -ne 0) {
      throw "Windows target_cpu mismatch or ambiguous GN arguments: $gnArgs (expected $Arch)"
    }
  } elseif ($Arch -eq "arm64" -and (Test-Path -LiteralPath $out) -and
            (Get-ChildItem -LiteralPath $out -Force | Select-Object -First 1)) {
    throw "Windows build output has no architecture-bearing args.gn: $out"
  }
}
if ($Initialize -and -not (Test-Path -LiteralPath $marker)) {
  New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
  Set-Content -LiteralPath $marker -Value $Arch -Encoding ASCII
}
