"""Real Windows donor v6/v4 records; strict read-only retained-state checks."""
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import struct
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import windows157_ninja_state as state

FIXTURE = json.loads(gzip.decompress((ROOT / 'tools/tests/fixtures/windows157_ninja_donor.json.gz').read_bytes()))


def test_actual_complete_log_and_dependency_prefix_match_official_schema():
    raw = FIXTURE['log']['text'].encode()
    assert hashlib.sha256(raw).hexdigest() == FIXTURE['log']['sha256']
    parsed = state.parse_log(raw)
    assert parsed['version'] == 6 and parsed['records'] == 71953
    deps = state.parse_deps(base64.b64decode(FIXTURE['deps']['prefix_base64']))
    assert deps['version'] == 4 and deps['records'] == 3
    assert deps['outputs'].issubset(parsed['outputs'])
    for item in FIXTURE['official_sources'].values():
        assert hashlib.sha256(item['text'].encode()).hexdigest() == item['sha256']
    assert 'const int kCurrentVersion = 6;' in FIXTURE['official_sources']['build_log.cc']['text']
    assert 'const unsigned kMaxRecordSize = (1 << 19) - 1;' in FIXTURE['official_sources']['deps_log.cc']['text']
    assert state.NINJA_SHA256 == FIXTURE['ninja_sha256']


@pytest.mark.parametrize('header', [b'# ninja log v5\n', b'# ninja log v7\n', b'# ninja log v999\n',
                                    b'# ninja log v6\r\n', b'\xef\xbb\xbf# ninja log v6\n', b'API_KEY=secret\n'])
def test_unknown_header_is_not_guessed_or_echoed(header):
    with pytest.raises(ValueError) as error:
        state.parse_log(header + b'1\t2\t3\tobj/a.obj\tabc\n')
    assert 'secret' not in str(error.value)
    if header.startswith(b'# ninja log'):
        assert repr(header) in str(error.value)


@pytest.mark.parametrize('record', [b'3\t2\t4\tobj/a.obj\tabc\n', b'-1\t2\t4\tobj/a.obj\tabc\n',
    b'1\t2147483648\t4\tobj/a.obj\tabc\n', b'1\t2\t9223372036854775808\tobj/a.obj\tabc\n',
    b'1\t2\t4\tobj/a.obj\txyz\n', b'1\t2\t4\tobj/a.obj\t12345678901234567\n',
    b'1\t2\t4\t../outside.obj\tabc\n', b'1\t2\t4\tC:/outside.obj\tabc\n',
    b'1\t2\t4\tobj/a.obj\tabc', b'1\t2\t4\tobj/a.obj\tabc\textra\n'])
def test_malformed_record_duration_output_and_hash_are_rejected(record):
    with pytest.raises(ValueError): state.parse_log(state.LOG_HEADER + record)


def path_record(name, identifier):
    raw = name.encode()
    raw += b'\0' * ((-len(raw)) % 4)
    raw += struct.pack('<I', ~identifier & 0xffffffff)
    return struct.pack('<I', len(raw)) + raw


def deps_record(output, refs):
    raw = struct.pack('<III', output, 123, 0) + b''.join(struct.pack('<I', ref) for ref in refs)
    return struct.pack('<I', 0x80000000 | len(raw)) + raw


def valid_deps():
    return state.DEPS_HEADER + path_record('obj/a.obj', 0) + path_record('../../src/a.h', 1) + deps_record(0, [1])


@pytest.mark.parametrize('kind', ['version','truncated','size','checksum','outid','depid','escape','padding','empty'])
def test_malformed_deps_never_truncates_or_repairs(kind):
    raw = valid_deps()
    if kind == 'version': raw = raw[:12] + struct.pack('<I', 5) + raw[16:]
    elif kind == 'truncated': raw = raw[:-1]
    elif kind == 'size': raw = raw[:16] + struct.pack('<I', state.MAX_RECORD + 1) + raw[20:]
    elif kind == 'checksum': raw = state.DEPS_HEADER + path_record('obj/a.obj', 9)
    elif kind == 'outid': raw = state.DEPS_HEADER + path_record('obj/a.obj', 0) + deps_record(1, [])
    elif kind == 'depid': raw = state.DEPS_HEADER + path_record('obj/a.obj', 0) + deps_record(0, [1])
    elif kind == 'escape': raw = state.DEPS_HEADER + path_record('../../../outside', 0) + deps_record(0, [])
    elif kind == 'padding': raw = state.DEPS_HEADER + struct.pack('<I', 12) + b'a\0\0\0\0\0\0\0' + struct.pack('<I', 0xffffffff)
    elif kind == 'empty': raw = state.DEPS_HEADER
    with pytest.raises(ValueError): state.parse_deps(raw)


def test_parser_never_modifies_input_buffers_or_duplicate_command_records():
    raw = state.LOG_HEADER + b'1\t2\t3\tobj/a.obj\ta\n1\t4\t5\tobj/a.obj\tb\n'
    before = bytes(raw)
    parsed = state.parse_log(raw)
    assert parsed['records'] == 2 and parsed['outputs']['obj/a.obj'] == (1,4,5,'b')
    assert raw == before
    assert state.parse_deps(valid_deps())['records'] == 1


