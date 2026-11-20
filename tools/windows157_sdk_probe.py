#!/usr/bin/env python3
"""Read native Windows157 SDK PE/COFF identities without executing SDK binaries."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import struct
import sys

try:
    from .verify_windows_bundle import check_pe, product_version, sha256
except ImportError:
    from verify_windows_bundle import check_pe, product_version, sha256

SDK_VERSION = '10.0.28000.0'
MACHINES = {'x86': 0x14C, 'x64': 0x8664, 'arm64': 0xAA64}


def check_library(path, arch):
    """Check every object/import member, excluding archive linker/name tables."""
    size = path.stat().st_size
    counts = Counter()
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
                if data[:4] == b'\0\0\xff\xff':
                    machine = struct.unpack_from('<H', data, 6)[0]
                if machine != MACHINES[arch]:
                    raise ValueError(f'wrong COFF machine: {path}: {name}: 0x{machine:04x}, expected {arch}')
                counts[f'0x{machine:04x}'] += 1
            stream.seek(end)
            if length % 2 and stream.read(1) != b'\n':
                raise ValueError(f'invalid archive padding: {path}')
    if not counts:
        raise ValueError(f'no COFF objects in library: {path}')
    return {'size': size, 'sha256': sha256(path), 'object_machines': dict(counts)}


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
        if relative.startswith('bin/') and not record['product_version'].startswith('10.0.28000.'):
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
    report = {'status': 'started', 'sdk_version': SDK_VERSION, 'files': []}
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
