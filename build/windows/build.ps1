<#
  Native Windows build using pinned ungoogled-chromium source layers.

  Prerequisites:
    - Visual Studio 2022 Desktop C++ workload
    - Windows 11 SDK 10.0.26100 with Debugging Tools
    - Python 3, Git, and 7-Zip
#>
[CmdletBinding()]
param(
  [string]$WorkDir = "$PSScriptRoot\..\..\.chromix-build-win",
  [switch]$Resume,
  [int]$Jobs = 8,
  [switch]$ApplyDomainSubstitution
)
$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$Revisions = Import-PowerShellDataFile (Join-Path $Repo "build\ungoogled-revisions.psd1")
$WorkDir = [IO.Path]::GetFullPath($WorkDir)
$Src = Join-Path $WorkDir "src"
$Out = Join-Path $Src "out\Chromix"
$UngoogledTooling = Join-Path $WorkDir "tooling\ungoogled-chromium"
$WindowsTooling = Join-Path $WorkDir "tooling\ungoogled-chromium-windows"

Write-Host "==> Chromix Windows build | Chromium $($Revisions.ChromiumVersion) | $WorkDir"
if ($Resume -and -not (Test-Path (Join-Path $Src ".chromix-source-ready"))) {
  throw "-Resume requested but $Src is not prepared"
}
& "$PSScriptRoot\prepare-ungoogled.ps1" -Root $WorkDir -Repo $Repo

Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
$env:DEPOT_TOOLS_WIN_TOOLCHAIN = "0"
$env:DEPOT_TOOLS_METRICS = "0"
$env:DEPOT_TOOLS_COLLECT_METRICS = "0"

$env:PATH = "$(Join-Path $Src 'third_party\ninja');$(Join-Path $Src 'third_party\node\win');$env:PATH"
$mergedArgs = Join-Path $Out "args.gn"
New-Item -ItemType Directory -Force -Path $Out | Out-Null
python (Join-Path $Repo "tools\merge_gn_args.py") $mergedArgs `
  (Join-Path $UngoogledTooling "flags.gn") `
  (Join-Path $WindowsTooling "flags.windows.gn") `
  (Join-Path $Repo "build\args.windows.gn")
if ($LASTEXITCODE -ne 0) { throw "GN argument merge failed" }

Push-Location $Src
try {
  $gn = Join-Path $Out "gn.exe"
  if (-not (Test-Path $gn)) {
    python tools\gn\bootstrap\bootstrap.py -o $gn --skip-generate-buildfiles
    if ($LASTEXITCODE -ne 0) { throw "GN bootstrap failed" }
  }

  if (-not (Test-Path "third_party\rust-toolchain\bin\bindgen.exe")) {
    python tools\rust\build_bindgen.py --skip-test
    if ($LASTEXITCODE -ne 0) { throw "bindgen build failed" }
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

  & "third_party\ninja\ninja.exe" -C $Out -j $Jobs chrome
  if ($LASTEXITCODE -ne 0) { throw "ninja failed" }
} finally {
  Pop-Location
}

Write-Host "==> Done: $Out\chrome.exe"
& "$Out\chrome.exe" --version
