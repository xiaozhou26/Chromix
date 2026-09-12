<#
  One stage of the GitHub-hosted Windows x64 build.

  The source is prepared through the same pinned ungoogled-chromium pipeline as
  build.ps1. Each stage restores C:\c\chromix, resumes ninja, and snapshots the
  tree before the GitHub job deadline.
#>
[CmdletBinding()]
param(
  [int]$StageIndex = 1,
  [int]$MaxStages = 12,
  [switch]$FromArtifact,
  [switch]$UseUpstreamCache,
  [ValidatePattern('\A[0-9]*\z')] [string]$UpstreamRunId = "",
  [switch]$ValidateOnly
)
$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$Revisions = Import-PowerShellDataFile (Join-Path $Repo "build\ungoogled-revisions.psd1")
$BuildProfile = if ($env:CHROMIX_BUILD_PROFILE) { $env:CHROMIX_BUILD_PROFILE } else { "native" }
if ($BuildProfile -notin @("native", "fast", "release")) { throw "invalid CHROMIX_BUILD_PROFILE" }

$Root = "C:\c"
$WorkDir = "$Root\chromix"
$Src = "$WorkDir\src"
$OutDir = "$Src\out\Chromix"
$RestoredUpstream = $false
# CI opt-in requires a full restore, including validation and artifact resumes.
$RequireUpstreamCache = $UseUpstreamCache -or $UpstreamRunId -or ($env:CHROMIX_USE_UPSTREAM_CACHE -eq "1")
$PartsDir = "C:\parts"
$UpstreamCacheDir = "C:\u"
# Standalone validation remains available; CI validates inside the first build job.
$StageMinutes = if ($ValidateOnly) { 230 } else { 300 }
$Deadline = (Get-Date).AddMinutes($StageMinutes)
$PackReserveMin = if ($ValidateOnly) { 15 } else { 40 }

function Write-OutVar($key, $value) {
  if ($env:GITHUB_OUTPUT) { Add-Content -Path $env:GITHUB_OUTPUT -Value "$key=$value" }
  Write-Host "==> outvar $key=$value"
}

function Get-RemainingMin {
  return [int][Math]::Floor((New-TimeSpan -Start (Get-Date) -End $Deadline).TotalMinutes)
}

function Test-LastStage { return $StageIndex -ge $MaxStages }

function Start-TrackedProcess {
  param([string]$File, [string]$Arguments, [string]$Cwd, [string]$Stdout, [string]$Stderr)
  if (-not ("Chromix.TrackedProcess" -as [type])) {
    Add-Type -TypeDefinition @'
namespace Chromix {
  public sealed class TrackedProcess : System.IDisposable {
    public readonly System.Diagnostics.Process Process;
    private readonly System.Threading.Tasks.Task logs;

    private static async System.Threading.Tasks.Task CopyLog(System.IO.Stream input, System.IO.Stream output) {
      // The copy task owns its streams until EOF, even if the parent exits first.
      using (input)
      using (output) {
        byte[] buffer = new byte[8192];
        int count;
        while ((count = await input.ReadAsync(buffer, 0, buffer.Length).ConfigureAwait(false)) != 0) {
          await output.WriteAsync(buffer, 0, count).ConfigureAwait(false);
          await output.FlushAsync().ConfigureAwait(false);
        }
      }
    }

    public TrackedProcess(string file, string arguments, string cwd, string stdout, string stderr) {
      var info = new System.Diagnostics.ProcessStartInfo(file, arguments);
      info.WorkingDirectory = cwd;
      info.UseShellExecute = false;
      info.CreateNoWindow = true;
      info.RedirectStandardOutput = true;
      info.RedirectStandardError = true;
      Process = new System.Diagnostics.Process();
      Process.StartInfo = info;
      System.IO.FileStream output = null, error = null;
      try {
        output = new System.IO.FileStream(stdout, System.IO.FileMode.Create, System.IO.FileAccess.Write, System.IO.FileShare.Read);
        error = new System.IO.FileStream(stderr, System.IO.FileMode.Create, System.IO.FileAccess.Write, System.IO.FileShare.Read);
        if (!Process.Start()) throw new System.InvalidOperationException("tracked process did not start");
      } catch {
        if (output != null) output.Dispose();
        if (error != null) error.Dispose();
        Process.Dispose();
        throw;
      }
      logs = System.Threading.Tasks.Task.WhenAll(CopyLog(Process.StandardOutput.BaseStream, output),
                                                CopyLog(Process.StandardError.BaseStream, error));
    }

    public bool Drain(int timeoutMs) { return logs.Wait(timeoutMs); }

    public void Dispose() {
      // Do not close a pipe or writer while a descendant still owns the other end.
      logs.ContinueWith(task => {
        if (task.IsFaulted) System.Console.Error.WriteLine("tracked log copy failed: " + task.Exception.GetBaseException().Message);
        Process.Dispose();
      }, System.Threading.Tasks.TaskScheduler.Default);
    }
  }
}
'@
  }
  return [Chromix.TrackedProcess]::new($File, $Arguments, $Cwd, $Stdout, $Stderr)
}

function Wait-TrackedDrain {
  param($Tracked, [int]$TimeoutMs = 10000)
  return $Tracked.Drain($TimeoutMs)
}

