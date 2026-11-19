"""Real filesystem links, Cargo deployment behavior and latest checkpoint lineage."""
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import windows157_tools as tools
import windows157_crubit_resume as resume
from test_windows157_crubit_resume import executable, message, cargo_json

AUDIT = json.loads(gzip.decompress((ROOT / 'tools/tests/fixtures/windows157_crubit_retry2.json.gz').read_bytes()))
NATIVE = json.loads(gzip.decompress((ROOT / 'tools/tests/fixtures/windows157_handle_native.json.gz').read_bytes()))


def linked_output(root):
    target = root / 'out/crubit'
    release = target / tools.HOST_TRIPLE / 'release'
    original = executable(release / 'build/cc_bindings_from_rs/b8c8cf717b68bfe0/out/cc_bindings_from_rs.exe')
    alias = release / 'cc_bindings_from_rs.exe'
    os.link(original, alias)
    assert alias.samefile(original) and alias.stat().st_nlink == 2
    return target, original, alias


def test_real_filesystem_cargo_owned_hardlink_private_copy(tmp_path):
    root = tmp_path.resolve()
    target, original, alias = linked_output(root)
    with pytest.raises(ValueError, match='"nlink": 2'):
        tools.file(alias)
    before = tools.file_identity(alias.stat())
    assert tools.cargo_output(cargo_json(message(root, alias)), root, target) == alias
    with tools.private_cargo_output(alias) as private:
        assert private.stat().st_nlink == 1 and not private.samefile(alias)
        assert private.read_bytes() == original.read_bytes()
        tools.pe_executable(private)
    assert tools.file_identity(alias.stat()) == before
    assert original.samefile(alias)


def test_real_external_alias_or_third_link_rejected(tmp_path):
    root = tmp_path.resolve()
    _, original, alias = linked_output(root)
    outside = root / 'outside.exe'
    os.link(alias, outside)
    with pytest.raises(ValueError, match='unverified Cargo output'):
        with tools.private_cargo_output(alias): pass
    outside.unlink()
    original.unlink()
    os.link(alias, outside)
    with pytest.raises(ValueError, match='owned build output alias'):
        with tools.private_cargo_output(alias): pass


def test_real_symlink_and_changed_output_rejected(tmp_path):
    root = tmp_path.resolve()
    _, original, alias = linked_output(root)
    alias.unlink()
    alias.symlink_to(original)
    with pytest.raises(ValueError):
        with tools.private_cargo_output(alias): pass


def test_diagnostic_distinguishes_missing_empty_and_hardlink(tmp_path):
    root = tmp_path.resolve()
    absent = tools.file_evidence(root / 'absent.exe')
    assert absent['exists'] is False and absent['stat_error'] == 'FileNotFoundError'
    empty = root / 'empty.exe'
    empty.write_bytes(b'')
    info = tools.file_evidence(empty)
    assert info['exists'] is True and info['size'] == 0 and info['nlink'] == 1
    _, _, alias = linked_output(root)
    info = tools.file_evidence(alias)
    assert info['exists'] is True and info['size'] > 0 and info['nlink'] == 2 and info['is_symlink'] is False


@pytest.mark.skipif(os.name != 'nt', reason='Windows NTFS share-deny semantics require native Windows')
def test_windows_locked_alias_blocks_actual_external_writer_and_delete(tmp_path):
    _, original, alias = linked_output(tmp_path.resolve())
    with tools.private_cargo_output(alias) as private:
        for path in (original, alias):
            result = subprocess.run([sys.executable, '-c',
                'import sys; open(sys.argv[1], "r+b").write(b"changed")', str(path)], capture_output=True)
            assert result.returncode != 0
            with pytest.raises(OSError): path.unlink()
        tools.pe_executable(private)
    with original.open('r+b') as stream: stream.write(b'MZ')


