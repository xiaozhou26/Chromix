<#
  Windows build using pinned ungoogled-chromium source layers (x64 host).

  Prerequisites:
    - Visual Studio 2022 Desktop C++ workload (ARM64 tools for -Arch arm64)
    - Windows 11 SDK 10.0.28000.0 with Debugging Tools (10.0.26100.0 for Chromium 152)
    - Python 3, Git, and 7-Zip
#>
[CmdletBinding()]
param(
  [string]$WorkDir = "$PSScriptRoot\..\..\.chromix-build-win",
  [switch]$Resume,
  [int]$Jobs = 8,
  [switch]$ApplyDomainSubstitution,
  [ValidateSet("x64", "arm64")]
  [string]$Arch = $(if ($env:CHROMIX_TARGET_ARCH) { $env:CHROMIX_TARGET_ARCH } else { "x64" })
)
$ErrorActionPreference = "Stop"
if ($Arch -cnotin @("x64", "arm64")) { throw "Arch/CHROMIX_TARGET_ARCH must be x64 or arm64" }
$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$Revisions = & "$PSScriptRoot\read-platform-pins.ps1" -Repo $Repo
$WorkDir = [IO.Path]::GetFullPath($WorkDir)
$Src = Join-Path $WorkDir "src"
$Out = Join-Path $Src "out\Chromix"
$UngoogledTooling = Join-Path $WorkDir "tooling\ungoogled-chromium"
$WindowsTooling = Join-Path $WorkDir "tooling\ungoogled-chromium-windows"