function Get-UpstreamTimeoutSummary {
  $values = [ordered]@{ phase = "unknown"; members = "unknown"; extracted_bytes = "unknown"; elapsed_seconds = "unknown" }
  try {
    $path = Join-Path $UpstreamCacheDir "result.json"
    if ((Get-Item -LiteralPath $path -ErrorAction Stop).Length -gt 64KB) { throw "oversized report" }
    $report = Get-Content -LiteralPath $path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    if ($report.phase -is [string] -and $report.phase -cmatch '\A[a-zA-Z0-9_-]{1,80}\z') {
      $values.phase = $report.phase
    }
    foreach ($key in @("members", "extracted_bytes", "elapsed_seconds")) {
      $value = $report.$key
      if ($key -eq "elapsed_seconds" -and $null -eq $value) { $value = $report.duration_seconds }
      $text = [string]$value
      $pattern = if ($key -eq "elapsed_seconds") { '\A[0-9]+(?:\.[0-9]+)?\z' } else { '\A[0-9]+\z' }
      if ($text.Length -le 32 -and $text -cmatch $pattern) { $values[$key] = $text }
    }
  } catch {
    # Missing or partially written diagnostics must not hide the timeout.
  }
  return ($values.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join "; "
}

function Invoke-Tracked {
  param(
    [string]$File,
    [string]$ArgList,
    [string]$Cwd,
    [int]$TimeoutSec,
    [switch]$Quiet,
    [switch]$FullFailureOutput
  )
  $log = Join-Path $env:TEMP "ci-tracked.log"
  $err = Join-Path $env:TEMP "ci-tracked.err"
  $wrapperName = "ci-tracked-$PID-$([Guid]::NewGuid().ToString('N'))"
  $wrapper = Join-Path $env:TEMP "$wrapperName.cmd"
  $status = Join-Path $env:TEMP "$wrapperName.exit"
  Remove-Item $log, $err, $wrapper, $status -ErrorAction SilentlyContinue

  $cmdFile = $File.Replace("%", "%%")
  $cmdArgs = $ArgList.Replace("%", "%%")
  $cmdStatus = $status.Replace("%", "%%")
  $wrapperLines = @(
    "@echo off",
    "`"$cmdFile`" $cmdArgs",
    'set "ci_tracked_exit=%ERRORLEVEL%"',
    ">`"$cmdStatus`" echo %ci_tracked_exit%",
    "exit /b %ci_tracked_exit%"
  )

  $writeFailureOutput = {
    param([switch]$ProducerMayBeRunning)
    if ($ProducerMayBeRunning) {
      Write-Host "==> tracked cleanup incomplete; bounded log excerpts (up to 64 KiB and 200 lines per stream)"
      foreach ($path in @($log, $err)) {
        $stream = $null
        try {
          $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
          $length = $stream.Length
          $count = [int][Math]::Min(65536, $length)
          $null = $stream.Seek($length - $count, [IO.SeekOrigin]::Begin)
          $buffer = New-Object byte[] $count
          $read = 0
          while ($read -lt $count) {
            $chunk = $stream.Read($buffer, $read, $count - $read)
            if ($chunk -eq 0) { break }
            $read += $chunk
          }
          $text = [Text.Encoding]::UTF8.GetString($buffer, 0, $read)
          $lines = $text -split "`r?`n"
          $start = [Math]::Max(0, $lines.Length - 200)
          for ($index = $start; $index -lt $lines.Length; $index++) {
            $line = $lines[$index]
            if ($line.Length -gt 500) { $line = $line.Substring(0, 500) + "..." }
            Write-Host "  ! | $line"
          }
        } catch {
          Write-Host "==> bounded tracked log excerpt unavailable"
        } finally {
          if ($null -ne $stream) { $stream.Dispose() }
        }
      }
      return
    }
    if (Test-Path $log) {
      if ($FullFailureOutput) {
        Write-Host "==> tracked process stdout (complete)"
        Get-Content $log -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  ! | $_" }
      } else {
        Get-Content $log -Tail 200 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  ! | $_" }
      }
    }
    if (Test-Path $err) {
      Get-Content $err -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  ! | $_" }
    }
  }
  $tracked = $null
  try {
    [IO.File]::WriteAllLines($wrapper, $wrapperLines, [Text.Encoding]::ASCII)
    Write-OutVar snapshot_safe false
    $tracked = Start-TrackedProcess -File $env:COMSPEC `
      -Arguments "/d /s /c `"`"$wrapper`"`"" -Cwd $Cwd -Stdout $log -Stderr $err
    $process = $tracked.Process
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    $tick = 0
    $lastTail = @{}
    while (-not $process.HasExited) {
      if ($stopwatch.Elapsed.TotalSeconds -gt $TimeoutSec) {
        Write-Host "==> timeout after $([int]$stopwatch.Elapsed.TotalMinutes) min; killing process tree"
        $killer = $null
        try {
          $killer = Start-Process -FilePath (Join-Path $env:SystemRoot "System32\taskkill.exe") `
            -ArgumentList "/PID $($process.Id) /T /F" -PassThru -WindowStyle Hidden
          $null = $killer.Handle
          if (-not $killer.WaitForExit(10000)) {
            try { $killer.Kill() } catch {}
            throw "tracked process tree cleanup timed out; refusing safe snapshot"
          }
          if ($killer.ExitCode -ne 0) {
            throw "tracked process tree cleanup failed; refusing safe snapshot"
          }
          if (-not $process.WaitForExit(10000)) {
            throw "tracked process is still running after taskkill; refusing safe snapshot"
          }
          if (-not (Wait-TrackedDrain -Tracked $tracked -TimeoutMs 10000)) {
            throw "tracked process log drain timed out after taskkill; refusing safe snapshot"
          }
        } finally {
          if ($null -ne $killer) { $killer.Dispose() }
        }
        & $writeFailureOutput
        Write-OutVar snapshot_safe true
        return 124
      }
      Start-Sleep -Seconds 10
      $tick++
      if (-not $Quiet -and ($tick % 6) -eq 0) {
        Write-Host "==> tracked process heartbeat: $([int]$stopwatch.Elapsed.TotalSeconds)s elapsed"
        foreach ($path in @($log, $err)) {
          if (-not (Test-Path $path)) { continue }
          $tail = @(Get-Content -LiteralPath $path -Tail 3 -ErrorAction SilentlyContinue |
            ForEach-Object { if ($_.Length -gt 500) { $_.Substring(0, 500) + "..." } else { $_ } })
          $text = $tail -join "`n"
          if ($text -and $lastTail[$path] -cne $text) {
            $label = if ($path -eq $err) { "stderr |" } else { "|" }
            $tail | ForEach-Object { Write-Host "    $label $_" }
            $lastTail[$path] = $text
          }
        }
      }
    }
    if (-not $process.WaitForExit(10000)) {
      throw "tracked process exit wait timed out; refusing safe snapshot"
    }
    if (-not (Wait-TrackedDrain -Tracked $tracked -TimeoutMs 10000)) {
      throw "tracked process log drain timed out; refusing safe snapshot"
    }
    Write-OutVar snapshot_safe true

    $code = 1
    if (-not (Test-Path -LiteralPath $status -PathType Leaf)) {
      Write-Host "==> tracked process exit status file is missing: $status; treating as failure"
    } else {
      $statusText = Get-Content -LiteralPath $status -Raw -ErrorAction SilentlyContinue
      $statusValue = if ($null -eq $statusText) { "" } else { $statusText.Trim() }
      $parsedCode = 0
      if ($statusValue -notmatch '^-?\d+$' -or
          -not [int]::TryParse($statusValue, [ref]$parsedCode)) {
        $displayStatus = if ($statusValue) { $statusValue } else { "<empty>" }
        Write-Host "==> tracked process exit status is invalid: '$displayStatus'; treating as failure"
      } else {
        $code = $parsedCode
        Write-Host "==> tracked process exit code: $code"
      }
    }
    if ($code -ne 0) { & $writeFailureOutput }
    return $code
  } catch {
    & $writeFailureOutput -ProducerMayBeRunning
    throw
  } finally {
    Remove-Item $wrapper, $status -ErrorAction SilentlyContinue
    if ($null -ne $tracked) { $tracked.Dispose() }
  }
}

function Get-FreeGB { return [math]::Round((Get-PSDrive C).Free / 1GB, 1) }

function Resolve-7Zip {
  $command = Get-Command 7z.exe -ErrorAction SilentlyContinue
  if ($command) { return $command.Source }
  $installed = "$env:ProgramFiles\7-Zip\7z.exe"
  if (Test-Path $installed) { return $installed }
  throw "7z.exe is not available"
}

function Save-Handoff {
  param([ValidateSet("Synced", "Unsynced")] [string]$Mode)
  if (Test-LastStage) { throw "build did not finish within $MaxStages stages" }
  Write-OutVar upload_parts true
  . "$PSScriptRoot\ci-parts.ps1" -Root $Root -PartsDir $PartsDir -Mode $Mode
}

function Assert-CiScripts {
  foreach ($path in @(
    "$PSScriptRoot\ci-stage.ps1",
    "$PSScriptRoot\ci-parts.ps1",
    "$PSScriptRoot\prepare-ungoogled.ps1",
    "$PSScriptRoot\update-restored-source.ps1",
    "$PSScriptRoot\package-win.ps1"
  )) {
    $tokens = $null
    $errors = $null
    [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors) | Out-Null
    if ($errors.Count -gt 0) { throw "$path failed PowerShell parsing: $($errors[0].Message)" }
  }
  Write-Host "==> CI PowerShell preflight passed"
}

function Free-Disk {
  Write-Host "==> disk before cleanup: $(Get-FreeGB) GB free"
  foreach ($target in @(
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
  )) {
    if (Test-Path $target) { Remove-Item -Recurse -Force $target -ErrorAction SilentlyContinue }
  }
  Write-Host "==> disk after cleanup: $(Get-FreeGB) GB free"
}

function Initialize-VisualStudio {
  $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
  if (-not (Test-Path $vswhere)) { throw "vswhere.exe is not available: $vswhere" }
  $installation = (& $vswhere -latest -products * `
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
    -property installationPath).Trim()
  if (-not $installation) { throw "Visual Studio 2022 C++ tools are not installed" }
  $devCmd = Join-Path $installation "Common7\Tools\VsDevCmd.bat"
  if (-not (Test-Path $devCmd)) { throw "VsDevCmd.bat is missing: $devCmd" }

  $command = "`"$devCmd`" -no_logo -arch=x64 -host_arch=x64 >nul && set"
  $environment = & $env:COMSPEC /d /s /c $command
  if ($LASTEXITCODE -ne 0) { throw "VsDevCmd.bat failed with exit $LASTEXITCODE" }
  foreach ($line in $environment) {
    if ($line -match '^([^=]+)=(.*)$') {
      [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
    }
  }
  $compiler = (Get-Command cl.exe -ErrorAction Stop).Source
  Write-Host "==> Visual Studio compiler: $compiler"
}

function Install-Debuggers {
  $dbghelp = "${env:ProgramFiles(x86)}\Windows Kits\10\Debuggers\x64\dbghelp.dll"
  if (Test-Path $dbghelp) { return }
  Write-Host "==> installing Windows SDK Debugging Tools"
  New-Item -ItemType Directory -Force -Path $Root | Out-Null
  $iso = "$Root\winsdk.iso"
  for ($attempt = 1; $attempt -le 5; $attempt++) {
    & curl.exe -sSL -o $iso "https://go.microsoft.com/fwlink/?linkid=2348707"
    if ((Test-Path $iso) -and ((Get-Item $iso).Length -gt 10MB)) { break }
    Start-Sleep -Seconds 10
  }
  if (-not (Test-Path $iso) -or ((Get-Item $iso).Length -lt 10MB)) { throw "Windows SDK ISO download failed" }
  $image = Mount-DiskImage -ImagePath $iso -StorageType ISO -PassThru
  $letter = ($image | Get-Volume).DriveLetter
  try {
    $setup = Start-Process -FilePath "$letter`:\WinSDKSetup.exe" `
      -ArgumentList "/features", "OptionId.WindowsDesktop.Debuggers", "/q", "/norestart" -PassThru -Wait
    if ($setup.ExitCode -ne 0) { throw "WinSDKSetup failed with exit $($setup.ExitCode)" }
  } finally {
    Dismount-DiskImage -ImagePath $iso | Out-Null
    Remove-Item $iso -Force -ErrorAction SilentlyContinue
  }
  if (-not (Test-Path $dbghelp)) { throw "dbghelp.dll is missing after Debugging Tools install" }
}

function Invoke-BoundedBrowser {
  param(
    [Parameter(Mandatory)] [string]$Launcher,
    [Parameter(Mandatory)] [string[]]$Arguments,
    [Parameter(Mandatory)] [string]$WorkingDirectory,
    [int]$TimeoutSec = 60
  )
  $quoted = foreach ($value in (@($Launcher) + $Arguments)) {
    # Smoke inputs do not need embedded quotes or cmd variable expansion.
    if ($value -match '["%\r\n\0]') { throw "browser smoke input contains unsupported shell characters" }
    # Preserve trailing backslashes before the closing quote in native argv.
    '"' + ($value -replace '(\\+)$', '$1$1') + '"'
  }
  $commandLine = '/d /v:off /s /c "' + ($quoted -join ' ') + '"'
  $id = [Guid]::NewGuid().ToString('N')
  $stdout = Join-Path $env:TEMP "chromix-browser-$id.out"
  $stderr = Join-Path $env:TEMP "chromix-browser-$id.err"
  $process = $null
  try {
    $process = Start-Process -FilePath $env:COMSPEC -ArgumentList $commandLine `
      -WorkingDirectory $WorkingDirectory -PassThru -WindowStyle Hidden `
      -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    # Retain the native handle before polling so Windows PowerShell keeps ExitCode.
    $null = $process.Handle
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    while (-not $process.HasExited) {
      if ($stopwatch.Elapsed.TotalSeconds -gt $TimeoutSec) {
        $killer = $null
        try {
          $killer = Start-Process -FilePath (Join-Path $env:SystemRoot "System32\taskkill.exe") `
            -ArgumentList "/PID $($process.Id) /T /F" -PassThru -WindowStyle Hidden
          $null = $killer.Handle
          if (-not $killer.WaitForExit(2000)) {
            try { $killer.Kill() } catch {}
            throw "taskkill timed out"
          }
          $killCode = $killer.ExitCode
          if ($null -eq $killCode -or $killCode -ne 0) {
            throw "taskkill failed with exit $killCode"
          }
        } catch {
          Write-Host "==> browser smoke tree cleanup failed: $($_.Exception.Message)"
        } finally {
          if ($null -ne $killer) { $killer.Dispose() }
        }
        # Windows PowerShell 5.1 has no Kill(entireProcessTree) overload.
        try {
          if (-not $process.HasExited) { $process.Kill() }
        } catch {
          Write-Host "==> browser smoke fallback termination failed: $($_.Exception.Message)"
        }
        if (-not $process.WaitForExit(2000)) {
          Write-Host "==> browser smoke process $($process.Id) is still running after bounded cleanup"
        }
        throw "browser smoke command timed out after $TimeoutSec seconds"
      }
      Start-Sleep -Milliseconds 250
    }
    $process.WaitForExit()
    $output = if (Test-Path -LiteralPath $stdout) { Get-Content -LiteralPath $stdout -Raw } else { "" }
    $errors = if (Test-Path -LiteralPath $stderr) { Get-Content -LiteralPath $stderr -Raw } else { "" }
    Write-Host $output
    if ($errors) { Write-Host $errors }
    $code = $process.ExitCode
    if ($null -eq $code) { throw "browser smoke command exit code is unavailable" }
    if ($code -ne 0) {
      throw "browser smoke command failed with exit $code"
    }
    return $output
  } finally {
    Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue
    if ($null -ne $process) { $process.Dispose() }
  }
}

