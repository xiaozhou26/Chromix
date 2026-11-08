"""Windows SDK selection, check-only guards and offline installer failure modes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / 'build/windows/ensure-windows-sdk.ps1'
SDK_VERSION = '10.0.28000.0'
INSTALLERS = {
    '156.0.8078.79': ('10.0.28000.0', '38ab8f6d-3676-4860-ae84-3361308b9d7f',
                     'b534d37a1f3140c8be1298ed88855b8cbddc35d2626c4d0afd39f186a4b578e3'),
    '155.0.8059.39': ('10.0.28000.0', '38ab8f6d-3676-4860-ae84-3361308b9d7f',
                     'b534d37a1f3140c8be1298ed88855b8cbddc35d2626c4d0afd39f186a4b578e3'),
    '154.0.8037.57': ('10.0.28000.0', '38ab8f6d-3676-4860-ae84-3361308b9d7f',
                     'b534d37a1f3140c8be1298ed88855b8cbddc35d2626c4d0afd39f186a4b578e3'),
    '153.0.8010.36': ('10.0.28000.0', '38ab8f6d-3676-4860-ae84-3361308b9d7f',
                     'b534d37a1f3140c8be1298ed88855b8cbddc35d2626c4d0afd39f186a4b578e3'),
    '152.0.7977.82': ('10.0.26100.0', 'f4b30f2a-4fc3-430e-9b03-c842b5f5f9f1',
                     '6fa0fa27db77a909f5ecb35183cb26a969a6775936780936fe239e4f9c66b458'),
}
PWSH = shutil.which('pwsh') or ('/opt/pwsh/pwsh' if Path('/opt/pwsh/pwsh').is_file() else None)


def required_files(arch, version=SDK_VERSION):
    paths = [
        f'Include/{version}/um/Windows.h',
        f'Include/{version}/um/winnt.h',
        f'Include/{version}/shared/sdkddkver.h',
        f'Include/{version}/ucrt/stdio.h',
        f'Include/{version}/winrt/windows.foundation.h',
        f'Include/{version}/cppwinrt/winrt/base.h',
        f'bin/{version}/x64/rc.exe',
        f'bin/{version}/x64/midl.exe',
        'Debuggers/x64/dbghelp.dll',
    ]
    if arch == 'arm64':
        paths.append('Debuggers/arm64/dbghelp.dll')
    for cpu in ('x86', 'x64', 'arm64') if arch == 'arm64' else ('x86', 'x64'):
        for component, names in (('um', ('kernel32.lib', 'user32.lib')),
                                 ('ucrt', ('ucrt.lib', 'libucrt.lib'))):
            paths.extend(f'Lib/{version}/{component}/{cpu}/{name}' for name in names)
    return paths


def sdk_fixture(root, arch='arm64', version=SDK_VERSION):
    for relative in required_files(arch, version):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#define NTDDI_WIN11_BR 0x0A000010\n'
                        if path.name == 'sdkddkver.h' and version == SDK_VERSION else 'fixture')


MOCKS = r'''
function Record-Call($Name, $Data) {
  @{ name = $Name; data = $Data } | ConvertTo-Json -Compress -Depth 5 | Add-Content -LiteralPath $env:CALLS
}
function curl.exe {
  Record-Call 'curl' @($args)
  $outputIndex = [Array]::IndexOf($args, '--output')
  if ($outputIndex -lt 0) { throw 'missing --output' }
  $path = $args[$outputIndex + 1]
  if ($env:DOWNLOAD_MODE -ne 'missing') {
    $size = 1MB
    if ($env:DOWNLOAD_MODE -eq 'empty') { $size = 0 }
    if ($env:DOWNLOAD_MODE -eq 'small') { $size = 1024 }
    if ($env:DOWNLOAD_MODE -eq 'large') { $size = 16MB + 1 }
    $file = [IO.File]::Create($path)
    try { $file.SetLength($size) } finally { $file.Dispose() }
  }
  $global:LASTEXITCODE = [int]$env:CURL_EXIT
}
function Get-AuthenticodeSignature {
  param($LiteralPath)
  Record-Call 'signature' $LiteralPath
  if ($env:SIGNATURE_STATUS -eq 'Throw') { throw 'signature validation unavailable' }
  $certificate = [pscustomobject]@{ Subject = $env:SIGNER_SUBJECT }
  if ($env:SIGNER_SUBJECT -eq 'NONE') { $certificate = $null }
  [pscustomobject]@{ Status = $env:SIGNATURE_STATUS; SignerCertificate = $certificate }
}
function Get-FileHash {
  param($LiteralPath, $Algorithm)
  Record-Call 'hash' @{ path = $LiteralPath; algorithm = $Algorithm }
  [pscustomobject]@{ Hash = $env:INSTALLER_HASH }
}
function New-MockProcess($Kind, $ProcessId, $ExitCode) {
  $process = [pscustomobject]@{ Kind = $Kind; Id = $ProcessId; Handle = 1; Result = $ExitCode; Completed = $false }
  $process | Add-Member ScriptMethod WaitForExit {
    param($Milliseconds)
    Record-Call 'wait' @{ kind = $this.Kind; milliseconds = $Milliseconds }
    $this.Completed = if ($this.Kind -eq 'setup') { $env:SETUP_COMPLETES -eq 'yes' }
      else { $env:TERMINATION_MODE -ne 'timeout' }
    return $this.Completed
  }
  $process | Add-Member ScriptProperty ExitCode {
    if (-not $this.Completed) { throw 'exit code read before process completion' }
    return $this.Result
  }
  $process | Add-Member ScriptMethod Kill { Record-Call 'kill' $this.Kind }
  $process | Add-Member ScriptMethod Dispose { Record-Call 'dispose' $this.Kind }
  return $process
}
function Start-Process {
  param($FilePath, $ArgumentList, [switch]$Wait, [switch]$PassThru)
  $kind = if ($FilePath -like '*taskkill.exe') { 'taskkill' } else { 'setup' }
  Record-Call $kind @{ path = $FilePath; arguments = @($ArgumentList); wait = $Wait.IsPresent; passthru = $PassThru.IsPresent }
  if ($kind -eq 'taskkill') {
    if ($env:TERMINATION_MODE -eq 'Throw') { throw 'taskkill launch denied' }
    return New-MockProcess 'taskkill' 24681 ([int]$env:TASKKILL_EXIT)
  }
  if ($env:SETUP_EXIT -eq 'Throw') { throw 'process launch failed' }
  if ($env:INSTALL_FILES -eq 'yes') {
    New-Item -ItemType Directory -Path $env:SDK -Force | Out-Null
    Get-ChildItem -LiteralPath $env:TEMPLATE | Copy-Item -Destination $env:SDK -Recurse -Force
    if ($env:POST_INSTALL_BAD) { [IO.File]::WriteAllText((Join-Path $env:SDK $env:POST_INSTALL_BAD), '') }
  }
  $exitCode = if ($env:SETUP_EXIT -eq 'null') { $null } else { [int]$env:SETUP_EXIT }
  return New-MockProcess 'setup' 24680 $exitCode
}
'''


class WindowsSdkSourceTest(unittest.TestCase):
    def test_ci_provisions_before_assertions_and_source_preparation(self):
        stage = (REPO / 'build/windows/ci-stage.ps1').read_text()
        start = stage.index('function Install-WindowsSdk {')
        provision = stage[start:stage.index('\nfunction ', start + 1)]
        self.assertIn('ensure-windows-sdk.ps1" -Arch $Arch', provision)
        self.assertIn('-ChromiumVersion $Revisions.ChromiumVersion -DownloadDir $Root -Install', provision)
        execution = stage[stage.index('\nAssert-CiScripts\n'):]
        self.assertLess(execution.index('Install-WindowsSdk'), execution.index('assert-arm64-toolchain.ps1'))
        self.assertLess(execution.index('Install-WindowsSdk'), execution.index('prepare-ungoogled.ps1'))
        self.assertIn('"$PSScriptRoot\\ensure-windows-sdk.ps1",', stage)
        self.assertNotIn('Install-Debuggers', stage)
        self.assertNotIn('Mount-DiskImage', stage)

    def test_local_build_and_arm64_guard_never_install(self):
        build = (REPO / 'build/windows/build.ps1').read_text()
        guard = (REPO / 'build/windows/assert-arm64-toolchain.ps1').read_text()
        for source in (build, guard):
            self.assertIn('ensure-windows-sdk.ps1', source)
            self.assertIn('-ChromiumVersion', source)
            self.assertNotRegex(source, r'-Install\b')
        self.assertIn('-Arch arm64 -ChromiumVersion $ChromiumVersion', guard)
        for forbidden in ('Start-Process', 'curl.exe', 'Remove-Item', '26100', '28000'):
            self.assertNotIn(forbidden, guard)

    def test_official_installers_and_ps51_compatible_bounded_install(self):
        source = HELPER.read_text()
        for version, guid, digest in INSTALLERS.values():
            self.assertIn(version, source)
            self.assertIn(f'https://download.microsoft.com/download/{guid}/', source)
            self.assertIn(digest, source)
        for marker in ('#requires -Version 5.1', 'Import-PowerShellDataFile',
                       '$setup.WaitForExit(1800000)', '$terminator.WaitForExit(10000)',
                       '@("/PID", "$($setup.Id)", "/T", "/F")',
                       '"/features", "+", "/quiet", "/norestart"'):
            self.assertIn(marker, source)
        for forbidden in ('-Wait', 'WaitForExit()', '/IM', '/repair', '/uninstall',
                          'Set-ItemProperty', 'Set-Content', 'Add-Content', '-AsHashtable', '??', '&&'):
            self.assertNotIn(forbidden, source)
        self.assertEqual(source.count('Remove-Item'), 1)
        self.assertIn('Remove-Item -LiteralPath $staging -Recurse -Force', source)

    def test_156_uses_reviewed_28000_mapping_without_admitting_157(self):
        source = HELPER.read_text()
        mapping = source.split('{ $_ -in @(153, 154, 155, 156) } {', 1)[1].split('\n  }', 1)[0]
        self.assertIn('$SdkVersion = "10.0.28000.0"', mapping)
        self.assertIn(INSTALLERS['156.0.8078.79'][2], mapping)
        self.assertIn('default { throw "No Windows SDK mapping', source)

    def test_prerequisites_document_current_sdk(self):
        for name in ('BUILDING.md', 'README.md', 'readme_cn.md', 'build/windows/build.ps1'):
            self.assertIn(SDK_VERSION, (REPO / name).read_text(encoding='utf-8'))


@unittest.skipUnless(PWSH, 'PowerShell is unavailable')
class WindowsSdkBehaviorTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='windows sdk ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sdk = self.root / 'Windows Kits/10'
        self.template = self.root / 'complete sdk'
        self.download = self.root / 'download cache'
        self.download.mkdir()
        self.sentinel = self.download / 'keep-user-file'
        self.sentinel.write_text('keep')
        self.calls = self.root / 'calls.jsonl'
        sdk_fixture(self.template)

    def run_ps(self, code, **environment):
        return subprocess.run(
            [PWSH, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command',
             '$ErrorActionPreference = "Stop"\n$ErrorView = "NormalView"\n' + MOCKS + code],
            env={**os.environ, 'HELPER': str(HELPER), 'SDK': str(self.sdk),
                 'WINDOWSSDKDIR': str(self.sdk), 'DOWNLOAD': str(self.download),
                 'TEMPLATE': str(self.template), 'CALLS': str(self.calls),
                 'ARCH': 'arm64', 'CHROMIUM': '', 'DOWNLOAD_MODE': 'ok', 'CURL_EXIT': '0',
                 'SIGNATURE_STATUS': 'Valid',
                 'SIGNER_SUBJECT': 'CN=Microsoft Corporation, O=Microsoft Corporation, C=US',
                 'INSTALLER_HASH': INSTALLERS['153.0.8010.36'][2].upper(), 'SETUP_EXIT': '0',
                 'SystemRoot': str(self.root / 'Windows'), 'SETUP_COMPLETES': 'yes',
                 'TERMINATION_MODE': 'ok', 'TASKKILL_EXIT': '0',
                 'INSTALL_FILES': 'yes', 'POST_INSTALL_BAD': '', **environment},
            capture_output=True, text=True, timeout=60)

    def invoke(self, install=False, **environment):
        return self.run_ps(r'''
& $env:HELPER -Arch $env:ARCH -ChromiumVersion $env:CHROMIUM -DownloadDir $env:DOWNLOAD ''' + ('-Install' if install else '') + r'''
Write-Output "SDK_ROOT=$env:WINDOWSSDKDIR"
Write-Output 'CONTINUED'
''', **environment)

    def events(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text(encoding='utf-8-sig').splitlines()]

    def assert_clean_downloads(self):
        self.assertEqual(list(self.download.iterdir()), [self.sentinel])
        self.assertEqual(self.sentinel.read_text(), 'keep')

    def assert_failure(self, result, message):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, result.stderr)
        self.assertNotIn('CONTINUED', result.stdout)
        self.assertNotIn('SDK_ROOT=', result.stdout)
        self.assert_clean_downloads()

    def test_missing_and_directory_only_sdk_checks_do_not_download_or_install(self):
        self.assert_failure(self.invoke(), 'prerequisites are missing or empty')
        self.assertFalse(self.sdk.exists())
        for path in required_files('arm64'):
            (self.sdk / path).mkdir(parents=True)
        self.assert_failure(self.invoke(), 'prerequisites are missing or empty')
        self.assertEqual(self.events(), [])

    def test_complete_sdk_is_noop_even_with_install_and_x64_needs_no_arm64(self):
        for arch in ('x64', 'arm64'):
            sdk_fixture(self.sdk, arch)
            if arch == 'x64':
                self.assertFalse((self.sdk / 'Debuggers/arm64').exists())
            before = {str(p): p.read_bytes() for p in self.sdk.rglob('*') if p.is_file()}
            for install in (False, True):
                with self.subTest(arch=arch, install=install):
                    result = self.invoke(install, ARCH=arch)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f'SDK_ROOT={self.sdk}', result.stdout)
                    self.assertEqual(self.events(), [])
                    self.assert_clean_downloads()
            self.assertEqual(before, {str(p): p.read_bytes() for p in self.sdk.rglob('*') if p.is_file()})

    def test_program_files_fallback_and_default_arch(self):
        programs = self.root / 'Program Files (x86)'
        sdk = programs / 'Windows Kits/10'
        sdk_fixture(sdk, 'x64')
        result = self.run_ps(r'''
Set-Item -LiteralPath 'Env:ProgramFiles(x86)' -Value $env:PROGRAMS
& $env:HELPER
Write-Output "SDK_ROOT=$env:WINDOWSSDKDIR"
''', WINDOWSSDKDIR='', PROGRAMS=str(programs))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'SDK_ROOT={sdk}', result.stdout)
        self.assertIn('verified for x64', result.stdout)
        self.assertEqual(self.events(), [])

    def test_legacy_152_uses_26100_but_never_satisfies_153(self):
        sdk_fixture(self.sdk, version='10.0.26100.0')
        before = {str(p): p.read_bytes() for p in self.sdk.rglob('*') if p.is_file()}
        for install in (False, True):
            result = self.invoke(install, CHROMIUM='152.0.7977.82')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('10.0.26100.0 verified', result.stdout)
        self.assert_failure(self.invoke(CHROMIUM='153.0.8010.36'), '10.0.28000.0 prerequisites')
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.sdk.rglob('*') if p.is_file()})
        self.assertEqual(self.events(), [])

    def test_default_version_comes_from_checked_out_windows_pins(self):
        repo = self.root / 'legacy checkout'
        helper = repo / 'build/windows/ensure-windows-sdk.ps1'
        helper.parent.mkdir(parents=True)
        helper.write_text(HELPER.read_text())
        (repo / 'build/ungoogled-revisions.psd1').write_text(
            '@{ ChromiumVersion = "152.0.7977.82"; MacOSChromiumVersion = "153.0.8010.36" }')
        sdk_fixture(self.sdk, version='10.0.26100.0')
        result = self.invoke(True, HELPER=str(helper))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('10.0.26100.0 verified', result.stdout)
        self.assertEqual(self.events(), [])

    def test_ci_wrapper_passes_arch_version_and_explicit_install(self):
        source = (REPO / 'build/windows/ci-stage.ps1').read_text()
        start = source.index('function Install-WindowsSdk {')
        function = source[start:source.index('\nfunction ', start + 1)]
        wrapper = self.root / 'ci wrapper/ci-stage.ps1'
        wrapper.parent.mkdir()
        shutil.copyfile(HELPER, wrapper.parent / HELPER.name)
        wrapper.write_text('$Arch = $env:ARCH\n$Root = $env:DOWNLOAD\n'
                           '$Revisions = @{ ChromiumVersion = $env:CHROMIUM }\n'
                           + function + '\nInstall-WindowsSdk\n')
        for arch, chromium in (('x64', '156.0.8078.79'), ('arm64', '156.0.8078.79'),
                               ('x64', '155.0.8059.39'), ('x64', '154.0.8037.57'), ('arm64', '154.0.8037.57'),
                               ('x64', '153.0.8010.36'), ('arm64', '152.0.7977.82')):
            with self.subTest(arch=arch, chromium=chromium):
                version, _, digest = INSTALLERS[chromium]
                shutil.rmtree(self.template)
                (self.sdk / f'Include/{version}/um/Windows.h').unlink(missing_ok=True)
                sdk_fixture(self.template, arch, version)
                result = self.run_ps('& $env:WRAPPER', WRAPPER=str(wrapper),
                                     ARCH=arch, CHROMIUM=chromium, INSTALLER_HASH=digest)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'{version} verified for {arch}', result.stdout)
                self.assertIn('setup', [event['name'] for event in self.events()])
                self.assert_clean_downloads()
                self.calls.unlink()

    def test_unknown_version_and_invalid_arch_fail_before_download(self):
        for version in ('157.0.1.0', '151.0.1.0', '153', 'invalid'):
            self.assert_failure(self.invoke(True, CHROMIUM=version), 'Chromium')
        self.assert_failure(self.invoke(True, ARCH='x86'), 'ValidateSet')
        self.assertEqual(self.events(), [])

    def test_newer_sdk_directory_is_not_an_implicit_fallback(self):
        sdk_fixture(self.sdk, version='10.0.29000.0')
        self.assert_failure(self.invoke(), '10.0.28000.0 prerequisites')
        self.assertEqual(self.events(), [])

    def test_every_required_file_rejects_missing_and_empty(self):
        sdk_fixture(self.sdk)
        for arch in ('x64', 'arm64'):
            result = self.run_ps(r'''
foreach ($relative in ($env:REQUIRED_FILES | ConvertFrom-Json)) {
  $path = Join-Path $env:SDK $relative
  $contents = [IO.File]::ReadAllBytes($path)
  foreach ($mode in @('empty', 'missing')) {
    if ($mode -eq 'empty') { [IO.File]::WriteAllBytes($path, [byte[]]@()) }
    else { Remove-Item -LiteralPath $path }
    $failed = $false
    try { & $env:HELPER -Arch $env:ARCH }
    catch {
      $failed = $true
      if (-not $_.Exception.Message.Contains($path)) { throw }
    }
    finally { [IO.File]::WriteAllBytes($path, $contents) }
    if (-not $failed) { throw "Accepted $mode prerequisite: $path" }
  }
}
''', ARCH=arch, REQUIRED_FILES=json.dumps(required_files(arch)))
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), [])

    def test_version_header_requires_br_definition_not_comment(self):
        sdk_fixture(self.sdk)
        header = self.sdk / f'Include/{SDK_VERSION}/shared/sdkddkver.h'
        for contents in ('#define NTDDI_WIN11_GE 1', '// #define NTDDI_WIN11_BR 1'):
            header.write_text(contents)
            self.assert_failure(self.invoke(), 'NTDDI_WIN11_BR is required')
            self.assertEqual(header.read_text(), contents)
        self.assertEqual(self.events(), [])

    def test_x64_sdk_does_not_satisfy_arm64(self):
        sdk_fixture(self.sdk, 'x64')
        self.assert_failure(self.invoke(), 'arm64')
        self.assertEqual(self.events(), [])

    def test_installs_both_pinned_sdks_revalidates_and_accepts_reboot_code(self):
        old = self.sdk / 'Include/10.0.22621.0/um/Windows.h'
        old.parent.mkdir(parents=True)
        old.write_text('existing user SDK')
        for chromium, (version, guid, digest) in INSTALLERS.items():
            shutil.rmtree(self.template)
            (self.sdk / f'Include/{version}/um/Windows.h').unlink(missing_ok=True)
            sdk_fixture(self.template, version=version)
            exit_code = '3010' if chromium.startswith('152.') else '0'
            result = self.invoke(True, CHROMIUM=chromium, INSTALLER_HASH=digest, SETUP_EXIT=exit_code)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = self.events()
            self.assertEqual([e['name'] for e in events], ['curl', 'signature', 'hash', 'setup', 'wait', 'dispose'])
            args = events[0]['data']
            self.assertEqual(args[0], '--disable')
            self.assertIn(f'https://download.microsoft.com/download/{guid}/', args[-1])
            for flag, value in (('--proto', '=https'), ('--proto-redir', '=https'),
                                ('--connect-timeout', '20'), ('--max-time', '180'),
                                ('--retry', '2'), ('--retry-max-time', '180'),
                                ('--max-filesize', str(16 * 1024 * 1024)), ('--max-redirs', '5')):
                self.assertEqual(str(args[args.index(flag) + 1]), value)
            self.assertIn('--fail', args)
            setup = events[3]['data']
            self.assertFalse(setup['wait'])
            self.assertTrue(setup['passthru'])
            self.assertEqual(setup['arguments'], ['/features', '+', '/quiet', '/norestart',
                                                 '/installpath', f'"{self.sdk}"'])
            self.assertEqual(events[4]['data'], {'kind': 'setup', 'milliseconds': 1800000})
            self.assertFalse(Path(setup['path']).exists())
            self.assertEqual(old.read_text(), 'existing user SDK')
            self.assert_clean_downloads()
            if exit_code == '3010':
                self.assertIn('requested a reboot', result.stdout)
            self.calls.unlink()

    def test_default_download_directory_is_private_and_cleaned(self):
        result = self.run_ps('& $env:HELPER -Arch x64 -Install')
        self.assertEqual(result.returncode, 0, result.stderr)
        setup = next(event['data'] for event in self.events() if event['name'] == 'setup')
        self.assertTrue(Path(setup['path']).parent.name.startswith('chromix-windows-sdk-'))
        self.assertFalse(Path(setup['path']).parent.exists())

    def test_download_failure_and_invalid_sizes_never_verify_or_execute(self):
        cases = [({'CURL_EXIT': '28'}, 'curl exit 28'),
                 ({'DOWNLOAD_MODE': 'missing'}, 'produced no file'),
                 *[({'DOWNLOAD_MODE': mode}, 'invalid size') for mode in ('empty', 'small', 'large')]]
        for environment, message in cases:
            self.assert_failure(self.invoke(True, **environment), message)
            self.assertEqual([event['name'] for event in self.events()], ['curl'])
            self.calls.unlink()

    def test_untrusted_or_non_microsoft_signatures_never_execute(self):
        cases = [{'SIGNATURE_STATUS': status} for status in ('NotSigned', 'HashMismatch', 'UnknownError')]
        cases += [{'SIGNER_SUBJECT': subject} for subject in (
            'NONE', 'CN=Other Vendor, O=Other Vendor',
            'CN=Microsoft Corporation Malware, O=Microsoft Corporation',
            'CN=Microsoft Corporation, O=Other Vendor',
            'CN=Other Vendor, O=Microsoft Corporation')]
        for environment in cases:
            self.assert_failure(self.invoke(True, **environment), 'Authenticode signature')
            self.assertEqual([event['name'] for event in self.events()], ['curl', 'signature'])
            self.calls.unlink()

    def test_hash_mismatch_never_executes_even_with_valid_signature(self):
        self.assert_failure(self.invoke(True, INSTALLER_HASH='0' * 64), 'SHA256 mismatch')
        self.assertEqual([event['name'] for event in self.events()], ['curl', 'signature', 'hash'])

    def test_signature_and_process_exceptions_clean_private_download(self):
        for environment, message in (({'SIGNATURE_STATUS': 'Throw'}, 'signature validation unavailable'),
                                     ({'SETUP_EXIT': 'Throw'}, 'process launch failed')):
            self.assert_failure(self.invoke(True, **environment), message)

    def test_failed_or_unknown_installer_exit_rejected_even_if_files_appear(self):
        for exit_code in ('1603', '1641', '1', 'null'):
            self.assert_failure(self.invoke(True, SETUP_EXIT=exit_code), 'installation failed')
            self.assertEqual([event['name'] for event in self.events()],
                             ['curl', 'signature', 'hash', 'setup', 'wait', 'dispose'])
            (self.sdk / f'Include/{SDK_VERSION}/um/Windows.h').unlink()
            self.calls.unlink()

    def test_timeout_and_termination_failure_cannot_become_success(self):
        for environment in ({}, {'TASKKILL_EXIT': '5'}, {'TERMINATION_MODE': 'Throw'},
                            {'TERMINATION_MODE': 'timeout'}):
            result = self.invoke(True, SETUP_COMPLETES='no', **environment)
            self.assert_failure(result, 'timed out after 30 minutes')
            self.assertIn('SDK is not ready', result.stderr)
            events = self.events()
            taskkill = [event for event in events if event['name'] == 'taskkill']
            self.assertEqual(len(taskkill), 1)
            self.assertEqual(taskkill[0]['data']['arguments'], ['/PID', '24680', '/T', '/F'])
            self.assertFalse(taskkill[0]['data']['wait'])
            self.assertEqual(events[-1], {'name': 'dispose', 'data': 'setup'})
            if environment:
                self.assertIn('Unable to confirm termination', result.stdout)
            (self.sdk / f'Include/{SDK_VERSION}/um/Windows.h').unlink()
            self.calls.unlink()

    def test_success_exit_with_incomplete_files_still_fails(self):
        for exit_code in ('0', '3010'):
            self.assert_failure(self.invoke(True, SETUP_EXIT=exit_code, INSTALL_FILES='no'),
                                'verification failed after installation')
        result = self.invoke(True, POST_INSTALL_BAD=f'bin/{SDK_VERSION}/x64/midl.exe')
        self.assert_failure(result, 'verification failed after installation')
        self.assertIn('midl.exe', result.stderr)


if __name__ == '__main__':
    unittest.main()
