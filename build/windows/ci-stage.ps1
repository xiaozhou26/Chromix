<#
  One stage of the GitHub-hosted Windows build (x64 host, x64 or ARM64 target).

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
  [switch]$ValidateOnly,
  [ValidateSet("x64", "arm64")]
  [string]$Arch = $(if ($env:CHROMIX_TARGET_ARCH) { $env:CHROMIX_TARGET_ARCH } else { "x64" })
)
$ErrorActionPreference = "Stop"
if ($Arch -cnotin @("x64", "arm64")) { throw "Arch/CHROMIX_TARGET_ARCH must be x64 or arm64" }
$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$Revisions = & "$PSScriptRoot\read-platform-pins.ps1" -Repo $Repo
$BuildProfile = if ($env:CHROMIX_BUILD_PROFILE) { $env:CHROMIX_BUILD_PROFILE } else { "native" }
if ($BuildProfile -notin @("native", "fast", "release")) { throw "invalid CHROMIX_BUILD_PROFILE" }

$Root = "C:\c"
$WorkDir = "$Root\chromix"
$Src = "$WorkDir\src"
$OutDir = "$Src\out\Chromix"
$RestoredUpstream = $false
$RecoveryDiagnostics = $null
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
    $typeOptions = @{}
    if ($PSVersionTable.PSEdition -eq "Desktop") {
      $typeOptions.ReferencedAssemblies = @("System.dll", "System.Core.dll")
    }
    Add-Type @typeOptions -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace Chromix {
  public sealed class TrackedProcess : IDisposable {
    public readonly Process Process;
    private readonly Task logs;
    private IntPtr job;
    private const uint CREATE_SUSPENDED = 0x00000004;
    private const uint CREATE_NO_WINDOW = 0x08000000;
    private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct StartupInfo {
      public int Size;
      public string Reserved, Desktop, Title;
      public uint X, Y, XSize, YSize, XCountChars, YCountChars, FillAttribute, Flags;
      public ushort ShowWindow, ReservedSize;
      public IntPtr ReservedData, StdInput, StdOutput, StdError;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct ProcessInfo {
      public IntPtr Process, Thread;
      public uint ProcessId, ThreadId;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct BasicLimits {
      public long ProcessUserTime, JobUserTime;
      public uint Flags;
      public UIntPtr MinWorkingSet, MaxWorkingSet;
      public uint ActiveProcessLimit;
      public UIntPtr Affinity;
      public uint PriorityClass, SchedulingClass;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters {
      public ulong ReadOperations, WriteOperations, OtherOperations, ReadBytes, WriteBytes, OtherBytes;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct ExtendedLimits {
      public BasicLimits Basic;
      public IoCounters Io;
      public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct Accounting {
      public long UserTime, KernelTime, PeriodUserTime, PeriodKernelTime;
      public uint PageFaults, TotalProcesses, ActiveProcesses, TerminatedProcesses;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref ExtendedLimits info, uint length);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool QueryInformationJobObject(IntPtr job, int infoClass, out Accounting info, uint length, IntPtr returnedLength);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateJobObject(IntPtr job, uint exitCode);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CreateProcess(string application, StringBuilder commandLine, IntPtr processAttributes,
      IntPtr threadAttributes, bool inheritHandles, uint flags, IntPtr environment, string cwd,
      ref StartupInfo startup, out ProcessInfo process);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(IntPtr process, uint exitCode);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr handle, uint timeoutMs);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    private static Win32Exception NativeError(string operation) {
      int code = Marshal.GetLastWin32Error();
      return new Win32Exception(code, operation + " failed (Win32 " + code + "): " + new Win32Exception(code).Message);
    }

    private static async Task CopyLog(Stream input, Stream output) {
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
      ProcessInfo native = new ProcessInfo();
      FileStream output = null, error = null;
      AnonymousPipeServerStream inputPipe = null, outputPipe = null, errorPipe = null;
      try {
        // The unnamed, non-inheritable job permits neither explicit nor silent breakaway.
        job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero) throw NativeError("CreateJobObject");
        ExtendedLimits limits = new ExtendedLimits();
        limits.Basic.Flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if (!SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf(typeof(ExtendedLimits))))
          throw NativeError("SetInformationJobObject");
        output = new FileStream(stdout, FileMode.Create, FileAccess.Write, FileShare.Read);
        error = new FileStream(stderr, FileMode.Create, FileAccess.Write, FileShare.Read);
        inputPipe = new AnonymousPipeServerStream(PipeDirection.Out, HandleInheritability.Inheritable);
        outputPipe = new AnonymousPipeServerStream(PipeDirection.In, HandleInheritability.Inheritable);
        errorPipe = new AnonymousPipeServerStream(PipeDirection.In, HandleInheritability.Inheritable);
        StartupInfo startup = new StartupInfo();
        startup.Size = Marshal.SizeOf(typeof(StartupInfo));
        startup.Flags = 0x00000100; // STARTF_USESTDHANDLES
        startup.StdInput = inputPipe.ClientSafePipeHandle.DangerousGetHandle();
        startup.StdOutput = outputPipe.ClientSafePipeHandle.DangerousGetHandle();
        startup.StdError = errorPipe.ClientSafePipeHandle.DangerousGetHandle();
        if (!CreateProcess(file, new StringBuilder("\"" + file + "\" " + arguments), IntPtr.Zero, IntPtr.Zero,
                           true, CREATE_SUSPENDED | CREATE_NO_WINDOW, IntPtr.Zero, cwd, ref startup, out native))
          throw NativeError("CreateProcess");
        // No user code (and therefore no descendant) runs before job assignment succeeds.
        if (!AssignProcessToJobObject(job, native.Process)) throw NativeError("AssignProcessToJobObject");
        Process = System.Diagnostics.Process.GetProcessById((int)native.ProcessId);
        IntPtr retainedHandle = Process.Handle;
        inputPipe.DisposeLocalCopyOfClientHandle();
        outputPipe.DisposeLocalCopyOfClientHandle();
        errorPipe.DisposeLocalCopyOfClientHandle();
        Task outputLog = CopyLog(outputPipe, output);
        outputPipe = null;
        output = null;
        Task errorLog = CopyLog(errorPipe, error);
        errorPipe = null;
        error = null;
        logs = Task.WhenAll(outputLog, errorLog);
        if (ResumeThread(native.Thread) == uint.MaxValue) throw NativeError("ResumeThread");
      } catch {
        // Assignment failure leaves a suspended root, which still needs explicit termination.
        if (native.Process != IntPtr.Zero) {
          if (!TerminateProcess(native.Process, 1)) Console.Error.WriteLine(NativeError("TerminateProcess during startup cleanup"));
          if (WaitForSingleObject(native.Process, 10000) != 0) Console.Error.WriteLine("tracked startup cleanup could not confirm root exit");
        }
        Dispose();
        throw;
      } finally {
        if (native.Thread != IntPtr.Zero) CloseHandle(native.Thread);
        if (native.Process != IntPtr.Zero) CloseHandle(native.Process);
        if (inputPipe != null) inputPipe.Dispose();
        if (outputPipe != null) outputPipe.Dispose();
        if (errorPipe != null) errorPipe.Dispose();
        if (output != null) output.Dispose();
        if (error != null) error.Dispose();
      }
    }

    public uint GetActiveProcesses() {
      if (job == IntPtr.Zero) throw new ObjectDisposedException("tracked job");
      Accounting info;
      if (!QueryInformationJobObject(job, 1, out info, (uint)Marshal.SizeOf(typeof(Accounting)), IntPtr.Zero))
        throw NativeError("QueryInformationJobObject");
      return info.ActiveProcesses;
    }

    public bool WaitForTreeExit(int timeoutMs) {
      if (timeoutMs < 0) throw new ArgumentOutOfRangeException("timeoutMs");
      Stopwatch watch = Stopwatch.StartNew();
      while (GetActiveProcesses() != 0) {
        if (watch.ElapsedMilliseconds >= timeoutMs) return false;
        Thread.Sleep((int)Math.Max(1, Math.Min(50, timeoutMs - watch.ElapsedMilliseconds)));
      }
      return true;
    }

    public bool TerminateTree(int timeoutMs) {
      if (job == IntPtr.Zero) throw new ObjectDisposedException("tracked job");
      if (!TerminateJobObject(job, 124)) throw NativeError("TerminateJobObject");
      return WaitForTreeExit(timeoutMs);
    }

    public bool Drain(int timeoutMs) { return logs.Wait(timeoutMs); }

    public void Dispose() {
      IntPtr handle = Interlocked.Exchange(ref job, IntPtr.Zero);
      if (handle != IntPtr.Zero && !CloseHandle(handle)) Console.Error.WriteLine(NativeError("CloseHandle(job)"));
      // Kill-on-close is a fallback, not proof of quiescence for a snapshot.
      if (logs == null) {
        if (Process != null) Process.Dispose();
      } else {
        logs.ContinueWith(task => {
          if (task.IsFaulted) Console.Error.WriteLine("tracked log copy failed: " + task.Exception.GetBaseException().Message);
          if (Process != null) Process.Dispose();
        }, TaskScheduler.Default);
      }
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
    while (-not $process.HasExited -or $tracked.GetActiveProcesses() -ne 0) {
      if ($stopwatch.Elapsed.TotalSeconds -gt $TimeoutSec) {
        Write-Host "==> timeout after $([int]$stopwatch.Elapsed.TotalMinutes) min; killing process tree"
        $killer = $null
        $killCode = $null
        $killStatus = "not-started"
        $killerStopped = $true
        $killOut = Join-Path $env:TEMP "$wrapperName.taskkill.out"
        $killErr = Join-Path $env:TEMP "$wrapperName.taskkill.err"
        try {
          $killer = Start-Process -FilePath (Join-Path $env:SystemRoot "System32\taskkill.exe") `
            -ArgumentList "/PID $($process.Id) /T /F" -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput $killOut -RedirectStandardError $killErr
          $killerStopped = $false
          $null = $killer.Handle
          $killerStopped = $killer.WaitForExit(10000)
          $killStatus = if ($killerStopped) { "exited" } else { "timed-out" }
        } catch {
          $killStatus = if ($null -eq $killer) { "start-failed" } else { "wait-failed" }
          Write-Host "==> taskkill $killStatus`: $($_.Exception.Message)"
        } finally {
          if ($null -ne $killer -and -not $killerStopped) {
            try { $killer.Kill() } catch {
              Write-Host "==> taskkill termination failed: $($_.Exception.Message)"
            }
            # Kill may race with helper exit; the bounded wait, not Kill, confirms it stopped.
            try { $killerStopped = $killer.WaitForExit(2000) } catch {
              Write-Host "==> taskkill exit wait failed: $($_.Exception.Message)"
            }
          }
          if ($null -ne $killer -and $killerStopped) {
            try { $killCode = $killer.ExitCode } catch {
              Write-Host "==> taskkill exit code read failed: $($_.Exception.Message)"
            }
          }
          $displayCode = if ($null -eq $killCode) { "unavailable" } else { [string]$killCode }
          Write-Host "==> taskkill status: $killStatus; helper stopped: $killerStopped"
          Write-Host "==> taskkill exit code: $displayCode; stdout: $killOut; stderr: $killErr"
          try {
            foreach ($path in @($killOut, $killErr)) {
              if (Test-Path -LiteralPath $path) {
                Get-Content -LiteralPath $path -Tail 200 -ErrorAction SilentlyContinue |
                  ForEach-Object { Write-Host "  taskkill | $_" }
              }
            }
          } catch {
            Write-Host "==> taskkill output unavailable: $($_.Exception.Message)"
          }
          if ($null -ne $killer) {
            try { $killer.Dispose() } catch {
              Write-Host "==> taskkill handle disposal failed: $($_.Exception.Message)"
            }
          }
        }
        # taskkill is diagnostic/best-effort; only the non-breakaway job proves tree exit.
        try {
          $treeEmpty = $tracked.TerminateTree(10000)
        } catch {
          throw "tracked job cleanup failed: $($_.Exception.Message); refusing safe snapshot"
        }
        if (-not $treeEmpty) { throw "tracked job still has active processes; refusing safe snapshot" }
        Write-Host "==> tracked job active processes: 0 (independently verified)"
        if (-not $process.WaitForExit(10000)) {
          throw "tracked process is still running after job termination; refusing safe snapshot"
        }
        if (-not (Wait-TrackedDrain -Tracked $tracked -TimeoutMs 10000)) {
          throw "tracked process log drain timed out after job termination; refusing safe snapshot"
        }
        if (-not $killerStopped) {
          throw "taskkill helper exit could not be confirmed after bounded cleanup; refusing safe snapshot"
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
    if (-not $tracked.WaitForTreeExit(10000)) {
      throw "tracked job still has active processes after root exit; refusing safe snapshot"
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
  if ($Arch -eq "arm64" -or (Test-Path (Join-Path $WorkDir ".chromix-target-arch"))) {
    & "$PSScriptRoot\assert-target-arch.ps1" -WorkDir $WorkDir -Arch $Arch -Initialize
  }
  Write-OutVar upload_parts true
  . "$PSScriptRoot\ci-parts.ps1" -Root $Root -PartsDir $PartsDir -Mode $Mode -Arch $Arch
}

function Assert-CiScripts {
  foreach ($path in @(
    "$PSScriptRoot\ci-stage.ps1",
    "$PSScriptRoot\ci-parts.ps1",
    "$PSScriptRoot\assert-target-arch.ps1",
    "$PSScriptRoot\assert-arm64-toolchain.ps1",
    "$PSScriptRoot\ensure-windows-sdk.ps1",
    "$PSScriptRoot\read-platform-pins.ps1",
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
  $installations = @(& $vswhere -latest -products * `
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
    -property installationPath)
  $discoveryExit = $LASTEXITCODE
  if ($discoveryExit -ne 0) { throw "Visual Studio discovery failed (vswhere exit $discoveryExit)" }
  $installation = $installations | Select-Object -First 1
  if (-not $installation) { throw "Visual Studio 2022 C++ tools are not installed" }
  $installation = $installation.Trim()
  if ($Arch -eq "arm64") {
    $installations = @(& $vswhere -latest -products * -version '[17.0,18.0)' `
      -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 Microsoft.VisualStudio.Component.VC.Tools.ARM64 `
      -property installationPath)
    $discoveryExit = $LASTEXITCODE
    if ($discoveryExit -ne 0) { throw "VS2022 ARM64 discovery failed (vswhere exit $discoveryExit)" }
    $installation = $installations | Select-Object -First 1
    if (-not $installation) {
      throw "VS2022 ARM64 tools are missing; add Microsoft.VisualStudio.Component.VC.Tools.ARM64 with the existing VS installer --add"
    }
    $env:GYP_MSVS_OVERRIDE_PATH = $installation.Trim()
    $env:vs2022_install = $installation.Trim()
  }
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

function Install-WindowsSdk {
  & "$PSScriptRoot\ensure-windows-sdk.ps1" -Arch $Arch `
    -ChromiumVersion $Revisions.ChromiumVersion -DownloadDir $Root -Install
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
  $diagnostics = if ($RecoveryDiagnostics) { Join-Path $RecoveryDiagnostics 'runtime' } else {
    Join-Path $WorkDir ("fingerprint-diagnostics\runtime-" + [Guid]::NewGuid().ToString('N'))
  }
  if (Test-Path -LiteralPath $diagnostics) { throw 'Current runtime diagnostics already exist' }
  $hash = (Get-FileHash -LiteralPath $Browser -Algorithm SHA256).Hash.ToLowerInvariant()
  $script = Join-Path $Repo "tools\fingerprint_acceptance.py"
  $arguments = "-X utf8 `"$script`" --browser `"$Browser`" --expected-sha256 $hash " +
    "--expected-version $($Revisions.ChromiumVersion) --source-report `"$FingerprintSourceReport`" " +
    "--source-root `"$Src`" --output-dir `"$diagnostics`""
  $result = Invoke-Tracked -File $python -Cwd $Repo -ArgList $arguments -TimeoutSec 2100 -FullFailureOutput
  if ($result -ne 0) { throw "fingerprint acceptance failed (exit $result); diagnostics: $diagnostics" }
}

function Verify-FinalBundle {
  $assetName = if ($Arch -eq "arm64") { "chromix-win-arm64.zip" } else { "chromix-win-x64.zip" }
  $asset = Join-Path $Root "dist\$assetName"
  $manifest = Join-Path $Root "dist\SHA256SUMS"
  if (-not (Test-Path $asset) -or -not (Test-Path $manifest)) {
    throw "final Windows bundle or SHA256SUMS is missing"
  }
  $entryPattern = '^([0-9a-fA-F]{64})\s+' + [regex]::Escape($assetName) + '$'
  $entry = @(Get-Content $manifest | Where-Object { $_ -match $entryPattern })
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
  if ($Arch -eq "arm64") {
    python (Join-Path $Repo "tools\verify_windows_bundle.py") --bundle $bundle --arch $Arch
    if ($LASTEXITCODE -ne 0) { throw "extracted Windows ARM64 bundle metadata verification failed" }
    Write-OutVar runtime_verified false
    Write-OutVar status compiled
    Write-Host "==> Windows ARM64 ZIP and PE metadata verified; native runtime verification is required on windows-11-arm"
    return
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
Write-OutVar snapshot_safe $(if ($env:CHROMIX_WINDOWS_VERIFY_SOURCE_REPO -or $env:CHROMIX_WINDOWS_VERIFY_SOURCE_SHA) { "false" } else { "true" })
Assert-CiScripts
Free-Disk
Initialize-VisualStudio
Install-WindowsSdk
if ($Arch -eq "arm64") {
  & "$PSScriptRoot\assert-arm64-toolchain.ps1" -Installation $env:GYP_MSVS_OVERRIDE_PATH `
    -ChromiumVersion $Revisions.ChromiumVersion
}
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

if ($env:CHROMIX_WINDOWS_VERIFY_SOURCE_REPO -or $env:CHROMIX_WINDOWS_VERIFY_SOURCE_SHA) {
  Write-OutVar snapshot_safe false
  $stage6Verify = $StageIndex -eq 6 -and $env:CHROMIX_WINDOWS_VERIFY_SOURCE_SHA -ceq 'e5b29c58b44e381924a2dd4bd60f54d01abb9d9c'
  $stage8Verify = $StageIndex -eq 8 -and $env:CHROMIX_WINDOWS_VERIFY_SOURCE_SHA -ceq '2a55082adb89cb8bac7aa7ab8bb61162b53f4c35'
  if (-not $FromArtifact -or -not ($stage6Verify -or $stage8Verify) -or $Arch -cne "x64" -or
      -not $RequireUpstreamCache -or $BuildProfile -cne "native" -or ($stage8Verify -and $ValidateOnly) -or
      -not $env:CHROMIX_WINDOWS_VERIFY_SOURCE_REPO -or -not $env:CHROMIX_WINDOWS_VERIFY_SOURCE_SHA -or
      $env:CHROMIX_WINDOWS_MIGRATION_REPO -or $env:CHROMIX_WINDOWS_MIGRATION_SHA -or $env:CHROMIX_WINDOWS_MIGRATION_PROFILE) {
    throw "unchanged-source verification requires an exact native x64 stage6 or stage8 upstream snapshot, without migration"
  }
  $verifyDiagnostics = Join-Path $WorkDir "fingerprint-diagnostics"
  if ($stage8Verify) {
    if ($env:GITHUB_RUN_ID -cnotmatch '\A[1-9][0-9]*\z' -or $env:GITHUB_RUN_ATTEMPT -cnotmatch '\A[1-9][0-9]*\z' -or
        $env:GITHUB_JOB -cne 'build-8' -or $env:GITHUB_SHA -cnotmatch '\A[0-9a-f]{40}\z') {
      throw 'stage8 recovery requires an exact consumer run/attempt/job/SHA'
    }
    $recoveryRelative = "recovery-hops/d35485726877-a1-s8-j106081168976/c$env:GITHUB_RUN_ID-a$env:GITHUB_RUN_ATTEMPT-$env:GITHUB_JOB-$env:GITHUB_SHA"
    $RecoveryDiagnostics = Join-Path $verifyDiagnostics $recoveryRelative
    if (Test-Path -LiteralPath $RecoveryDiagnostics) { throw 'Current recovery evidence directory already exists' }
    New-Item -ItemType Directory -Path $RecoveryDiagnostics | Out-Null
    $verifyDiagnostics = $RecoveryDiagnostics
    $verifyReport = Join-Path $verifyDiagnostics 'windows-unchanged-source.json'
  } else {
    New-Item -ItemType Directory -Force -Path $verifyDiagnostics | Out-Null
    $verifyReport = Join-Path $verifyDiagnostics ("windows-unchanged-source-" + [Guid]::NewGuid().ToString('N') + ".json")
  }
  & python -X utf8 (Join-Path $Repo "tools\verify_windows_snapshot_source.py") --workdir $WorkDir `
    --previous-repo $env:CHROMIX_WINDOWS_VERIFY_SOURCE_REPO --repo $Repo `
    --expected-previous-sha $env:CHROMIX_WINDOWS_VERIFY_SOURCE_SHA --arch $Arch --build-profile $BuildProfile --report $verifyReport
  if ($LASTEXITCODE -ne 0) { throw "Windows unchanged-source verification failed; restore a clean matching snapshot" }
  foreach ($name in @("windows-snapshot.json", "windows-snapshot-download.json")) {
    [IO.File]::Copy((Join-Path $env:RUNNER_TEMP $name), (Join-Path $verifyDiagnostics $name), $false)
  }
  if ($stage8Verify) {
    $publishHop = @'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'tools'))
from verify_windows_snapshot_source import publish_stage8_hop
publish_stage8_hop(Path(sys.argv[2]))
'@
    & python -X utf8 -c $publishHop $Repo $verifyDiagnostics
    if ($LASTEXITCODE -ne 0) { throw 'Current stage8 recovery evidence binding failed' }
    Write-OutVar recovery_report_dir $recoveryRelative
    Write-Host "==> current strict recovery evidence: $recoveryRelative"
  }
  Remove-Item Env:CHROMIX_WINDOWS_VERIFY_SOURCE_REPO, Env:CHROMIX_WINDOWS_VERIFY_SOURCE_SHA
  Write-OutVar snapshot_safe true
}

if ($env:CHROMIX_WINDOWS_MIGRATION_REPO -or $env:CHROMIX_WINDOWS_MIGRATION_SHA) {
  if (-not $FromArtifact -or $Arch -ne "x64" -or $RequireUpstreamCache -or
      -not $env:CHROMIX_WINDOWS_MIGRATION_REPO -or -not $env:CHROMIX_WINDOWS_MIGRATION_SHA) {
    throw "explicit Windows migration requires a verified x64 cold snapshot and both source identity inputs"
  }
  Write-OutVar snapshot_safe false
  $hostGit = (Get-Command git.exe -ErrorAction Stop).Source
  $gitDirectory = Split-Path $hostGit
  $patchCandidates = @($gitDirectory, (Split-Path $gitDirectory), (Split-Path (Split-Path $gitDirectory))) |
    ForEach-Object { Join-Path $_ "usr\bin\patch.exe" } |
    Select-Object -Unique | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }
  if (-not $patchCandidates) { throw "explicit Windows migration requires host Git for Windows GNU patch" }
  $patchCandidates = @($patchCandidates)
  $probeArgs = @(
    (Join-Path $Repo "tools\apply_restored_patches.py"), "--select-patch-bin", "--src", $Src, "--repo", $Repo,
    "--core", (Join-Path $WorkDir "tooling\ungoogled-chromium"),
    "--platform-tooling", (Join-Path $WorkDir "tooling\ungoogled-chromium-windows"),
    "--platform", "windows", "--patch-bin", $patchCandidates[0]
  )
  foreach ($candidate in $patchCandidates | Select-Object -Skip 1) { $probeArgs += @("--patch-candidate", $candidate) }
  $selectedPatch = @(& python @probeArgs)
  if ($LASTEXITCODE -ne 0 -or $selectedPatch.Count -ne 1 -or [string]::IsNullOrWhiteSpace($selectedPatch[0])) {
    throw "explicit Windows migration host patch capability probe failed"
  }
  $hostPatch = $selectedPatch[0]
  $migrationDiagnostics = Join-Path $WorkDir "fingerprint-diagnostics"
  New-Item -ItemType Directory -Force -Path $migrationDiagnostics | Out-Null
  $migrationReport = Join-Path $migrationDiagnostics ("windows-source-migration-" + [Guid]::NewGuid().ToString('N') + ".json")
  & python -X utf8 (Join-Path $Repo "tools\migrate_windows_snapshot.py") --workdir $WorkDir `
    --previous-repo $env:CHROMIX_WINDOWS_MIGRATION_REPO --repo $Repo `
    --expected-previous-sha $env:CHROMIX_WINDOWS_MIGRATION_SHA --patch-bin $hostPatch --report $migrationReport
  if ($LASTEXITCODE -ne 0) { throw "verified Windows source migration failed; restore a clean donor snapshot" }
  Write-OutVar snapshot_safe true
}

# Cache restores establish the marker only after receipt verification.
$InitializeTarget = $Arch -eq "arm64" -and ((Test-Path $Src) -or
  (-not $RequireUpstreamCache -and $env:CHROMIX_PREFER_UPSTREAM_CACHE -ne "1"))
& "$PSScriptRoot\assert-target-arch.ps1" -WorkDir $WorkDir -Arch $Arch -Initialize:$InitializeTarget `
  -RequireMarker:($FromArtifact -and $Arch -eq "arm64")

$domainProgress = Join-Path $Src ".chromix-domain-substitution-in-progress"
$domainMarker = Join-Path $Src ".chromix-domain-substituted"
$restoreReceipt = Join-Path $Src ".chromix-upstream-restored.json"
if (Get-ChildItem -LiteralPath $WorkDir -Directory -Filter ".chromix-upstream-restore-*" -ErrorAction SilentlyContinue) {
  throw "upstream restore transaction was interrupted; use a clean work directory"
}
if (Test-Path $restoreReceipt) {
  Write-Host "==> verifying restored upstream source receipt and pins"
  & python (Join-Path $Repo "tools\restore_upstream_cache.py") --phase verify `
    --platform windows --arch $Arch --workdir $WorkDir
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

$VerifyRestoredSource = $false
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
    throw "restored tree targets Chromium $restoredVersion, expected $($Revisions.ChromiumVersion); use a new work directory for a cold build instead of a cross-version snapshot"
  } elseif (Test-Path $readyMarker) {
    $VerifyRestoredSource = -not $RestoredUpstream
  } else {
    Write-Host "==> restored source is not ready; completing patch preparation before verification"
  }
}

if ($StageIndex -eq 1 -and -not $FromArtifact -and
    -not (Test-Path $Src) -and
    ($RequireUpstreamCache -or $env:CHROMIX_PREFER_UPSTREAM_CACHE -eq "1")) {
  # Leave the stage reserve and at least 30 minutes for restore/preparation.
  $fetchTimeoutSec = [Math]::Min(10800, ((Get-RemainingMin) - $PackReserveMin - 30) * 60)
  if ($fetchTimeoutSec -lt 60) {
    throw "required upstream cache: insufficient stage budget for restore"
  }
  $fetchArgs = @(
    (Join-Path $Repo "tools\fetch_upstream_cache.py"),
    "--platform", "windows", "--arch", $Arch, "--destination", $UpstreamCacheDir
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
  $ExpiredOptionalCache = -not $RequireUpstreamCache -and
    $fetchResult.status -eq "miss" -and $fetchResult.reason -eq "artifact_expired" -and
    $fetchResult.phase -eq "metadata" -and -not (Test-Path $Src)
  if ($ExpiredOptionalCache) {
    # Expiry is checked AFTER pinned run/artifact provenance. Never downgrade a
    # digest, archive, source-pin, interrupted restore, or download failure.
    # Fetch extraction lives outside WorkDir; no cached source/output is reused.
    Write-Host "==> pinned upstream artifact expired; starting pinned cold-source preparation (out/Chromix)"
  } elseif ($fetchResult.status -ne "hit") {
    throw ("required upstream cache fetch failed: $($fetchResult.reason); " +
           "phase=$($fetchResult.phase); duration_seconds=$($fetchResult.duration_seconds)")
  } else {
    python (Join-Path $Repo "tools\restore_upstream_cache.py") --phase restore `
      --platform windows --arch $Arch --workdir $WorkDir --cache-dir $UpstreamCacheDir
    if ($LASTEXITCODE -ne 0) { throw "upstream restore helper failed (exit $LASTEXITCODE)" }
    if (-not (Test-Path -LiteralPath $restoreReceipt -PathType Leaf)) {
      throw "required upstream cache: restore receipt missing after restore; refusing cold preparation or compilation"
    }
    & python (Join-Path $Repo "tools\restore_upstream_cache.py") --phase verify `
      --platform windows --arch $Arch --workdir $WorkDir
    if ($LASTEXITCODE -ne 0) { throw "restored upstream source verification failed (exit $LASTEXITCODE)" }
    if ($Arch -eq "arm64") {
      & "$PSScriptRoot\assert-target-arch.ps1" -WorkDir $WorkDir -Arch $Arch -Initialize
    }
    $RestoredUpstream = $true
    $OutDir = "$Src\out\Default"
    Write-Host "==> restored upstream source/out/Default; appending Chromix patches before incremental Ninja"
  }
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
  $prepareOptions = @{}
  if ($Arch) { $prepareOptions.Arch = $Arch }
  & "$PSScriptRoot\prepare-ungoogled.ps1" -Root $WorkDir -Repo $Repo `
    -DeadlineEpoch $prepareDeadline -ReserveMinutes $PackReserveMin @prepareOptions
} catch {
  if ($_.Exception.Message -like "PREPARE_BUDGET_EXHAUSTED:*") {
    if ($ValidateOnly) { throw }
    Save-Handoff -Mode Unsynced
    return
  }
  throw
}
$UngoogledTooling = Join-Path $WorkDir "tooling\ungoogled-chromium"
$WindowsTooling = Join-Path $WorkDir "tooling\ungoogled-chromium-windows"
if ($VerifyRestoredSource) {
  # Ready snapshots must match the current stack without legacy source rewrites.
  $resumeDiagnostics = Join-Path $WorkDir "fingerprint-diagnostics"
  New-Item -ItemType Directory -Force -Path $resumeDiagnostics | Out-Null
  $resumeSourceReport = Join-Path $resumeDiagnostics ("resume-source-" + [Guid]::NewGuid().ToString('N') + ".json")
  & python -X utf8 (Join-Path $Repo "tools\verify_patch_stack.py") --src $Src --repo $Repo `
    --core $UngoogledTooling --platform-tooling $WindowsTooling --platform windows --output $resumeSourceReport
  if ($LASTEXITCODE -ne 0) {
    throw "restored source does not contain the current fingerprint patch stack; refusing legacy rewrites; restore a clean matching source"
  }
  Write-Host "==> restored source verified; preserving current source without legacy migration"
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$gnArgs = Join-Path $OutDir "args.gn"
$mergeArgs = @((Join-Path $Repo "tools\merge_gn_args.py"), $gnArgs)
if ($BuildProfile -in @("fast", "release")) { $mergeArgs += @("--build-profile", $BuildProfile) }
if ($RestoredUpstream) {
  $mergeArgs += @("--preserve-pgo-from", $gnArgs)
}
if ($RestoredUpstream) { $mergeArgs += $gnArgs }
$mergeArgs += @(
  (Join-Path $UngoogledTooling "flags.gn"),
  (Join-Path $WindowsTooling "flags.windows.gn"),
  (Join-Path $Repo "build\args.windows.gn")
)
if ($Arch -eq "arm64") { $mergeArgs += (Join-Path $Repo "build\args.windows.arm64.gn") }
python @mergeArgs
if ($LASTEXITCODE -ne 0) { throw "GN argument merge failed" }
if ($Arch -eq "arm64") {
  & "$PSScriptRoot\assert-target-arch.ps1" -WorkDir $WorkDir -Arch $Arch
}

$env:PATH = "$(Join-Path $Src 'third_party\ninja');$(Join-Path $Src 'third_party\node\win');$env:PATH"
$Ninja = Join-Path $Src "third_party\ninja\ninja.exe"
if ($RestoredUpstream) {
  $Ninja = & python (Join-Path $Repo "tools\restore_ninja.py") --workdir $WorkDir --platform windows --arch $Arch
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
      --platform windows --arch $Arch --workdir $WorkDir
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
  # revised patches. Verify actual hunks after preparation/substitution.
  $fingerprintDiagnostics = Join-Path $WorkDir "fingerprint-diagnostics"
  New-Item -ItemType Directory -Force -Path $fingerprintDiagnostics | Out-Null
  $FingerprintSourceReport = if ($RecoveryDiagnostics) { Join-Path $RecoveryDiagnostics 'source-verification.json' } else {
    Join-Path $fingerprintDiagnostics ("source-" + [Guid]::NewGuid().ToString('N') + ".json")
  }
  if (Test-Path -LiteralPath $FingerprintSourceReport) { throw 'Current source verification report already exists' }
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
    --workdir $WorkDir --platform windows --arch $Arch --ninja $Ninja --target chrome
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
      --workdir $WorkDir --platform windows --arch $Arch --ninja $Ninja --target chrome --exit-code $rc
    if ($LASTEXITCODE -ne 0) { throw "restored reuse evidence collection failed after Ninja (exit $LASTEXITCODE)" }
  } catch {
    if ($rc -ne 0) { throw "ninja failed (exit $rc); $($_.Exception.Message)" }
    throw
  }
}

if ($rc -eq 0) {
  New-Item -ItemType Directory -Force -Path "$Root\dist" | Out-Null
  $packageOptions = @{}
  if ($Arch) { $packageOptions.Arch = $Arch }
  & "$PSScriptRoot\package-win.ps1" -Out $OutDir -Dest "$Root\dist" @packageOptions
  Verify-FinalBundle
  if ($Arch -eq "arm64") {
    if (-not (Test-Path -LiteralPath $FingerprintSourceReport -PathType Leaf)) {
      throw "Windows ARM64 source verification receipt is missing"
    }
    Copy-Item -LiteralPath $FingerprintSourceReport -Destination "$Root\dist\source-verification.json" -Force
  }
  Write-OutVar finished true
  return
}
if ($rc -eq 124) {
  Save-Handoff -Mode Synced
  return
}
throw "ninja failed (exit $rc)"