function Invoke-FingerprintAcceptance([string]$Browser) {
  $python = (Get-Command python -ErrorAction Stop).Source
  $requirements = Join-Path $Repo "tools\fingerprint-requirements.txt"
  $install = Invoke-Tracked -File $python -Cwd $Repo -TimeoutSec 300 `
    -ArgList "-m pip install --disable-pip-version-check --timeout 30 --retries 1 -r `"$requirements`""
  if ($install -ne 0) { throw "fingerprint audit dependency installation failed (exit $install)" }
  $diagnostics = Join-Path $WorkDir ("fingerprint-diagnostics\runtime-" + [Guid]::NewGuid().ToString('N'))
  $hash = (Get-FileHash -LiteralPath $Browser -Algorithm SHA256).Hash.ToLowerInvariant()
  $script = Join-Path $Repo "tools\fingerprint_acceptance.py"
  $arguments = "-X utf8 `"$script`" --browser `"$Browser`" --expected-sha256 $hash " +
    "--expected-version $($Revisions.ChromiumVersion) --source-report `"$FingerprintSourceReport`" " +
    "--source-root `"$Src`" --output-dir `"$diagnostics`""
  $result = Invoke-Tracked -File $python -Cwd $Repo -ArgList $arguments -TimeoutSec 2100 -FullFailureOutput
  if ($result -ne 0) { throw "fingerprint acceptance failed (exit $result); diagnostics: $diagnostics" }
}

