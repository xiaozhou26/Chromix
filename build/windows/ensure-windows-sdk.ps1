#requires -Version 5.1
<# Validate the pinned Chromium SDK; installation requires an explicit -Install. #>
param(
  [ValidateSet("x64", "arm64")][string]$Arch = "x64",
  [string]$ChromiumVersion = "",
  [string]$DownloadDir = "",
  [switch]$Install
)
$ErrorActionPreference = "Stop"
if (-not $ChromiumVersion) {
  $pins = Import-PowerShellDataFile (Join-Path $PSScriptRoot "..\ungoogled-revisions.psd1")
  $ChromiumVersion = $pins.ChromiumVersion
}
if ($ChromiumVersion -notmatch '\A\d+\.\d+\.\d+\.\d+\z') { throw "Invalid Chromium version: $ChromiumVersion" }
# Servicing releases install into the unserviced SDK version directory.
switch (([version]$ChromiumVersion).Major) {
  152 {
    $SdkVersion = "10.0.26100.0"
    $SdkRelease = "10.0.26100.7705"
    $InstallerUrl = "https://download.microsoft.com/download/f4b30f2a-4fc3-430e-9b03-c842b5f5f9f1/KIT_BUNDLE_WINDOWSSDK_MEDIACREATION/winsdksetup.exe"
    $InstallerSha256 = "6fa0fa27db77a909f5ecb35183cb26a969a6775936780936fe239e4f9c66b458"
  }
  { $_ -in @(153, 154, 155, 156) } {
    $SdkVersion = "10.0.28000.0"
    $SdkRelease = "10.0.28000.2705"
    $InstallerUrl = "https://download.microsoft.com/download/38ab8f6d-3676-4860-ae84-3361308b9d7f/KIT_BUNDLE_WINDOWSSDK_MEDIACREATION/winsdksetup.exe"
    $InstallerSha256 = "b534d37a1f3140c8be1298ed88855b8cbddc35d2626c4d0afd39f186a4b578e3"
  }
  default { throw "No Windows SDK mapping for Chromium $ChromiumVersion; update the SDK prerequisite before building" }
}
$MaxInstallerBytes = 16MB
$sdk = $env:WINDOWSSDKDIR
if (-not $sdk) {
  if (-not ${env:ProgramFiles(x86)}) { throw "ProgramFiles(x86) or WINDOWSSDKDIR must locate the Windows SDK" }
  $sdk = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10"
}
$sdk = [IO.Path]::GetFullPath($sdk)

function Get-MissingSdkFiles {
  $required = @(
    "Include\$SdkVersion\um\Windows.h",
    "Include\$SdkVersion\um\winnt.h",
    "Include\$SdkVersion\shared\sdkddkver.h",
    "Include\$SdkVersion\ucrt\stdio.h",
    "Include\$SdkVersion\winrt\windows.foundation.h",
    "Include\$SdkVersion\cppwinrt\winrt\base.h",
    "bin\$SdkVersion\x64\rc.exe",
    "bin\$SdkVersion\x64\midl.exe",
    "Debuggers\x64\dbghelp.dll"
  )
  # GN configures x86 tools even for x64/ARM64 targets.
  $libraryArches = @("x86", "x64")
  if ($Arch -eq "arm64") {
    $libraryArches += "arm64"
    $required += "Debuggers\arm64\dbghelp.dll"
  }
  foreach ($libraryArch in $libraryArches) {
    $required += @(
      "Lib\$SdkVersion\um\$libraryArch\kernel32.lib",
      "Lib\$SdkVersion\um\$libraryArch\user32.lib",
      "Lib\$SdkVersion\ucrt\$libraryArch\ucrt.lib",
      "Lib\$SdkVersion\ucrt\$libraryArch\libucrt.lib"
    )
  }
  foreach ($relative in $required) {
    $path = Join-Path $sdk $relative
    if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-Item -LiteralPath $path).Length -eq 0) {
      $path
    }
  }
  $versionHeader = Join-Path $sdk "Include\$SdkVersion\shared\sdkddkver.h"
  if ($SdkVersion -eq "10.0.28000.0" -and (Test-Path -LiteralPath $versionHeader -PathType Leaf)) {
    if ((Get-Content -LiteralPath $versionHeader -Raw) -notmatch '(?m)^\s*#\s*define\s+NTDDI_WIN11_BR\b') {
      "$versionHeader (NTDDI_WIN11_BR is required by base/win/windows_version.cc)"
    }
  }
}

