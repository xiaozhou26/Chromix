"""Small restored POSIX snapshots exercise real receipt validation and GNU patch."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import apply_restored_patches as arp  # noqa: E402
import migrate_restored_snapshot as migration  # noqa: E402
import restore_upstream_cache as restore  # noqa: E402
from platform_pins import load_shared_pins  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PATCH_BIN = shutil.which("gpatch") or shutil.which("patch")
pytestmark = pytest.mark.skipif(PATCH_BIN is None or shutil.which("git") is None,
                                reason="host GNU patch and Git are required")


@pytest.fixture(params=["macos", "linux"], autouse=True)
def platform_fixture(request, monkeypatch):
    monkeypatch.setattr(Fixture, "platform", request.param)


def put(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


def historical_runner(root, monkeypatch, revision):
    """Bind historical migrations to exact Git-pinned preparation files."""
    runner = root / "historical-runner"
    names = dict.fromkeys((*migration.SCRIPT_FILES, *migration.LINUX155_PRESERVED,
                          "tools/migrate_restored_snapshot.py"))
    for name in names:
        data = subprocess.run(["git", "-C", str(ROOT), "show", revision + ":" + name],
                              check=True, capture_output=True).stdout
        put(runner, name, data)
    script = runner / "tools/migrate_restored_snapshot.py"
    assert script.read_bytes() == Path(migration.__file__).read_bytes()
    monkeypatch.setattr(migration, "__file__", str(script))
    return runner


def patch(name="listed.txt", old="base example.com", new="old example.com"):
    return (f"diff --git a/{name} b/{name}\nindex 1111111..2222222 100644\n"
            f"--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-{old}\n+{new}\n").encode()


def create(name, text="created", mode="100644"):
    return (f"diff --git a/{name} b/{name}\nnew file mode {mode}\n"
            f"--- /dev/null\n+++ b/{name}\n@@ -0,0 +1 @@\n+{text}\n").encode()


def delete(name, text="base", mode="100644"):
    return (f"diff --git a/{name} b/{name}\ndeleted file mode {mode}\n"
            f"--- a/{name}\n+++ /dev/null\n@@ -1 +0,0 @@\n-{text}\n").encode()


def series(repo, *patches):
    names = []
    for number, data in enumerate(patches):
        name = f"patches/{number:04d}.patch"
        put(repo, name, data)
        names.append(name)
    put(repo, "patches/series", "\n".join(names) + "\n")


def snapshot(root):
    return {path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns,
                                               stat.S_IMODE(path.stat().st_mode))
            for path in root.rglob("*") if path.is_file()}


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


def commit(root):
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "core.hooksPath=" + os.devnull, "commit", "-qm", "fixture")
    return git(root, "rev-parse", "HEAD").decode().strip()


class Fixture:
    platform = "macos"

    def __init__(self, root, arch="x64", *, legacy=False, version=None):
        self.previous, self.repo, self.work = (root / name for name in ("previous", "current", "work"))
        self.src = self.work / "src"
        self.core = self.work / "tooling/ungoogled-chromium"
        platform_name = ("ungoogled-chromium-macos" if self.platform == "macos"
                         else "ungoogled-chromium-portablelinux")
        self.tooling = self.work / "tooling" / platform_name
        self.arch = arch
        version = version or ("152.0.7977.82" if legacy and self.platform == "linux" else "153.0.8010.36")
        put(self.core, "chromium_version.txt", version + "\n")
        put(self.core, "domain_regex.list", rb"example\.com#blocked.test" + b"\n")
        put(self.core, "domain_substitution.list", "listed.txt\nlite.txt\n")
        put(self.core, "utils/domain_substitution.py", "raise RuntimeError('do not execute')\n")
        put(self.tooling, "retrieve_and_unpack_resource.py", "raise RuntimeError('do not execute')\n")
        core_commit = commit(self.core)
        commit(self.tooling)
        git(self.tooling, "update-index", "--add", "--cacheinfo",
            f"160000,{core_commit},ungoogled-chromium")
        git(self.tooling, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "core.hooksPath=" + os.devnull, "commit", "-qm", "pin core submodule")
        platform_commit = git(self.tooling, "rev-parse", "HEAD").decode().strip()
        for repo in (self.previous, self.repo):
            for name in (*migration.PIN_FILES, *migration.SCRIPT_FILES):
                if (ROOT / name).is_file():
                    put(repo, name, (ROOT / name).read_bytes())
            if legacy:
                values = load_shared_pins(repo)
                for field in ("ChromiumVersion", "UngoogledVersion", "UngoogledCommit"):
                    values[field] = values["MacOS" + field]
                    values.pop("Linux" + field)
                if self.platform == "linux":
                    values["ChromiumVersion"] = version
                    values["UngoogledVersion"] = version + "-1"
                values["UngoogledLinuxVersion"] = values["UngoogledVersion"]
                put(repo, "CHROMIUM_VERSION", values["ChromiumVersion"] + "\n")
                put(repo, "CHROMIUM_WINDOWS_VERSION", values["WindowsChromiumVersion"] + "\n")
                put(repo, migration.PIN_FILES[1],
                    "@{\n" + "".join(f'  {key} = "{value}"\n' for key, value in values.items()) + "}\n")
                manifest = json.loads((repo / migration.PIN_FILES[2]).read_bytes())
                manifest.update(chromium_version=values["ChromiumVersion"], ungoogled_commit=values["UngoogledCommit"])
                put(repo, migration.PIN_FILES[2], json.dumps(manifest))
                (repo / "CHROMIUM_LINUX_VERSION").unlink(missing_ok=True)
            # Small synthetic stacks use legacy pins, not production override inventories.
            values = load_shared_pins(repo)
            fields, filename = migration.OVERRIDES[self.platform]
            if not legacy or self.platform != "linux":
                values[fields[0]] = version
                values[fields[1]] = version + "-1"
                put(repo, filename, version + "\n")
            platform_prefix = "UngoogledMacOS" if self.platform == "macos" else "UngoogledLinux"
            values[platform_prefix + "Version"] = version + "-1"
            put(repo, migration.PIN_FILES[1],
                "@{\n" + "".join(f'  {key} = "{value}"\n' for key, value in values.items()) + "}\n")
            pins = (repo / migration.PIN_FILES[1]).read_text()
            prefix = migration.OVERRIDES[self.platform][0][2]
            core_field = prefix if prefix in pins else "UngoogledCommit"
            platform_field = migration.PLATFORM_TOOLING[self.platform][1]
            for field, value in ((core_field, core_commit), (platform_field, platform_commit)):
                pins = re.sub(r'(?m)^(\s*' + field + r'\s*=\s*)"[^"\n]+"',
                              lambda match: match[1] + '"' + value + '"', pins)
            put(repo, migration.PIN_FILES[1], pins)
            manifest = json.loads((repo / migration.PIN_FILES[2]).read_bytes())
            target = manifest["sources"][self.platform]
            target.update(chromium_version=version, head_branch=version + "-1")
            if legacy:
                linux = manifest["sources"]["linux"]
                for field in ("chromium_version", "ungoogled_commit"):
                    linux.pop(field, None)
                linux["head_branch"] = manifest["chromium_version"] + "-1"
            if core_field != "UngoogledCommit":
                target["ungoogled_commit"] = core_commit
            else:
                manifest["ungoogled_commit"] = core_commit
                if "ungoogled_commit" in target:
                    target["ungoogled_commit"] = core_commit
            target["head_sha"] = platform_commit
            put(repo, migration.PIN_FILES[2], json.dumps(manifest))
            series(repo, patch())
        put(self.src, "listed.txt", "base blocked.test\n")
        version = migration.load_pins(self.repo, self.platform)["ChromiumVersion"]
        put(self.src, "chrome/VERSION", "".join(f"{key}={value}\n" for key, value in
            zip(("MAJOR", "MINOR", "BUILD", "PATCH"), version.split("."))))
        put(self.src, "BUILD.gn", 'group("fixture") {}\n')
        put(self.src, "out/Default/args.gn", f'target_cpu = "{arch}"\n')
        put(self.src, "out/Default/build.ninja", "# untouched graph\n")
        put(self.src, "out/Default/.ninja_log", "# ninja log v5\n")
        put(self.src, "out/Default/.ninja_deps", b"# ninjadeps\n\x04\x00\x00\x00")
        put(self.src, "out/Default/obj/cached.o", "cached object")
        self.receipt()

    def receipt(self):
        identity, _, manifest = restore.identities(self.previous, self.platform, self.arch)
        put(self.src, restore.MARKER, arp._json({
            "schema_version": 1, "owner": restore.OWNER, "status": "restored",
            "extraction_scope": restore.fetcher.SOURCE_SCOPE, "identity": identity,
            "manifest": manifest, "platform": self.platform, "arch": self.arch,
            "original_args": restore.source_args(self.src, identity), "external_symlink_paths": [],
        }))

    def prepare(self):
        arp.run_apply(self.src, self.previous, self.core, self.tooling, self.platform, PATCH_BIN)
        put(self.src, migration.READY, migration.source_ready_key(self.previous, self.platform, self.arch) + "\n")

    def run(self, **kwargs):
        return migration.migrate(self.work, self.previous, self.repo, self.platform, self.arch,
                                 patch_bin=PATCH_BIN, **kwargs)

    def check(self):
        assert arp.run_apply(self.src, self.repo, self.core, self.tooling, self.platform, PATCH_BIN,
                             check=True)["status"] == "checked"
        assert (self.src / migration.READY).read_text().strip() == migration.source_ready_key(
            self.repo, self.platform, self.arch)
        assert not (self.src / migration.TRANSACTION).exists()
        assert not (self.src / arp.IN_PROGRESS).exists()


def move_work(fx, destination):
    core = fx.core.relative_to(fx.work)
    tooling = fx.tooling.relative_to(fx.work)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fx.work.rename(destination)
    fx.work, fx.src = destination, destination / "src"
    fx.core, fx.tooling = destination / core, destination / tooling


def prepare_relocated(fx, root):
    old = root / "home/runner/work/_temp/chromix-build"
    move_work(fx, old)
    fx.prepare()
    marker = (fx.src / arp.MARKER).read_bytes()
    move_work(fx, root / "root/.grok/linux/x64-stage2/restored-checkpoint")
    assert (fx.src / arp.MARKER).read_bytes() == marker
    return old


@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_cross_host_relocation_checks_old_identity_without_rewriting_it(tmp_path, arch):
    fx = Fixture(tmp_path, arch)
    series(fx.previous, patch(), create("obsolete.txt"), delete("removed.txt"))
    series(fx.repo, patch(new="current example.com"), create("new.txt"))
    put(fx.src, "removed.txt", "base\n")
    old = prepare_relocated(fx, tmp_path)
    before = snapshot(fx.src)
    identity, patches, lite = arp._load(fx.previous, fx.core, fx.tooling, fx.platform)
    names = migration._names(patches, lite)
    with pytest.raises(arp.ApplyError, match="identities"):
        arp._completed(fx.src, identity, names)
    saved = json.loads(migration._previous_completed(fx.src, identity, names))
    assert saved["identity"]["regex"]["path"].startswith(str(old))
    assert saved["identity_sha256"] == arp._sha(arp._json(saved["identity"]))
    assert snapshot(fx.src) == before
    result = fx.run()
    assert result["changed_files"] == ["listed.txt", "new.txt", "obsolete.txt", "removed.txt"]
    assert (fx.src / "listed.txt").read_bytes() == b"current blocked.test\n"
    after = snapshot(fx.src)
    for name, value in before.items():
        if name.startswith("out/") or name == restore.MARKER:
            assert after[name] == value
    fx.check()


@pytest.mark.parametrize("change", [
    "hash", "missing-hash", "schema-type", "identity-type", "platform", "series", "series-path",
    "lite", "regex", "list", "extra", "field-extra", "relative", "traversal", "double-slash",
    "dot", "backslash", "wrong-suffix", "wrong-tooling", "split-root", "old-symlink",
    "current-symlink", "tooling-bytes", "tooling-mode", "outputs", "extra-output", "deleted-output",
    "source", "ready", "receipt",
])
def test_relocation_tamper_fails_before_any_write(tmp_path, change):
    fx = Fixture(tmp_path)
    series(fx.previous, patch(), delete("removed.txt"))
    put(fx.src, "removed.txt", "base\n")
    old = prepare_relocated(fx, tmp_path)
    saved = json.loads((fx.src / arp.MARKER).read_bytes())
    identity = saved["identity"]
    if change == "hash":
        saved["identity_sha256"] = "0" * 64
    elif change == "missing-hash":
        del saved["identity_sha256"]
    elif change == "schema-type":
        saved["schema_version"] = True
    elif change == "identity-type":
        identity["schema_version"] = True
    elif change == "platform":
        identity["platform"] = "windows"
    elif change in ("series", "lite", "regex", "list"):
        identity[change]["sha256"] = "0" * 64
    elif change == "series-path":
        identity["series"]["path"] = "other/series"
    elif change == "extra":
        identity["unrecognized"] = "value"
    elif change == "field-extra":
        identity["regex"]["unrecognized"] = "value"
    elif change == "relative":
        identity["regex"]["path"] = identity["regex"]["path"].lstrip("/")
    elif change in ("traversal", "double-slash", "dot", "backslash"):
        part = {"traversal": "/sub/../tooling/", "double-slash": "//tooling/",
                "dot": "/./tooling/", "backslash": "/sub\\dir/tooling/"}[change]
        identity["regex"]["path"] = identity["regex"]["path"].replace("/tooling/", part)
    elif change == "wrong-suffix":
        identity["regex"]["path"] = str(old / "tooling/ungoogled-chromium/other.list")
    elif change == "wrong-tooling":
        for field in ("regex", "list"):
            identity[field]["path"] = identity[field]["path"].replace("/tooling/", "/not-tooling/")
    elif change == "split-root":
        identity["list"]["path"] = str(old / "another/tooling/ungoogled-chromium/domain_substitution.list")
    elif change == "old-symlink":
        old.symlink_to(fx.work, target_is_directory=True)
    elif change == "current-symlink":
        path = fx.core / "domain_regex.list"
        outside = put(tmp_path, "regex.list", path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
    elif change == "tooling-bytes":
        put(fx.core, "domain_regex.list", "example#tampered\n")
    elif change == "tooling-mode":
        (fx.core / "domain_substitution.list").chmod(0o755)
    elif change == "outputs":
        del saved["outputs"]["listed.txt"]
    elif change == "extra-output":
        saved["outputs"]["unowned.txt"] = None
    elif change == "deleted-output":
        put(fx.src, "removed.txt", "unowned\n")
    elif change == "source":
        put(fx.src, "listed.txt", "tampered\n")
    elif change == "ready":
        put(fx.src, migration.READY, "tampered\n")
    elif change == "receipt":
        put(fx.src, restore.MARKER, "{}\n")
    if change not in ("hash", "missing-hash"):
        saved["identity_sha256"] = arp._sha(arp._json(identity))
    put(fx.src, arp.MARKER, arp._json(saved))
    before = snapshot(fx.src)
    with pytest.raises((arp.ApplyError, restore.Miss)):
        fx.run()
    assert snapshot(fx.src) == before
    assert not (fx.src / migration.TRANSACTION).exists()
    assert not (fx.src / arp.IN_PROGRESS).exists()


def test_relocation_real_patch_conflict_keeps_original_receipts_and_outputs(tmp_path):
    fx = Fixture(tmp_path)
    prepare_relocated(fx, tmp_path)
    series(fx.repo, patch(old="wrong base", new="current example.com"))
    before = snapshot(fx.src)
    with pytest.raises(arp.ApplyError, match="patch failed"):
        fx.run()
    after = snapshot(fx.src)
    for name, value in before.items():
        assert after[name] == value
    assert set(after) - set(before) == {migration.TRANSACTION, arp.IN_PROGRESS}
    with pytest.raises(arp.ApplyError, match="in-progress"):
        fx.run()


def test_relocation_marker_race_before_snapshot_is_rejected(tmp_path, monkeypatch):
    fx = Fixture(tmp_path)
    prepare_relocated(fx, tmp_path)
    take_snapshot = arp._snapshot

    def race(src, names):
        if src == fx.src:
            saved = json.loads((src / arp.MARKER).read_bytes())
            for field in ("regex", "list"):
                saved["identity"][field]["path"] = saved["identity"][field]["path"].replace(
                    "/chromix-build/", "/another-build/")
            saved["identity_sha256"] = arp._sha(arp._json(saved["identity"]))
            put(src, arp.MARKER, arp._json(saved))
        return take_snapshot(src, names)

    monkeypatch.setattr(arp, "_snapshot", race)
    with pytest.raises(arp.ApplyError, match="changed during validation"):
        fx.run()
    assert not (fx.src / migration.TRANSACTION).exists()
    assert not (fx.src / arp.IN_PROGRESS).exists()


@pytest.mark.parametrize("change", ["marker", "old-symlink", "tooling-mode", "ready", "receipt", "pins"])
def test_relocation_revalidates_before_publication(tmp_path, monkeypatch, change):
    fx = Fixture(tmp_path)
    old = prepare_relocated(fx, tmp_path)
    before = snapshot(fx.src)
    series(fx.repo, patch(new="current example.com"))
    apply = arp.run_apply

    def race(src, *args, **kwargs):
        result = apply(src, *args, **kwargs)
        if src != fx.src and kwargs.get("check"):
            if change == "marker":
                put(fx.src, arp.MARKER, (fx.src / arp.MARKER).read_bytes() + b"\n")
            elif change == "old-symlink":
                old.symlink_to(fx.work, target_is_directory=True)
            elif change == "tooling-mode":
                (fx.core / "domain_regex.list").chmod(0o755)
            elif change == "ready":
                put(fx.src, migration.READY, "concurrent\n")
            elif change == "receipt":
                put(fx.src, restore.MARKER, "{}\n")
            else:
                put(fx.repo, "CHROMIUM_VERSION", "0.0.0.0\n")
        return result

    monkeypatch.setattr(arp, "run_apply", race)
    with pytest.raises((arp.ApplyError, restore.Miss)):
        fx.run()
    after = snapshot(fx.src)
    for name, value in before.items():
        if name not in (arp.MARKER, migration.READY, restore.MARKER):
            assert after[name] == value
    assert (fx.src / migration.TRANSACTION).is_file()
    assert (fx.src / arp.IN_PROGRESS).is_file()


def test_untracked_regex_list_are_not_pinned_tooling(tmp_path):
    fx = Fixture(tmp_path)
    old_core = git(fx.core, "rev-parse", "HEAD").decode().strip()
    old_platform = git(fx.tooling, "rev-parse", "HEAD").decode().strip()
    git(fx.core, "rm", "--cached", "domain_regex.list")
    git(fx.core, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "core.hooksPath=" + os.devnull, "commit", "-qm", "untrack regex")
    new_core = git(fx.core, "rev-parse", "HEAD").decode().strip()
    git(fx.tooling, "update-index", "--cacheinfo", f"160000,{new_core},ungoogled-chromium")
    git(fx.tooling, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "core.hooksPath=" + os.devnull, "commit", "-qm", "pin new core")
    new_platform = git(fx.tooling, "rev-parse", "HEAD").decode().strip()
    for root in (fx.previous, fx.repo):
        for name in ("build/ungoogled-revisions.psd1", "build/upstream-cache.json"):
            put(root, name, (root / name).read_text().replace(old_core, new_core).replace(old_platform, new_platform))
    fx.receipt()
    prepare_relocated(fx, tmp_path)
    before = snapshot(fx.src)
    with pytest.raises(arp.ApplyError, match="missing from pinned tooling"):
        fx.run()
    assert snapshot(fx.src) == before


@pytest.mark.parametrize("tamper", [False, True])
def test_independent_trusted_runner_uses_copied_sources_without_monkeypatch(tmp_path, tamper):
    fx = Fixture(tmp_path)
    prepare_relocated(fx, tmp_path)
    series(fx.repo, patch(new="current example.com"))
    runner = tmp_path / "trusted-runner"
    modules = ("migrate_restored_snapshot", "apply_restored_patches", "restore_upstream_cache",
               "fetch_upstream_cache", "import_upstream_cache", "upstream_object_cache",
               "upstream_script_identity", "platform_pins")
    files = {*("tools/" + name + ".py" for name in modules), *migration.SCRIPT_FILES}
    origins = {}
    for name in sorted(files):
        raw = (ROOT / name).read_bytes()
        put(runner, name, raw)
        origins[name] = {"source": str(ROOT / name), "sha256": arp._sha(raw)}
        assert arp._sha((runner / name).read_bytes()) == origins[name]["sha256"]
    if tamper:
        put(runner, "build/prepare-ungoogled.sh", "different trusted script\n")
    before = snapshot(fx.src)
    candidate = snapshot(fx.repo)
    result = subprocess.run([
        sys.executable, "-E", "-s", "-B", str(runner / "tools/migrate_restored_snapshot.py"),
        "--workdir", str(fx.work), "--previous-repo", str(fx.previous), "--repo", str(fx.repo),
        "--platform", fx.platform, "--arch", fx.arch, "--patch-bin", PATCH_BIN,
    ], cwd=runner, text=True, capture_output=True, check=False)
    assert snapshot(fx.repo) == candidate
    if tamper:
        assert result.returncode == 1
        assert "running trusted checkout" in result.stderr
        assert snapshot(fx.src) == before
    else:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["changed_files"] == ["listed.txt"]
        fx.check()


def test_linux_version_file_is_a_platform_specific_migration_input(tmp_path):
    fx = Fixture(tmp_path, legacy=True)
    fx.prepare()
    before = snapshot(fx.src)
    put(fx.repo, "CHROMIUM_LINUX_VERSION", "153.0.8010.36\n")
    if fx.platform == "linux":
        with pytest.raises(arp.ApplyError, match="CHROMIUM_LINUX_VERSION"):
            fx.run()
        assert snapshot(fx.src) == before
    else:
        migration._same_inputs(fx.previous, fx.repo, "macos")
        # The complete manifest still rejects an incomplete Linux pin set.
        with pytest.raises(restore.importer.Miss, match="pin_mismatch"):
            fx.run()
        assert snapshot(fx.src) == before
        fx.check()


@pytest.mark.parametrize("change", ["different", "missing", "symlink"])
def test_linux_version_file_change_or_missing_side_fails_closed(tmp_path, change):
    fx = Fixture(tmp_path)
    name = "CHROMIUM_LINUX_VERSION"
    for root in (fx.previous, fx.repo):
        put(root, name, "153.0.8010.36\n")
    if change == "different":
        put(fx.repo, name, "154.0.0.0\n")
    else:
        (fx.repo / name).unlink()
        if change == "symlink":
            (fx.repo / name).symlink_to(fx.previous / name)
    with pytest.raises(arp.ApplyError):
        migration._same_inputs(fx.previous, fx.repo, "linux")
    migration._same_inputs(fx.previous, fx.repo, "macos")


@pytest.mark.parametrize("change", ["different", "missing", "symlink"])
def test_macos_version_file_is_a_platform_specific_migration_input(tmp_path, change):
    fx = Fixture(tmp_path)
    name = "CHROMIUM_MACOS_VERSION"
    if change == "different":
        put(fx.repo, name, "153.0.8010.47\n")
    else:
        (fx.repo / name).unlink()
        if change == "symlink":
            (fx.repo / name).symlink_to(fx.previous / name)
    with pytest.raises(arp.ApplyError):
        migration._same_inputs(fx.previous, fx.repo, "macos")
    migration._same_inputs(fx.previous, fx.repo, "linux")


def test_absent_linux_version_file_on_both_sides_keeps_legacy_comparison(tmp_path):
    fx = Fixture(tmp_path, legacy=True)
    migration._same_inputs(fx.previous, fx.repo, fx.platform)
    fx.prepare()
    assert fx.run()["changed_files"] == []
    fx.check()


def test_linux_effective_core_is_used_for_key_and_tooling_verification(tmp_path, monkeypatch):
    monkeypatch.setattr(Fixture, "platform", "linux")
    fx = Fixture(tmp_path)
    current = migration.load_pins(fx.repo, "linux")
    for root in (fx.previous, fx.repo):
        values = load_shared_pins(root)
        values.update(LinuxChromiumVersion="153.0.8010.36",
                      LinuxUngoogledVersion="153.0.8010.36-1",
                      LinuxUngoogledCommit=current["UngoogledCommit"],
                      UngoogledLinuxVersion="153.0.8010.36-1",
                      UngoogledCommit="a" * 40)
        put(root, "build/ungoogled-revisions.psd1",
            "@{\n" + "".join(f'  {key} = "{value}"\n' for key, value in values.items()) + "}\n")
        put(root, "CHROMIUM_LINUX_VERSION", "153.0.8010.36\n")
        manifest = json.loads((root / "build/upstream-cache.json").read_bytes())
        manifest["ungoogled_commit"] = "a" * 40
        manifest["sources"]["linux"].update(
            chromium_version="153.0.8010.36", ungoogled_commit=current["UngoogledCommit"],
            head_branch="153.0.8010.36-1")
        put(root, "build/upstream-cache.json", json.dumps(manifest))
    put(fx.src, "chrome/VERSION", "MAJOR=153\nMINOR=0\nBUILD=8010\nPATCH=36\n")
    fx.receipt()
    fx.prepare()
    assert migration.source_ready_key(fx.repo, "linux", fx.arch).split("|")[2:4] == [
        "153.0.8010.36", current["UngoogledCommit"]]
    assert fx.run()["changed_files"] == []
    fx.check()


def test_migration_never_upgrades_a_152_donor_to_153(tmp_path, monkeypatch):
    monkeypatch.setattr(Fixture, "platform", "linux")
    fx = Fixture(tmp_path, legacy=True)
    fx.prepare()
    before = snapshot(fx.src)
    put(fx.repo, "CHROMIUM_LINUX_VERSION", "153.0.8010.36\n")
    with pytest.raises(arp.ApplyError, match="identical pins"):
        fx.run()
    assert snapshot(fx.src) == before


@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_changed_patch_new_file_shared_patches_and_actual_source_updates(tmp_path, arch):
    fx = Fixture(tmp_path, arch)
    series(fx.previous, patch(), patch(old="old example.com", new="older example.com"),
           create("obsolete.txt"), delete("restore.txt"))
    series(fx.repo, patch(new="middle example.com"),
           patch(old="middle example.com", new="current example.com"), create("new/file.txt"),
           delete("remove.txt"))
    put(fx.src, "restore.txt", "base\n")
    put(fx.src, "remove.txt", "base\n")
    fx.prepare()
    before = snapshot(fx.src)
    result = fx.run()
    assert result["changed_files"] == ["listed.txt", "new/file.txt", "obsolete.txt", "remove.txt", "restore.txt"]
    assert (fx.src / "listed.txt").read_bytes() == b"current blocked.test\n"
    assert (fx.src / "new/file.txt").read_bytes() == b"created\n"
    assert (fx.src / "restore.txt").read_bytes() == b"base\n"
    assert not (fx.src / "obsolete.txt").exists()
    assert not (fx.src / "remove.txt").exists()
    assert (fx.src / "listed.txt").stat().st_mtime_ns > before["listed.txt"][1]
    after = snapshot(fx.src)
    for name in before:
        if name not in result["changed_files"] and name not in (arp.MARKER, migration.READY):
            assert after[name] == before[name]
    marker = json.loads((fx.src / arp.MARKER).read_bytes())
    assert set(marker["outputs"]) == {"listed.txt", "new/file.txt", "remove.txt"}
    assert marker["outputs"]["remove.txt"] is None
    fx.check()


def test_changed_series_identical_output_preserves_mtimes_modes_and_objects(tmp_path):
    fx = Fixture(tmp_path)
    for repo in (fx.previous, fx.repo):
        put(repo, arp.LITE + "/lite.txt", "base example.com\n")
    series(fx.previous, patch(), patch("lite.txt"))
    series(fx.repo, patch(new="temporary example.com"),
           patch(old="temporary example.com", new="old example.com"), patch("lite.txt"))
    fx.prepare()
    source = fx.src / "listed.txt"
    source.chmod(0o444)
    future = source.stat().st_mtime_ns + 10_000_000_000
    os.utime(source, ns=(future, future))
    before = snapshot(fx.src)
    result = fx.run()
    assert result["changed_files"] == []
    after = snapshot(fx.src)
    for name, value in before.items():
        if name not in (arp.MARKER, migration.READY):
            assert after[name] == value
    fx.check()


@pytest.mark.parametrize("change", ["dirty", "missing", "old-key", "old-patch", "old-pins",
                                     "current-pins", "receipt", "manifest", "missing-manifest", "tooling", "tooling-head",
                                     "scripts", "arch"])
def test_invalid_old_snapshot_fails_without_modification(tmp_path, change):
    fx = Fixture(tmp_path)
    fx.prepare()
    if change == "dirty":
        put(fx.src, "listed.txt", "dirty\n")
    elif change == "missing":
        (fx.src / "listed.txt").unlink()
    elif change == "old-key":
        put(fx.src, migration.READY, "wrong\n")
    elif change == "old-patch":
        series(fx.previous, patch(new="tampered example.com"))
    elif change in ("old-pins", "current-pins"):
        put(fx.previous if change == "old-pins" else fx.repo, "CHROMIUM_VERSION", "0.0.0.0\n")
    elif change == "receipt":
        value = json.loads((fx.src / restore.MARKER).read_bytes())
        value["identity"]["artifact_digest"] = "sha256:" + "0" * 64
        put(fx.src, restore.MARKER, arp._json(value))
    elif change == "manifest":
        value = json.loads((fx.src / arp.MARKER).read_bytes())
        value["outputs"] = {}
        put(fx.src, arp.MARKER, arp._json(value))
    elif change == "missing-manifest":
        (fx.src / arp.MARKER).unlink()
    elif change == "tooling":
        put(fx.core, "domain_regex.list", "changed#bad\n")
    elif change == "tooling-head":
        put(fx.tooling, "new-data", "changed")
        commit(fx.tooling)
    elif change == "scripts":
        put(fx.repo, "build/prepare-ungoogled.sh", "do not execute\n")
    else:
        fx.arch = "arm64"
    before = snapshot(fx.src)
    with pytest.raises((arp.ApplyError, restore.Miss)):
        fx.run()
    assert snapshot(fx.src) == before


@pytest.mark.parametrize("change", ["content", "add", "delete", "mode"])
def test_unsupported_lite_changes_rejected_before_writes(tmp_path, change):
    fx = Fixture(tmp_path)
    for repo in (fx.previous, fx.repo):
        put(repo, arp.LITE + "/lite.txt", "lite example.com\n")
    fx.prepare()
    if change == "content":
        put(fx.repo, arp.LITE + "/lite.txt", "different\n")
    elif change == "add":
        put(fx.repo, arp.LITE + "/added.txt", "new\n")
    elif change == "delete":
        (fx.repo / arp.LITE / "lite.txt").unlink()
    else:
        (fx.repo / arp.LITE / "lite.txt").chmod(0o755)
    before = snapshot(fx.src)
    with pytest.raises(arp.ApplyError, match="unsupported lite"):
        fx.run()
    assert snapshot(fx.src) == before


@pytest.mark.parametrize("failure", ["reverse", "apply", "publish", "ready", "final-check"])
def test_failure_remains_blocked_and_cannot_resume(tmp_path, monkeypatch, failure):
    fx = Fixture(tmp_path)
    fx.prepare()
    series(fx.repo, patch(new="current example.com"), create("new.txt"))
    before = snapshot(fx.src)
    if failure in ("reverse", "apply"):
        run = migration.subprocess.run

        def fail_patch(command, **kwargs):
            if ("chromix-snapshot-migration-" in str(kwargs.get("cwd", ""))
                    and ("--reverse" in command) == (failure == "reverse")):
                return subprocess.CompletedProcess(command, 1, b"injected patch failure")
            if (failure == "apply" and "--input" in command
                    and "chromix-restored-patches-" in str(kwargs.get("cwd", ""))):
                return subprocess.CompletedProcess(command, 1, b"injected patch failure")
            return run(command, **kwargs)

        monkeypatch.setattr(migration.subprocess, "run", fail_patch)
    elif failure in ("publish", "ready"):
        write = arp._atomic_write

        def fail_write(path, *args, **kwargs):
            if path.is_relative_to(fx.src) and path.name == ("new.txt" if failure == "publish" else migration.READY):
                assert (fx.src / migration.TRANSACTION).is_file()
                assert (fx.src / arp.IN_PROGRESS).is_file()
                raise OSError("injected publish failure")
            return write(path, *args, **kwargs)

        monkeypatch.setattr(arp, "_atomic_write", fail_write)
    else:
        apply = arp.run_apply

        def fail_check(src, *args, **kwargs):
            if src == fx.src and kwargs.get("check"):
                assert (fx.src / migration.TRANSACTION).is_file()
                raise arp.ApplyError("injected final-check failure")
            return apply(src, *args, **kwargs)

        monkeypatch.setattr(arp, "run_apply", fail_check)
    with pytest.raises((OSError, arp.ApplyError), match="failure|failed"):
        fx.run()
    assert (fx.src / migration.TRANSACTION).is_file()
    with pytest.raises(arp.ApplyError, match="in-progress"):
        fx.run()
    after = snapshot(fx.src)
    for name, value in before.items():
        if name.startswith("out/") or failure in ("reverse", "apply"):
            assert after[name] == value
    if failure in ("publish", "ready", "final-check"):
        assert (fx.src / "listed.txt").read_bytes() == b"current blocked.test\n"
    assert not list(fx.src.rglob("*.rej"))
    assert not list(fx.src.rglob("*.orig"))


def test_new_creation_collision_is_not_overwritten(tmp_path):
    fx = Fixture(tmp_path)
    fx.prepare()
    series(fx.repo, patch(), create("new.txt"))
    put(fx.src, "new.txt", "unowned\n")
    before = snapshot(fx.src)
    with pytest.raises(arp.ApplyError, match="already exists"):
        fx.run()
    for name, value in before.items():
        assert snapshot(fx.src)[name] == value
    assert (fx.src / migration.TRANSACTION).exists()


def test_key_matches_shell_hash_recipe_without_executing_previous_scripts(tmp_path):
    fx = Fixture(tmp_path)
    put(fx.previous, arp.LITE + "/b/second", "payload2")
    put(fx.previous, arp.LITE + "/a/first", "payload1")
    paths = [fx.previous / name for name in ("build/prepare-ungoogled.sh", "build/apply-patches.sh", "patches/series")]
    paths.extend(fx.previous / line for line in (fx.previous / "patches/series").read_text().splitlines())
    paths.extend(sorted((fx.previous / arp.LITE).rglob("*")))
    digest = hashlib.sha256()
    for path in paths:
        if path.is_file():
            digest.update(str(path.relative_to(fx.previous)).encode())
            digest.update(path.read_bytes())
    assert migration.source_ready_key(fx.previous, fx.platform, fx.arch) == "|".join((
        fx.platform, fx.arch, migration.load_pins(fx.previous, fx.platform)["ChromiumVersion"],
        git(fx.core, "rev-parse", "HEAD").decode().strip(),
        git(fx.tooling, "rev-parse", "HEAD").decode().strip(), digest.hexdigest()))


def test_only_host_git_and_patch_execute_and_staging_is_external(tmp_path, monkeypatch):
    fx = Fixture(tmp_path)
    fx.prepare()
    series(fx.repo, patch(new="current example.com"))
    real_run = migration.subprocess.run
    commands = []

    def capture(command, **kwargs):
        program = Path(command[0]).resolve() if Path(command[0]).is_absolute() else Path(shutil.which(command[0])).resolve()
        assert not any(program.is_relative_to(root) for root in (fx.work, fx.previous, fx.repo))
        assert program.name in ("git", "patch", "gpatch")
        if "--input" in command:
            patch_file = Path(command[-1])
            assert not any(patch_file.is_relative_to(root) for root in (fx.work, fx.previous, fx.repo))
        commands.append(command)
        return real_run(command, **kwargs)

    monkeypatch.setattr(migration.subprocess, "run", capture)
    fx.run()
    assert any("--reverse" in command for command in commands)
    assert all("diff" not in command for command in commands)
    fx.check()


@pytest.mark.parametrize("location", ["previous", "work"])
def test_rejects_donor_patch_executable_before_execution(tmp_path, location):
    fx = Fixture(tmp_path)
    fx.prepare()
    program = put(getattr(fx, location), "fake-patch", "#!/bin/sh\nexit 99\n")
    program.chmod(0o755)
    before = snapshot(fx.src)
    with pytest.raises(arp.ApplyError, match="donor code"):
        migration.migrate(fx.work, fx.previous, fx.repo, fx.platform, fx.arch, patch_bin=str(program))
    assert snapshot(fx.src) == before


def test_cli_requires_explicit_arguments_and_runs_migration(tmp_path):
    fx = Fixture(tmp_path)
    fx.prepare()
    series(fx.repo, patch(new="current example.com"))
    result = subprocess.run([sys.executable, migration.__file__, "--workdir", str(fx.work),
                             "--previous-repo", str(fx.previous), "--repo", str(fx.repo),
                             "--platform", fx.platform, "--arch", fx.arch, "--patch-bin", PATCH_BIN],
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["changed_files"] == ["listed.txt"]
    fx.check()


def test_future_source_mtime_advances_for_changed_content(tmp_path):
    fx = Fixture(tmp_path)
    fx.prepare()
    series(fx.repo, patch(new="current example.com"))
    source = fx.src / "listed.txt"
    before = source.stat().st_mtime_ns + 60_000_000_000
    os.utime(source, ns=(before, before))
    fx.run()
    assert source.stat().st_mtime_ns >= before + 1_000_000_000


def test_create_followed_by_modify_reverses_and_preserves_identical_output(tmp_path):
    fx = Fixture(tmp_path)
    for repo in (fx.previous, fx.repo):
        series(repo, create("added.txt", "first", "100755"),
               patch("added.txt", "first", "second"), patch())
    fx.prepare()
    before = snapshot(fx.src)
    assert fx.run()["changed_files"] == []
    assert snapshot(fx.src)["added.txt"] == before["added.txt"]
    assert (fx.src / "added.txt").stat().st_mode & stat.S_IXUSR
    fx.check()


def test_wrong_core_submodule_pin_is_rejected_before_writes(tmp_path):
    fx = Fixture(tmp_path)
    old = git(fx.tooling, "rev-parse", "HEAD").decode().strip()
    git(fx.tooling, "update-index", "--cacheinfo", "160000," + "1" * 40 + ",ungoogled-chromium")
    git(fx.tooling, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "core.hooksPath=" + os.devnull, "commit", "-qm", "wrong core pin")
    new = git(fx.tooling, "rev-parse", "HEAD").decode().strip()
    for repo in (fx.previous, fx.repo):
        for name in ("build/ungoogled-revisions.psd1", "build/upstream-cache.json"):
            put(repo, name, (repo / name).read_text().replace(old, new))
    fx.receipt()
    fx.prepare()
    before = snapshot(fx.src)
    with pytest.raises(arp.ApplyError, match="unsupported pinned tooling entry"):
        fx.run()
    assert snapshot(fx.src) == before


def test_pinned_tooling_never_runs_configured_git_filters(tmp_path):
    fx = Fixture(tmp_path)
    sentinel = tmp_path / "executed"
    put(fx.core, ".gitattributes", "*.list filter=evil diff=evil\n")
    old = git(fx.core, "rev-parse", "HEAD").decode().strip()
    new = commit(fx.core)
    old_platform = git(fx.tooling, "rev-parse", "HEAD").decode().strip()
    git(fx.tooling, "update-index", "--cacheinfo", f"160000,{new},ungoogled-chromium")
    git(fx.tooling, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "core.hooksPath=" + os.devnull, "commit", "-qm", "update core pin")
    new_platform = git(fx.tooling, "rev-parse", "HEAD").decode().strip()
    for repo in (fx.previous, fx.repo):
        for name in ("build/ungoogled-revisions.psd1", "build/upstream-cache.json"):
            put(repo, name, (repo / name).read_text().replace(old, new).replace(old_platform, new_platform))
    for key in ("filter.evil.clean", "diff.evil.command", "diff.evil.textconv", "core.fsmonitor"):
        git(fx.core, "config", key, f"touch {sentinel}; exit 1")
    fx.receipt()
    fx.prepare()
    fx.run()
    assert not sentinel.exists()


@pytest.mark.parametrize("kind", ["source-symlink", "old-marker-symlink", "object-patch", "temp-root"])
def test_unsafe_paths_and_object_cache_targets_fail_before_writes(tmp_path, monkeypatch, kind):
    fx = Fixture(tmp_path)
    fx.prepare()
    if kind == "source-symlink":
        source = fx.src / "listed.txt"
        outside = put(tmp_path, "outside", source.read_bytes())
        source.unlink()
        source.symlink_to(outside)
    elif kind == "old-marker-symlink":
        marker = fx.src / arp.MARKER
        outside = put(tmp_path, "outside", marker.read_bytes())
        marker.unlink()
        marker.symlink_to(outside)
    elif kind == "object-patch":
        series(fx.repo, patch(), create("out/Default/new.o"))
    else:
        monkeypatch.setattr(migration.tempfile, "gettempdir", lambda: str(fx.work))
    before = snapshot(fx.src)
    with pytest.raises(arp.ApplyError, match="symlink|object cache|temporary directory"):
        fx.run()
    assert snapshot(fx.src) == before


def test_concurrent_source_change_during_staging_is_blocked(tmp_path, monkeypatch):
    fx = Fixture(tmp_path)
    fx.prepare()
    series(fx.repo, patch(new="current example.com"))
    run = arp.run_apply

    def change_source(src, *args, **kwargs):
        result = run(src, *args, **kwargs)
        if src != fx.src and not kwargs.get("check"):
            put(fx.src, "listed.txt", "concurrent edit\n")
        return result

    monkeypatch.setattr(arp, "run_apply", change_source)
    with pytest.raises(arp.ApplyError, match="changed concurrently"):
        fx.run()
    assert (fx.src / "listed.txt").read_bytes() == b"concurrent edit\n"
    assert (fx.src / migration.TRANSACTION).is_file()


def test_existing_partial_migration_marker_is_never_removed(tmp_path):
    fx = Fixture(tmp_path)
    fx.prepare()
    put(fx.src, migration.TRANSACTION, "interrupted\n")
    before = snapshot(fx.src)
    with pytest.raises(arp.ApplyError, match="in-progress"):
        fx.run()
    assert snapshot(fx.src) == before


def test_real_ninja_rebuilds_only_changed_dependency(tmp_path):
    ninja = shutil.which("ninja")
    if not ninja:
        pytest.skip("Ninja is required")
    fx = Fixture(tmp_path)
    series(fx.previous, patch(), patch("unchanged.txt", "base", "same"))
    series(fx.repo, patch(new="current example.com"), patch("unchanged.txt", "base", "same"))
    put(fx.src, "unchanged.txt", "base\n")
    out = fx.src / "out/Default"
    put(out, "build.ninja", "rule copy\n  command = cp $in $out\n"
        "build changed.o: copy ../../listed.txt\n"
        "build unchanged.o: copy ../../unchanged.txt\n"
        "default changed.o unchanged.o\n")
    fx.prepare()
    for name in ("listed.txt", "unchanged.txt"):
        os.utime(fx.src / name, ns=(1_700_000_000_000_000_000,) * 2)
    subprocess.run([ninja, "-C", str(out)], check=True, capture_output=True)
    cached = snapshot(out)
    fx.run()
    assert snapshot(out) == cached
    plan = subprocess.run([ninja, "-C", str(out), "-n"], check=True, capture_output=True, text=True)
    assert "cp ../../listed.txt changed.o" in plan.stdout
    assert "unchanged.o" not in plan.stdout


def test_same_but_unproven_lite_post_reverse_content_is_rejected(tmp_path):
    fx = Fixture(tmp_path)
    for repo in (fx.previous, fx.repo):
        put(repo, arp.LITE + "/lite.txt", "base example.com\n")
    fx.prepare()
    put(fx.src, "lite.txt", "unproven\n")
    marker = json.loads((fx.src / arp.MARKER).read_bytes())
    marker["outputs"]["lite.txt"] = arp._sha(b"unproven\n")
    put(fx.src, arp.MARKER, arp._json(marker))
    before = snapshot(fx.src)
    with pytest.raises(arp.ApplyError, match="lite content cannot be proven"):
        fx.run()
    for name, value in before.items():
        assert snapshot(fx.src)[name] == value
    assert (fx.src / migration.TRANSACTION).is_file()


@pytest.mark.parametrize("mode", ["100644", "100755"])
def test_reverse_old_delete_restore_preserves_declared_mode(tmp_path, mode):
    fx = Fixture(tmp_path)
    series(fx.previous, patch(), delete("removed.sh", "base", mode))
    series(fx.repo, patch())
    put(fx.src, "removed.sh", "base\n").chmod(int(mode, 8) & 0o777)
    fx.prepare()
    result = fx.run()
    assert result["changed_files"] == ["removed.sh"]
    assert stat.S_IMODE((fx.src / "removed.sh").stat().st_mode) == int(mode, 8) & 0o777
    fx.check()


@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_version_is_staged_without_becoming_a_patch_output_and_survives_rollback(tmp_path, monkeypatch, arch):
    fx = Fixture(tmp_path, arch)
    fx.prepare()
    series(fx.repo, patch(new="current example.com"))
    version = arp._snapshot(fx.src, {migration.SOURCE_VERSION})
    cached = snapshot(fx.src / "out")
    apply = arp.run_apply
    staged = []

    def check_version(src, *args, **kwargs):
        if src != fx.src:
            assert arp._read(src, migration.SOURCE_VERSION) == version[migration.SOURCE_VERSION][0]
            assert arp.patch_selection.source_version(src) == migration.load_pins(fx.repo, fx.platform)["ChromiumVersion"]
            staged.append(src)
        return apply(src, *args, **kwargs)

    monkeypatch.setattr(arp, "run_apply", check_version)
    assert fx.run()["changed_files"] == ["listed.txt"]
    assert staged
    assert migration.SOURCE_VERSION not in json.loads((fx.src / arp.MARKER).read_bytes())["outputs"]
    fx.check()
    fx.previous, fx.repo = fx.repo, fx.previous
    assert fx.run()["changed_files"] == ["listed.txt"]
    assert (fx.src / "listed.txt").read_bytes() == b"old blocked.test\n"
    migration._unchanged(fx.src, version)
    assert snapshot(fx.src / "out") == cached
    fx.check()


def test_macos152_selector_runs_with_authenticated_staged_version(tmp_path, monkeypatch):
    monkeypatch.setattr(Fixture, "platform", "macos")
    selection = arp.patch_selection
    fx = Fixture(tmp_path, "arm64", version=selection.MACOS152_VERSION)
    base = patch()
    replacement = patch(new="selected example.com")
    monkeypatch.setattr(selection, "MACOS152_CORE", git(fx.core, "rev-parse", "HEAD").decode().strip())
    monkeypatch.setattr(selection, "MACOS152_PLATFORM", git(fx.tooling, "rev-parse", "HEAD").decode().strip())
    monkeypatch.setattr(selection, "MACOS152_OVERRIDES", {
        "0000.patch": (selection.digest(base), selection.digest(replacement)),
    })
    for repo, text in ((fx.previous, "old"), (fx.repo, "current")):
        pins = (repo / migration.PIN_FILES[1]).read_text().replace(
            'UngoogledMacOSVersion = "152.0.7977.82-1"',
            'UngoogledMacOSVersion = "152.0.7977.82-1.1"')
        put(repo, migration.PIN_FILES[1], pins)
        manifest = json.loads((repo / migration.PIN_FILES[2]).read_bytes())
        manifest["sources"]["macos"]["head_branch"] = selection.MACOS152_VERSION
        put(repo, migration.PIN_FILES[2], json.dumps(manifest))
        series(repo, base, create("revision.txt", text), *(create(f"file-{i}.txt") for i in range(222)))
        put(repo, selection.MACOS152_OVERRIDE_ROOT + "/0000.patch", replacement)
    fx.receipt()
    fx.prepare()
    before = arp._snapshot(fx.src, {migration.SOURCE_VERSION})
    assert fx.run()["changed_files"] == ["revision.txt"]
    assert (fx.src / "listed.txt").read_bytes() == b"selected blocked.test\n"
    assert json.loads((fx.src / arp.MARKER).read_bytes())["identity"]["selection"]["version"] == selection.MACOS152_VERSION
    migration._unchanged(fx.src, before)
    fx.check()


@pytest.mark.parametrize("change", ["missing", "wrong", "malformed", "duplicate", "symlink", "parent-symlink"])
def test_source_version_must_be_real_and_match_pins_before_writes(tmp_path, change):
    fx = Fixture(tmp_path)
    fx.prepare()
    path = fx.src / migration.SOURCE_VERSION
    raw = path.read_bytes()
    if change == "missing":
        path.unlink()
    elif change == "wrong":
        path.write_bytes(b"MAJOR=0\nMINOR=0\nBUILD=0\nPATCH=0\n")
    elif change == "malformed":
        path.write_bytes(raw + b"unexpected\n")
    elif change == "duplicate":
        path.write_bytes(raw + b"MAJOR=153\n")
    elif change == "symlink":
        outside = put(tmp_path, "version", raw)
        path.unlink()
        path.symlink_to(outside)
    else:
        outside = tmp_path / "chrome"
        path.parent.rename(outside)
        path.parent.symlink_to(outside, target_is_directory=True)
    before = snapshot(fx.src)
    with pytest.raises((arp.ApplyError, restore.Miss)):
        fx.run()
    assert snapshot(fx.src) == before
    assert not (fx.src / migration.TRANSACTION).exists()
    assert not (fx.src / arp.IN_PROGRESS).exists()


@pytest.mark.parametrize("location", ["source", "stage"])
@pytest.mark.parametrize("change", ["bytes", "mode", "replace", "rewrite", "missing", "symlink"])
def test_version_tamper_during_staging_blocks_publication(tmp_path, monkeypatch, location, change):
    fx = Fixture(tmp_path)
    fx.prepare()
    series(fx.repo, patch(new="current example.com"))
    before = snapshot(fx.src)
    apply = arp.run_apply

    def tamper(src, *args, **kwargs):
        result = apply(src, *args, **kwargs)
        if src != fx.src and kwargs.get("check"):
            path = (fx.src if location == "source" else src) / migration.SOURCE_VERSION
            raw, info = path.read_bytes(), path.stat()
            if change == "bytes":
                path.write_bytes(b"\n".join(reversed(raw.splitlines())) + b"\n")
            elif change == "mode":
                path.chmod(0o755)
            elif change == "replace":
                replacement = path.with_name("replacement")
                replacement.write_bytes(raw)
                os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns))
                replacement.replace(path)
            elif change == "rewrite":
                path.write_bytes(raw)
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
            else:
                path.unlink()
                if change == "symlink":
                    path.symlink_to(put(tmp_path, "version", raw))
        return result

    monkeypatch.setattr(arp, "run_apply", tamper)
    with pytest.raises((arp.ApplyError, restore.Miss)):
        fx.run()
    after = snapshot(fx.src)
    for name, value in before.items():
        if name != migration.SOURCE_VERSION:
            assert after[name] == value
    assert (fx.src / migration.TRANSACTION).is_file()
    assert (fx.src / arp.IN_PROGRESS).is_file()
    with pytest.raises(arp.ApplyError, match="in-progress"):
        fx.run()


@pytest.mark.parametrize("when", ["snapshot", "final-check"])
def test_same_version_byte_change_cannot_escape_validation_windows(tmp_path, monkeypatch, when):
    fx = Fixture(tmp_path)
    fx.prepare()
    before = snapshot(fx.src)
    take_snapshot, apply = arp._snapshot, arp.run_apply

    def tamper():
        path = fx.src / migration.SOURCE_VERSION
        path.write_bytes(b"\n".join(reversed(path.read_bytes().splitlines())) + b"\n")

    def race_snapshot(src, names):
        result = take_snapshot(src, names)
        if src == fx.src:
            tamper()
        return result

    def race_check(src, *args, **kwargs):
        result = apply(src, *args, **kwargs)
        if src == fx.src and kwargs.get("check"):
            tamper()
        return result

    monkeypatch.setattr(arp, "_snapshot" if when == "snapshot" else "run_apply",
                        race_snapshot if when == "snapshot" else race_check)
    with pytest.raises(arp.ApplyError, match="changed concurrently: chrome/VERSION"):
        fx.run()
    if when == "snapshot":
        assert not (fx.src / migration.TRANSACTION).exists()
        assert not (fx.src / arp.IN_PROGRESS).exists()
        for name, value in before.items():
            if name != migration.SOURCE_VERSION:
                assert snapshot(fx.src)[name] == value
    else:
        assert (fx.src / migration.TRANSACTION).is_file()


@pytest.mark.parametrize("owner", ["previous", "current", "lite"])
def test_version_is_never_a_mutable_patch_or_lite_target(tmp_path, owner):
    fx = Fixture(tmp_path)
    raw = arp._read(fx.src, migration.SOURCE_VERSION)
    reordered = b"\n".join(reversed(raw.splitlines())) + b"\n"
    version_patch = (b"diff --git a/chrome/VERSION b/chrome/VERSION\n"
                     b"--- a/chrome/VERSION\n+++ b/chrome/VERSION\n@@ -1,4 +1,4 @@\n"
                     + b"".join(b"-" + line + b"\n" for line in raw.splitlines())
                     + b"".join(b"+" + line + b"\n" for line in reordered.splitlines()))
    if owner == "previous":
        series(fx.previous, patch(), version_patch)
    elif owner == "current":
        series(fx.repo, patch(), version_patch)
    else:
        for repo in (fx.previous, fx.repo):
            put(repo, arp.LITE + "/" + migration.SOURCE_VERSION, raw)
    fx.prepare()
    before = snapshot(fx.src)
    with pytest.raises(arp.ApplyError, match="immutable chrome/VERSION"):
        fx.run()
    assert snapshot(fx.src) == before
