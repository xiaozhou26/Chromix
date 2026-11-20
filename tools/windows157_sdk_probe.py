#!/usr/bin/env python3
"""Read native Windows157 SDK PE/COFF identities without executing SDK binaries."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys

try:
    from .verify_windows_bundle import check_pe, product_version, sha256
except ImportError:
    from verify_windows_bundle import check_pe, product_version, sha256

SDK_VERSION = '10.0.28000.0'
SDK_RELEASE = '10.0.28000.2705'
# Native run 44805916373 and its Store Apps Tools MSI File/Component records.
MIDL_IDENTITY = {
    'relative': f'bin/{SDK_VERSION}/x64/midl.exe',
    'product_version': '8.1.628.1',
    'sha256': '7e8ab31406275501c337b32cbca079da2954e181bb7a3606a9908d588088513c',
    'size': 154056,
    'machine': 0x8664,
    'msi_product': '{8EB65EF3-DCA2-FAD1-49DD-E522C112F1CF}',
    'msi_component': '{23C25388-38ED-72CC-55D9-3B1EF499FC2E}',
    'msi_version': '10.1.28000.2705',
    'bundle_sha256': 'b534d37a1f3140c8be1298ed88855b8cbddc35d2626c4d0afd39f186a4b578e3',
}
MACHINES = {'x86': 0x14C, 'x64': 0x8664, 'arm64': 0xAA64}


def check_library(path, arch):
    """Check every object/import member, excluding archive linker/name tables."""
    size = path.stat().st_size
    counts = Counter()
    flags = Counter()
    with path.open('rb') as stream:
        if stream.read(8) != b'!<arch>\n':
            raise ValueError(f'not a COFF library: {path}')
        while stream.tell() < size:
            header = stream.read(60)
            if len(header) != 60 or header[58:] != b'`\n':
                raise ValueError(f'invalid archive member header: {path}')
            name = header[:16].decode('ascii').strip()
            length = int(header[48:58].decode('ascii').strip())
            start = stream.tell()
            end = start + length
            if length < 0 or end + (length % 2) > size:
                raise ValueError(f'archive member outside file: {path}')
            if name not in ('/', '//'):
                data = stream.read(min(length, 56))
                if len(data) < 20:
                    raise ValueError(f'truncated COFF member: {path}: {name}')
                machine = struct.unpack_from('<H', data)[0]
                kind = 'coff_characteristics'
                if data[:4] == b'\0\0\xff\xff':
                    machine = struct.unpack_from('<H', data, 6)[0]
                    kind = 'import_type_info' if struct.unpack_from('<H', data, 4)[0] == 0 else None
                if kind:
                    flags[f'{kind}:0x{struct.unpack_from("<H", data, 18)[0]:04x}'] += 1
                if machine != MACHINES[arch]:
                    raise ValueError(f'wrong COFF machine: {path}: {name}: 0x{machine:04x}, expected {arch}')
                counts[f'0x{machine:04x}'] += 1
            stream.seek(end)
            if length % 2 and stream.read(1) != b'\n':
                raise ValueError(f'invalid archive padding: {path}')
    if not counts:
        raise ValueError(f'no COFF objects in library: {path}')
    return {'size': size, 'sha256': sha256(path), 'object_machines': dict(counts), 'member_flags': dict(flags)}


def midl_registration(path):
    import ctypes
    from ctypes import wintypes
    msi = ctypes.WinDLL('msi', use_last_error=True)
    msi.MsiGetComponentPathW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR,
                                        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    msi.MsiGetComponentPathW.restype = ctypes.c_int
    msi.MsiGetProductInfoW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR,
                                      wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    msi.MsiGetProductInfoW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = wintypes.DWORD(len(buffer))
    state = msi.MsiGetComponentPathW(MIDL_IDENTITY['msi_product'], MIDL_IDENTITY['msi_component'],
                                     buffer, ctypes.byref(length))
    registered = buffer.value
    length.value = len(buffer)
    code = msi.MsiGetProductInfoW(MIDL_IDENTITY['msi_product'], 'VersionString', buffer, ctypes.byref(length))
    if state != 3 or code != 0 or buffer.value != MIDL_IDENTITY['msi_version']:
        raise ValueError('MIDL MSI product/component is not locally installed at the pinned version')
    if os.path.normcase(registered) != os.path.normcase(str(path)):
        raise ValueError('MIDL MSI component path does not match the checked SDK file')
    return {'product': MIDL_IDENTITY['msi_product'], 'component': MIDL_IDENTITY['msi_component'],
            'product_version': buffer.value, 'component_path': registered, 'install_state': state}


def microsoft_signature(path):
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    command = ("$ErrorActionPreference='Stop'; $s=Get-AuthenticodeSignature -LiteralPath $env:CHROMIX_SDK_PROBE_FILE; "
               "@{status=[string]$s.Status; subject=$s.SignerCertificate.Subject; "
               "thumbprint=$s.SignerCertificate.Thumbprint} | ConvertTo-Json -Compress")
    result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', command],
                            env={**os.environ, 'CHROMIX_SDK_PROBE_FILE': str(path)},
                            capture_output=True, text=True, timeout=60, check=True)
    signature = json.loads(result.stdout.lstrip('\ufeff'))
    if (signature.get('status') != 'Valid' or not signature.get('thumbprint') or
            not all(re.search(r'(^|,\s*)' + field + r'=Microsoft Corporation(,|$)',
                              signature.get('subject') or '') for field in ('CN', 'O'))):
        raise ValueError(f'file must have a valid Microsoft Corporation signature: {path}')
    return signature


def check_midl_identity(sdk, path, record):
    if path != sdk / MIDL_IDENTITY['relative']:
        raise ValueError('MIDL identity exception is restricted to the pinned x64 SDK path')
    for key in ('product_version', 'sha256', 'size', 'machine'):
        if record.get(key) != MIDL_IDENTITY[key]:
            raise ValueError(f'MIDL {key} does not match SDK {SDK_RELEASE}')
    record['msi_registration'] = midl_registration(path)
    record['signature'] = microsoft_signature(path)
    record['sdk_bundle'] = SDK_RELEASE
    record['sdk_bundle_sha256'] = MIDL_IDENTITY['bundle_sha256']


def verify(sdk, report):
    if sys.platform != 'win32':
        raise ValueError('SDK probe requires native Windows')
    header = sdk / f'Include/{SDK_VERSION}/shared/sdkddkver.h'
    if not re.search(r'^\s*#\s*define\s+NTDDI_WIN11_BR\b', header.read_text(), re.MULTILINE):
        raise ValueError('SDK header lacks NTDDI_WIN11_BR')
    report['files'].append({'path': str(header), 'sha256': sha256(header)})
    for relative, arch in (
        (f'bin/{SDK_VERSION}/x64/rc.exe', 'x64'),
        (f'bin/{SDK_VERSION}/x64/midl.exe', 'x64'),
        ('Debuggers/x64/dbghelp.dll', 'x64'),
        ('Debuggers/arm64/dbghelp.dll', 'arm64'),
    ):
        path = sdk / relative
        record = {'path': str(path), 'expected_arch': arch}
        report['files'].append(record)
        record.update(check_pe(path, arch))
        with path.open('rb') as stream:
            stream.seek(60)
            offset = struct.unpack('<I', stream.read(4))[0]
            stream.seek(offset + 22)
            characteristics = struct.unpack('<H', stream.read(2))[0]
        if bool(characteristics & 0x2000) != (path.suffix.lower() == '.dll'):
            raise ValueError(f'PE DLL/executable kind mismatch: {path}')
        record['product_version'] = product_version(path)
        if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', record['product_version']):
            raise ValueError(f'invalid product version: {path}')
        if relative == MIDL_IDENTITY['relative']:
            check_midl_identity(sdk, path, record)
        elif relative.startswith('bin/') and not record['product_version'].startswith('10.0.28000.'):
            raise ValueError(f'host tool is not SDK 28000: {path}')
    for arch in MACHINES:
        for component, names in (('um', ('kernel32.lib', 'user32.lib')),
                                 ('ucrt', ('ucrt.lib', 'libucrt.lib'))):
            for name in names:
                path = sdk / f'Lib/{SDK_VERSION}/{component}/{arch}/{name}'
                record = {'path': str(path), 'expected_arch': arch}
                report['files'].append(record)
                record.update(check_library(path, arch))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args(argv)
    report = {'status': 'started', 'sdk_version': SDK_VERSION, 'sdk_release': SDK_RELEASE, 'files': []}
    result = 0
    try:
        verify(args.sdk, report)
        report['status'] = 'verified'
    except Exception as error:
        report.update(status='failed', error=str(error))
        print(f'SDK probe failed: {error}', file=sys.stderr)
        result = 1
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return result


if __name__ == '__main__':
    sys.exit(main())