@pytest.mark.skipif(not shutil.which('cargo') or not shutil.which('rustc'), reason='native Rust/Cargo is unavailable')
def test_real_native_cargo_no_dependency_output_link_behavior(tmp_path):
    root = tmp_path.resolve()
    (root / 'src').mkdir()
    (root / 'Cargo.toml').write_text('[package]\nname="chromix_link_probe"\nversion="0.1.0"\nedition="2021"\n')
    (root / 'src/main.rs').write_text('fn main() { println!("actual cargo output"); }\n')
    version = subprocess.check_output(['rustc', '-vV'], text=True)
    host = next(line.split(': ', 1)[1] for line in version.splitlines() if line.startswith('host:'))
    run = subprocess.run(['cargo', 'build', '--offline', '--release', '--target', host,
                          '--message-format=json-render-diagnostics'], cwd=root, capture_output=True, text=True, check=True)
    output = next(json.loads(line)['executable'] for line in run.stdout.splitlines() if json.loads(line).get('executable'))
    path = Path(output)
    assert path.is_file() and path.stat().st_size > 0 and not path.is_symlink()
    if os.name == 'nt':
        assert path.stat().st_nlink == 2
        matches = [p for p in path.parent.rglob('*.exe') if p != path and p.samefile(path)]
        assert len(matches) == 1
    elif sys.platform == 'darwin':
        assert path.stat().st_nlink == 1  # Cargo uses copy-on-write on macOS, not Windows hard_link.
    subprocess.run([str(path)], check=True, capture_output=True)


def test_fixed_cargo_source_documents_windows_link_not_mac_copy():
    source = AUDIT['cargo_source']
    assert hashlib.sha256(source['text'].encode()).hexdigest() == source['sha256']
    assert 'cfg!(target_os = "macos")' in source['text']
    assert 'fs::hard_link(src, dst)' in source['text']
    assert 'fs::copy(src, dst)' in source['text']


def test_latest_parent_admission_complete_proof_not_just_markers(tmp_path):
    work = tmp_path.resolve()
    value = json.loads(NATIVE['parent_admission']['text'])
    receipt = work / resume.RECEIPT
    receipt.write_bytes(NATIVE['parent_admission']['text'].encode())
    (work / '.chromix-windows157-crubit-resume.json').write_bytes(AUDIT['tree_parent_receipt']['text'].encode())
    result = resume.previous_admission(work)
    assert result['run_id'] == 44576227466 and result['head_sha'] == resume.DONOR_SHA
    value['full_source'][next(iter(value['full_source']))] = '0' * 64
    receipt.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='complete latest checkpoint parent admission changed'):
        resume.previous_admission(work)


def test_latest_metadata_and_old_checkpoint_rejected():
    class Client:
        def get(self, path): return NATIVE['latest_checkpoint']['run']
        def items(self, path, key): return NATIVE['latest_checkpoint'][key][key]
    result = resume.metadata(Client(), datetime(2026, 11, 19, 17, tzinfo=timezone.utc))
    assert result['run_id'] == 44576227466
    assert [p['id'] for p in result['artifacts']] == [14559964146, 14561755654]
    old = json.loads(gzip.decompress((ROOT / 'tools/tests/fixtures/windows157_crubit_checkpoint.json.gz').read_bytes()))
    class Old(Client):
        def get(self, path): return old['run']
    with pytest.raises(ValueError, match='donor run'):
        resume.metadata(Old(), datetime(2026, 11, 19, 17, tzinfo=timezone.utc))


def test_real_hardlink_source_mutation_detected_on_non_windows(tmp_path):
    if os.name == 'nt':
        pytest.skip('Windows denies the write; covered by the native share-deny test')
    _, original, alias = linked_output(tmp_path.resolve())
    with pytest.raises(ValueError, match='identity changed while locked'):
        with tools.private_cargo_output(alias):
            with original.open('r+b') as stream:
                stream.seek(30)
                stream.write(b'changed')


def test_no_generic_hardlink_exemption(tmp_path):
    root = tmp_path.resolve()
    source = executable(root / 'arbitrary.exe')
    alias = root / 'cc_bindings_from_rs.exe'
    os.link(source, alias)
    with pytest.raises(ValueError, match='exact host release output'):
        with tools.private_cargo_output(alias): pass


