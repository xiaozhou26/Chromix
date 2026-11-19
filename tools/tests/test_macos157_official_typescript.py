"""Chromium's filtered Node TypeScript package, replayed against real donor bytes."""
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from unittest import mock

import pytest

from tools import install_macos_generators as installer
from tools import macos_restored_generators as gen
from tools import platform_pins
from tools import prepare_restored_build as prepare
from tools import restore_upstream_cache as restore
from tools.tests import test_macos_restored_generators as generators

FIXTURES = Path(__file__).parent / "fixtures"
KEY = "node-typescript157"
PACKAGE = gen.package_path(KEY)


@pytest.fixture(scope="module")
def evidence():
    return json.loads(gzip.decompress((FIXTURES / "macos157_official_typescript.json.gz").read_bytes()))


def inventory(root):
    return sorted([[path.relative_to(root).as_posix(), path.stat().st_mode & 0o777,
                    gen.sha256(path.read_bytes())] for path in root.rglob("*") if path.is_file()])


def write_files(root, files):
    for name, mode, text in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)


@pytest.fixture
def donor(tmp_path, evidence):
    write_files(tmp_path / PACKAGE, evidence["donor_files"])
    return tmp_path


def test_real_donor_inventory_is_identical_on_both_architectures(donor, evidence):
    records = inventory(donor / PACKAGE)
    assert len(records) == 118
    assert gen.tree_digest(records) == gen.NODE_TYPESCRIPT157_OFFICIAL["tree_sha256"]
    for arch, artifact in (("arm64", 14214802687), ("x64", 14228304612)):
        proof = evidence["donors"][arch]
        assert proof["artifact_id"] == artifact
        assert proof["inventory"] == records
        assert proof["package_tree_sha256"] == gen.tree_digest(records)
        assert proof["inner_sha256"] == evidence["inner_sha256"][arch]
    identity = gen.package_identity(donor, KEY)
    assert identity["variant"] == "chromium157-official-patched-pruned-domains"
    assert identity["archive_sha256"] == gen.NODE_TYPESCRIPT157_OFFICIAL["archive_sha256"]