def test_full_validation_preserves_real_log_bytes_and_mtime(tmp_path):
    import io
    import zipfile
    out = tmp_path / 'out'
    out.mkdir()
    log = out / '.ninja_log'
    deps = out / '.ninja_deps'
    log.write_bytes(FIXTURE['log']['text'].encode())
    deps.write_bytes(base64.b64decode(FIXTURE['deps']['prefix_base64']))
    obj = out / 'obj/third_party/abseil-cpp/absl/base/base/casts.obj'
    obj.parent.mkdir(parents=True)
    obj.write_bytes(b'retained object')
    ninja = tmp_path / 'ninja.exe'
    ninja.write_bytes(zipfile.ZipFile(io.BytesIO(base64.b64decode(FIXTURE['official_ninja_zip_base64']))).read('ninja.exe'))
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (log, deps, obj)]
    result = state.validate(out, ninja)
    assert result['log_records'] == 71953 and result['deps_records'] == 3 and result['retained_objects'] == 1
    assert [(p.read_bytes(), p.stat().st_mtime_ns) for p in (log, deps, obj)] == before
    ninja.write_bytes(ninja.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='official1.12.1'): state.validate(out, ninja)


def test_actual_windows_args_markers_are_crlf_but_ninja_log_is_binary_lf():
    audit = FIXTURE['phase_audit']
    args = base64.b64decode(audit['args_base64'])
    overlay = base64.b64decode(audit['overlay_base64'])
    assert b'\r\n' in args and b'\r\n' in overlay
    assert b'\r' not in FIXTURE['log']['text'].encode()
    assert 'C:/c/chromix/src/third_party/llvm-build' in overlay.decode()
    assert all(value.endswith('\r\n') for value in audit['markers'].values())
    assert audit['missing_archived_outputs'] == []


def test_retained_gn_contract_matches_real_windows_crlf_inputs(tmp_path):
    import windows157_compiled_resume as recovery
    import merge_gn_args
    work = tmp_path.resolve() / 'chromix'
    view = tmp_path.resolve() / 'view'
    audit = FIXTURE['phase_audit']
    def put(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    put(work / 'windows157-native.gn', base64.b64decode(audit['overlay_base64']))
    put(work / 'src/out/Chromix/args.gn', base64.b64decode(audit['args_base64']))
    evidence = json.loads(gzip.decompress((ROOT / 'tools/tests/fixtures/windows157_compiled_stage3.json.gz').read_bytes()))
    upstream = json.loads(gzip.decompress((ROOT / 'tools/tests/fixtures/windows157_sources.json.gz').read_bytes()))
    put(work / 'tooling/ungoogled-chromium/flags.gn', evidence['core_flags'].encode())
    put(work / 'tooling/ungoogled-chromium-windows/flags.windows.gn', upstream['files']['windows/flags.windows.gn']['text'].encode())
    put(view / 'build/args.windows.gn', (ROOT / 'build/args.windows.gn').read_bytes())
    recovery.retained_gn_contract(view, work)
    path = work / 'src/out/Chromix/args.gn'
    before = path.read_bytes()
    path.write_bytes(before.replace(b'chrome_pgo_phase = 2', b'chrome_pgo_phase = 0'))
    with pytest.raises(ValueError, match='arguments'): recovery.retained_gn_contract(view, work)
    path.write_bytes(before)
    overlay = work / 'windows157-native.gn'
    overlay.write_bytes(overlay.read_bytes().replace(b'C:/c/chromix', b'D:/other'))
    with pytest.raises(ValueError, match='overlay'): recovery.retained_gn_contract(view, work)


def test_retained_ninja_output_parent_symlink_is_rejected(tmp_path):
    import io
    import zipfile
    out=tmp_path/'out';out.mkdir()
    (out/'.ninja_log').write_bytes(FIXTURE['log']['text'].encode())
    (out/'.ninja_deps').write_bytes(base64.b64decode(FIXTURE['deps']['prefix_base64']))
    outside=tmp_path/'outside';outside.mkdir()
    (out/'obj').symlink_to(outside,target_is_directory=True)
    ninja=tmp_path/'ninja.exe'
    ninja.write_bytes(zipfile.ZipFile(io.BytesIO(base64.b64decode(FIXTURE['official_ninja_zip_base64']))).read('ninja.exe'))
    with pytest.raises(ValueError,match='symlink/reparse'):state.validate(out,ninja)


def test_actual_complete_preflight_evidence_preserves_all_donor_outputs():
    proof = FIXTURE['exact_preflight']
    assert proof['status'] == 'passed' and proof['donor_run'] == 44591948738
    assert (proof['patches_old'], proof['patches_new']) == (232,235)
    assert proof['all_out_files'] == proof['out_files'] == 103527
    assert proof['out_hashes_mtime_unchanged'] and proof['source_unchanged']
    assert proof['ninja_state']['retained_objects'] == 25613
    assert proof['native_windows_execution'] is False
    assert proof['out_inventory_sha256'] == '391ce57d4b3ebd83a96d1e8fc61abbb7adb137bd16a9360716ae89c2703491c4'
    assert {entry['artifact'] for entry in FIXTURE['downloads']} == {14611575270,14611525307}