def test_latest_admission_artifact_tamper_rejected():
    import copy
    data = copy.deepcopy(NATIVE['latest_checkpoint'])
    item = next(a for a in data['artifacts']['artifacts'] if a['id'] == 14559844166)
    item['digest'] = 'sha256:' + '0' * 64
    class Client:
        def get(self, path): return data['run']
        def items(self, path, key): return data[key][key]
    with pytest.raises(ValueError, match='admission artifact'):
        resume.metadata(Client(), datetime(2026, 11, 19, 17, tzinfo=timezone.utc))


def test_actual_archived_windows_pe_header_and_full_executable_evidence(tmp_path):
    import base64
    data = base64.b64decode(AUDIT['pe_header']['bytes'])
    assert hashlib.sha256(data).hexdigest() == AUDIT['pe_header']['sha256']
    release = tmp_path.resolve() / 'out/crubit' / tools.HOST_TRIPLE / 'release'
    original = release / 'build/cc_bindings_from_rs/b8c8cf717b68bfe0/out/cc_bindings_from_rs.exe'
    original.parent.mkdir(parents=True)
    original.write_bytes(data)
    alias = release / 'cc_bindings_from_rs.exe'
    os.link(original, alias)
    with tools.private_cargo_output(alias) as private:
        tools.pe_executable(private)
        assert private.read_bytes() == data
    target = [e for e in AUDIT['extracted_executables'] if '/x86_64-pc-windows-msvc/' in e['path']]
    assert len(target) == 2
    assert {e['sha256'] for e in target} == {'7b865065dc41c7f5e00c104cb40f8b3f3b33ebe4ee43dfaf9a2d10afdafccaa6'}
    assert {e['size'] for e in target} == {6106624}
    assert {e['machine'] for e in target} == {'0x8664'}


def test_actual_nightly_cargo_revision_is_pinned():
    import tomllib
    manifest = AUDIT['nightly_manifest']
    assert hashlib.sha256(manifest['text'].encode()).hexdigest() == manifest['sha256']
    parsed = tomllib.loads(manifest['text'])
    assert parsed['pkg']['cargo']['version'] == '0.101.0-nightly (3c0b53475 2026-09-04)'
    assert AUDIT['cargo_source']['commit'] == '3c0b534756e166d12eb9fd2e1abfe5b42ac6101e'


def test_latest_admission_preserves_parent_receipt_and_has_new_scope():
    source = (ROOT / "tools/windows157_crubit_resume.py").read_text()
    assert resume.ADMISSION != resume.RECEIPT
    assert 'with receipt.open("x", encoding="utf-8")' in source
    assert 'with receipt.open("w"' not in source
    assert 'result["full_source"] != previous["full_source"]' in source
    assert 'result["source"] != previous["source"]' in source
    assert 'result.get("previous_admission") != previous' in source


def test_production_atomic_publish_uses_private_file_and_refuses_existing_destination(tmp_path):
    root = tmp_path.resolve()
    _, original, alias = linked_output(root)
    destination = root / 'installed/cc_bindings_from_rs.exe'
    result = tools.publish_cargo_output(alias, destination)
    assert result['status'] == 'published' and result['nlink'] == 1
    assert not destination.samefile(alias)
    assert destination.read_bytes() == alias.read_bytes()
    before = destination.read_bytes()
    with pytest.raises(ValueError, match='already exists'):
        tools.publish_cargo_output(alias, destination)
    with original.open('r+b') as stream:
        stream.seek(64)
        stream.write(b'changed')
    assert destination.read_bytes() == before
    assert not list(destination.parent.glob('.chromix-crubit-install-*'))


def test_latest_parent_receipt_requires_intact_original_ancestor(tmp_path):
    work = tmp_path.resolve()
    (work / resume.RECEIPT).write_bytes(NATIVE['parent_admission']['text'].encode())
    (work / '.chromix-windows157-crubit-resume.json').write_bytes(b'{}')
    with pytest.raises(ValueError, match='original source admission bytes'):
        resume.previous_admission(work)