def test_reconstruct_official_patch_filter_and_core_domains(tmp_path, evidence):
    npm_root = tmp_path / "node_modules/typescript"
    write_files(npm_root, evidence["npm_files"])
    assert inventory(npm_root) == evidence["npm_inventory"]
    assert gen.tree_digest(inventory(npm_root)) == gen.NODE_TYPESCRIPT157["tree_sha256"]
    recipe = tmp_path / "recipe"
    for name, entry in evidence["recipe"].items():
        path = recipe / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(entry["text"])
        assert gen.sha256(path.read_bytes()) == gen.NODE_TYPESCRIPT157_OFFICIAL["recipe_sha256"][name]
    result = subprocess.run([shutil.which("gpatch") or "patch", "--batch", "--fuzz=0", "-p1", "-i",
                             str(recipe / "typescript.patch")], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not re.search(r"fuzz|FAILED", result.stdout + result.stderr, re.I)
    subprocess.run(["rsync", "-c", "--delete", "-r", "-q",
                    "--include-from=" + str(recipe / "npm_include.txt"),
                    "--exclude-from=" + str(recipe / "npm_exclude.txt"), "--prune-empty-dirs",
                    str(tmp_path / "node_modules") + "/", str(tmp_path / "filtered") + "/"], check=True)
    subprocess.run([sys.executable, str(recipe / "clean_json_attrs.py"), "--attr_pattern=^_",
                    r"--file_pattern=^package\.json$", str(tmp_path / "filtered")], check=True)
    filtered = tmp_path / "filtered/typescript"
    assert inventory(filtered) == evidence["official_inventory"]
    assert gen.tree_digest(inventory(filtered)) == gen.NODE_TYPESCRIPT157_OFFICIAL["official_tree_sha256"]
    assert evidence["core_pruned_typescript_paths"] == []
    assert evidence["core_mac_typescript_patch_paths"] == []
    assert gen.sha256(evidence["domain_regex"].encode()) == evidence["domain_regex_sha256"]
    for name in evidence["domain_targets"]:
        path = filtered / name
        text = path.read_text()
        for rule in evidence["domain_regex"].splitlines():
            if rule:
                pattern, replacement = rule.split("#")
                text = re.sub(pattern, replacement, text)
        path.write_text(text)
    expected = [[name, mode, gen.sha256(text.encode())] for name, mode, text in evidence["donor_files"]]
    assert inventory(filtered) == sorted(expected)
    assert len(evidence["removed_paths"]) == 22
    assert len(evidence["official_changed_paths"]) == 1
    assert evidence["official_changed_paths"][0] == "lib/lib.dom.d.ts"


@pytest.mark.parametrize("kind", ["extra", "missing", "mode", "content", "same_size_content", "empty_directory", "symlink", "hardlink"])
def test_official_variant_rejects_every_unknown_inventory_change(donor, kind):
    root = donor / PACKAGE
    path = root / "lib/lib.dom.d.ts"
    if kind == "extra":
        (root / "extra.js").write_bytes(b"unexpected")
    elif kind == "missing":
        path.unlink()
    elif kind == "mode":
        path.chmod(0o755)
    elif kind == "content":
        path.write_bytes(path.read_bytes() + b"unknown")
    elif kind == "same_size_content":
        before = path.stat()
        data = path.read_bytes()
        path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif kind == "empty_directory":
        (root / "empty").mkdir()
    elif kind == "symlink":
        outside = donor / "outside"
        path.rename(outside)
        path.symlink_to(outside)
    elif kind == "hardlink":
        os.link(path, donor / "hardlink")
    with pytest.raises((ValueError, OSError)):
        gen.package_identity(donor, KEY)


def test_mixing_official_pruning_and_npm_dom_is_not_an_accepted_variant(donor, evidence):
    text = next(text for name, _, text in evidence["npm_files"] if name == "lib/lib.dom.d.ts")
    for old, new in gen.NODE_TYPESCRIPT157["domain_repairs"]["lib/lib.dom.d.ts"]["replacements"]:
        text = text.replace(old, new)
    (donor / PACKAGE / "lib/lib.dom.d.ts").write_text(text)
    with pytest.raises(ValueError, match="bytes/inventory"):
        gen.package_identity(donor, KEY)


@pytest.mark.parametrize("arch", ["arm64", "x64"])
def test_real_donor_prepare_and_install_resume_without_package_download(evidence, arch):
    helper = generators.MacGeneratorsTest()
    helper.setUp()
    try:
        sources = json.loads(gzip.decompress((FIXTURES / "macos157_generators.json.gz").read_bytes()))
        for name, text in sources["sources"].items():
            helper.write(gen.DEVTOOLS + "/" + name, text.encode())
        for name, entry in sources["upstream_sources"].items():
            helper.write(name, entry["text"].encode())
        helper.patch(platform_pins, "load_pins", return_value=dict(zip(
            ("ChromiumVersion", "UngoogledCommit", "UngoogledMacOSCommit"), gen.PINS157)))
        helper.install(arch)
        write_files(helper.src / PACKAGE, evidence["donor_files"])
        helper.write(restore.MARKER, b'{"fixture":"receipt"}')
        before = gen.prepare(helper.src, host_arch=arch, repo=helper.work)
        assert not before["install_needed"]
        with mock.patch.object(restore, "verify_restored", return_value={"identity": "fixture"}), \
                mock.patch.object(prepare, "host_identity", return_value=("macos", arch)), \
                mock.patch.object(installer, "ensure_archive", side_effect=AssertionError("donor needs no download")):
            installed = installer.install(helper.work, arch, helper.work / "missing-downloads", repo=helper.work)
            assert installed["status"] == "already_installed"
        with mock.patch.object(gen, "smoke", return_value={"fixture": "not a native browser probe"}):
            state = gen.prepare(helper.src, host_arch=arch, repo=helper.work, repair=True)
            assert gen.prepare(helper.src, host_arch=arch, repo=helper.work, repair=True) == state
        assert inventory(helper.src / PACKAGE) == evidence["donors"][arch]["inventory"]
    finally:
        helper.doCleanups()


def test_official_variant_changes_only_157_manifest_identity():
    versions = (gen.PINS, gen.PINS155, gen.PINS156)
    old = {pins: gen.manifest_digest(pins) for pins in versions}
    current = gen.manifest_digest(gen.PINS157)
    with mock.patch.dict(gen.NODE_TYPESCRIPT157_OFFICIAL, {"tree_sha256": "0" * 64}):
        assert gen.manifest_digest(gen.PINS157) != current
        assert {pins: gen.manifest_digest(pins) for pins in versions} == old