function Verify-FinalBundle {
  $asset = Join-Path $Root "dist\chromix-win-x64.zip"
  $manifest = Join-Path $Root "dist\SHA256SUMS"
  if (-not (Test-Path $asset) -or -not (Test-Path $manifest)) {
    throw "final Windows bundle or SHA256SUMS is missing"
  }
  $entry = @(Get-Content $manifest | Where-Object { $_ -match '^([0-9a-fA-F]{64})\s+chromix-win-x64\.zip$' })
  if ($entry.Count -ne 1) { throw "SHA256SUMS has no unique Windows ZIP entry" }
  $expected = [regex]::Match($entry[0], '^([0-9a-fA-F]{64})').Groups[1].Value.ToLowerInvariant()
  $actual = (Get-FileHash $asset -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -ne $expected) { throw "Windows ZIP checksum mismatch" }
  Write-Host "==> Windows ZIP checksum verified: $actual"

  $smokeRoot = Join-Path $Root "smoke"
  Remove-Item $smokeRoot -Recurse -Force -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force -Path $smokeRoot | Out-Null
  Expand-Archive -LiteralPath $asset -DestinationPath $smokeRoot -Force
  $bundle = Join-Path $smokeRoot "chromix"
  $launcher = Join-Path $bundle "chromix.cmd"
  $chrome = Join-Path $bundle "chrome.exe"
  $chromeDll = Join-Path $bundle "chrome.dll"
  if (-not (Test-Path $launcher) -or -not (Test-Path $chrome) -or -not (Test-Path $chromeDll)) {
    throw "extracted Windows bundle is missing chromix.cmd, chrome.exe, or chrome.dll"
  }
  # Chromium's --version handler is POSIX-only; check both Windows PE resources.
  foreach ($binary in @($chrome, $chromeDll)) {
    $info = (Get-Item -LiteralPath $binary).VersionInfo
    $version = '{0}.{1}.{2}.{3}' -f $info.ProductMajorPart, $info.ProductMinorPart, `
      $info.ProductBuildPart, $info.ProductPrivatePart
    if ($version -cne $Revisions.ChromiumVersion) {
      throw "extracted Windows browser version does not match the pinned Chromium version: $binary ($version)"
    }
    Write-Host "==> Windows product version verified: $binary ($version)"
  }
  # Windows prefers a versioned DLL directory over the adjacent portable DLL.
  $versionedDll = Join-Path (Join-Path $bundle $Revisions.ChromiumVersion) "chrome.dll"
  if (Test-Path -LiteralPath $versionedDll) {
    if ((Get-FileHash -LiteralPath $versionedDll -Algorithm SHA256).Hash -ne
        (Get-FileHash -LiteralPath $chromeDll -Algorithm SHA256).Hash) {
      throw "versioned Windows DLL differs from the newly linked portable DLL"
    }
  }
  $profile = Join-Path $smokeRoot "profile"
  $dom = Invoke-BoundedBrowser -Launcher $launcher -Arguments @(
    "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--user-data-dir=$profile", "--dump-dom", "data:text/html,<p>chromix-smoke-ok</p>"
  ) -WorkingDirectory $bundle -TimeoutSec 60
  if ($dom -notmatch '<p>chromix-smoke-ok</p>') {
    throw "extracted Windows browser did not render the smoke page"
  }
  Write-Host "==> Windows ZIP extraction, version, and headless smoke checks passed"
  # Startup smoke cannot exercise fingerprint APIs or confirm a current patch stack.
  Invoke-FingerprintAcceptance -Browser $chrome
}

Write-Host "==> Chromix CI stage $StageIndex | Chromium $($Revisions.ChromiumVersion) | remaining $(Get-RemainingMin) min"
Write-OutVar finished false
Write-OutVar upload_parts false
Write-OutVar snapshot_safe true
Assert-CiScripts
Free-Disk
Initialize-VisualStudio
Install-Debuggers
git config --global core.longpaths true

Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
$env:DEPOT_TOOLS_WIN_TOOLCHAIN = "0"
$env:DEPOT_TOOLS_METRICS = "0"
$env:DEPOT_TOOLS_COLLECT_METRICS = "0"

if ($FromArtifact -and -not (Test-Path "C:\restore\tree.7z.001")) {
  throw "resume artifact missing: C:\restore\tree.7z.001"
}
if (-not $FromArtifact -and $StageIndex -gt 1) {
  throw "stage $StageIndex requires -FromArtifact"
}
if ($FromArtifact) {
  $sevenZip = Resolve-7Zip
  & $sevenZip t "C:\restore\tree.7z.001" | Select-Object -Last 3
  if ($LASTEXITCODE -ne 0) { throw "7z archive test failed" }
  & $sevenZip x "C:\restore\tree.7z.001" -o"$Root" -y | Select-Object -Last 3
  if ($LASTEXITCODE -ne 0) { throw "7z restore failed" }
  Remove-Item C:\restore -Recurse -Force -ErrorAction SilentlyContinue
}

$domainProgress = Join-Path $Src ".chromix-domain-substitution-in-progress"
$domainMarker = Join-Path $Src ".chromix-domain-substituted"
$restoreReceipt = Join-Path $Src ".chromix-upstream-restored.json"
if (Get-ChildItem -LiteralPath $WorkDir -Directory -Filter ".chromix-upstream-restore-*" -ErrorAction SilentlyContinue) {
  throw "upstream restore transaction was interrupted; use a clean work directory"
}
if (Test-Path $restoreReceipt) {
  Write-Host "==> verifying restored upstream source receipt and pins"
  & python (Join-Path $Repo "tools\restore_upstream_cache.py") --phase verify `
    --platform windows --arch x64 --workdir $WorkDir
  if ($LASTEXITCODE -ne 0) { throw "restored upstream source verification failed (exit $LASTEXITCODE)" }
  $RestoredUpstream = $true
  $OutDir = "$Src\out\Default"
}
if (Test-Path $domainProgress) {
  throw "domain substitution was interrupted; use a clean work directory"
}
if ($RequireUpstreamCache -and -not $RestoredUpstream -and
    ($FromArtifact -or $StageIndex -ne 1 -or (Test-Path $Src))) {
  throw "required upstream cache: restore receipt missing; refusing cold preparation or compilation"
}

$MigrateRestoredSource = $false
if ($FromArtifact -and -not $RestoredUpstream) {
  $unpackedMarker = Join-Path $Src ".chromix-source-unpacked"
  $readyMarker = Join-Path $Src ".chromix-source-ready"
  $restoredVersion = ""
  if (Test-Path $unpackedMarker) {
    $restoredVersion = (Get-Content $unpackedMarker -Raw).Trim()
  } elseif (Test-Path $readyMarker) {
    $restoredVersion = ((Get-Content $readyMarker -Raw).Trim() -split '\|', 2)[0]
  }
  if ($restoredVersion -and $restoredVersion -ne $Revisions.ChromiumVersion) {
    Write-Host "==> restored tree targets Chromium $restoredVersion; preserving tooling/download_cache and removing incompatible src/out"
    Remove-Item $Src -Recurse -Force
  } elseif (Test-Path $readyMarker) {
    $MigrateRestoredSource = -not $RestoredUpstream
  } else {
    Write-Host "==> restored source is not ready; deferring migrations until patch preparation completes"
  }
}

if ($StageIndex -eq 1 -and -not $FromArtifact -and
    -not (Test-Path $Src) -and $RequireUpstreamCache) {
  # Leave the stage reserve and at least 30 minutes for restore/preparation.
  $fetchTimeoutSec = [Math]::Min(10800, ((Get-RemainingMin) - $PackReserveMin - 30) * 60)
  if ($fetchTimeoutSec -lt 60) {
    throw "required upstream cache: insufficient stage budget for restore"
  }
  $fetchArgs = @(
    (Join-Path $Repo "tools\fetch_upstream_cache.py"),
    "--platform", "windows", "--arch", "x64", "--destination", $UpstreamCacheDir
  )
  if ($UpstreamRunId) { $fetchArgs += @("--run-id", $UpstreamRunId) }
  # Bound download/extraction by both the cap and this job's remaining deadline.
  $fetchCommandLine = ($fetchArgs | ForEach-Object { "`"$_`"" }) -join " "
  try {
    $fetchRc = Invoke-Tracked -File (Get-Command python -ErrorAction Stop).Source `
      -ArgList $fetchCommandLine -Cwd $Repo -TimeoutSec $fetchTimeoutSec
  } catch {
    Write-Host "==> upstream cache progress: $(Get-UpstreamTimeoutSummary)"
    throw
  }
  if ($fetchRc -eq 124) {
    throw "required upstream cache fetch timed out; $(Get-UpstreamTimeoutSummary)"
  } elseif ($fetchRc -ne 0) {
    throw "upstream cache fetch helper failed (exit $fetchRc)"
  }
  # The fetcher exits zero for cache misses; report the cause before restore.
  $fetchResult = Get-Content -LiteralPath (Join-Path $UpstreamCacheDir "result.json") -Raw | ConvertFrom-Json
  if ($fetchResult.status -ne "hit") {
    throw ("required upstream cache fetch failed: $($fetchResult.reason); " +
           "phase=$($fetchResult.phase); duration_seconds=$($fetchResult.duration_seconds)")
  }
  python (Join-Path $Repo "tools\restore_upstream_cache.py") --phase restore `
    --platform windows --arch x64 --workdir $WorkDir --cache-dir $UpstreamCacheDir
  if ($LASTEXITCODE -ne 0) { throw "upstream restore helper failed (exit $LASTEXITCODE)" }
  if (-not (Test-Path -LiteralPath $restoreReceipt -PathType Leaf)) {
    throw "required upstream cache: restore receipt missing after restore; refusing cold preparation or compilation"
  }
  & python (Join-Path $Repo "tools\restore_upstream_cache.py") --phase verify `
    --platform windows --arch x64 --workdir $WorkDir
  if ($LASTEXITCODE -ne 0) { throw "restored upstream source verification failed (exit $LASTEXITCODE)" }
  $RestoredUpstream = $true
  $OutDir = "$Src\out\Default"
  Write-Host "==> restored upstream source/out/Default; appending Chromix patches before incremental Ninja"
}

if (-not (Test-Path (Join-Path $Src ".chromix-source-ready")) -and
    (Get-RemainingMin) -lt ($PackReserveMin + 30)) {
  if ($ValidateOnly) { throw "validate-only: insufficient preparation budget" }
  Save-Handoff -Mode Unsynced
  return
}
$prepareDeadline = [DateTimeOffset]::new($Deadline).ToUnixTimeSeconds()
try {
  # Revalidate ready markers on every stage, including artifact resumes.
  & "$PSScriptRoot\prepare-ungoogled.ps1" -Root $WorkDir -Repo $Repo `
    -DeadlineEpoch $prepareDeadline -ReserveMinutes $PackReserveMin
} catch {
  if ($_.Exception.Message -like "PREPARE_BUDGET_EXHAUSTED:*") {
    if ($ValidateOnly) { throw }
    Save-Handoff -Mode Unsynced
    return
  }
  throw
}
if ($MigrateRestoredSource) {
  & "$PSScriptRoot\update-restored-source.ps1" -Src $Src -OutDir $OutDir
}

$UngoogledTooling = Join-Path $WorkDir "tooling\ungoogled-chromium"
$WindowsTooling = Join-Path $WorkDir "tooling\ungoogled-chromium-windows"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$gnArgs = Join-Path $OutDir "args.gn"
$mergeArgs = @((Join-Path $Repo "tools\merge_gn_args.py"), $gnArgs)
if ($BuildProfile -in @("fast", "release")) { $mergeArgs += @("--build-profile", $BuildProfile) }
if ($RestoredUpstream) { $mergeArgs += $gnArgs }
$mergeArgs += @(
  (Join-Path $UngoogledTooling "flags.gn"),
  (Join-Path $WindowsTooling "flags.windows.gn"),
  (Join-Path $Repo "build\args.windows.gn")
)
python @mergeArgs
if ($LASTEXITCODE -ne 0) { throw "GN argument merge failed" }

$env:PATH = "$(Join-Path $Src 'third_party\ninja');$(Join-Path $Src 'third_party\node\win');$env:PATH"
$Ninja = Join-Path $Src "third_party\ninja\ninja.exe"
if ($RestoredUpstream) {
  $Ninja = & python (Join-Path $Repo "tools\restore_ninja.py") --workdir $WorkDir --platform windows --arch x64
  if ($LASTEXITCODE -ne 0 -or -not $Ninja) { throw "restored Ninja compatibility check failed" }
  $env:NINJA = $Ninja
}
Push-Location $Src
try {
  if (-not (Test-Path "third_party\rust-toolchain\bin\bindgen.exe")) {
    # bindgen's build script hard-requires cargo+rustc that prepare merged
    # into third_party\rust-toolchain; failing fast here with the directory
    # state keeps a silent merge regression from dying 45 minutes of ninja
    # bootstrap output later with only a bare missing-cargo line.
    foreach ($binary in @("cargo.exe", "rustc.exe")) {
      if (-not (Test-Path "third_party\rust-toolchain\bin\$binary")) {
        Get-ChildItem third_party -Directory |
          Where-Object { $_.Name -like "rust-toolchain*" } | ForEach-Object {
            Write-Host "    toolchain dir: $($_.Name)"
          }
        throw ("bindgen precondition failed: third_party\rust-toolchain\bin\$binary is missing")
      }
    }
    if ($RestoredUpstream) {
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
  if ($RestoredUpstream) {
    python (Join-Path $Repo "tools\prepare_restored_build.py") --phase finish `
      --platform windows --arch x64 --workdir $WorkDir
    if ($LASTEXITCODE -ne 0) { throw "restored build preparation failed (exit $LASTEXITCODE)" }
  }
  $gn = Join-Path $OutDir "gn.exe"
  if (-not (Test-Path $gn)) {
    python tools\gn\bootstrap\bootstrap.py -o $gn --skip-generate-buildfiles
    if ($LASTEXITCODE -ne 0) { throw "GN bootstrap failed" }
  }
  if (-not (Test-Path $domainMarker)) {
    # The pinned helper selects compression from the cache filename's suffix.
    $domainCache = Join-Path $WorkDir "domain_substitution_cache.tar.gz"
    if ((Test-Path $domainCache) -or
        (Test-Path (Join-Path $WorkDir "domain_substitution_cache.tar"))) {
      throw "domain substitution cache exists without a completion marker; use a clean work directory"
    }
    Set-Content -Path $domainProgress -Value $Revisions.UngoogledCommit -Encoding ASCII
    Write-Host "==> applying ungoogled domain substitution"
    python (Join-Path $UngoogledTooling "utils\domain_substitution.py") apply `
      -r (Join-Path $UngoogledTooling "domain_regex.list") `
      -f (Join-Path $WindowsTooling "domain_substitution.list") `
      -c $domainCache $Src
    if ($LASTEXITCODE -ne 0) { throw "domain substitution failed" }
    Move-Item -LiteralPath $domainProgress -Destination $domainMarker
  }
  # A matching readiness stamp is not proof that a resumed tree contains all
  # revised patches. Verify actual hunks after legacy migrations/substitution.
  $fingerprintDiagnostics = Join-Path $WorkDir "fingerprint-diagnostics"
  New-Item -ItemType Directory -Force -Path $fingerprintDiagnostics | Out-Null
  $FingerprintSourceReport = Join-Path $fingerprintDiagnostics ("source-" + [Guid]::NewGuid().ToString('N') + ".json")
  & python -X utf8 (Join-Path $Repo "tools\verify_patch_stack.py") --src $Src --repo $Repo `
    --core $UngoogledTooling --platform-tooling $WindowsTooling --platform windows --output $FingerprintSourceReport
  if ($LASTEXITCODE -ne 0) { throw "restored source does not contain the current fingerprint patch stack" }
  & $gn gen $OutDir --fail-on-unused-args
  if ($LASTEXITCODE -ne 0) { throw "gn gen failed" }
  if ($RestoredUpstream) {
    Write-Host "==> recording incremental Ninja plan for restored upstream source/out/Default"
    & $Ninja -C $OutDir -n chrome `
      *> (Join-Path $WorkDir "upstream-cache-plan.log")
    if ($LASTEXITCODE -ne 0) { throw "restored upstream build-plan check failed" }
  }
} finally {
  Pop-Location
}

if ($ValidateOnly -or ($StageIndex -eq 1 -and -not $FromArtifact)) {
  # Keep validation and compilation on the same prepared tree and runner.
  Write-Host "==> validating V8 Torque generation target"
  $validationBudget = (Get-RemainingMin) - $PackReserveMin
  if ($validationBudget -lt 1) { throw "validate-only: insufficient V8 Torque budget" }
  $validationRc = Invoke-Tracked -File $Ninja `
    -ArgList "-C `"$OutDir`" -j 1 -v gen/v8/torque-generated/bit-field-asserts.cc" `
    -Cwd $Src -TimeoutSec ($validationBudget * 60) -FullFailureOutput
  if ($validationRc -ne 0) { throw "V8 Torque validation failed (exit $validationRc)" }
  Write-Host "==> gn gen and V8 Torque generation passed"
  if ($ValidateOnly) {
    Write-OutVar finished true
    return
  }
}

$ninjaBudget = (Get-RemainingMin) - $PackReserveMin
if ($ninjaBudget -lt 20) {
  Save-Handoff -Mode Synced
  return
}
if ($RestoredUpstream) {
  # The collector preserves the initial baseline across artifact resumes.
  & python (Join-Path $Repo "tools\restored_reuse_evidence.py") --phase before `
    --workdir $WorkDir --platform windows --arch x64 --ninja $Ninja --target chrome
  if ($LASTEXITCODE -ne 0) { throw "restored reuse evidence collection failed before Ninja (exit $LASTEXITCODE)" }
}
$CompileJobs = 4
if ($env:CHROMIX_JOBS) {
  if ($env:CHROMIX_JOBS -notmatch '\A[1-9][0-9]{0,3}\z' -or [int]$env:CHROMIX_JOBS -gt 1024) {
    throw "CHROMIX_JOBS must be an integer from 1 to 1024"
  }
  $CompileJobs = [int]$env:CHROMIX_JOBS
}
Write-Host "==> Ninja compile jobs: $CompileJobs"
& "$PSScriptRoot\configure-node.ps1" -NodePath (Join-Path $Src 'third_party\node\win\node.exe')
$rc = Invoke-Tracked -File $Ninja `
  -ArgList "-C `"$OutDir`" -j $CompileJobs chrome" -Cwd $Src -TimeoutSec ($ninjaBudget * 60)
if ($RestoredUpstream) {
  try {
    & python (Join-Path $Repo "tools\restored_reuse_evidence.py") --phase after `
      --workdir $WorkDir --platform windows --arch x64 --ninja $Ninja --target chrome --exit-code $rc
    if ($LASTEXITCODE -ne 0) { throw "restored reuse evidence collection failed after Ninja (exit $LASTEXITCODE)" }
  } catch {
    if ($rc -ne 0) { throw "ninja failed (exit $rc); $($_.Exception.Message)" }
    throw
  }
}

if ($rc -eq 0) {
  New-Item -ItemType Directory -Force -Path "$Root\dist" | Out-Null
  & "$PSScriptRoot\package-win.ps1" -Out $OutDir -Dest "$Root\dist"
  Verify-FinalBundle
  Write-OutVar finished true
  return
}
if ($rc -eq 124) {
  Save-Handoff -Mode Synced
  return
}
throw "ninja failed (exit $rc)"
