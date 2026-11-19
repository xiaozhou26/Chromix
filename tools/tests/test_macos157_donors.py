"""Exact mac157 provenance and generator replay, not browser acceptance."""
import configparser
from datetime import datetime, timezone
import gzip
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
from unittest import mock

import pytest

from tools import apply_restored_patches as arp
from tools import fetch_upstream_cache as fetch
from tools import install_macos_generators as installer
from tools import linux157_source
from tools import macos_restored_generators as gen
from tools import macos_runtime as runtime
from tools import platform_pins
from tools import prepare_restored_build as prepare
from tools import restore_upstream_cache as restore
from tools.tests import test_macos_restored_generators as generators
from tools.tests import test_macos_toolchain as toolchain
from tools.tests import test_restore_upstream_cache as restores
from tools.tests import test_mac154_restore_links as links

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads(gzip.decompress((Path(__file__).parent / "fixtures/macos157_generators.json.gz").read_bytes()))
NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)


def copy_linux_recipe(repo):
    recipe = linux157_source.recipe(ROOT)
    for name in recipe["inputs"]:
        destination = repo / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / name).read_bytes())
    assert linux157_source.recipe(repo) == recipe


@pytest.mark.parametrize("arch,artifact,size", [("arm64", 14214802687, 11255458166),
                                               ("x64", 14228304612, 12909424988)])
def test_exact_successful_attempt_one_and_external_link_identity(arch, artifact, size):
    pin, manifest = fetch.load_manifest("macos", arch, root=ROOT)
    assert (pin["chromium_version"], pin["ungoogled_commit"], pin["head_sha"]) == gen.PINS157
    assert (pin["run_id"], pin["run_attempt"], manifest["run_attempt"]) == (43804136377, 1, 1)
    assert (pin["artifact"]["id"], pin["artifact"]["size_in_bytes"]) == (artifact, size)
    assert pin["source_roots"] == ["src"]
    fetch.validate_metadata(pin, FIXTURE["run"], FIXTURE["artifacts"][arch], now=NOW)
    for field, value in (("run_attempt", 3), ("run_attempt", 1.0), ("head_sha", gen.PINS156[2]),
                         ("head_branch", "master"), ("conclusion", "failure")):
        with pytest.raises(fetch.CacheMiss):
            fetch.validate_metadata(pin, dict(FIXTURE["run"], **{field: value}), FIXTURE["artifacts"][arch], now=NOW)
    for field, value in (("id", 13284623799), ("size_in_bytes", size + 1), ("expired", True),
                         ("digest", "sha256:" + "0" * 64), ("expires_at", "2026-11-18T00:00:00Z")):
        with pytest.raises(fetch.CacheMiss):
            fetch.validate_metadata(pin, FIXTURE["run"], dict(FIXTURE["artifacts"][arch], **{field: value}), now=NOW)
    identity, _, _ = restore.identities(ROOT, "macos", arch)
    for name in (restore.MAC154_ESBUILD_LINK, restore.MAC154_ESBUILD_ALIAS):
        assert restore.is_known_external_link(name, "macos", identity)
        for field in identity:
            bad = dict(identity)
            bad.pop(field)
            assert not restore.is_known_external_link(name, "macos", bad)
            assert not restore.is_known_external_link(name, "macos", dict(identity, **{field: "unreviewed"}))
        for extra in ({"extra": True}, {"run_attempt": 1.0}, {"run_attempt": 3}):
            assert not restore.is_known_external_link(name, "macos", dict(identity, **extra))