$missing = @(Get-MissingSdkFiles)
if ($missing.Count -gt 0) {
  if (-not $Install) {
    throw "Windows SDK $SdkVersion prerequisites are missing or empty for ${Arch}:`n$($missing -join "`n")`nRun ensure-windows-sdk.ps1 -Arch $Arch -ChromiumVersion $ChromiumVersion -Install to install Microsoft's $SdkRelease SDK."
  }
  if (-not $DownloadDir) { $DownloadDir = [IO.Path]::GetTempPath() }
  $DownloadDir = [IO.Path]::GetFullPath($DownloadDir)
  # Only this invocation's private download directory is removed.
  $staging = Join-Path $DownloadDir ("chromix-windows-sdk-" + [guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Path $staging | Out-Null
  try {
    $installer = Join-Path $staging "winsdksetup.exe"
    Write-Host "==> Downloading Microsoft Windows SDK $SdkRelease installer"
    & curl.exe --disable --fail --location --silent --show-error `
      --proto '=https' --proto-redir '=https' --max-redirs 5 `
      --connect-timeout 20 --max-time 180 --retry 2 --retry-max-time 180 `
      --max-filesize $MaxInstallerBytes --output $installer $InstallerUrl
    $downloadExit = $LASTEXITCODE
    if ($downloadExit -ne 0) { throw "Windows SDK installer download failed (curl exit $downloadExit)" }
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) { throw "Windows SDK installer download produced no file" }
    $size = (Get-Item -LiteralPath $installer).Length
    if ($size -lt 1MB -or $size -gt $MaxInstallerBytes) { throw "Windows SDK installer has an invalid size: $size bytes" }
    $signature = Get-AuthenticodeSignature -LiteralPath $installer
    if ($signature.Status -ne "Valid" -or -not $signature.SignerCertificate -or
        $signature.SignerCertificate.Subject -notmatch '(^|,\s*)CN=Microsoft Corporation(,|$)' -or
        $signature.SignerCertificate.Subject -notmatch '(^|,\s*)O=Microsoft Corporation(,|$)') {
      throw "Windows SDK installer must have a valid Microsoft Corporation Authenticode signature"
    }
    $hash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash
    if ($hash -ne $InstallerSha256) { throw "Windows SDK $SdkRelease installer SHA256 mismatch: $hash" }
    Write-Host "==> Installing verified Microsoft Windows SDK $SdkRelease for $Arch"
    # '+' selects all SDK features, including ARM64 libraries, UWP and Debugging Tools.
    $setup = Start-Process -FilePath $installer -ArgumentList @(
      "/features", "+", "/quiet", "/norestart", "/installpath", ('"{0}"' -f $sdk.TrimEnd('\'))
    ) -PassThru
    try {
      # Retain the process handle for a reliable exit code and PID ownership.
      $null = $setup.Handle
      if (-not $setup.WaitForExit(1800000)) {
        Write-Warning "Windows SDK installation timed out after 30 minutes; attempting to terminate installer PID $($setup.Id)"
        try {
          $terminator = Start-Process -FilePath (Join-Path $env:SystemRoot "System32\taskkill.exe") `
            -ArgumentList @("/PID", "$($setup.Id)", "/T", "/F") -PassThru
          try {
            $null = $terminator.Handle
            if (-not $terminator.WaitForExit(10000)) {
              $terminator.Kill()
              throw "taskkill did not exit within 10 seconds"
            }
            if ($terminator.ExitCode -ne 0) { throw "taskkill failed (exit $($terminator.ExitCode))" }
          } finally {
            $terminator.Dispose()
          }
        } catch {
          Write-Warning "Unable to confirm termination of Windows SDK installer PID $($setup.Id): $($_.Exception.Message)"
        }
        throw "Windows SDK installation timed out after 30 minutes (PID $($setup.Id)); SDK is not ready"
      }
      $setupExit = $setup.ExitCode
    } finally {
      $setup.Dispose()
    }
    if ($null -eq $setupExit -or $setupExit -notin @(0, 3010)) { throw "Windows SDK installation failed (exit $setupExit)" }
    if ($setupExit -eq 3010) { Write-Warning "Windows SDK setup requested a reboot; verifying prerequisites before continuing" }
    $missing = @(Get-MissingSdkFiles)
    if ($missing.Count -gt 0) {
      throw "Windows SDK $SdkVersion verification failed after installation:`n$($missing -join "`n")"
    }
  } finally {
    Remove-Item -LiteralPath $staging -Recurse -Force
  }
}
$env:WINDOWSSDKDIR = $sdk
Write-Host "==> Windows SDK $SdkVersion verified for $Arch (x64 host tools)"
