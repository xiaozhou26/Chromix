"""Source freshness checks never trust stamps and never mutate original files."""
from pathlib import Path
import os
import subprocess
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import verify_patch_stack as stack


def source_tree(tmp_path, newline=b'\n'):
    repo, src = tmp_path / 'repo', tmp_path / 'source'
    (repo / 'patches').mkdir(parents=True); src.mkdir()
    (repo / 'patches/series').write_text('patches/0001-new.patch\npatches/0002-change.patch\n', encoding='utf-8')
    (repo / 'patches/0001-new.patch').write_text('''diff --git a/file.cc b/file.cc
new file mode 100644
--- /dev/null
+++ b/file.cc
@@ -0,0 +1,2 @@
+int version = 1;
+// https://example.com
''', encoding='utf-8', newline='\n')
    (repo / 'patches/0002-change.patch').write_text('''diff --git a/file.cc b/file.cc
--- a/file.cc
+++ b/file.cc
@@ -1,2 +1,2 @@
-int version = 1;
+int version = 2;
 // https://example.com
''', encoding='utf-8', newline='\n')
    (src / 'file.cc').write_bytes(newline.join([b'int version = 2;', b'// https://example.com', b'']))
    (src / '.chromix-source-ready').write_text('not an attestation', encoding='utf-8')
    return repo, src


@pytest.mark.parametrize('newline', [b'\n', b'\r\n'])
def test_reverse_forward_is_read_only_and_detects_old_source(tmp_path, newline):
    repo, src = source_tree(tmp_path, newline)
    path = src / 'file.cc'; before = path.read_bytes(), path.stat().st_mtime_ns
    result = stack.verify(src, repo)
    assert result['status'] == 'verified' and result['patch_count'] == 2
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)
    path.write_bytes(before[0].replace(b'= 2;', b'= 1;'))
    before = path.read_bytes(), path.stat().st_mtime_ns
    with pytest.raises(ValueError, match='stale/incompatible'):
        stack.verify(src, repo)
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)


def test_new_file_must_match_completely(tmp_path):
    repo, src = source_tree(tmp_path)
    with (src / 'file.cc').open('ab') as out:
        out.write(b'// unexpected old-version tail\n')
    with pytest.raises(ValueError):
        stack.verify(src, repo)


def test_nested_temp_cannot_silently_skip_patches(tmp_path, monkeypatch):
    repo, src = source_tree(tmp_path)
    subprocess.run(['git','init',str(repo)], capture_output=True, check=True)
    temporary = repo / 'nested'; temporary.mkdir()
    monkeypatch.setattr(stack.tempfile, 'tempdir', str(temporary))
    assert stack.verify(src, repo)['status'] == 'verified'


def test_domain_substitution_and_required_metadata(tmp_path):
    repo, src = source_tree(tmp_path)
    core = tmp_path / 'core'; core.mkdir()
    (core / 'domain_regex.list').write_text(r'example\.com#blocked.test' + '\n', encoding='utf-8')
    (core / 'domain_substitution.list').write_text('file.cc\n', encoding='utf-8')
    (src / '.chromix-domain-substituted').touch()
    p = src / 'file.cc'; p.write_bytes(p.read_bytes().replace(b'example.com', b'blocked.test'))
    with pytest.raises(ValueError, match='require'):
        stack.verify(src, repo)
    assert stack.verify(src, repo, core=core, tooling=core, platform='linux')['domain_substituted'] is True


@pytest.mark.parametrize('marker', ['.chromix-layer-in-progress', '.chromix-patch-in-progress',
    '.chromix-domain-substitution-in-progress', '.chromix-restored-patches-in-progress'])
def test_interrupted_source_is_rejected(tmp_path, marker):
    repo, src = source_tree(tmp_path); (src / marker).touch()
    with pytest.raises(ValueError, match='unfinished'):
        stack.verify(src, repo)


def test_symlink_cannot_escape_source_copy(tmp_path):
    repo, src = source_tree(tmp_path)
    outside = tmp_path / 'outside'; (src / 'file.cc').rename(outside)
    try:
        os.symlink(outside, src / 'file.cc')
    except OSError:
        pytest.skip('symlink creation is unavailable on this host')
    with pytest.raises(ValueError, match='symlink'):
        # The shared patch reader uses ApplyError, a RuntimeError subclass.
        try:
            stack.verify(src, repo)
        except stack.arp.ApplyError as error:
            raise ValueError(str(error)) from error