def test_real_upstream_core_overlay_and_domain_replay(tmp_path):
    assert (FIXTURE["chromium_version"], FIXTURE["core"], FIXTURE["mac"]) == gen.PINS157
    assert FIXTURE["devtools"] == "6f9a57d502c857e2412a57fe03c96286d7877479"
    for name, text in FIXTURE["pristine"].items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for entry in FIXTURE["upstream_patches"]:
        assert entry["path"] in FIXTURE["upstream_series"][entry["layer"]].splitlines()
        assert gen.sha256(entry["text"].encode()) == entry["sha256"]
        patch = tmp_path / "upstream.patch"
        patch.write_text(entry["text"])
        result = subprocess.run([shutil.which("gpatch") or "patch", *arp.PATCH_OPTIONS, "--input", str(patch)],
                                cwd=tmp_path, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not re.search(r"fuzz|FAILED", result.stdout + result.stderr, re.I)
    for name in FIXTURE["domain_targets"]:
        path = tmp_path / name
        text = path.read_text()
        for rule in FIXTURE["domain_regex"].splitlines():
            if rule:
                pattern, replacement = rule.split("#")
                text = re.sub(pattern, replacement, text)
        path.write_text(text)
    for name, text in FIXTURE["sources"].items():
        assert (tmp_path / gen.DEVTOOLS / name).read_text() == text
    for name, entry in FIXTURE["upstream_sources"].items():
        assert (tmp_path / name).read_text() == entry["text"]
        assert gen.sha256((tmp_path / name).read_bytes()) == entry["sha256"]
    runtime.repair_bindgen_wrapper(tmp_path, "arm64")
    assert gen.sha256((tmp_path / runtime.BINDGEN_WRAPPER).read_bytes()) == runtime.BINDGEN_REPAIRS[gen.PINS157[0]]["patched_sha256"]
    runtime.repair_bindgen_wrapper(tmp_path, "arm64")
    _, changes = gen.source_plan(tmp_path, gen.PINS157)
    for name, before, after, _ in changes:
        assert gen.sha256(after) == FIXTURE["repaired_sha256"][name.removeprefix(gen.DEVTOOLS + "/")]


@pytest.fixture
def generator_fixture():
    helper = generators.MacGeneratorsTest()
    helper.setUp()
    try:
        for name, text in FIXTURE["sources"].items():
            helper.write(gen.DEVTOOLS + "/" + name, text.encode())
        for name, entry in FIXTURE["upstream_sources"].items():
            helper.write(name, entry["text"].encode())
        helper.patch(platform_pins, "load_pins", return_value=dict(zip(
            ("ChromiumVersion", "UngoogledCommit", "UngoogledMacOSCommit"), gen.PINS157)))
        files = {"package.json": (b'{"name":"typescript","version":"6.0.2"}', 0o644),
                 "bin/tsc": (b"fixture compiler, never executed", 0o755)}
        archive = helper.tar(files)
        helper.patch(gen, "NODE_TYPESCRIPT157", dict(gen.NODE_TYPESCRIPT157,
            sha256=gen.sha256(archive), integrity=helper.integrity(archive), files=len(files), domain_repairs={},
            tree_sha256=gen.tree_digest([[n, m, gen.sha256(d)] for n, (d, m) in files.items()]),
            installed_tree_sha256=gen.tree_digest([[n, m, gen.sha256(d)] for n, (d, m) in files.items()])))
        helper.archives["node-typescript157"] = archive
        yield helper
    finally:
        helper.doCleanups()


@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_exact_sources_repair_idempotence_and_failed_probe(generator_fixture, arch):
    helper = generator_fixture
    helper.install(arch)
    gen.install_package(helper.src, "node-typescript157", gen.archive_files(helper.archives["node-typescript157"], "node-typescript157"))
    before = gen.prepare(helper.src, host_arch=arch, repo=helper.work)
    assert not before["install_needed"] and before["repair_needed"]
    with mock.patch.object(gen, "smoke", side_effect=ValueError("probe failed")), pytest.raises(ValueError, match="probe failed"):
        gen.prepare(helper.src, host_arch=arch, repo=helper.work, repair=True)
    assert gen.prepare(helper.src, host_arch=arch, repo=helper.work) == before
    with mock.patch.object(gen, "smoke", return_value={"fixture": "not a native probe"}):
        after = gen.prepare(helper.src, host_arch=arch, repo=helper.work, repair=True)
        assert not after["repair_needed"]
        assert gen.prepare(helper.src, host_arch=arch, repo=helper.work, repair=True) == after
    for name, digest in FIXTURE["repaired_sha256"].items():
        assert gen.sha256((helper.src / gen.DEVTOOLS / name).read_bytes()) == digest
    path = helper.src / "third_party/node/package-lock.json"
    path.write_bytes(path.read_bytes() + b"unknown")
    with pytest.raises(ValueError, match="unknown macOS157 Node TypeScript source"):
        gen.prepare(helper.src, host_arch=arch, repo=helper.work)


def test_node_typescript_install_only_missing_never_fallback(generator_fixture):
    helper = generator_fixture
    helper.install("arm64")
    helper.write(restore.MARKER, b'{"fixture":"receipt"}')
    downloads = helper.work / "downloads"
    downloads.mkdir()
    key = "node-typescript157"
    (downloads / "typescript-6.0.2.tgz").write_bytes(helper.archives[key])
    with mock.patch.object(restore, "verify_restored", return_value={"identity": "fixture"}), \
            mock.patch.object(prepare, "host_identity", return_value=("macos", "arm64")), \
            mock.patch.object(installer.downloads_api, "fetch_archive", side_effect=AssertionError("no network")), \
            mock.patch.object(installer, "ensure_archive", wraps=installer.ensure_archive) as ensure:
        result = installer.install(helper.work, "arm64", downloads, repo=helper.work)
        assert result["status"] == "installed"
        ensure.assert_called_once_with(downloads, key, download=False)
        (downloads / "typescript-6.0.2.tgz").unlink()
        ensure.reset_mock()
        assert installer.install(helper.work, "arm64", downloads, repo=helper.work)["status"] == "already_installed"
        ensure.assert_not_called()
        path = helper.src / gen.package_path(key) / "bin/tsc"
        path.write_bytes(b"wrong package")
        with pytest.raises(ValueError, match="unknown macOS generator package"):
            installer.install(helper.work, "arm64", downloads, repo=helper.work, download=True)
        ensure.assert_not_called()


@pytest.mark.parametrize("version", ["156.0.8078.79", "157.0.8103.78"])
@pytest.mark.parametrize("sdk,xcode,success", [("26.5", "26.5", True), ("26.5", "26.6", True),
                                            ("26.4", "26.5", False), ("26.5", "26.4", False)])
def test_sdk_265_gate_both_versions(version, sdk, xcode, success):
    helper = toolchain.MacOSToolchainTest()
    helper.setUp()
    try:
        helper.version.write_text(version + "\n")
        chosen = helper.add_xcode(sdk=sdk, xcode=xcode)
        helper.env["SDKROOT"] = "/stale.sdk"
        result = helper.run_helper()
        assert (result.returncode == 0) == success, result.stderr
        if success:
            assert f'SDKROOT={helper.config["xcodes"][str(chosen)]["path"]}\n' in helper.github_env.read_text()
        else:
            assert not helper.github_env.exists()
    finally:
        helper.doCleanups()


@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_attempt_endpoint_before_any_download(tmp_path, arch):
    client = mock.Mock()
    client.json.side_effect = [FIXTURE["run"], FIXTURE["artifacts"][arch]]
    client.download.side_effect = fetch.CacheMiss("metadata_verified")
    with mock.patch.object(fetch, "require_space"):
        result = fetch.fetch("macos", arch, tmp_path / "cache", root=ROOT, client=client)
    assert result["reason"] == "metadata_verified"
    assert client.json.call_args_list[0] == mock.call(
        "/repos/ungoogled-software/ungoogled-chromium-macos/actions/runs/43804136377/attempts/1")


@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_restore157_keeps_objects_and_exact_receipt(arch):
    helper = restores.RestoreUpstreamCacheTest()
    try:
        # The shared setup initializes Linux, whose exact 157 donor is unavailable.
        with mock.patch.object(helper, "make_cache"):
            helper.setUp()
        copy_linux_recipe(helper.repo)
        assert json.loads((helper.repo / "build/upstream-cache.json").read_text()) == json.loads(
            (ROOT / "build/upstream-cache.json").read_text())
        helper.make_cache("macos", arch)
        identity, _, _ = restore.identities(helper.repo, "macos", arch)
        assert identity["run_attempt"] == 1
        metadata = fetch.extract_tar(io.BytesIO(restores.archive_bytes(links.links())), helper.cache / "tree",
                                     fetch.SourceSelection(["src"], platform="macos"))
        helper.result.update(metadata)
        helper.write(helper.cache / "result.json", json.dumps(helper.result))
        result = helper.invoke()
        assert result["status"] == "hit", result
        assert (helper.work / "src/out/Default/obj/output.o").read_bytes() == b"object"
        assert helper.invoke("verify", cache=False)["receipt"] == result["receipt"]
    finally:
        helper.doCleanups()


def test_157_refuses_cold_source_before_selector(tmp_path):
    result = subprocess.run(["bash", str(ROOT / "build/prepare-ungoogled.sh"), str(tmp_path / "work"), "macos", "arm64"],
                            capture_output=True, text=True)
    assert result.returncode and "cold fallback forbidden" in result.stderr
    assert not (tmp_path / "work/tooling").exists()


def test_157_tool_versions_are_distinct_from_historical_webui_packages():
    assert gen.NODE_TYPESCRIPT157 == FIXTURE["node_typescript"]
    assert gen.NODE_TYPESCRIPT157["version"] == "6.0.2"
    assert gen.NODE_TYPESCRIPT157["sha256"] != gen.ARCHIVES["typescript"]["sha256"]
    assert "node-typescript157" not in gen.package_keys("arm64", gen.PINS156)
    assert "node-typescript157" in gen.package_keys("arm64", gen.PINS157)
    for name, entry in FIXTURE["tooling_files"].items():
        assert gen.sha256(entry["text"].encode()) == entry["sha256"]
    for key, entry in FIXTURE["verified_generator_packages"].items():
        assert all(gen.ARCHIVES[key][field] == value for field, value in entry.items())
    assert gen.SOURCE156_HASHES["scripts/build/typescript/typescript.gni"] != gen.SOURCE157_HASHES["scripts/build/typescript/typescript.gni"]


@pytest.mark.parametrize("pins", [("157.0.8103.79", *gen.PINS157[1:]),
                                  (gen.PINS157[0], gen.PINS156[1], gen.PINS157[2]),
                                  (gen.PINS157[0], gen.PINS157[1], gen.PINS156[2])])
def test_mixed_generator_pins_fail_closed(pins):
    with pytest.raises(ValueError, match="exact reviewed pins"):
        gen.source_repairs(pins)


@pytest.mark.parametrize("value", [None, True, 0, 1.0, "1"])
def test_mac157_manifest_requires_exact_integer_attempt(tmp_path, value):
    for name in ("CHROMIUM_VERSION", "CHROMIUM_LINUX_VERSION", "CHROMIUM_MACOS_VERSION",
                 "CHROMIUM_WINDOWS_VERSION", "build/ungoogled-revisions.psd1", "build/upstream-cache.json"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    copy_linux_recipe(tmp_path)
    path = tmp_path / "build/upstream-cache.json"
    manifest = json.loads(path.read_text())
    if value is None:
        manifest["sources"]["macos"].pop("run_attempt")
    else:
        manifest["sources"]["macos"]["run_attempt"] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(fetch.CacheMiss):
        fetch.load_manifest("macos", "arm64", root=tmp_path)


def test_node_domain_transform_checks_pre_post_and_installed_inventory(generator_fixture):
    helper = generator_fixture
    before = b"mozilla.org chromium.org"
    after = b"m0z111a.qjz9zk ch40m1um.qjz9zk"
    files = {"package.json": (b'{"name":"typescript","version":"6.0.2"}', 0o644),
             "lib/_tsc.js": (before, 0o644)}
    archive = helper.tar(files)
    records = lambda files: [[name, mode, gen.sha256(data)] for name, (data, mode) in files.items()]
    entry = dict(gen.NODE_TYPESCRIPT157, sha256=gen.sha256(archive), integrity=helper.integrity(archive),
                 files=len(files), tree_sha256=gen.tree_digest(records(files)),
                 installed_tree_sha256=gen.tree_digest(records(dict(files, **{"lib/_tsc.js": (after, 0o644)}))),
                 domain_repairs={"lib/_tsc.js": {"before": gen.sha256(before), "after": gen.sha256(after),
                     "replacements": [["mozilla.org", "m0z111a.qjz9zk"], ["chromium.org", "ch40m1um.qjz9zk"]]}})
    with mock.patch.object(gen, "NODE_TYPESCRIPT157", entry):
        transformed = gen.archive_files(archive, "node-typescript157")
        assert transformed["lib/_tsc.js"][0] == after
        gen.install_package(helper.src, "node-typescript157", transformed)
        assert gen.package_identity(helper.src, "node-typescript157")["tree_sha256"] == entry["installed_tree_sha256"]
        for field in ("before", "after"):
            with mock.patch.dict(entry["domain_repairs"]["lib/_tsc.js"], {field: "0" * 64}), pytest.raises(ValueError):
                gen.archive_files(archive, "node-typescript157")
        with mock.patch.dict(entry, {"installed_tree_sha256": "0" * 64}), pytest.raises(ValueError):
            gen.archive_files(archive, "node-typescript157")


def test_mac_node_resource_refresh_is_only_for_missing_or_foreign_native_tools():
    text = (ROOT / "build/posix/prepare-restored-tools.sh").read_text()
    assert 'if [ "$PLATFORM" = macos ] && { [ "$COMPILERS_NATIVE" != 1 ] || [ "$NODE_NATIVE" != 1 ]; }; then' in text
    assert 'if [ "$RETRIEVE" = 1 ]; then' in text
    assert 'retrieve_and_unpack_resource.py" -p "$RESOURCE_ARCH"' in text
    for arch in ("arm64", "x86-64"):
        config = configparser.ConfigParser()
        config.read_string(FIXTURE["tooling_files"][f"downloads-{arch}.ini"]["text"])
        assert config["nodejs"]["version"] == "24.12.0"
        assert len(config["nodejs"]["sha512"]) == 128
        assert config["llvm"]["version"] == "23.1.0"


def test_157_smoke_probes_both_typescript_compilers_without_path_search(generator_fixture):
    helper = generator_fixture
    helper.install("arm64")
    gen.install_package(helper.src, "node-typescript157", gen.archive_files(helper.archives["node-typescript157"], "node-typescript157"))
    paths = gen.native_paths("arm64")
    node = helper.write(paths["node"], helper.files["esbuild-arm64"]["bin/esbuild"][0])
    node.chmod(0o755)
    node_tsc = str(helper.src / gen.package_path("node-typescript157") / "bin/tsc")
    commands = []

    def run(command, **kwargs):
        assert kwargs["env"]["PATH"] == ""
        commands.append(command)
        if command[-1] == "--version":
            output = ("v24.12.0" if command == [str(node), "--version"] else
                      "0.28.2" if command[0].endswith("/bin/esbuild") else
                      "Version 6.0.2" if node_tsc in command else "Version 7.0.2")
        else:
            output = "42" if command[-1].endswith(".js") else ""
        return mock.Mock(returncode=0, stdout=output)

    with mock.patch.object(prepare, "host_identity", return_value=("macos", "arm64")), \
            mock.patch.object(gen.subprocess, "run", side_effect=run):
        probe = gen.smoke(helper.src, "arm64", pins=gen.PINS157)
    assert probe["node_typescript"] == "Version 6.0.2"
    assert probe["typescript_native"] == "Version 7.0.2"
    assert [str(node), node_tsc, "sample.ts", "--outDir", "webui-compiled", "--target", "es2023"] in commands


def test_node_package_domain_records_match_exact_core_rules():
    assert gen.sha256(FIXTURE["domain_regex"].encode()) == FIXTURE["domain_regex_sha256"]
    for entry in gen.NODE_TYPESCRIPT157["domain_repairs"].values():
        for original, substituted in entry["replacements"]:
            text = original
            for rule in FIXTURE["domain_regex"].splitlines():
                if rule:
                    pattern, replacement = rule.split("#")
                    text = re.sub(pattern, replacement, text)
            assert text == substituted
    package = json.loads(FIXTURE["upstream_sources"]["third_party/node/package-lock.json"]["text"])
    entry = package["packages"]["node_modules/typescript"]
    assert entry["version"] == gen.NODE_TYPESCRIPT157["version"]
    assert entry["resolved"] == gen.NODE_TYPESCRIPT157["url"]
    assert entry["integrity"] == gen.NODE_TYPESCRIPT157["integrity"]
