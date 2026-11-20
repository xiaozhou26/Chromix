#!/usr/bin/env python3
"""Read native Windows157 SDK PE/COFF identities without executing SDK binaries."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import shutil
import stat
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


BIGOBJ_MAGIC = bytes.fromhex('c7a1bad1eebaa94baf20faf66aa4dcb8')


def _symbol_names(data, start, count):
    if count > len(data) - start:
        raise ValueError('symbol names exceed archive table')
    for _ in range(count):
        end = data.find(b'\0', start)
        if end <= start:
            raise ValueError('missing or empty archive symbol name')
        start = end + 1
    if data[start:] not in (b'', b'\0'):
        raise ValueError('unexpected trailing symbol table data')


def _indices(data, start, count, code):
    width = struct.calcsize(code)
    if start < 0 or count > (len(data) - start) // width:
        raise ValueError('archive symbol index array is truncated')
    return [value[0] for value in struct.iter_unpack(code, data[start:start + count * width])]


def _object_identity(data):
    if len(data) < 20:
        raise ValueError('truncated COFF member')
    machine, sections, _, symbols, count, optional, flags = struct.unpack_from('<HHIIIHH', data)
    header_size, symbol_size = 20, 18
    if data[:4] == b'\0\0\xff\xff':
        version, machine = struct.unpack_from('<HH', data, 4)
        if version == 0:
            size, _, flags = struct.unpack_from('<IHH', data, 12)
            name_type = (flags >> 2) & 7
            if size != len(data) - 20 or flags >> 5 or flags & 3 > 2 or name_type > 4:
                raise ValueError('invalid COFF import header')
            strings = data[20:].split(b'\0')
            if (len(strings) != (4 if name_type == 4 or (machine == 0xa641 and name_type == 0) else 3) or
                    strings[-1] or not all(strings[:-1])):
                raise ValueError('invalid COFF import names')
            return machine, 'import_type_info', flags
        if version != 2 or len(data) < 56 or data[12:28] != BIGOBJ_MAGIC:
            raise ValueError('unknown anonymous COFF member')
        if any(data[28:44]):
            raise ValueError('unsupported BigObj metadata')
        sections, symbols, count = struct.unpack_from('<III', data, 44)
        header_size, symbol_size, optional, flags = 56, 20, 0, 0
    if optional or not sections or sections > (len(data) - header_size) // 40:
        raise ValueError('invalid COFF section headers')
    section_end = header_size + sections * 40
    for i in range(sections):
        section = header_size + i * 40
        size, raw, relocs, lines, reloc_count, line_count, characteristics = struct.unpack_from('<IIIIHHI', data, section + 16)
        if raw and (raw < section_end or size > len(data) - raw):
            raise ValueError('COFF section data outside member')
        if reloc_count == 0xffff and characteristics & 0x01000000:
            if relocs < section_end or relocs + 10 > len(data):
                raise ValueError('truncated COFF extended relocation count')
            reloc_count = struct.unpack_from('<I', data, relocs)[0]
        if reloc_count and (relocs < section_end or reloc_count > (len(data) - relocs) // 10):
            raise ValueError('COFF relocations outside member')
        if line_count and (lines < section_end or line_count > (len(data) - lines) // 6):
            raise ValueError('COFF line numbers outside member')
    if symbols:
        if symbols < section_end or count > (len(data) - symbols - 4) // symbol_size:
            raise ValueError('COFF symbols outside member')
        string_start = symbols + count * symbol_size
        string_size = struct.unpack_from('<I', data, string_start)[0]
        if string_size < 4 or string_size > len(data) - string_start:
            raise ValueError('COFF string table outside member')
    elif count:
        raise ValueError('COFF symbol count without symbol table')
    return machine, 'bigobj_flags' if header_size == 56 else 'coff_characteristics', flags


def check_library(path, arch):
    """Validate bounded COFF archives, including the ARM64EC symbol map."""
    size = path.stat().st_size
    if not 8 <= size <= 128 * 1024 ** 2:
        raise ValueError(f'invalid library size: {path}')
    blob = path.read_bytes()
    if len(blob) != size or blob[:8] != b'!<arch>\n':
        raise ValueError(f'not a COFF library: {path}')
    members, linkers, long_names, ec_data = {}, [], None, None
    padding_counts = Counter()
    offset = 8
    while offset < size:
        header_offset = offset
        header = blob[offset:offset + 60]
        if len(header) != 60 or header[58:] != b'`\n' or offset % 2:
            raise ValueError(f'invalid archive member header: {path}; offset={offset}')
        name = header[:16].decode('ascii').rstrip(' ')
        raw_size = header[48:58].rstrip(b' ')
        if not raw_size.isdigit():
            raise ValueError('invalid archive decimal size')
        length = int(raw_size)
        start, end = offset + 60, offset + 60 + length
        if end + length % 2 > size:
            raise ValueError(f'archive member outside file: {path}; header_offset={offset}; member={name!r}')
        data = blob[start:end]
        if name in ('/', '//', '/<ECSYMBOLS>/'):
            if members:
                raise ValueError('archive metadata after object members')
            if name == '/':
                if len(linkers) >= 2 or long_names is not None or ec_data is not None:
                    raise ValueError('misplaced archive linker table')
                linkers.append(data)
            elif name == '//':
                if long_names is not None or ec_data is not None:
                    raise ValueError('duplicate or misplaced archive long names')
                long_names = data
            else:
                if arch != 'arm64' or ec_data is not None or len(linkers) != 2:
                    raise ValueError('EC symbol map requires ARM64 and two linker tables')
                ec_data = data
        else:
            if name.startswith('/'):
                if not name[1:].isdigit() or long_names is None:
                    raise ValueError(f'unknown archive member: {name!r}')
                index = int(name[1:])
                if (index >= len(long_names) or (index and long_names[index - 1] != 0) or
                        long_names.find(b'\0', index) <= index):
                    raise ValueError('invalid archive long name offset')
            elif not name.endswith('/') or len(name) < 2:
                raise ValueError('invalid archive short name')
            members[offset] = _object_identity(data)
        offset = end + length % 2
        if length % 2:
            pad = blob[end:end + 1]
            # Microsoft SDK UCRT archives use NUL; other SDK libraries use IMAGE_ARCHIVE_PAD (LF).
            if pad not in (b'\0', b'\n'):
                raise ValueError(f'invalid archive padding: {path}; header_offset={header_offset}; '
                                 f'member={name!r}; data_size={length}; pad_offset={end}; '
                                 f'pad_hex={pad.hex()}; file_size={size}')
            padding_counts[pad.hex()] += 1
    if not members:
        raise ValueError(f'no COFF objects in library: {path}')
    regular, ec, offsets = set(), set(), []
    if linkers:
        if len(linkers) != 2 or any(len(table) < 4 for table in linkers):
            raise ValueError('COFF archive requires two complete linker tables')
        first, second = linkers
        count = struct.unpack_from('>I', first)[0]
        first_offsets = _indices(first, 4, count, '>I')
        _symbol_names(first, 4 + count * 4, count)
        if not set(first_offsets) <= members.keys():
            raise ValueError('first linker table points outside object members')
        count = struct.unpack_from('<I', second)[0]
        offsets = _indices(second, 4, count, '<I')
        if len(offsets) != len(members) or set(offsets) != members.keys():
            raise ValueError('second linker table must index every object exactly once')
        cursor = 4 + count * 4
        if cursor + 4 > len(second):
            raise ValueError('truncated second linker table')
        symbols = struct.unpack_from('<I', second, cursor)[0]
        indices = _indices(second, cursor + 4, symbols, '<H')
        if any(not 1 <= i <= count for i in indices):
            raise ValueError('ordinary symbol index outside member table')
        _symbol_names(second, cursor + 4 + symbols * 2, symbols)
        regular = {offsets[i - 1] for i in indices} | set(first_offsets)
    if ec_data is not None:
        if len(ec_data) < 4:
            raise ValueError('truncated EC symbol map')
        count = struct.unpack_from('<I', ec_data)[0]
        indices = _indices(ec_data, 4, count, '<H')
        if not count or any(not 1 <= i <= len(offsets) for i in indices):
            raise ValueError('EC symbol index outside member table')
        _symbol_names(ec_data, 4 + count * 2, count)
        ec = {offsets[i - 1] for i in indices}
    counts, flags = Counter(), Counter()
    for offset, (machine, kind, flag) in members.items():
        if machine != MACHINES[arch]:
            # LLVM's EC map covers ARM64EC and x64 objects, never the ordinary ARM64 symbol view.
            allowed_ec = (arch == 'arm64' and ec_data is not None and offset not in regular and
                          (machine == 0xa641 or (machine == 0x8664 and offset in ec)))
            if not allowed_ec:
                raise ValueError(f'wrong COFF machine: {path}; offset={offset}; machine=0x{machine:04x}; expected {arch}')
        counts[f'0x{machine:04x}'] += 1
        flags[f'{kind}:0x{flag:04x}'] += 1
    if f'0x{MACHINES[arch]:04x}' not in counts:
        raise ValueError('archive lacks native target objects')
    return {'size': size, 'sha256': sha256(path), 'object_machines': dict(counts), 'member_flags': dict(flags),
            'padding_bytes': dict(padding_counts), 'ec_symbol_members': len(ec)}


def required_libraries():
    for arch in MACHINES:
        for component, names in (('um', ('kernel32.lib', 'user32.lib')),
                                 ('ucrt', ('ucrt.lib', 'libucrt.lib'))):
            for name in names:
                yield Path(f'Lib/{SDK_VERSION}/{component}/{arch}/{name}'), arch


def capture_libraries(sdk, destination):
    """Preserve original files and all parser outcomes; never grant SDK approval."""
    destination.mkdir(parents=True, exist_ok=False)
    manifest = {'status': 'collecting', 'purpose': 'diagnostic-only', 'sdk_release': SDK_RELEASE,
                'run_id': os.environ.get('GITHUB_RUN_ID'), 'attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
                'sha': os.environ.get('GITHUB_SHA'), 'files': []}
    total = 0
    errors = []
    try:
        for relative, arch in required_libraries():
            source = sdk / relative
            record = {'relative': relative.as_posix(), 'source': str(source), 'expected_arch': arch}
            manifest['files'].append(record)
            try:
                info = source.lstat()
                if (not stat.S_ISREG(info.st_mode) or source.is_symlink() or
                        getattr(info, 'st_file_attributes', 0) & 0x400 or
                        not 0 < info.st_size <= 128 * 1024 ** 2):
                    raise ValueError('library must be a nonempty regular file of at most 128 MiB')
                if total + info.st_size > 512 * 1024 ** 2:
                    raise ValueError('library diagnostic capture exceeds 512 MiB')
                total += info.st_size
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                record.update(size=info.st_size, sha256=sha256(source))
                with source.open('rb') as reader, target.open('xb') as writer:
                    shutil.copyfileobj(reader, writer, 1024 * 1024)
                if target.stat().st_size != info.st_size or sha256(target) != record['sha256']:
                    raise ValueError('library diagnostic copy identity mismatch')
                record['capture_status'] = 'copied'
                try:
                    record['parser'] = {'status': 'verified', **check_library(target, arch)}
                except Exception as error:
                    record['parser'] = {'status': 'failed', 'error': str(error)}
            except Exception as error:
                record.update(capture_status='failed', capture_error=str(error))
                errors.append(str(error))
        manifest['status'] = 'incomplete' if errors else 'captured'
    finally:
        (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    if errors:
        raise ValueError(f'library diagnostic capture incomplete: {len(errors)} files failed')


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
    for relative, arch in required_libraries():
        path = sdk / relative
        record = {'path': str(path), 'expected_arch': arch}
        report['files'].append(record)
        record.update(check_library(path, arch))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--capture-libraries', type=Path, help='diagnostic-only raw library copies, even on failure')
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
        if args.capture_libraries:
            try:
                capture_libraries(args.sdk, args.capture_libraries)
                report['library_diagnostics'] = str(args.capture_libraries / 'manifest.json')
            except Exception as error:
                report.update(status='failed', capture_error=str(error))
                result = 1
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return result


if __name__ == '__main__':
    sys.exit(main())