Write-Host "==> Chromix Windows $Arch build | Chromium $($Revisions.ChromiumVersion) | $WorkDir"
& "$PSScriptRoot\assert-target-arch.ps1" -WorkDir $WorkDir -Arch $Arch -Initialize:($Arch -eq "arm64") `
  -RequireMarker:($Resume -and $Arch -eq "arm64")
if ($Arch -eq "arm64") {
  & "$PSScriptRoot\assert-arm64-toolchain.ps1" -ChromiumVersion $Revisions.ChromiumVersion
} else {
  & "$PSScriptRoot\ensure-windows-sdk.ps1" -Arch $Arch -ChromiumVersion $Revisions.ChromiumVersion
}
if ($Resume -and -not (Test-Path (Join-Path $Src ".chromix-source-ready"))) {
  throw "-Resume requested but $Src is not prepared"
}
& "$PSScriptRoot\prepare-ungoogled.ps1" -Root $WorkDir -Repo $Repo -Arch $Arch

Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
$env:DEPOT_TOOLS_WIN_TOOLCHAIN = "0"
$env:DEPOT_TOOLS_METRICS = "0"
$env:DEPOT_TOOLS_COLLECT_METRICS = "0"

$env:PATH = "$(Join-Path $Src 'third_party\ninja');$(Join-Path $Src 'third_party\node\win');$env:PATH"
$Ninja = Join-Path $Src "third_party\ninja\ninja.exe"
& "$PSScriptRoot\configure-node.ps1" -NodePath (Join-Path $Src 'third_party\node\win\node.exe')
if (Test-Path (Join-Path $Src ".chromix-upstream-restored.json")) {
  $Out = Join-Path $Src "out\Default"
  $Ninja = & python (Join-Path $Repo "tools\restore_ninja.py") --workdir $WorkDir --platform windows --arch $Arch
  if ($LASTEXITCODE -ne 0 -or -not $Ninja) { throw "restored Ninja compatibility check failed" }
  $env:NINJA = $Ninja
}
$mergedArgs = Join-Path $Out "args.gn"
New-Item -ItemType Directory -Force -Path $Out | Out-Null
$mergeArgs = @((Join-Path $Repo "tools\merge_gn_args.py"), $mergedArgs)
if (Test-Path (Join-Path $Src ".chromix-upstream-restored.json")) { $mergeArgs += $mergedArgs }
$mergeArgs += @(
  (Join-Path $UngoogledTooling "flags.gn"),
  (Join-Path $WindowsTooling "flags.windows.gn"),
  (Join-Path $Repo "build\args.windows.gn")
)
if ($Arch -eq "arm64") { $mergeArgs += (Join-Path $Repo "build\args.windows.arm64.gn") }
if ($Arch -eq "arm64" -and (Test-Path (Join-Path $Src ".chromix-upstream-restored.json"))) {
  $mergeArgs += @("--preserve-pgo-from", $mergedArgs)
}
python @mergeArgs
if ($LASTEXITCODE -ne 0) { throw "GN argument merge failed" }
if ($Arch -eq "arm64") {
  & "$PSScriptRoot\assert-target-arch.ps1" -WorkDir $WorkDir -Arch $Arch
}

Push-Location $Src
try {
  if (-not (Test-Path "third_party\rust-toolchain\bin\bindgen.exe")) {
    if (Test-Path (Join-Path $Src ".chromix-upstream-restored.json")) {
      # Only restore known tool-download endpoints, never browser source domains.
      $normalizeToolUrls = @'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'tools'))
from upstream_script_identity import ENDPOINTS, RESTORED
src = Path(sys.argv[2])
for relative, keys in RESTORED.items():
    path = src / relative
    original = path.read_bytes()
    normalized = original
    for key in keys:
        before, after = ENDPOINTS[key]
        normalized = normalized.replace(before.encode('ascii'), after.encode('ascii'))
    if normalized != original:
        path.write_bytes(normalized)
'@
      python -c $normalizeToolUrls $Repo $Src
      if ($LASTEXITCODE -ne 0) { throw "restored tool download endpoint normalization failed" }
    }
    python tools\rust\build_bindgen.py --skip-test
    if ($LASTEXITCODE -ne 0) { throw "bindgen build failed" }
  }
  if (Test-Path (Join-Path $Src ".chromix-upstream-restored.json")) {
    python (Join-Path $Repo "tools\prepare_restored_build.py") --phase finish `
      --platform windows --arch $Arch --workdir $WorkDir
    if ($LASTEXITCODE -ne 0) { throw "restored build preparation failed (exit $LASTEXITCODE)" }
  }
  $gn = Join-Path $Out "gn.exe"
  if (-not (Test-Path $gn)) {
    python tools\gn\bootstrap\bootstrap.py -o $gn --skip-generate-buildfiles
    if ($LASTEXITCODE -ne 0) { throw "GN bootstrap failed" }
  }

  if ($ApplyDomainSubstitution) {
    $cache = Join-Path $WorkDir "domain_substitution_cache.tar"
    if (-not (Test-Path $cache)) {
      Write-Host "==> applying ungoogled domain substitution"
      python (Join-Path $UngoogledTooling "utils\domain_substitution.py") apply `
        -r (Join-Path $UngoogledTooling "domain_regex.list") `
        -f (Join-Path $WindowsTooling "domain_substitution.list") `
        -c $cache $Src
      if ($LASTEXITCODE -ne 0) { throw "domain substitution failed" }
    }
  }

  & $gn gen $Out --fail-on-unused-args
  if ($LASTEXITCODE -ne 0) { throw "gn gen failed" }

  if (Test-Path (Join-Path $Src ".chromix-upstream-restored.json")) {
    & $Ninja -C $Out -n chrome *> (Join-Path $WorkDir "upstream-cache-plan.log")
    if ($LASTEXITCODE -ne 0) { throw "restored upstream build-plan check failed" }
    # The collector preserves the initial baseline across resumed builds.
    & python (Join-Path $Repo "tools\restored_reuse_evidence.py") --phase before `
      --workdir $WorkDir --platform windows --arch $Arch --ninja $Ninja --target chrome
    if ($LASTEXITCODE -ne 0) { throw "restored reuse evidence collection failed before Ninja (exit $LASTEXITCODE)" }
  }
  & $Ninja -C $Out -j $Jobs chrome
  $ninjaRc = $LASTEXITCODE
  if (Test-Path (Join-Path $Src ".chromix-upstream-restored.json")) {
    try {
      & python (Join-Path $Repo "tools\restored_reuse_evidence.py") --phase after `
        --workdir $WorkDir --platform windows --arch $Arch --ninja $Ninja --target chrome --exit-code $ninjaRc
      if ($LASTEXITCODE -ne 0) { throw "restored reuse evidence collection failed after Ninja (exit $LASTEXITCODE)" }
    } catch {
      if ($ninjaRc -ne 0) { throw "ninja failed (exit $ninjaRc); $($_.Exception.Message)" }
      throw
    }
  }
  if ($ninjaRc -ne 0) { throw "ninja failed (exit $ninjaRc)" }
} finally {
  Pop-Location
}

$chrome = Join-Path $Out "chrome.exe"
if (-not (Test-Path -LiteralPath $chrome -PathType Leaf)) {
  throw "built Windows browser is missing: $chrome"
}
# Chromium's --version handler is POSIX-only; read the Windows PE resource.
$info = (Get-Item -LiteralPath $chrome).VersionInfo
if ($null -eq $info) { throw "built Windows browser version metadata is missing: $chrome" }
$version = '{0}.{1}.{2}.{3}' -f $info.ProductMajorPart, $info.ProductMinorPart, `
  $info.ProductBuildPart, $info.ProductPrivatePart
if ($version -cne $Revisions.ChromiumVersion) {
  throw "built Windows browser version does not match the pinned Chromium version: $chrome ($version)"
}
Write-Host "==> Windows PE product version verified: $version (metadata only; not a runtime smoke test)"
Write-Host "==> Done: $chrome"
