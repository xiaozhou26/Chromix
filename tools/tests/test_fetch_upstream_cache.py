import copy
import errno
import hashlib
import http.client
import io
import json
import os
import shutil
import stat
import struct
import subprocess
import tarfile
import tempfile
import unittest
import urllib.error
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from socket import create_connection
from unittest import mock

from tools import fetch_upstream_cache as cache

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
MTIME = 1700000000123456700


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def metadata(pin):
    repo = {"full_name": pin["repository"], "id": pin["repository_id"], "private": False}
    run = {key: pin[key] for key in ("head_sha", "head_branch", "event")}
    run.update(id=pin["run_id"], path=pin["workflow_path"], status="completed", conclusion="success",
               repository=repo.copy(), head_repository=repo.copy())
    artifact = {**pin["artifact"], "expired": False, "expires_at": "2099-01-01T00:00:00Z",
                "workflow_run": {"id": pin["run_id"], "head_sha": pin["head_sha"],
                                 "head_branch": pin["head_branch"],
                                 "repository_id": pin["repository_id"],
                                 "head_repository_id": pin["repository_id"]}}
    return run, artifact


def tar_bytes(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, kind, data in entries:
            info = tarfile.TarInfo(name)
            info.mode = 0o755 if kind in ("file", "dir") else 0o777
            info.pax_headers = {"mtime": "1700000000.123456700"}
            info.type = {"dir": tarfile.DIRTYPE, "sym": tarfile.SYMTYPE,
                         "hard": tarfile.LNKTYPE, "file": tarfile.REGTYPE,
                         "fifo": tarfile.FIFOTYPE}[kind]
            if kind in ("sym", "hard"):
                info.linkname = data
            if kind == "file":
                info.size = len(data)
            archive.addfile(info, io.BytesIO(data) if kind == "file" else None)
    return output.getvalue()


def zip_bytes(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, kind, data in entries:
            info = zipfile.ZipInfo(name + ("/" if kind == "dir" else ""))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = {"file": stat.S_IFREG | 0o755, "dir": stat.S_IFDIR | 0o755,
                                  "sym": stat.S_IFLNK | 0o777}[kind] << 16
            ntfs = struct.pack("<IHHQQQ", 0, 1, 24, MTIME // 100 + 116444736000000000, 0, 0)
            info.extra = struct.pack("<HH", 10, len(ntfs)) + ntfs
            archive.writestr(info, data)
    return output.getvalue()


def synthetic_windows_source(repo):
    """Return a test-only donor; it is not an upstream artifact claim."""
    pins = cache.load_pins(repo, "windows")
    payload = zip_bytes([("artifacts.zip", "file", zip_bytes([]))])
    return {
        "chromium_version": pins["ChromiumVersion"], "ungoogled_commit": pins["UngoogledCommit"],
        "repository": "ungoogled-software/ungoogled-chromium-windows", "repository_id": 177210827,
        "head_sha": pins["UngoogledWindowsCommit"], "head_branch": pins["UngoogledWindowsVersion"],
        "event": "push", "workflow_path": ".github/workflows/build-x64.yml",
        "run_id": 101, "source_roots": ["src", "build/src"],
        "artifacts": {"x64": {
            "id": 102, "name": "synthetic-windows-donor", "size_in_bytes": len(payload),
            "digest": digest(payload), "expires_at": "2099-01-01T00:00:00Z",
            "inner_archive": "artifacts.zip",
        }},
    }


def windows153_source(repo):
    """Configure a test-only Windows153 target independently of production pins."""
    pins = cache.load_shared_pins(repo)
    pins.update(WindowsChromiumVersion="153.0.8010.36", WindowsUngoogledVersion="153.0.8010.36-1",
                WindowsUngoogledCommit="e" * 40, UngoogledWindowsVersion="153.0.8010.36-1.1",
                UngoogledWindowsCommit="f" * 40)
    (repo / "build/ungoogled-revisions.psd1").write_text(
        "@{\n" + "".join(f'  {key} = "{value}"\n' for key, value in pins.items()) + "}\n")
    (repo / "CHROMIUM_WINDOWS_VERSION").write_text("153.0.8010.36\n")
    return synthetic_windows_source(repo)


def unavailable_source(repo, target, source):
    pins = cache.load_pins(repo, target)
    return {key: value for key, value in dict(
        source, available=False, chromium_version=pins["ChromiumVersion"],
        ungoogled_commit=pins["UngoogledCommit"]).items()
        if key not in ("run_id", "artifacts")}


class Response(io.BytesIO):
    status = 200


class ScriptedResponse(Response):
    def __init__(self, chunks, *, status=200, headers=None, clock=None, elapsed=0):
        super().__init__()
        self.chunks = iter(chunks)
        self.status = status
        self.headers = headers or {}
        self.clock = clock
        self.elapsed = elapsed

    def read(self, size=-1):
        if self.closed:
            raise ValueError("read from closed response")
        if self.clock is not None:
            self.clock[0] += self.elapsed
        chunk = next(self.chunks, b"")
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk


class FetchUpstreamCacheTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "build").mkdir()
        for name in ("CHROMIUM_VERSION", "CHROMIUM_LINUX_VERSION", "CHROMIUM_MACOS_VERSION", "CHROMIUM_WINDOWS_VERSION",
                     "build/ungoogled-revisions.psd1", "build/upstream-cache.json"):
            if (cache.ROOT / name).is_file():
                shutil.copyfile(cache.ROOT / name, self.root / name)
        self.manifest = json.loads((self.root / "build/upstream-cache.json").read_text())
        self.manifest["sources"]["windows"] = synthetic_windows_source(self.root)
        self.save_manifest()
        self.destination = self.root / "cache"
        self.pin, self.identity = cache.load_manifest("windows", "x64", root=self.root)
        disk = mock.patch.object(cache.shutil, "disk_usage", return_value=mock.Mock(free=1024**4))
        self.disk_usage = disk.start()
        self.addCleanup(disk.stop)

    def save_manifest(self):
        (self.root / "build/upstream-cache.json").write_text(json.dumps(self.manifest))

    def fixture_client(self, platform="windows", arch="x64", source="src", extra=()):
        version = cache.load_pins(self.root, platform)["ChromiumVersion"]
        version_bytes = "".join(f"{key}={value}\n" for key, value in zip(
            ("MAJOR", "MINOR", "BUILD", "PATCH"), version.split("."))).encode()
        entries = [(source, "dir", b""), (source + "/BUILD.gn", "file", b"build"),
                   (source + "/chrome/VERSION", "file", version_bytes),
                   (source + "/chrome/browser/file.cc", "file", b"source"),
                   (source + "/out/Default/args.gn", "file", b"target_cpu=\"x64\""),
                   (source + "/out/Default/build.ninja", "file", b"build-ninja"),
                   (source + "/out/Default/.ninja_log", "file", b"ninja-log"),
                   (source + "/out/Default/.ninja_deps", "file", b"ninja-deps"),
                   (source + "/out/Default/obj/file.o", "file", b"object"),
                   (source + "/out/Default/gen/generated.h", "file", b"generated"),
                   ("build/download_cache/package.tar.xz", "file", b"excluded"), *extra]
        if platform == "windows":
            inner = zip_bytes(entries)
        else:
            inner = subprocess.check_output([shutil.which("zstd"), "-q", "-c"], input=tar_bytes(entries))
        artifact = self.manifest["sources"][platform]["artifacts"][arch]
        outer = zip_bytes([(artifact["inner_archive"], "file", inner)])
        artifact["digest"], artifact["size_in_bytes"] = digest(outer), len(outer)
        self.save_manifest()
        pin, _ = cache.load_manifest(platform, arch, root=self.root)
        run, artifact = metadata(pin)
        client = cache.GitHub("fixture-token")
        client.json = mock.Mock(side_effect=[run, artifact])
        client.open = mock.Mock(side_effect=lambda *a, **kw: Response(outer))
        return client

    def linux_overrides(self):
        pins = cache.load_shared_pins(self.root)
        pins.update(LinuxChromiumVersion="153.0.8010.36",
                    LinuxUngoogledVersion="153.0.8010.36-1",
                    LinuxUngoogledCommit="e" * 40,
                    UngoogledLinuxVersion="153.0.8010.36-1", UngoogledLinuxCommit="f" * 40)
        (self.root / "build/ungoogled-revisions.psd1").write_text(
            "@{\n" + "".join(f'  {key} = "{value}"\n' for key, value in pins.items()) + "}\n")
        (self.root / "CHROMIUM_LINUX_VERSION").write_text("153.0.8010.36\n")
        self.manifest["sources"]["linux"].update(
            chromium_version="153.0.8010.36", ungoogled_commit="e" * 40,
            head_sha="f" * 40, head_branch="153.0.8010.36-1")
        self.save_manifest()

    def test_linux_source_overrides_preserve_schema_and_nonlinux_identity(self):
        before = {target: cache.load_manifest(target, "x64", root=self.root)
                  for target in ("macos", "windows")}
        global_identity = {key: self.manifest[key] for key in ("chromium_version", "ungoogled_commit")}
        sources = copy.deepcopy(self.manifest["sources"])
        self.linux_overrides()
        for arch in ("x64", "arm64"):
            pin, identity = cache.load_manifest("linux", arch, root=self.root)
            self.assertEqual(pin["chromium_version"], "153.0.8010.36")
            self.assertEqual(pin["ungoogled_commit"], "e" * 40)
            self.assertEqual(identity["chromium_version"], "153.0.8010.36")
            self.assertEqual(identity["schema_version"], 1)
        for key, value in global_identity.items():
            self.assertEqual(self.manifest[key], value)
        for target, (old_pin, old_identity) in before.items():
            self.assertEqual(self.manifest["sources"][target], sources[target])
            pin, identity = cache.load_manifest(target, "x64", root=self.root)
            self.assertEqual(pin, old_pin)
            self.assertEqual({key: value for key, value in identity.items() if key != "sha256"},
                             {key: value for key, value in old_identity.items() if key != "sha256"})

    def test_source_override_mismatches_are_rejected(self):
        self.linux_overrides()
        for target in ("linux", "macos", "windows"):
            for field in ("chromium_version", "ungoogled_commit"):
                with self.subTest(target=target, field=field):
                    changed = copy.deepcopy(self.manifest)
                    changed["sources"][target][field] = "wrong"
                    (self.root / "build/upstream-cache.json").write_text(json.dumps(changed))
                    with self.assertRaisesRegex(cache.CacheMiss, "pin_mismatch"):
                        cache.load_manifest(target, "x64", root=self.root)
        self.save_manifest()

    def test_linux_manifest_fallback_requires_matching_shared_identity(self):
        self.linux_overrides()
        for field in ("chromium_version", "ungoogled_commit"):
            with self.subTest(field=field):
                saved = self.manifest["sources"]["linux"].pop(field)
                self.save_manifest()
                if saved == self.manifest[field]:
                    cache.load_manifest("linux", "x64", root=self.root)
                else:
                    with self.assertRaisesRegex(cache.CacheMiss, "pin_mismatch"):
                        cache.load_manifest("linux", "x64", root=self.root)
                self.manifest["sources"]["linux"][field] = saved
        self.save_manifest()

    def test_each_source_validates_its_platform_version_file(self):
        self.linux_overrides()
        for value in ("invalid\n", None):
            path = self.root / "CHROMIUM_LINUX_VERSION"
            if value is None:
                path.unlink()
            else:
                path.write_text(value)
            for target in cache.SOURCES:
                with self.subTest(value=value, target=target):
                    with self.assertRaisesRegex(cache.CacheMiss, "pin_mismatch"):
                        cache.load_manifest(target, "x64", root=self.root)

    def test_checked_in_windows_cache_matches_exact_production_metadata(self):
        manifest = json.loads(cache.MANIFEST.read_text())
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["chromium_version"], "153.0.8010.36")
        self.assertEqual(manifest["ungoogled_commit"], "dd8fb9b5c837982faf41ba58cd30a5664e77c329")
        source = manifest["sources"]["windows"]
        x64_source = copy.deepcopy(source)
        x64_source["artifacts"].pop("arm64")
        self.assertEqual(x64_source, {
            "chromium_version": "153.0.8010.47",
            "ungoogled_commit": "31e6f2dd3bb2f113800d25ae359f024684addb51",
            "repository": "ungoogled-software/ungoogled-chromium-windows",
            "repository_id": 177210827,
            "head_sha": "657b9731b68aae35d4ee02428684ab8bdceb9181",
            "head_branch": "153.0.8010.47-1.1",
            "event": "push", "workflow_path": ".github/workflows/build-x64.yml",
            "run_id": 35059013905, "source_roots": ["src", "build/src"],
            "artifacts": {"x64": {
                "id": 10523508661, "name": "build-artifact", "size_in_bytes": 15713545950,
                "digest": "sha256:d5ae2b64ba9f819613482a9321107946b60b8d44bc2adad13ee9206c9dab238c",
                "expires_at": "2026-09-21T22:53:13Z", "inner_archive": "artifacts.zip",
            }},
        })
        pin, identity = cache.load_manifest("windows", "x64", 35059013905, root=cache.ROOT)
        self.assertEqual(pin, {**{key: value for key, value in source.items() if key != "artifacts"},
                               "artifact": source["artifacts"]["x64"]})
        self.assertEqual({key: identity[key] for key in (
            "chromium_version", "head_sha", "run_id", "artifact_id", "artifact_digest")}, {
            "chromium_version": "153.0.8010.47", "head_sha": source["head_sha"],
            "run_id": 35059013905, "artifact_id": 10523508661,
            "artifact_digest": source["artifacts"]["x64"]["digest"],
        })
        self.assert_metadata_provenance(pin, datetime(2026, 9, 19, tzinfo=timezone.utc))
        for arch, run_id, reason in (("arm64", 35059013905, "run_id_mismatch"),
                                     ("x64", 35059013950, "run_id_mismatch"),
                                     ("x64", 34806882978, "run_id_mismatch"),
                                     ("x64", 33898278106, "run_id_mismatch")):
            with self.subTest(arch=arch, run_id=run_id):
                client = mock.Mock()
                result = cache.fetch("windows", arch, self.destination, run_id, root=cache.ROOT, client=client)
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result["download_bytes"], 0)
                self.assertEqual(client.mock_calls, [])

    def test_macos_identity_requires_explicit_152_source_under_shared_153(self):
        pin, _ = cache.load_manifest("macos", "x64", root=self.root)
        self.assertEqual(pin["chromium_version"], "152.0.7977.82")
        for field in ("chromium_version", "ungoogled_commit"):
            value = self.manifest["sources"]["macos"].pop(field)
            self.save_manifest()
            for target in cache.SOURCES:
                with self.subTest(field=field, target=target), self.assertRaisesRegex(cache.CacheMiss, "pin_mismatch"):
                    cache.load_manifest(target, "x64", root=self.root)
            self.manifest["sources"]["macos"][field] = value
            self.save_manifest()

    def test_manifest_shared_identity_cannot_follow_macos_override(self):
        self.manifest.update(chromium_version="152.0.7977.82",
                             ungoogled_commit="e71b91c6e336d0f25cfc6b9ef09298a9d2506e24")
        self.save_manifest()
        for target in cache.SOURCES:
            with self.subTest(target=target), self.assertRaisesRegex(cache.CacheMiss, "pin_mismatch"):
                cache.load_manifest(target, "x64", root=self.root)

    def test_disabled_windows_fixture_refuses_network_before_donor_validation(self):
        manifest = self.manifest
        manifest["sources"]["windows"] = unavailable_source(
            self.root, "windows", manifest["sources"]["windows"])
        self.save_manifest()
        source = manifest["sources"]["windows"]
        pins = cache.load_pins(self.root, "windows")
        self.assertIs(source["available"], False)
        self.assertEqual(set(source), cache.UNAVAILABLE_SOURCE_FIELDS)
        self.assertEqual(source["chromium_version"], pins["ChromiumVersion"])
        self.assertEqual(source["ungoogled_commit"], pins["UngoogledCommit"])
        self.assertEqual(source["head_sha"], pins["UngoogledWindowsCommit"])
        self.assertEqual(source["head_branch"], pins["UngoogledWindowsVersion"])
        self.assertEqual(manifest["chromium_version"], "153.0.8010.36")
        for arch in ("x64", "arm64"):
            for run_id in (None, 34806882978, 33898278106):
                with self.subTest(arch=arch, run_id=run_id):
                    client = mock.Mock()
                    result = cache.fetch("windows", arch, self.destination, run_id, root=self.root, client=client)
                    self.assertEqual(result["reason"], "source_unavailable")
                    self.assertEqual(result["status"], "miss")
                    self.assertIsNone(result["source"])
                    self.assertEqual([result[key] for key in
                                      ("download_bytes", "inner_bytes", "extracted_bytes")], [0, 0, 0])
                    self.assertEqual(result["manifest"]["chromium_version"], pins["ChromiumVersion"])
                    self.assertIs(result["manifest"]["available"], False)
                    self.assertNotIn("run_id", result["manifest"])
                    self.assertNotIn("artifact_id", result["manifest"])
                    self.assertEqual(list(self.destination.iterdir()), [self.destination / "result.json"])
                    self.assertEqual(json.loads((self.destination / "result.json").read_text()), result)
                    self.assertEqual(client.mock_calls, [])
        for target, version in (("linux", "153.0.8010.36"), ("macos", "152.0.7977.82")):
            for arch in ("x64", "arm64"):
                self.assertEqual(cache.load_manifest(target, arch)[0]["chromium_version"], version)

    def test_disabled_source_cannot_reuse_previous_verified_hit(self):
        client = self.fixture_client()
        self.assertEqual(cache.fetch("windows", "x64", self.destination, root=self.root,
                                     client=client)["status"], "hit")
        self.manifest["sources"]["windows"] = unavailable_source(
            self.root, "windows", self.manifest["sources"]["windows"])
        self.save_manifest()
        client = mock.Mock()
        result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["reason"], "source_unavailable")
        self.assertEqual(result["download_bytes"], 0)
        self.assertFalse((self.destination / "tree").exists())
        self.assertEqual(client.mock_calls, [])

    def test_disabled_sources_are_platform_scoped_and_legacy_sources_still_validate(self):
        original = copy.deepcopy(self.manifest)
        for disabled in cache.SOURCES:
            self.manifest = copy.deepcopy(original)
            self.manifest["sources"][disabled] = unavailable_source(
                self.root, disabled, self.manifest["sources"][disabled])
            self.save_manifest()
            for target in cache.SOURCES:
                with self.subTest(disabled=disabled, target=target):
                    if target == disabled:
                        for arch in ("x64", "arm64"):
                            with self.assertRaisesRegex(cache.CacheMiss, "source_unavailable"):
                                cache.load_manifest(target, arch, root=self.root)
                    else:
                        pin, _ = cache.load_manifest(target, "x64", root=self.root)
                        self.assertEqual(pin["head_sha"], original["sources"][target]["head_sha"])

    def test_disabled_source_requires_exact_fields_before_any_target_is_selected(self):
        original = copy.deepcopy(self.manifest)
        for disabled in cache.SOURCES:
            source = unavailable_source(self.root, disabled, original["sources"][disabled])
            variants = [{key: value for key, value in source.items() if key != missing}
                        for missing in source]
            variants += [dict(source, **{key: value}) for key, value in (
                ("run_id", None), ("run_id", 101), ("artifacts", {}), ("artifacts", None),
                ("artifact", {}), ("reason", "not provided"), ("repository_id", True),
                ("available", None), ("available", 0), ("available", 1),
                ("available", "false"), ("available", []), ("available", True))]
            for index, changed in enumerate(variants):
                self.manifest = copy.deepcopy(original)
                self.manifest["sources"][disabled] = changed
                self.save_manifest()
                for target in cache.SOURCES:
                    with self.subTest(disabled=disabled, variant=index, target=target):
                        with self.assertRaises(cache.CacheMiss) as caught:
                            cache.load_manifest(target, "x64", root=self.root)
                        self.assertNotEqual(str(caught.exception), "source_unavailable")

    def test_disabled_source_cannot_hide_cross_platform_or_old_identity(self):
        original = copy.deepcopy(self.manifest)
        for disabled in cache.SOURCES:
            source = unavailable_source(self.root, disabled, original["sources"][disabled])
            variants = [dict(source, **{key: value}) for key, value in (
                ("chromium_version", "151.0.0.0"), ("ungoogled_commit", "0" * 40),
                ("head_sha", "0" * 40), ("head_branch", "151.0.0.0-1"),
                ("event", "pull_request"), ("workflow_path", "wrong.yml"),
                ("source_roots", ["wrong"])) if source[key] != value]
            variants += [unavailable_source(self.root, other, original["sources"][other])
                         for other in cache.SOURCES if other != disabled]
            for index, changed in enumerate(variants):
                self.manifest = copy.deepcopy(original)
                self.manifest["sources"][disabled] = changed
                self.save_manifest()
                for target in cache.SOURCES:
                    with self.subTest(disabled=disabled, variant=index, target=target):
                        with self.assertRaises(cache.CacheMiss) as caught:
                            cache.load_manifest(target, "x64", root=self.root)
                        self.assertNotEqual(str(caught.exception), "source_unavailable")

    def test_available_true_and_legacy_absent_flag_share_artifact_identity(self):
        for target in cache.SOURCES:
            self.assertNotIn("available", self.manifest["sources"][target])
            old_pin, old_identity = cache.load_manifest(target, "x64", root=self.root)
            self.manifest["sources"][target]["available"] = True
            self.save_manifest()
            pin, identity = cache.load_manifest(target, "x64", root=self.root)
            self.assertIs(pin.pop("available"), True)
            self.assertEqual(pin, old_pin)
            self.assertEqual({k: v for k, v in identity.items() if k != "sha256"},
                             {k: v for k, v in old_identity.items() if k != "sha256"})

    @unittest.skipUnless(shutil.which("zstd"), "host zstd required")
    def test_linux_and_macos_can_fetch_with_windows_disabled(self):
        self.manifest["sources"]["windows"] = unavailable_source(
            self.root, "windows", self.manifest["sources"]["windows"])
        self.save_manifest()
        for platform in ("linux", "macos"):
            for arch in ("x64", "arm64"):
                with self.subTest(platform=platform, arch=arch):
                    source = self.manifest["sources"][platform]["source_roots"][0]
                    client = self.fixture_client(platform, arch, source)
                    result = cache.fetch(platform, arch, self.destination, root=self.root, client=client)
                    self.assertEqual(result["status"], "hit", result)
                    self.assertGreater(result["download_bytes"], 0)

    def test_all_five_pins_match_and_windows_arm64_misses(self):
        for platform, arch in (("linux", "x64"), ("linux", "arm64"), ("macos", "x64"),
                               ("macos", "arm64"), ("windows", "x64")):
            pin, identity = cache.load_manifest(platform, arch, root=self.root)
            self.assertEqual(identity["artifact_digest"], pin["artifact"]["digest"])
        with self.assertRaisesRegex(cache.CacheMiss, "unsupported_target"):
            cache.load_manifest("windows", "arm64", root=self.root)

    def test_pin_mismatches_fail_before_network(self):
        for target in ("linux", "macos", "windows"):
            with self.subTest(target=target):
                saved = self.manifest["sources"][target]["head_sha"]
                self.manifest["sources"][target]["head_sha"] = "0" * 40
                self.save_manifest()
                with self.assertRaisesRegex(cache.CacheMiss, "pin_mismatch"):
                    cache.load_manifest("windows", "x64", root=self.root)
                self.manifest["sources"][target]["head_sha"] = saved
        self.save_manifest()
        (self.root / "CHROMIUM_VERSION").write_text("153.0.0.0")
        client = mock.Mock()
        result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["reason"], "pin_mismatch")
        self.assertIn("sha256", result["manifest"])
        client.json.assert_not_called()

    def test_manifest_trust_and_missing_digest(self):
        for key, value in (("repository", "attacker/repo"), ("head_branch", "untrusted"),
                           ("event", "pull_request"), ("workflow_path", "other.yml")):
            with self.subTest(key=key):
                changed = copy.deepcopy(self.manifest)
                changed["sources"]["windows"][key] = value
                (self.root / "build/upstream-cache.json").write_text(json.dumps(changed))
                with self.assertRaises(cache.CacheMiss):
                    cache.load_manifest("windows", "x64", root=self.root)
        self.manifest["sources"]["windows"]["artifacts"]["x64"]["digest"] = None
        self.save_manifest()
        with self.assertRaisesRegex(cache.CacheMiss, "missing_pinned_digest"):
            cache.load_manifest("windows", "x64", root=self.root)

    def test_manual_run_id_must_match(self):
        cache.load_manifest("windows", "x64", self.pin["run_id"], self.root)
        result = cache.fetch("windows", "x64", self.destination, self.pin["run_id"] + 1, self.root)
        self.assertEqual(result["reason"], "run_id_mismatch")
        self.assertEqual(result["status"], "miss")

    def test_linux_donor_requires_completed_success_before_download(self):
        for arch in ("x64", "arm64"):
            pin, _ = cache.load_manifest("linux", arch, root=self.root)
            for status, conclusion in (("queued", None), ("in_progress", None),
                                       ("completed", "failure"), ("completed", "cancelled")):
                with self.subTest(arch=arch, status=status, conclusion=conclusion):
                    run, artifact = metadata(pin)
                    run.update(status=status, conclusion=conclusion)
                    client = mock.Mock()
                    client.json.side_effect = [run, artifact]
                    with mock.patch.object(cache.shutil, "which", return_value="/usr/bin/zstd"):
                        result = cache.fetch("linux", arch, self.destination, root=self.root, client=client)
                    self.assertEqual(result["status"], "miss")
                    self.assertEqual(result["reason"], "untrusted_run")
                    self.assertEqual(result["manifest"]["chromium_version"], "153.0.8010.36")
                    self.assertEqual(result["download_bytes"], 0)
                    self.assertIsNone(result["source"])
                    self.assertFalse((self.destination / "tree").exists())
                    client.download.assert_not_called()
                    self.assertEqual(json.loads((self.destination / "result.json").read_text()), result)

    def test_linux_completed_run_cannot_authorize_replaced_checkpoint(self):
        for arch in ("x64", "arm64"):
            with self.subTest(arch=arch):
                pin, _ = cache.load_manifest("linux", arch, root=self.root)
                run, artifact = metadata(pin)
                artifact.update(id=artifact["id"] + 1, digest=digest(b"replacement checkpoint"))
                client = mock.Mock()
                client.json.side_effect = [run, artifact]
                with mock.patch.object(cache.shutil, "which", return_value="/usr/bin/zstd"):
                    result = cache.fetch("linux", arch, self.destination, root=self.root, client=client)
                self.assertEqual(result["reason"], "artifact_mismatch")
                self.assertEqual(result["download_bytes"], 0)
                client.download.assert_not_called()

    def test_linux_153_rejects_completed_152_run_before_download(self):
        for arch in ("x64", "arm64"):
            with self.subTest(arch=arch):
                pin, _ = cache.load_manifest("linux", arch, root=self.root)
                run, artifact = metadata(pin)
                run.update(id=33895980718, head_branch="152.0.7977.82-1",
                           head_sha="02c59ed68d1963a647bb478064823d114e466ffb")
                client = mock.Mock()
                client.json.side_effect = [run, artifact]
                with mock.patch.object(cache.shutil, "which", return_value="/usr/bin/zstd"):
                    result = cache.fetch("linux", arch, self.destination, root=self.root, client=client)
                self.assertEqual(result["reason"], "untrusted_run")
                self.assertEqual(result["download_bytes"], 0)
                client.download.assert_not_called()

    def test_windows_153_rejects_152_run_metadata_before_download(self):
        self.manifest["sources"]["windows"] = windows153_source(self.root)
        self.save_manifest()
        self.pin, self.identity = cache.load_manifest("windows", "x64", root=self.root)
        run, artifact = metadata(self.pin)
        run.update(id=33898278106, head_branch="152.0.7977.82-1.1",
                   head_sha="333bc7dfff72ff4abc4d9cc76bc41de300a46e06")
        client = mock.Mock()
        client.json.side_effect = [run, artifact]
        result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["reason"], "untrusted_run")
        self.assertEqual(result["download_bytes"], 0)
        client.download.assert_not_called()

    def test_legacy_windows_152_expired_metadata_cannot_download(self):
        pins = cache.load_shared_pins(self.root)
        pins.update(ChromiumVersion="152.0.7977.82", UngoogledVersion="152.0.7977.82-1",
                    UngoogledCommit="e71b91c6e336d0f25cfc6b9ef09298a9d2506e24")
        (self.root / "CHROMIUM_VERSION").write_text(pins["ChromiumVersion"] + "\n")
        self.manifest.update(chromium_version=pins["ChromiumVersion"], ungoogled_commit=pins["UngoogledCommit"])
        for key in ("WindowsChromiumVersion", "WindowsUngoogledVersion", "WindowsUngoogledCommit"):
            pins.pop(key, None)
        pins.update(UngoogledWindowsVersion="152.0.7977.82-1.1",
                    UngoogledWindowsCommit="333bc7dfff72ff4abc4d9cc76bc41de300a46e06")
        (self.root / "build/ungoogled-revisions.psd1").write_text(
            "@{\n" + "".join(f'  {key} = "{value}"\n' for key, value in pins.items()) + "}\n")
        (self.root / "CHROMIUM_WINDOWS_VERSION").unlink(missing_ok=True)
        source = synthetic_windows_source(self.root)
        for key in ("chromium_version", "ungoogled_commit"):
            source.pop(key)
        self.manifest["sources"]["windows"] = source
        self.save_manifest()
        pin, _ = cache.load_manifest("windows", "x64", root=self.root)
        self.assertEqual(pin["chromium_version"], "152.0.7977.82")
        run, artifact = metadata(pin)
        artifact.update(expired=True, expires_at="2026-09-10T08:06:38Z")
        client = mock.Mock()
        client.json.side_effect = [run, artifact]
        result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["reason"], "artifact_expired")
        self.assertEqual(result["download_bytes"], 0)
        client.download.assert_not_called()

    def test_windows_manifest_fallback_requires_matching_shared_identity(self):
        self.manifest["sources"]["windows"] = windows153_source(self.root)
        self.save_manifest()
        for field in ("chromium_version", "ungoogled_commit"):
            with self.subTest(field=field):
                saved = self.manifest["sources"]["windows"].pop(field)
                self.save_manifest()
                if saved == self.manifest[field]:
                    cache.load_manifest("windows", "x64", root=self.root)
                else:
                    with self.assertRaisesRegex(cache.CacheMiss, "pin_mismatch"):
                        cache.load_manifest("windows", "x64", root=self.root)
                self.manifest["sources"]["windows"][field] = saved

    def assert_metadata_provenance(self, pin, now):
        run, artifact = metadata(pin)
        artifact["expires_at"] = pin["artifact"]["expires_at"]
        cache.validate_metadata(pin, run, artifact, now)
        for key, value in (("id", 1), ("head_sha", "0" * 40), ("head_branch", "main"),
                           ("event", "pull_request"), ("path", "other.yml"),
                           ("conclusion", "failure"), ("status", "in_progress")):
            with self.subTest(key=key), self.assertRaisesRegex(cache.CacheMiss, "untrusted_run"):
                cache.validate_metadata(pin, {**run, key: value}, artifact, now)
        for field in ("repository", "head_repository"):
            for key, value in (("id", 1), ("full_name", "attacker/fork"), ("private", True)):
                changed = copy.deepcopy(run)
                changed[field][key] = value
                with self.subTest(field=field, key=key), self.assertRaisesRegex(cache.CacheMiss, "untrusted_repository"):
                    cache.validate_metadata(pin, changed, artifact, now)
        for key, value in (("id", 1), ("name", "wrong"), ("digest", digest(b"wrong")),
                           ("size_in_bytes", 1)):
            with self.subTest(key=key), self.assertRaisesRegex(cache.CacheMiss, "artifact_mismatch"):
                cache.validate_metadata(pin, run, {**artifact, key: value}, now)
        for key in artifact["workflow_run"]:
            changed = copy.deepcopy(artifact)
            changed["workflow_run"][key] = "wrong"
            with self.subTest(workflow=key), self.assertRaisesRegex(cache.CacheMiss, "artifact_provenance"):
                cache.validate_metadata(pin, run, changed, now)
        for patch in ({"expired": True}, {"expires_at": now.isoformat()}, {"expires_at": None}):
            with self.subTest(patch=patch), self.assertRaises(cache.CacheMiss):
                cache.validate_metadata(pin, run, {**artifact, **patch}, now)

    def test_run_and_artifact_provenance(self):
        self.assert_metadata_provenance(self.pin, NOW)

    def test_expiry_flag_and_timestamp(self):
        run, artifact = metadata(self.pin)
        for patch in ({"expired": True}, {"expires_at": "2020-01-01T00:00:00Z"},
                      {"expires_at": "2026-09-08T00:00:00Z"}, {"expires_at": None}):
            with self.subTest(patch=patch), self.assertRaises(cache.CacheMiss):
                cache.validate_metadata(self.pin, run, {**artifact, **patch}, NOW)
        client = self.fixture_client()
        run, artifact = client.json.side_effect
        client.json.side_effect = [run, {**artifact, "expired": True}]
        result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["reason"], "artifact_expired")
        client.open.assert_not_called()

    def test_missing_and_rate_limited_api_is_bounded_miss(self):
        for status in (401, 403, 404, 410, 429, 503):
            with self.subTest(status=status):
                client = cache.GitHub("fixture")
                client.open = mock.Mock(side_effect=urllib.error.HTTPError(
                    "https://api.github.com", status, "error", {}, None))
                with mock.patch.object(cache.time, "sleep"):
                    result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
                self.assertEqual(result["reason"], f"github_http_{status}")
                self.assertEqual(client.open.call_count, 3 if status in (429, 503) else 1)
                self.assertEqual({p.name for p in self.destination.iterdir()}, {"result.json"})

    def test_redirect_never_forwards_auth_even_back_to_api(self):
        client = cache.GitHub("secret")
        destinations = ["https://x.blob.core.windows.net/signed?sig=secret",
                        "https://api.github.com/redirected"]
        requests = []

        def open_request(request, timeout):
            requests.append(request)
            self.assertEqual(timeout, cache.TIMEOUT)
            if len(requests) <= len(destinations):
                raise urllib.error.HTTPError(request.full_url, 302, "redirect",
                                             {"Location": destinations[len(requests) - 1]}, None)
            return Response(b"fixture")

        client.opener.open = open_request
        with client.open(cache.API + "/repos/o/r/actions/artifacts/1/zip", download=True):
            pass
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer secret")
        for request in requests[1:]:
            self.assertIsNone(request.get_header("Authorization"))
            self.assertIsNone(request.get_header("Accept"))
        self.assertIsNone(cache.NoRedirect().redirect_request(None, None, 302, "", {}, ""))

    def test_reject_unsafe_redirects_and_metadata_redirects(self):
        for url in ("http://x.blob.core.windows.net/file", "https://evil.example/file",
                    "https://api.github.com.evil.example/file", "file:///tmp/file",
                    "https://user@x.blob.core.windows.net/file", "https://127.0.0.1/file"):
            client = cache.GitHub("secret")
            client.opener.open = mock.Mock(side_effect=urllib.error.HTTPError(
                cache.API, 302, "redirect", {"Location": url}, None))
            with self.subTest(url=url), self.assertRaises(cache.CacheMiss):
                client.open(cache.API + "/artifact", download=True)
            self.assertEqual(client.opener.open.call_count, 1)
        client = cache.GitHub("secret")
        client.opener.open = mock.Mock(side_effect=urllib.error.HTTPError(
            cache.API, 302, "redirect", {"Location": cache.API + "/other"}, None))
        with self.assertRaises(urllib.error.HTTPError):
            client.open(cache.API + "/metadata")
        with mock.patch.dict(os.environ, {"GH_TOKEN": "env-token", "GITHUB_TOKEN": "not-used"}):
            self.assertEqual(cache.GitHub().token, "env-token")

    def test_download_digest_size_and_retry(self):
        data = b"fixture" * 100
        pin = copy.deepcopy(self.pin)
        pin["artifact"].update(digest=digest(data), size_in_bytes=len(data))
        path = self.root / "download"
        client = cache.GitHub("")
        client.open = mock.Mock(side_effect=[Response(data[:4]), Response(data)])
        with mock.patch.object(cache.time, "sleep"):
            self.assertEqual(client.download(pin, path), len(data))
        self.assertEqual(cache.sha256(path), digest(data))
        self.assertEqual(client.open.call_count, 2)
        for bad, reason in ((b"x" * len(data), "checksum_mismatch"),
                            (data + b"x", "download_size_mismatch")):
            client.open = mock.Mock(return_value=Response(bad))
            with self.subTest(reason=reason), self.assertRaisesRegex(cache.CacheMiss, reason):
                client.download(pin, path)
        with mock.patch.object(cache, "DOWNLOAD_SECONDS", -1), self.assertRaisesRegex(
                cache.CacheMiss, "download_timeout"):
            client.download(pin, path)

    def test_long_download_uses_full_budget_and_preserves_timeout_evidence(self):
        data = b"fixture" * 10
        pin = copy.deepcopy(self.pin)
        pin["artifact"].update(digest=digest(data), size_in_bytes=len(data))
        path = self.root / "long-download"
        client = cache.GitHub("")
        now = [0.0]
        client.open = mock.Mock(return_value=ScriptedResponse([data, b""], clock=now, elapsed=901))
        with mock.patch.object(cache.time, "monotonic", side_effect=lambda: now[0]):
            self.assertEqual(client.download(pin, path), len(data))
        self.assertEqual(cache.sha256(path), digest(data))
        now[0] = 0
        client.open = mock.Mock(return_value=ScriptedResponse([data, b""], clock=now, elapsed=1351))
        with mock.patch.object(cache.time, "monotonic", side_effect=lambda: now[0]), \
                self.assertRaisesRegex(cache.CacheMiss, "download_timeout") as failed:
            client.download(pin, path)
        details = failed.exception.details
        self.assertEqual({key: details[key] for key in (
            "download_partial_bytes", "download_expected_bytes", "download_timeout_seconds")}, {
            "download_partial_bytes": len(data), "download_expected_bytes": len(data),
            "download_timeout_seconds": 2700,
        })
        self.assertEqual(details["retry_attempts"][0]["elapsed_seconds"], 2702)
        self.assertFalse(details["retry_exhausted"])
        self.assertTrue(client.open.return_value.closed)

    def test_download_retries_share_a_single_deadline(self):
        data = b"fixture"
        pin = copy.deepcopy(self.pin)
        pin["artifact"].update(digest=digest(data), size_in_bytes=len(data))
        client = cache.GitHub("")
        client.open = mock.Mock(return_value=Response(data[:2]))
        now = [0.0]
        with mock.patch.object(cache.time, "sleep", side_effect=lambda _: now.__setitem__(0, 2701)), \
                mock.patch.object(cache.time, "monotonic", side_effect=lambda: now[0]), \
                self.assertRaisesRegex(cache.CacheMiss, "download_timeout") as failed:
            client.download(pin, self.root / "retry-download")
        self.assertEqual(client.open.call_count, 1)
        self.assertEqual(failed.exception.details["download_partial_bytes"], 2)
        self.assertEqual(len(failed.exception.details["retry_attempts"]), 2)

    def test_failed_retry_connection_preserves_prior_partial_byte_count(self):
        data = b"fixture"
        pin = copy.deepcopy(self.pin)
        pin["artifact"].update(digest=digest(data), size_in_bytes=len(data))
        path = self.root / "failed-retry-download"
        client = cache.GitHub("")
        client.open = mock.Mock(side_effect=[Response(data[:2]), TimeoutError("connect timeout")])
        now = [0.0]

        def sleep(delay):
            now[0] = 4 if now[0] == 0 else 2701

        with mock.patch.object(cache.time, "sleep", side_effect=sleep), \
                mock.patch.object(cache.time, "monotonic", side_effect=lambda: now[0]), \
                self.assertRaisesRegex(cache.CacheMiss, "download_timeout") as failed:
            client.download(pin, path)
        self.assertEqual(client.open.call_count, 2)
        self.assertEqual(path.read_bytes(), data[:2])
        self.assertEqual(failed.exception.details["download_partial_bytes"], 2)
        self.assertEqual([entry["bytes_written"] for entry in failed.exception.details["retry_attempts"]],
                         [2, 2, 2])

    def download_fixture(self, data=b"fixture"):
        pin = copy.deepcopy(self.pin)
        pin["artifact"].update(digest=digest(data), size_in_bytes=len(data))
        return cache.GitHub("fixture-token"), pin, self.root / "download-fixture"

    def redirected_download(self, client, responses):
        requests, redirects = [], []
        responses = iter(responses)
        generation = 0

        def open_request(request, timeout):
            nonlocal generation
            requests.append(request)
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, cache.TIMEOUT)
            if request.full_url.startswith(cache.API):
                generation += 1
                location = f"https://x.blob.core.windows.net/file?sig=secret-{generation}"
                redirect = urllib.error.HTTPError(request.full_url, 302, "secret", {"Location": location},
                                                  io.BytesIO(b"secret redirect body"))
                redirects.append(redirect)
                raise redirect
            return next(responses)

        client.opener.open = mock.Mock(side_effect=open_request)
        return requests, redirects

    def test_download_resume206_uses_written_partial_and_fresh_signed_url(self):
        client, pin, path = self.download_fixture()
        first = ScriptedResponse([b"f", http.client.IncompleteRead(b"ix", 4)])
        second = ScriptedResponse([b"ture", b""], status=206,
                                  headers={"Content-Range": "bytes 3-6/7", "Content-Length": "4"})
        requests, redirects = self.redirected_download(client, [first, second])
        with mock.patch.object(cache.time, "sleep") as sleep:
            self.assertEqual(client.download(pin, path), 7)
        self.assertEqual(path.read_bytes(), b"fixture")
        self.assertEqual(cache.sha256(path), pin["artifact"]["digest"])
        self.assertEqual(len(requests), 4)
        self.assertEqual(requests[0].full_url, requests[2].full_url)
        self.assertNotEqual(requests[1].full_url, requests[3].full_url)
        self.assertIsNone(requests[1].get_header("Range"))
        self.assertEqual(requests[3].get_header("Range"), "bytes=3-")
        for index in (0, 2):
            self.assertEqual(requests[index].get_header("Authorization"), "Bearer fixture-token")
            self.assertIsNone(requests[index].get_header("Range"))
        for index in (1, 3):
            for header in ("Authorization", "Accept", "X-github-api-version"):
                self.assertIsNone(requests[index].get_header(header))
        self.assertTrue(all(response.closed for response in [first, second, *redirects]))
        sleep.assert_called_once_with(1)

    def test_download_range_ignored200_resets_file_and_hash(self):
        client, pin, path = self.download_fixture()
        first = ScriptedResponse([b"BAD", TimeoutError("secret")])
        second = Response(b"fixture")
        requests, _ = self.redirected_download(client, [first, second])
        with mock.patch.object(cache.time, "sleep"):
            self.assertEqual(client.download(pin, path), 7)
        self.assertEqual(requests[-1].get_header("Range"), "bytes=3-")
        self.assertEqual(path.read_bytes(), b"fixture")
        self.assertEqual(cache.sha256(path), digest(b"fixture"))
        self.assertTrue(first.closed and second.closed)

    def test_download_mismatched_content_range_fails_closed_before_append(self):
        values = [None, "bytes 1-6/7", "bytes 2-5/7", "bytes 2-7/8", "bytes 2-6/*",
                  "bytes 2-6/8", "bytes 2-1/7", "items 2-6/7", "bytes 2-6/7, bytes 2-6/7",
                  "bytes " + "9" * 10000 + "-6/7", 206]
        for value in values:
            with self.subTest(value_type=type(value), value_length=len(value) if isinstance(value, str) else 0):
                client, pin, path = self.download_fixture()
                first = Response(b"fi")
                second = ScriptedResponse([b"xture"], status=206, headers={"Content-Range": value})
                client.open = mock.Mock(side_effect=[first, second])
                with mock.patch.object(cache.time, "sleep"), \
                        self.assertRaisesRegex(cache.CacheMiss, "invalid_content_range") as failed:
                    client.download(pin, path)
                self.assertEqual(path.read_bytes(), b"fi")
                self.assertEqual(failed.exception.details["download_partial_bytes"], 2)
                self.assertEqual(failed.exception.details["retry_attempts"][-1]["http_status"], 206)
                self.assertEqual(client.open.call_count, 2)
                self.assertTrue(first.closed and second.closed)
                self.assertLess(len(json.dumps(failed.exception.details)), 1000)

    def test_download_rejects_inconsistent_range_length_and_unsolicited206(self):
        for offset, length in ((2, "6"), (2, "secret" * 1000), (0, "7")):
            with self.subTest(offset=offset, length_size=len(length)):
                client, pin, path = self.download_fixture()
                second = ScriptedResponse([b"xture"], status=206, headers={
                    "Content-Range": f"bytes {offset}-6/7", "Content-Length": length})
                responses = [Response(b"fi"), second] if offset else [second]
                client.open = mock.Mock(side_effect=responses)
                with mock.patch.object(cache.time, "sleep"), \
                        self.assertRaisesRegex(cache.CacheMiss, "invalid_content_range"):
                    client.download(pin, path)
                self.assertEqual(path.read_bytes(), b"fi" if offset else b"")
                self.assertTrue(second.closed)

    def test_resumed_redirect_back_to_api_has_neither_auth_nor_range(self):
        client = cache.GitHub("secret-token")
        requests = []
        destinations = ["https://x.blob.core.windows.net/file?sig=secret",
                        cache.API + "/second", "https://y.actions.githubusercontent.com/file"]
        response = Response(b"fixture")

        def open_request(request, timeout):
            requests.append(request)
            if len(requests) <= len(destinations):
                raise urllib.error.HTTPError(request.full_url, 302, "secret", {
                    "Location": destinations[len(requests) - 1]}, io.BytesIO())
            return response

        client.opener.open = open_request
        with client.open(cache.API + "/artifact", download=True, offset=2):
            pass
        self.assertEqual([request.get_header("Range") for request in requests],
                         [None, "bytes=2-", None, "bytes=2-"])
        self.assertEqual([request.get_header("Authorization") for request in requests],
                         ["Bearer secret-token", None, None, None])
        self.assertTrue(response.closed)

    def test_download_retry_exhaustion_records_sanitized_attempts_after_cleanup(self):
        secret = "https://x.blob.core.windows.net/file?sig=DO-NOT-LOG"
        exception_type = type("DO-NOT-LOG", (ConnectionResetError,), {
            "__str__": lambda self: (_ for _ in ()).throw(AssertionError("exception string accessed"))})
        client = self.fixture_client()
        first = ScriptedResponse([b"fi", exception_type(errno.ECONNRESET, secret)])
        second = urllib.error.URLError(TimeoutError(errno.ETIMEDOUT, secret))
        third = urllib.error.HTTPError(secret, 503, secret, {"Authorization": secret}, io.BytesIO())
        client.open = mock.Mock(side_effect=[first, second, third])
        now = [0.0]

        def monotonic():
            now[0] += 0.25
            return now[0]

        with mock.patch.object(cache.time, "sleep") as sleep, \
                mock.patch.object(cache.time, "monotonic", side_effect=monotonic), \
                mock.patch("sys.stderr", new=io.StringIO()) as output:
            result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        entries = result["retry_attempts"]
        self.assertEqual(result["reason"], "github_http_503")
        self.assertTrue(result["retry_exhausted"])
        self.assertEqual([entry["attempt"] for entry in entries], [1, 2, 3])
        self.assertEqual([entry["exception_class"] for entry in entries],
                         ["ConnectionResetError", "URLError", "HTTPError"])
        self.assertEqual([entry["errno"] for entry in entries], [errno.ECONNRESET, errno.ETIMEDOUT, None])
        self.assertEqual(entries[1]["cause_class"], "TimeoutError")
        self.assertEqual([entry["http_status"] for entry in entries], [200, None, 503])
        self.assertEqual([entry["bytes_written"] for entry in entries], [2, 2, 2])
        self.assertEqual([entry["phase"] for entry in entries],
                         ["download_read", "download_request", "download_request"])
        self.assertTrue(all(entry["elapsed_seconds"] > 0 for entry in entries))
        self.assertEqual(sleep.call_args_list, [mock.call(1), mock.call(2)])
        saved = (self.destination / "result.json").read_text()
        for forbidden in (secret, "DO-NOT-LOG", "fixture-token", "Authorization", "sig="):
            self.assertNotIn(forbidden, saved + output.getvalue())
        self.assertEqual(json.loads(saved), result)
        self.assertEqual({path.name for path in self.destination.iterdir()}, {"result.json"})
        self.assertTrue(first.closed and third.closed)

    def test_metadata_retry_uses_bounded_exception_evidence(self):
        for status in (503, "secret" * 1000, 10**100):
            with self.subTest(status_type=type(status)):
                client = cache.GitHub("secret")
                errors = [urllib.error.HTTPError("https://secret", status, "secret", {}, io.BytesIO())
                          for _ in range(3)]
                client.opener.open = mock.Mock(side_effect=errors)
                with mock.patch.object(cache.time, "sleep"), self.assertRaises(cache.CacheMiss) as failed:
                    client.json("/metadata")
                entries = failed.exception.details["retry_attempts"]
                self.assertEqual(len(entries), 3 if status == 503 else 1)
                self.assertTrue(all(entry["phase"] == "api_request" for entry in entries))
                self.assertTrue(all(entry["bytes_written"] == 0 for entry in entries))
                self.assertEqual(entries[-1]["http_status"], 503 if status == 503 else None)
                self.assertNotIn("secret", json.dumps(failed.exception.details) + str(failed.exception))
                self.assertLess(len(json.dumps(failed.exception.details)), 1000)
                self.assertTrue(errors[0].closed)
                for error in errors:
                    error.close()
        client.opener.open = mock.Mock(side_effect=[ScriptedResponse([
            http.client.IncompleteRead(b"secret", 10)]) for _ in range(3)])
        with mock.patch.object(cache.time, "sleep"), self.assertRaisesRegex(cache.CacheMiss, "network_unavailable") as failed:
            client.json("/metadata")
        self.assertTrue(all(entry["phase"] == "metadata_read" for entry in failed.exception.details["retry_attempts"]))
        self.assertNotIn("secret", json.dumps(failed.exception.details))

    def test_exception_evidence_never_embeds_untrusted_classes_or_numeric_values(self):
        for base, expected in ((OSError, "OSError"), (Exception, "Exception")):
            error = type("secret" * 1000, (base,), {})("secret")
            error.errno = "secret" * 1000
            self.assertEqual(cache.exception_evidence(error), {
                "exception_class": expected, "errno": None, "http_status": None})
        error = urllib.error.URLError(OSError("secret"))
        error.reason.errno = 10**1000
        self.assertEqual(cache.exception_evidence(error), {
            "exception_class": "URLError", "cause_class": "OSError", "errno": None, "http_status": None})

    def test_download_cross_segment_checksum_mismatch_is_terminal(self):
        client, pin, path = self.download_fixture()
        first = Response(b"fi")
        second = ScriptedResponse([b"xturX"], status=206, headers={"Content-Range": "bytes 2-6/7"})
        client.open = mock.Mock(side_effect=[first, second])
        with mock.patch.object(cache.time, "sleep"), \
                self.assertRaisesRegex(cache.CacheMiss, "checksum_mismatch") as failed:
            client.download(pin, path)
        self.assertEqual(path.read_bytes(), b"fixturX")
        self.assertEqual(failed.exception.details["download_partial_bytes"], 7)
        self.assertEqual(client.open.call_count, 2)
        self.assertFalse(failed.exception.details["retry_exhausted"])
        self.assertTrue(first.closed and second.closed)

    def test_download_short_reads_resume_but_never_exceed_three_attempts(self):
        client, pin, path = self.download_fixture()
        responses = [Response(b"f"), ScriptedResponse([b"i"], status=206, headers={"Content-Range": "bytes 1-6/7"}),
                     ScriptedResponse([b"x"], status=206, headers={"Content-Range": "bytes 2-6/7"})]
        client.open = mock.Mock(side_effect=responses)
        with mock.patch.object(cache.time, "sleep") as sleep, \
                self.assertRaisesRegex(cache.CacheMiss, "network_unavailable") as failed:
            client.download(pin, path)
        self.assertEqual(client.open.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(path.read_bytes(), b"fix")
        self.assertEqual([call.kwargs["offset"] for call in client.open.call_args_list], [0, 1, 2])
        self.assertEqual([entry["bytes_written"] for entry in failed.exception.details["retry_attempts"]], [1, 2, 3])
        self.assertTrue(failed.exception.details["retry_exhausted"])
        self.assertTrue(all(response.closed for response in responses))

    def test_download_multiple_read_timeouts_exhaust_bounded_attempts(self):
        client, pin, path = self.download_fixture()
        responses = [ScriptedResponse([b"f", TimeoutError(errno.ETIMEDOUT, "secret")]),
                     ScriptedResponse([b"i", TimeoutError(errno.ETIMEDOUT, "secret")], status=206,
                                      headers={"Content-Range": "bytes 1-6/7"}),
                     ScriptedResponse([b"x", TimeoutError(errno.ETIMEDOUT, "secret")], status=206,
                                      headers={"Content-Range": "bytes 2-6/7"})]
        client.open = mock.Mock(side_effect=responses)
        with mock.patch.object(cache.time, "sleep") as sleep, \
                self.assertRaisesRegex(cache.CacheMiss, "network_unavailable") as failed:
            client.download(pin, path)
        self.assertEqual(client.open.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(path.read_bytes(), b"fix")
        entries = failed.exception.details["retry_attempts"]
        self.assertEqual([entry["exception_class"] for entry in entries], ["TimeoutError"] * 3)
        self.assertEqual([entry["bytes_written"] for entry in entries], [1, 2, 3])
        self.assertTrue(all(response.closed for response in responses))
        self.assertTrue(failed.exception.details["retry_exhausted"])

    def test_download_changing_ranges_across_three_attempts_verify_whole_file(self):
        client, pin, path = self.download_fixture()
        responses = [ScriptedResponse([b"f", http.client.IncompleteRead(b"i", 5)]),
                     ScriptedResponse([http.client.IncompleteRead(b"xt", 3)], status=206,
                                      headers={"Content-Range": "bytes 2-6/7"}),
                     ScriptedResponse([b"ure"], status=206, headers={"Content-Range": "bytes 4-6/7"})]
        requests, redirects = self.redirected_download(client, responses)
        with mock.patch.object(cache.time, "sleep"):
            self.assertEqual(client.download(pin, path), 7)
        self.assertEqual([request.get_header("Range") for request in requests[1::2]],
                         [None, "bytes=2-", "bytes=4-"])
        self.assertEqual(len({request.full_url for request in requests[1::2]}), 3)
        self.assertEqual(path.read_bytes(), b"fixture")
        self.assertEqual(cache.sha256(path), digest(b"fixture"))
        self.assertTrue(all(response.closed for response in [*responses, *redirects]))

    def test_download_complete_incomplete_read_partial_is_verified_in_same_attempt(self):
        client, pin, path = self.download_fixture()
        response = ScriptedResponse([b"fi", http.client.IncompleteRead(b"xture", 1)])
        client.open = mock.Mock(return_value=response)
        with mock.patch.object(cache.time, "sleep") as sleep:
            self.assertEqual(client.download(pin, path), 7)
        self.assertEqual(path.read_bytes(), b"fixture")
        sleep.assert_not_called()
        self.assertTrue(response.closed)

    def test_download_incomplete_read_partial_cannot_exceed_expected_size(self):
        client, pin, path = self.download_fixture()
        response = ScriptedResponse([b"fi", http.client.IncompleteRead(b"xture-extra", 1)])
        client.open = mock.Mock(return_value=response)
        with self.assertRaisesRegex(cache.CacheMiss, "download_size_mismatch") as failed:
            client.download(pin, path)
        self.assertEqual(path.read_bytes(), b"fi")
        self.assertEqual(failed.exception.details["download_partial_bytes"], path.stat().st_size)
        self.assertEqual(client.open.call_count, 1)
        self.assertTrue(response.closed)

    def test_download_preexisting_file_and_previous_call_are_never_resumed(self):
        client, pin, path = self.download_fixture()
        path.write_bytes(b"fixture")
        client.open = mock.Mock(side_effect=lambda *args, **kwargs: Response(b"fi"))
        with mock.patch.object(cache.time, "sleep"), self.assertRaisesRegex(cache.CacheMiss, "network_unavailable"):
            client.download(pin, path)
        self.assertEqual(client.open.call_args_list[0].kwargs["offset"], 0)
        self.assertEqual(path.read_bytes(), b"fi")
        client.open = mock.Mock(return_value=Response(b"fixture"))
        self.assertEqual(client.download(pin, path), 7)
        self.assertEqual(client.open.call_args.kwargs["offset"], 0)
        self.assertEqual(path.read_bytes(), b"fixture")

    def test_download_disk_failure_only_counts_confirmed_writes(self):
        client, pin, path = self.download_fixture()
        response = ScriptedResponse([b"fi", b"xture"])
        client.open = mock.Mock(return_value=response)
        with mock.patch.object(cache, "require_space", side_effect=[None, cache.CacheMiss("insufficient_disk_space")]), \
                self.assertRaisesRegex(cache.CacheMiss, "insufficient_disk_space") as failed:
            client.download(pin, path)
        self.assertEqual(path.read_bytes(), b"fi")
        self.assertEqual(failed.exception.details["download_partial_bytes"], 2)
        self.assertEqual(failed.exception.details["retry_attempts"][0]["phase"], "download_write")
        self.assertTrue(response.closed)

    def test_download_file_write_failure_keeps_hash_and_count_at_confirmed_bytes(self):
        client, pin, path = self.download_fixture()
        response = Response(b"fixture")
        client.open = mock.Mock(return_value=response)
        output = path.open("wb", buffering=0)
        self.addCleanup(output.close)
        writer = mock.Mock(wraps=output)
        writer.__enter__ = mock.Mock(return_value=writer)
        writer.__exit__ = mock.Mock(side_effect=lambda *args: output.close())
        writes = [0]

        def write(chunk):
            writes[0] += 1
            if writes[0] == 1:
                return output.write(chunk[:2])
            raise OSError(errno.ENOSPC, "secret disk path")

        writer.write.side_effect = write
        with mock.patch.object(Path, "open", return_value=writer), \
                self.assertRaisesRegex(cache.CacheMiss, "cache_unusable_OSError") as failed:
            client.download(pin, path)
        self.assertEqual(path.read_bytes(), b"fi")
        self.assertEqual(failed.exception.details["download_partial_bytes"], 2)
        self.assertEqual(failed.exception.details["retry_attempts"][0]["errno"], errno.ENOSPC)
        self.assertEqual(failed.exception.details["retry_attempts"][0]["bytes_written"], 2)
        self.assertEqual(client.open.call_count, 1)
        self.assertTrue(output.closed and response.closed)

    def test_download_partial_file_writes_update_full_digest(self):
        client, pin, path = self.download_fixture()
        client.open = mock.Mock(return_value=Response(b"fixture"))
        output = path.open("wb", buffering=0)
        self.addCleanup(output.close)
        writer = mock.Mock(wraps=output)
        writer.__enter__ = mock.Mock(return_value=writer)
        writer.__exit__ = mock.Mock(side_effect=lambda *args: output.close())
        writer.write.side_effect = lambda chunk: output.write(chunk[:2])
        with mock.patch.object(Path, "open", return_value=writer):
            self.assertEqual(client.download(pin, path), 7)
        self.assertEqual(path.read_bytes(), b"fixture")
        self.assertEqual(writer.write.call_count, 4)
        self.assertTrue(output.closed)

    def test_download_open_file_failure_is_diagnosed_without_network(self):
        client, pin, path = self.download_fixture()
        client.open = mock.Mock()
        with mock.patch.object(Path, "open", side_effect=OSError(errno.EACCES, "secret")), \
                self.assertRaisesRegex(cache.CacheMiss, "cache_unusable_OSError") as failed:
            client.download(pin, path)
        entry = failed.exception.details["retry_attempts"][0]
        self.assertEqual(entry["errno"], errno.EACCES)
        self.assertEqual(entry["phase"], "download_prepare")
        self.assertEqual(entry["bytes_written"], 0)
        client.open.assert_not_called()

    def test_download_local_timeouts_are_fatal_at_each_file_operation(self):
        for phase in ("open", "space", "write", "seek", "truncate", "close"):
            for exception_type in (TimeoutError, ConnectionResetError):
                with self.subTest(phase=phase, exception_type=exception_type):
                    client, pin, path = self.download_fixture()
                    first = ScriptedResponse([b"fi", TimeoutError("network")])
                    second = Response(b"fixture")
                    third = ScriptedResponse([b"xture"], status=206,
                                             headers={"Content-Range": "bytes 2-6/7"})
                    client.open = mock.Mock(side_effect=[first, second, third])
                    output = path.open("wb", buffering=0)
                    self.addCleanup(output.close)
                    writer = mock.Mock(wraps=output)
                    writer.__enter__ = mock.Mock(return_value=writer)
                    writer.__exit__ = mock.Mock(side_effect=lambda *args: output.close())
                    error = exception_type(errno.ETIMEDOUT, "secret disk path")
                    opens = mock.patch.object(Path, "open", return_value=writer)
                    space = mock.patch.object(cache, "require_space")
                    with opens as open_file, space as check_space, mock.patch.object(cache.time, "sleep") as sleep:
                        if phase == "open":
                            open_file.side_effect = error
                        elif phase == "space":
                            check_space.side_effect = error
                        elif phase == "write":
                            writer.write.side_effect = error
                        elif phase in ("seek", "truncate"):
                            method = getattr(output, phase)
                            calls = [0]

                            def reset(*args):
                                calls[0] += 1
                                if calls[0] == 2:
                                    raise error
                                return method(*args)

                            getattr(writer, phase).side_effect = reset
                        else:
                            def close(*args):
                                output.close()
                                raise error

                            writer.__exit__.side_effect = close
                        with self.assertRaisesRegex(cache.CacheMiss, "cache_unusable_OSError") as failed:
                            client.download(pin, path)
                    entry = failed.exception.details["retry_attempts"][-1]
                    self.assertEqual(entry["exception_class"], exception_type.__name__)
                    self.assertFalse(failed.exception.details["retry_exhausted"])
                    self.assertEqual(failed.exception.details["download_partial_bytes"], path.stat().st_size)
                    self.assertEqual(entry["bytes_written"], path.stat().st_size)
                    self.assertLessEqual(client.open.call_count, 2)
                    self.assertLessEqual(sleep.call_count, 1)
                    if phase == "open":
                        output.close()
                    self.assertTrue(output.closed)
                    for response in (first, second, third):
                        response.close()

    def test_download_truncate_timeout_does_not_retry_from_wrong_file_position(self):
        client, pin, path = self.download_fixture()
        responses = [Response(b"fi"), Response(b"fixture"), ScriptedResponse(
            [b"xture"], status=206, headers={"Content-Range": "bytes 2-6/7"})]
        client.open = mock.Mock(side_effect=responses)
        output = path.open("wb", buffering=0)
        self.addCleanup(output.close)
        writer = mock.Mock(wraps=output)
        writer.__enter__ = mock.Mock(return_value=writer)
        writer.__exit__ = mock.Mock(side_effect=lambda *args: output.close())
        calls = [0]

        def truncate(*args):
            calls[0] += 1
            if calls[0] == 2:
                raise TimeoutError("secret disk timeout")
            return output.truncate(*args)

        writer.truncate.side_effect = truncate
        with mock.patch.object(Path, "open", return_value=writer), mock.patch.object(cache.time, "sleep"), \
                self.assertRaisesRegex(cache.CacheMiss, "cache_unusable_OSError") as failed:
            client.download(pin, path)
        self.assertEqual(client.open.call_count, 2)
        self.assertEqual(path.read_bytes(), b"fi")
        self.assertEqual(failed.exception.details["download_partial_bytes"], 2)
        self.assertEqual(failed.exception.details["retry_attempts"][-1]["phase"], "download_reset")
        self.assertTrue(output.closed and responses[0].closed and responses[1].closed)
        responses[2].close()

    def test_download_local_mutation_then_timeout_reconciles_size_and_invalidates_hash(self):
        for mutation in ("truncate", "write"):
            with self.subTest(mutation=mutation):
                client, pin, path = self.download_fixture()
                responses = [Response(b"fi"), Response(b"fixture")]
                client.open = mock.Mock(side_effect=responses)
                output = path.open("wb", buffering=0)
                self.addCleanup(output.close)
                writer = mock.Mock(wraps=output)
                writer.__enter__ = mock.Mock(return_value=writer)
                writer.__exit__ = mock.Mock(side_effect=lambda *args: output.close())
                calls = [0]

                def mutate(*args):
                    calls[0] += 1
                    method = getattr(output, mutation)
                    if mutation == "truncate" and calls[0] == 1:
                        return method(*args)
                    if mutation == "write":
                        output.write(args[0][:1])
                    else:
                        output.truncate(0)
                    raise TimeoutError(errno.ETIMEDOUT, "secret post-mutation timeout")

                getattr(writer, mutation).side_effect = mutate
                with mock.patch.object(Path, "open", return_value=writer), mock.patch.object(cache.time, "sleep"), \
                        self.assertRaisesRegex(cache.CacheMiss, "cache_unusable_OSError") as failed:
                    client.download(pin, path)
                expected = 0 if mutation == "truncate" else 1
                self.assertEqual(path.stat().st_size, expected)
                self.assertEqual(failed.exception.details["download_partial_bytes"], expected)
                self.assertEqual(failed.exception.details["retry_attempts"][-1]["bytes_written"], expected)
                self.assertEqual(client.open.call_count, 2 if mutation == "truncate" else 1)
                self.assertTrue(output.closed)
                for response in responses:
                    response.close()

    def test_download_close_timeout_during_failed_cleanup_is_fatal_and_preserves_evidence(self):
        client, pin, path = self.download_fixture()
        response = ScriptedResponse([b"fi", TimeoutError("network")])
        client.open = mock.Mock(side_effect=[response, TimeoutError("network"), TimeoutError("network")])
        output = path.open("wb", buffering=0)
        self.addCleanup(output.close)
        writer = mock.Mock(wraps=output)
        writer.__enter__ = mock.Mock(return_value=writer)

        def close(*args):
            output.close()
            raise TimeoutError(errno.ETIMEDOUT, "secret close path")

        writer.__exit__ = mock.Mock(side_effect=close)
        with mock.patch.object(Path, "open", return_value=writer), mock.patch.object(cache.time, "sleep"), \
                self.assertRaisesRegex(cache.CacheMiss, "cache_unusable_OSError") as failed:
            client.download(pin, path)
        entries = failed.exception.details["retry_attempts"]
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[-1]["phase"], "download_request")
        self.assertEqual(entries[-1]["cleanup_failure"]["phase"], "download_close")
        self.assertEqual(entries[-1]["cleanup_failure"]["exception_class"], "TimeoutError")
        self.assertEqual(entries[-1]["cleanup_failure"]["bytes_written"], 2)
        self.assertEqual(path.read_bytes(), b"fi")
        self.assertEqual(client.open.call_count, 3)
        self.assertTrue(output.closed and response.closed)

    def test_download_read_timeout_does_not_retry_after_disk_reset_failure(self):
        client, pin, path = self.download_fixture()
        response = ScriptedResponse([b"fi", TimeoutError("network")])
        client.open = mock.Mock(side_effect=[response, Response(b"fixture")])
        output = path.open("wb", buffering=0)
        self.addCleanup(output.close)
        writer = mock.Mock(wraps=output)
        writer.__enter__ = mock.Mock(return_value=writer)
        writer.__exit__ = mock.Mock(side_effect=lambda *args: output.close())
        writes = [0]

        def write(chunk):
            writes[0] += 1
            if writes[0] > 1:
                raise ConnectionResetError(errno.ECONNRESET, "secret disk reset")
            return output.write(chunk)

        writer.write.side_effect = write
        with mock.patch.object(Path, "open", return_value=writer), mock.patch.object(cache.time, "sleep"), \
                self.assertRaisesRegex(cache.CacheMiss, "cache_unusable_OSError") as failed:
            client.download(pin, path)
        self.assertEqual(path.read_bytes(), b"")
        self.assertEqual(failed.exception.details["download_partial_bytes"], 0)
        self.assertEqual(failed.exception.details["retry_attempts"][-1]["bytes_written"], 0)
        self.assertEqual(failed.exception.details["retry_attempts"][-1]["phase"], "download_write")
        self.assertEqual(client.open.call_count, 2)
        self.assertTrue(output.closed)

    def test_download_final_file_size_and_position_must_match_confirmed_writes(self):
        for mismatch in ("position", "size"):
            with self.subTest(mismatch=mismatch):
                client, pin, path = self.download_fixture()
                client.open = mock.Mock(return_value=Response(b"fixture"))
                output = path.open("wb", buffering=0)
                self.addCleanup(output.close)
                writer = mock.Mock(wraps=output)
                writer.__enter__ = mock.Mock(return_value=writer)
                writer.__exit__ = mock.Mock(side_effect=lambda *args: output.close())
                if mismatch == "position":
                    writer.tell.return_value = 5
                else:
                    writer.tell.side_effect = lambda: (output.truncate(5), 7)[1]
                with mock.patch.object(Path, "open", return_value=writer), \
                        self.assertRaisesRegex(cache.CacheMiss, "download_size_mismatch"):
                    client.download(pin, path)
                self.assertEqual(client.open.call_count, 1)
                self.assertTrue(output.closed)

    def test_download_chunked_framing_partial_is_not_appended_as_body(self):
        client, pin, path = self.download_fixture()
        body = io.BytesIO(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nfi\r")
        sock = mock.Mock()
        sock.makefile.return_value = body
        first = http.client.HTTPResponse(sock)
        first.begin()
        self.assertTrue(first.chunked)
        second = ScriptedResponse([b"xture"], status=206, headers={"Content-Range": "bytes 2-6/7"})
        client.open = mock.Mock(side_effect=[first, second])
        with mock.patch.object(cache.time, "sleep"):
            self.assertEqual(client.download(pin, path), 7)
        self.assertEqual(client.open.call_args.kwargs["offset"], 2)
        self.assertEqual(path.read_bytes(), b"fixture")
        self.assertEqual(cache.sha256(path), digest(b"fixture"))
        self.assertTrue(first.closed and second.closed and body.closed)

    def test_download_unknown_exception_is_terminal_and_does_not_leak_name(self):
        client, pin, path = self.download_fixture()
        error = type("secret" * 1000, (RuntimeError,), {})("https://secret?sig=secret")
        client.open = mock.Mock(side_effect=error)
        with mock.patch.object(cache.time, "sleep") as sleep, \
                self.assertRaisesRegex(cache.CacheMiss, "network_error") as failed:
            client.download(pin, path)
        self.assertEqual(failed.exception.details["retry_attempts"][0]["exception_class"], "Exception")
        self.assertNotIn("secret", json.dumps(failed.exception.details))
        self.assertEqual(client.open.call_count, 1)
        sleep.assert_not_called()

    def test_download_unexpected_response_status_closes_body(self):
        for status in (201, 206, 416):
            with self.subTest(status=status):
                client, pin, path = self.download_fixture()
                response = ScriptedResponse([b"fixture"], status=status)
                client.opener.open = mock.Mock(return_value=response)
                with self.assertRaisesRegex(cache.CacheMiss, "unexpected_http_status") as failed:
                    client.download(pin, path)
                self.assertTrue(response.closed)
                self.assertEqual(failed.exception.details["retry_attempts"][0]["http_status"], status)
                self.assertEqual(path.read_bytes(), b"")

    def test_download_deadline_limits_every_redirect_connection_and_read(self):
        client, pin, path = self.download_fixture()
        now = [0.0]
        requests = []
        body = io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\nfixture")
        body.raw = mock.Mock()
        sock = mock.Mock()
        sock.makefile.return_value = body
        response = http.client.HTTPResponse(sock)
        response.begin()
        response.read1 = mock.Mock(wraps=response.read1)

        def open_request(request, timeout):
            requests.append((request, timeout))
            if len(requests) == 1:
                now[0] = 2690
                raise urllib.error.HTTPError(request.full_url, 302, "redirect", {
                    "Location": "https://x.blob.core.windows.net/file"}, io.BytesIO())
            now[0] = 2695
            return response

        client.opener.open = open_request
        with mock.patch.object(cache.time, "monotonic", side_effect=lambda: now[0]):
            self.assertEqual(client.download(pin, path), 7)
        self.assertEqual([timeout for _, timeout in requests], [60, 10])
        self.assertEqual(body.raw._sock.settimeout.call_args, mock.call(5))
        self.assertGreater(response.read1.call_count, 0)
        self.assertTrue(response.closed and body.closed)

    def test_download_httpresponse_read1_premature_eof_resumes_with_range(self):
        client, pin, path = self.download_fixture()
        body = io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\nfi")
        body.raw = mock.Mock()
        sock = mock.Mock()
        sock.makefile.return_value = body
        first = http.client.HTTPResponse(sock)
        first.begin()
        first.read1 = mock.Mock(wraps=first.read1)
        second = ScriptedResponse([b"xture"], status=206, headers={"Content-Range": "bytes 2-6/7"})
        client.open = mock.Mock(side_effect=[first, second])
        with mock.patch.object(cache.time, "sleep"):
            self.assertEqual(client.download(pin, path), 7)
        self.assertEqual(client.open.call_args.kwargs["offset"], 2)
        self.assertGreater(first.read1.call_count, 0)
        self.assertEqual(path.read_bytes(), b"fixture")
        self.assertTrue(first.closed and second.closed and body.closed)

    def test_download_deadline_caps_retry_wait_and_stops_opening_requests(self):
        client, pin, path = self.download_fixture()
        now = [0.0]

        def open_response(*args, **kwargs):
            now[0] = 2699.5
            raise TimeoutError("secret")

        def sleep(delay):
            now[0] += delay

        client.open = mock.Mock(side_effect=open_response)
        with mock.patch.object(cache.time, "monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(cache.time, "sleep", side_effect=sleep) as wait, \
                self.assertRaisesRegex(cache.CacheMiss, "download_timeout") as failed:
            client.download(pin, path)
        self.assertEqual(now[0], 2700)
        wait.assert_called_once_with(0.5)
        self.assertEqual(client.open.call_count, 1)
        self.assertEqual(failed.exception.details["download_partial_bytes"], 0)
        self.assertEqual(cache.ATTEMPTS, 3)
        self.assertEqual(cache.DOWNLOAD_SECONDS, 2700)

    def test_download_deadline_detects_but_cannot_interrupt_drip_response_headers(self):
        client, pin, path = self.download_fixture()
        now = [0.0]
        wire = b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\nX-Pad: " + b"x" * 61 + b"\r\n\r\nfixture"

        class DripHeaders(io.BytesIO):
            def readline(self, limit=-1):
                line = bytearray()
                while limit < 0 or len(line) < limit:
                    byte = super().read(1)
                    if not byte:
                        break
                    now[0] += 30
                    line.extend(byte)
                    if byte == b"\n":
                        break
                return bytes(line)

        body = DripHeaders(wire)
        sock = mock.Mock()
        sock.makefile.return_value = body
        response = http.client.HTTPResponse(sock)

        def open_request(request, timeout):
            self.assertEqual(timeout, 60)
            response.begin()
            return response

        client.opener.open = mock.Mock(side_effect=open_request)
        with mock.patch.object(cache.time, "monotonic", side_effect=lambda: now[0]), \
                self.assertRaisesRegex(cache.CacheMiss, "download_timeout") as failed:
            client.download(pin, path)
        self.assertEqual(now[0], 3240)
        self.assertEqual(failed.exception.details["download_partial_bytes"], 0)
        self.assertEqual(client.opener.open.call_count, 1)
        self.assertTrue(response.closed and body.closed)

    def test_download_deadline_detects_but_cannot_interrupt_multiaddress_connect(self):
        client, pin, path = self.download_fixture()
        now = [0.0]
        connections = [mock.Mock(), mock.Mock()]
        addresses = [(cache.socket.AF_INET, cache.socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443)),
                     (cache.socket.AF_INET, cache.socket.SOCK_STREAM, 6, "", ("192.0.2.2", 443))]
        redirect = urllib.error.HTTPError(cache.API, 302, "redirect", {
            "Location": "https://x.blob.core.windows.net/file"}, io.BytesIO())

        def connect(address):
            now[0] += 10
            raise TimeoutError("fixture connect")

        for connection in connections:
            connection.connect.side_effect = connect

        def open_request(request, timeout):
            if request.full_url.startswith(cache.API):
                now[0] = 2690
                raise redirect
            self.assertEqual(timeout, 10)
            with mock.patch.object(cache.socket, "getaddrinfo", return_value=addresses), \
                    mock.patch.object(cache.socket, "socket", side_effect=connections):
                return create_connection(("fixture.invalid", 443), timeout=timeout)

        client.opener.open = mock.Mock(side_effect=open_request)
        with mock.patch.object(cache.time, "monotonic", side_effect=lambda: now[0]), \
                self.assertRaisesRegex(cache.CacheMiss, "download_timeout") as failed:
            client.download(pin, path)
        self.assertEqual(now[0], 2710)
        self.assertEqual(failed.exception.details["download_partial_bytes"], 0)
        self.assertEqual(client.opener.open.call_count, 2)
        for connection in connections:
            connection.settimeout.assert_called_once_with(10)
            connection.close.assert_called_once()
        self.assertTrue(redirect.closed)

    def test_download_expired_redirect_chain_closes_response_and_stops(self):
        client, pin, path = self.download_fixture()
        now = [0.0]
        redirect = urllib.error.HTTPError(cache.API, 302, "redirect", {
            "Location": "https://x.blob.core.windows.net/file"}, io.BytesIO())

        def open_request(request, timeout):
            now[0] = 2700
            raise redirect

        client.opener.open = mock.Mock(side_effect=open_request)
        with mock.patch.object(cache.time, "monotonic", side_effect=lambda: now[0]), \
                self.assertRaisesRegex(cache.CacheMiss, "download_timeout"):
            client.download(pin, path)
        self.assertEqual(client.opener.open.call_count, 1)
        self.assertTrue(redirect.closed)

    def test_fetch_retains_partial_download_evidence_after_cleanup(self):
        client = self.fixture_client()
        details = {"download_partial_bytes": 2, "download_expected_bytes": 7,
                   "download_timeout_seconds": 2700}
        def download(pin, path):
            path.write_bytes(b"fi")
            raise cache.CacheMiss("download_timeout", details=details)
        client.download = mock.Mock(side_effect=download)
        result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["status"], "miss")
        self.assertEqual(result["reason"], "download_timeout")
        self.assertEqual(result["phase"], "download")
        self.assertEqual(result["download_bytes"], 0)
        self.assertEqual({key: result[key] for key in details}, details)
        self.assertEqual({path.name for path in self.destination.iterdir()}, {"result.json"})
        self.assertEqual(json.loads((self.destination / "result.json").read_text()), result)

    def test_bad_hash_never_opens_archive(self):
        client = self.fixture_client()
        client.open = mock.Mock(return_value=Response(b"corrupt"))
        with mock.patch.object(cache.time, "sleep"), mock.patch.object(cache, "unpack_outer") as unpack:
            result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["status"], "miss")
        unpack.assert_not_called()
        self.assertEqual({p.name for p in self.destination.iterdir()}, {"result.json"})

    def test_safe_tar_and_zip_preserve_links_modes_mtime_and_ninja(self):
        entries = [("src", "dir", b""), ("src/bin", "file", b"binary"),
                   ("src/out/.ninja_log", "file", b"log"),
                   ("src/link", "sym", "bin"), ("src/dangling", "sym", "missing"),
                   ("src/node", "sym", "/usr/bin/node")]
        for kind in ("tar", "zip"):
            tree = self.root / kind
            tree.mkdir()
            with self.subTest(kind=kind):
                if kind == "tar":
                    data = tar_bytes(entries + [("src/hard", "hard", "src/bin")])
                    result = cache.extract_tar(io.BytesIO(data), tree)
                    self.assertEqual((tree / "src/bin").stat().st_ino, (tree / "src/hard").stat().st_ino)
                else:
                    result = cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree)
                self.assertEqual((tree / "src/bin").stat().st_mode & 0o777, 0o755)
                for name in ("src", "src/bin", "src/out/.ninja_log"):
                    self.assertEqual((tree / name).stat().st_mtime_ns, MTIME)
                self.assertEqual(os.readlink(tree / "src/link"), "bin")
                self.assertEqual((tree / "src/link").lstat().st_mtime_ns, MTIME)
                self.assertEqual(os.readlink(tree / "src/dangling"), "missing")
                self.assertFalse((tree / "src/node").is_symlink())
                self.assertEqual(result["skipped_external_symlinks"], 1)

    def test_traversal_and_duplicate_members(self):
        for name in ("../outside", "/outside", "src/../../outside", "C:/outside",
                     "src\\outside", "src/file:stream", "src/NUL", "src/space "):
            for kind in ("tar", "zip"):
                tree = self.root / "unsafe"
                tree.mkdir(exist_ok=True)
                with self.subTest(name=name, kind=kind), self.assertRaises(cache.CacheMiss):
                    entries = [(name, "file", b"bad")]
                    if kind == "tar":
                        cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree)
                    else:
                        cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree)
        entries = [("src/file", "file", b"a"), ("./src/file", "file", b"b")]
        with self.assertRaisesRegex(cache.CacheMiss, "duplicate_archive_path"):
            cache.extract_tar(io.BytesIO(tar_bytes(entries)), self.root / "duplicate")
        self.assertFalse((self.root / "outside").exists())

    def test_escaping_links_chains_parent_writes_and_special_files(self):
        cases = [
            [("src/link", "sym", "../../outside")],
            [("src/link", "hard", "../outside")],
            [("src/link", "hard", "/outside")],
            [("src/link", "sym", "/tmp"), ("src/link/file", "file", b"bad")],
            [("src/up", "sym", ".."), ("src/escape", "sym", "up/../outside")],
            [("src/a", "sym", "b"), ("src/b", "sym", "a")],
            [("src/sym", "sym", "file"), ("src/hard", "hard", "src/sym")],
            [("src/fifo", "fifo", b"")],
        ]
        for index, entries in enumerate(cases):
            tree = self.root / f"links-{index}"
            tree.mkdir()
            with self.subTest(entries=entries), self.assertRaises(cache.CacheMiss):
                cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree)

    def test_outer_requires_exact_inner_and_checks_all_paths(self):
        for entries in ([("wrong.zip", "file", b"bad")],
                        [("artifacts.zip", "file", b"ok"), ("../outside", "file", b"bad")],
                        [("artifacts.zip", "sym", "/tmp/inner")]):
            outer = self.root / "outer.zip"
            outer.write_bytes(zip_bytes(entries))
            with self.subTest(entries=entries), self.assertRaises(cache.CacheMiss):
                cache.unpack_outer(outer, self.root / "inner", "artifacts.zip")
            self.assertFalse((self.root / "inner").exists())

    def test_windows_hit_both_shapes_cleanup_and_idempotence(self):
        for source in ("src", "build/src"):
            with self.subTest(source=source):
                client = self.fixture_client(source=source)
                destination = self.root / source.replace("/", "-")
                result = cache.fetch("windows", "x64", destination, root=self.root, client=client)
                self.assertEqual(result["status"], "hit", result)
                self.assertEqual(result["source"], str(destination / "tree" / source))
                self.assertEqual(result["manifest"]["artifact_id"], self.pin["artifact"]["id"])
                self.assertGreater(result["download_bytes"], 0)
                self.assertGreater(result["inner_bytes"], 0)
                self.assertGreater(result["extracted_bytes"], 0)
                self.assertEqual({p.name for p in destination.iterdir()}, {"result.json", "tree"})
                second = cache.fetch("windows", "x64", destination, root=self.root, client=mock.Mock())
                self.assertEqual(result, second)
                self.assertEqual(result["extraction_scope"], "source-and-objects")
                source_path = Path(result["source"])
                expected = {"chrome/browser/file.cc": b"source", "out/Default/args.gn": b'target_cpu="x64"',
                            "out/Default/build.ninja": b"build-ninja", "out/Default/obj/file.o": b"object",
                            "out/Default/gen/generated.h": b"generated", "out/Default/.ninja_log": b"ninja-log",
                            "out/Default/.ninja_deps": b"ninja-deps"}
                for name, content in expected.items():
                    self.assertEqual((source_path / name).read_bytes(), content)
                    self.assertEqual((source_path / name).stat().st_mtime_ns, MTIME)
                self.assertFalse((destination / "tree/build/download_cache").exists())

    @unittest.skipUnless(shutil.which("zstd"), "host zstd is unavailable")
    def test_linux_and_macos_zstd_hits(self):
        for platform, arch in (("linux", "x64"), ("linux", "arm64"), ("macos", "x64"), ("macos", "arm64")):
            with self.subTest(platform=platform, arch=arch):
                source = "build/src" if platform == "linux" else "src"
                retained = ["chrome/browser/file.cc", "out/Default/build.ninja",
                            "out/Default/obj/file.o", "out/Default/gen/generated.h"]
                extra = [(source + "/third_party/node/linux/node-linux-x64/bin/node", "sym", "/usr/bin/node")]
                client = self.fixture_client(platform, arch, source, extra)
                destination = self.root / f"{platform}-{arch}"
                result = cache.fetch(platform, arch, destination, root=self.root, client=client)
                self.assertEqual(result["status"], "hit", result)
                self.assertTrue(Path(result["source"]).is_absolute())
                self.assertEqual(result["extraction_scope"], "source-and-objects")
                for name in retained + ["out/Default/.ninja_log", "out/Default/.ninja_deps"]:
                    self.assertTrue((Path(result["source"]) / name).is_file(), name)
                    self.assertEqual((Path(result["source"]) / name).stat().st_mtime_ns, MTIME)
                self.assertFalse((destination / "tree/build/download_cache").exists())
                self.assertFalse((Path(result["source"]) / "third_party/node").exists())
                self.assertEqual(result["skipped_external_symlinks"], 1)
                offline = mock.Mock()
                second = cache.fetch(platform, arch, destination, root=self.root, client=offline)
                self.assertEqual(result, second)
                offline.json.assert_not_called()
                offline.download.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX archive names required")
    def test_linux_sysroot_preserves_literal_systemd_backslash(self):
        name = r"build/src/build/linux/sysroot/lib/systemd/system/system-systemd\x2dcryptsetup.slice"
        tree = self.root / "systemd"
        tree.mkdir()
        cache.extract_tar(io.BytesIO(tar_bytes([(name, "file", b"unit")])), tree,
                          cache.SourceSelection(["build/src"]))
        self.assertEqual((tree / name).read_bytes(), b"unit")
        with self.assertRaises(cache.CacheMiss):
            cache.safe_name(name)
        with self.assertRaises(cache.CacheMiss):
            cache.safe_name("build/src/../../escape", posix=True)

    @unittest.skipUnless(os.name == "posix", "POSIX archive names required")
    def test_posix_source_paths_and_link_targets_preserve_literal_names(self):
        names = [r"system-systemd\x2dcryptsetup.slice", "name:with:colon", "trailing.",
                 "trailing ", "NUL", 'literal<>"|?*']
        for platform in ("linux", "macos"):
            for kind in ("tar", "zip"):
                with self.subTest(platform=platform, kind=kind):
                    tree = self.root / f"literal-{platform}-{kind}"
                    tree.mkdir()
                    entries = [("src/" + name, "file", b"literal") for name in names]
                    entries += [(f"src/link-{index}", "sym", name) for index, name in enumerate(names)]
                    selection = cache.SourceSelection(["src"], platform=platform)
                    if kind == "tar":
                        entries += [(f"src/hard-{index}", "hard", "src/" + name)
                                    for index, name in enumerate(names)]
                        cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
                        for index, name in enumerate(names):
                            self.assertEqual((tree / "src" / name).stat().st_ino,
                                             (tree / f"src/hard-{index}").stat().st_ino)
                    else:
                        cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, selection)
                    for index, name in enumerate(names):
                        self.assertEqual((tree / "src" / name).read_bytes(), b"literal")
                        self.assertEqual((tree / f"src/link-{index}").read_bytes(), b"literal")
                        self.assertEqual(os.readlink(tree / f"src/link-{index}"), name)

    def test_windows_source_paths_and_link_targets_remain_strict(self):
        names = [r"literal\x2dname", r"..\outside", r"C:\outside", r"\\host\share", "C:/outside",
                 "file:stream", "NUL", "NUL.txt", "COM¹", "CONOUT$", "trailing.", "trailing ",
                 "bad<name", "bad>name", 'bad"name', "bad|name", "bad?name", "bad*name"]
        for kind in ("tar", "zip"):
            for index, name in enumerate(names):
                for member in ("file", "sym", "hard") if kind == "tar" else ("file", "sym"):
                    with self.subTest(kind=kind, name=name, member=member):
                        tree = self.root / f"strict-{kind}-{index}-{member}"
                        tree.mkdir()
                        entry = ("src/" + name, "file", b"bad") if member == "file" else (
                            "src/link", member, "src/" + name if member == "hard" else name)
                        selection = cache.SourceSelection(["src"], platform="windows")
                        with self.assertRaises(cache.CacheMiss):
                            if kind == "tar":
                                cache.extract_tar(io.BytesIO(tar_bytes([entry])), tree, selection)
                            else:
                                cache.extract_zip(io.BytesIO(zip_bytes([entry])), tree, selection)
        with mock.patch.object(cache.os, "name", "nt"), self.assertRaises(cache.CacheMiss):
            cache.safe_name(r"src/literal\x2dname", posix=True)
        client = self.fixture_client(extra=[(r"src/literal\x2dname", "file", b"bad")])
        result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["reason"], "unsafe_archive_path")
        self.assertEqual(result["extraction_scope"], "source-and-objects")
        self.assertEqual({p.name for p in self.destination.iterdir()}, {"result.json"})

    def test_case_folding_targets_reject_collisions_in_members_parents_and_links(self):
        cases = [
            [("src/File", "file", b"a"), ("src/file", "file", b"b")],
            [("src/Dir/a", "file", b"a"), ("src/dir/b", "file", b"b")],
            [("src/Dir", "dir", b""), ("src/dir", "sym", ".")],
            [("src/Dir/a", "file", b"a"), ("src/dir", "dir", b"")],
            [("src/é", "file", b"a"), ("src/e\u0301", "file", b"b")],
            [("src/A", "sym", "/outside"), ("src/link", "sym", "a")],
            [("src/A", "sym", "a")],
            [("src/A", "sym", "B"), ("src/b", "sym", "a")],
            [("src/A", "sym", ".."), ("src/link", "sym", "a/../outside")],
            [("foreign/Dir/a", "file", b"a"), ("foreign/dir/b", "file", b"b")],
        ]
        for platform in ("windows", "macos"):
            for kind in ("tar", "zip"):
                for index, entries in enumerate(cases):
                    with self.subTest(platform=platform, kind=kind, entries=entries):
                        tree = self.root / f"case-{platform}-{kind}-{index}"
                        tree.mkdir()
                        selection = cache.SourceSelection(["src"], platform=platform)
                        with self.assertRaises(cache.CacheMiss):
                            if kind == "tar":
                                cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
                            else:
                                cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, selection)
                tree = self.root / f"case-hard-{platform}-{kind}"
                tree.mkdir()
                entries = [("src/File", "file", b"a"), ("src/hard", "hard", "src/file")]
                with self.assertRaisesRegex(cache.CacheMiss, "archive_case_collision"):
                    cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree,
                                      cache.SourceSelection(["src"], platform=platform))

    def test_framework_and_forward_directory_symlink_chains(self):
        framework = "src/out/Default/Chromium Framework.framework"
        entries = [(framework + "/Resources", "sym", "Versions/Current/Resources"),
                   (framework + "/Versions/Current", "sym", "A"),
                   (framework + "/Versions/A/Resources/data", "file", b"resource"),
                   (framework + "/Chromium Framework", "sym", "Versions/Current/Chromium Framework"),
                   (framework + "/Versions/A/Chromium Framework", "file", b"binary"),
                   ("src/forward", "sym", "directory-link"),
                   ("src/directory-link", "sym", "empty"), ("src/empty", "dir", b""),
                   ("src/file-link", "sym", "hard-link"),
                   ("src/dangling", "sym", "missing")]
        for platform in ("windows", "macos"):
            for kind in ("tar", "zip"):
                with self.subTest(platform=platform, kind=kind):
                    tree = self.root / f"framework-{platform}-{kind}"
                    tree.mkdir()
                    selection = cache.SourceSelection(["src"], platform=platform)
                    with mock.patch.object(cache.os, "symlink", wraps=os.symlink) as symlink:
                        if kind == "tar":
                            data = tar_bytes(entries + [("src/hard-link", "hard",
                                                         framework + "/Versions/A/Chromium Framework")])
                            cache.extract_tar(io.BytesIO(data), tree, selection)
                        else:
                            data = zip_bytes(entries + [("src/hard-link", "file", b"binary")])
                            cache.extract_zip(io.BytesIO(data), tree, selection)
                    directories = {framework + "/Resources", framework + "/Versions/Current",
                                   "src/forward", "src/directory-link"}
                    for call in symlink.call_args_list:
                        name = call.args[1].relative_to(tree).as_posix()
                        self.assertEqual(call.kwargs["target_is_directory"], name in directories, name)
                    self.assertEqual((tree / framework / "Resources/data").read_bytes(), b"resource")
                    self.assertEqual((tree / framework / "Chromium Framework").read_bytes(), b"binary")
                    self.assertTrue((tree / "src/forward").is_dir())
                    self.assertEqual((tree / "src/file-link").read_bytes(), b"binary")
                    self.assertEqual(os.readlink(tree / "src/dangling"), "missing")

    def test_corrupt_archives_and_limits_fall_back(self):
        client = self.fixture_client()
        with mock.patch.object(cache, "extract_inner", side_effect=zipfile.BadZipFile("bad")):
            result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["status"], "miss")
        self.assertEqual({p.name for p in self.destination.iterdir()}, {"result.json"})
        tree = self.root / "limited"
        tree.mkdir()
        with mock.patch.object(cache, "MAX_EXTRACTED", 2), self.assertRaisesRegex(
                cache.CacheMiss, "archive_too_large"):
            cache.extract_tar(io.BytesIO(tar_bytes([("src/file", "file", b"large")])), tree)
        if shutil.which("zstd"):
            inner = self.root / "invalid.zst"
            inner.write_bytes(b"not zstd")
            with self.assertRaises((cache.CacheMiss, tarfile.TarError)):
                cache.extract_inner(inner, self.root / "bad-zstd", shutil.which("zstd"))

    def test_disk_failure_records_phase_and_required_space(self):
        client = self.fixture_client()
        self.disk_usage.return_value = mock.Mock(free=cache.DISK_HEADROOM - 1)
        result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["reason"], "insufficient_disk_space")
        self.assertEqual(result["phase"], "metadata")
        self.assertEqual(result["disk_free_bytes"], cache.DISK_HEADROOM - 1)
        self.assertGreater(result["disk_required_bytes"], cache.DISK_HEADROOM)
        self.assertEqual(result["disk_headroom_bytes"], cache.DISK_HEADROOM)
        self.assertEqual(result["disk_path"], str(self.destination))
        client.open.assert_not_called()
        self.assertEqual({p.name for p in self.destination.iterdir()}, {"result.json"})

    def test_extraction_disk_failure_records_partial_bytes_and_member(self):
        client = self.fixture_client()
        original = cache.require_space

        def fail_second_file(path, additional=0):
            first = Path(path) / "src/BUILD.gn"
            if Path(path).name == "tree" and additional and first.exists() and first.stat().st_size:
                original_disk = self.disk_usage.return_value
                self.disk_usage.return_value = mock.Mock(free=cache.DISK_HEADROOM - 1)
                try:
                    return original(path, additional)
                finally:
                    self.disk_usage.return_value = original_disk
            return original(path, additional)

        with mock.patch.object(cache, "require_space", side_effect=fail_second_file):
            result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["reason"], "insufficient_disk_space")
        self.assertEqual(result["phase"], "extract_source_and_objects")
        self.assertEqual(result["extracted_bytes"], len(b"build"))
        self.assertEqual(result["extraction_member"], "src/chrome/VERSION")
        self.assertEqual(result["disk_free_bytes"], cache.DISK_HEADROOM - 1)
        self.assertEqual({p.name for p in self.destination.iterdir()}, {"result.json"})

    def test_progress_reports_phase_without_credentials(self):
        client = self.fixture_client()
        with mock.patch("sys.stderr", new=io.StringIO()) as output:
            result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["status"], "hit")
        self.assertEqual(result["phase"], "complete")
        for phase in ("metadata", "download", "unpack_outer", "extract_source_and_objects", "verify_source"):
            self.assertIn("phase=" + phase, output.getvalue())
        self.assertNotIn("fixture-token", output.getvalue())
        self.assertEqual(json.loads((self.destination / "result.json").read_text())["phase"], "complete")

    def test_zip_fixture_really_uses_deflate(self):
        data = zip_bytes([("src/a", "file", b"fixture" * 100)])
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo("src/a")
            self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)
            self.assertLess(info.compress_size, info.file_size)

    def test_same_parent_is_created_once_without_skipping_link_checks(self):
        tree = self.root / "parent-cache"
        (tree / "src/out").mkdir(parents=True)
        extractor = cache.Extractor(tree, cache.SourceSelection(["src"], platform="windows"))
        original = Path.mkdir
        calls = []

        def mkdir(path, *args, **kwargs):
            calls.append(path)
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "mkdir", new=mkdir):
            for index in range(100):
                extractor.add(f"src/out/obj/file{index}.o", "file", 1, 0o644, MTIME, io.BytesIO(b"o"))
        self.assertEqual(calls.count(tree / "src/out/obj"), 1)
        # An existing cache entry must not let a newly introduced link redirect writes.
        original_link = extractor.is_link
        with mock.patch.object(extractor, "is_link", side_effect=lambda path: (
                path == tree / "src/out/obj" or original_link(path))):
            with self.assertRaisesRegex(cache.CacheMiss, "archive_link_parent"):
                extractor.add("src/out/obj/rejected.o", "file", 1, 0o644, MTIME, io.BytesIO(b"x"))
        self.assertFalse((tree / "src/out/obj/rejected.o").exists())

    def test_parent_junction_is_rejected_even_when_cached(self):
        tree = self.root / "junction-cache"
        tree.mkdir()
        extractor = cache.Extractor(tree, cache.SourceSelection(["src"], platform="windows"))
        extractor.add("src/out/first.o", "file", 1, 0o644, MTIME, io.BytesIO(b"o"))
        original_stat = Path.lstat

        def lstat(path, *args, **kwargs):
            if path == tree / "src/out":
                return mock.Mock(st_mode=stat.S_IFDIR | 0o755,
                                 st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return original_stat(path, *args, **kwargs)

        with mock.patch.object(Path, "lstat", new=lstat):
            with self.assertRaisesRegex(cache.CacheMiss, "archive_link_parent"):
                extractor.add("src/out/second.o", "file", 1, 0o644, MTIME, io.BytesIO(b"o"))
        self.assertFalse((tree / "src/out/second.o").exists())

    def test_extraction_progress_survives_interruption_with_partial_bytes(self):
        client = self.fixture_client()
        snapshots = []
        original = cache.write_result

        def interrupt(destination, result):
            original(destination, result)
            snapshots.append(copy.deepcopy(result))
            if result.get("phase") == "extract_source_and_objects" and result.get("extracted_bytes", 0) > 0:
                raise KeyboardInterrupt

        with mock.patch.object(cache, "PROGRESS_SECONDS", 0), \
                mock.patch.object(cache, "write_result", side_effect=interrupt), \
                self.assertRaises(KeyboardInterrupt):
            cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        saved = json.loads((self.destination / "result.json").read_text())
        self.assertEqual(saved["phase"], "extract_source_and_objects")
        self.assertEqual(saved["extraction_member"], "src/BUILD.gn")
        self.assertEqual(saved["extracted_bytes"], len(b"build"))
        self.assertGreater(saved["members"], 0)
        self.assertGreater(saved["duration_seconds"], 0)
        self.assertGreaterEqual(saved["phase_duration_seconds"], 0)
        self.assertEqual(set(saved["phase_seconds"]), {"metadata", "download", "unpack_outer"})
        self.assertEqual(saved["status"], "miss")

    def test_progress_is_throttled_but_records_latest_values_on_force(self):
        self.destination.mkdir()
        result = {"status": "miss"}
        now = [0.0]
        with mock.patch.object(cache.time, "monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(cache, "report_progress", wraps=cache.report_progress) as report:
            progress = cache.FetchProgress(self.destination, result, 0)
            progress.phase("extract_source_and_objects")
            for tick in range(1, 30):
                now[0] = float(tick)
                progress({"members": tick, "extracted_bytes": tick * 10})
            self.assertEqual(report.call_count, 1)
            now[0] = 30.0
            progress({"members": 30, "extracted_bytes": 300})
            self.assertEqual(report.call_count, 2)
            now[0] = 31.0
            progress({"members": 31, "extracted_bytes": 310}, force=True)
            self.assertEqual(report.call_count, 3)
        saved = json.loads((self.destination / "result.json").read_text())
        self.assertEqual(saved["duration_seconds"], 31.0)
        self.assertEqual(saved["members"], 31)
        self.assertEqual(saved["extracted_bytes"], 310)

    def test_failure_reason_is_written_before_potentially_slow_cleanup(self):
        client = self.fixture_client()
        original = cache.cleanup
        observed = []

        def inspect_cleanup(destination):
            saved = json.loads((destination / "result.json").read_text())
            if saved.get("cleanup_in_progress"):
                observed.append(saved)
            original(destination)

        with mock.patch.object(cache, "source_path", side_effect=cache.CacheMiss("source_version_mismatch")), \
                mock.patch.object(cache, "cleanup", side_effect=inspect_cleanup):
            result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["reason"], "source_version_mismatch")
        self.assertEqual(observed[0]["phase"], "verify_source")
        self.assertGreater(observed[0]["extracted_bytes"], 0)
        self.assertNotIn("cleanup_in_progress", result)

    def test_invalid_member_report_names_the_failing_member(self):
        for name, kind, payload, reason in (
                ("src/CON", "file", b"bad", "unsafe_archive_path"),
                ("src/BUILD.gn", "file", b"duplicate", "duplicate_archive_path"),
                ("src/large-link", "sym", "x" * 4097, "oversized_link")):
            with self.subTest(name=name):
                client = self.fixture_client(extra=[(name, kind, payload)])
                result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result["extraction_member"], name)

    def test_failed_final_progress_cannot_publish_hit(self):
        client = self.fixture_client()
        original = cache.report_progress

        def fail_complete(destination, result, phase):
            if phase == "complete":
                raise OSError("fixture progress failure")
            return original(destination, result, phase)

        with mock.patch.object(cache, "report_progress", side_effect=fail_complete):
            result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["status"], "miss")
        self.assertEqual(result["reason"], "cache_unusable_OSError")
        self.assertEqual({path.name for path in self.destination.iterdir()}, {"result.json"})

    def test_result_owned_stale_tree_cleanup_and_lock(self):
        client = self.fixture_client()
        result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["status"], "hit")
        (self.destination / ".lock").write_text("")
        with self.assertRaises(cache.LocalError):
            cache.fetch("windows", "x64", self.destination, root=self.root)
        self.assertTrue(Path(result["source"]).is_dir())
        (self.destination / ".lock").unlink()
        (self.root / "CHROMIUM_VERSION").write_text("0.0.0.0")
        result = cache.fetch("windows", "x64", self.destination, root=self.root)
        self.assertEqual(result["status"], "miss")
        self.assertEqual({p.name for p in self.destination.iterdir()}, {"result.json"})

    def test_source_shape_and_version_fallback(self):
        client = self.fixture_client(source="wrong")
        result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["reason"], "invalid_source_shape")
        client = self.fixture_client()
        with mock.patch.object(cache, "source_path", side_effect=cache.CacheMiss("source_version_mismatch")):
            result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["reason"], "source_version_mismatch")
        tree = self.root / "version-tree"
        (tree / "src/chrome").mkdir(parents=True)
        (tree / "src/BUILD.gn").write_text("fixture")
        (tree / "src/chrome/VERSION").write_text("MAJOR=0\nMINOR=0\nBUILD=0\nPATCH=0\n")
        with self.assertRaisesRegex(cache.CacheMiss, "source_version_mismatch"):
            cache.source_path(tree, self.pin)

    def test_local_destination_and_cli_failure(self):
        self.destination.mkdir()
        sentinel = self.destination / "do-not-delete"
        sentinel.write_text("important")
        with self.assertRaises(cache.LocalError):
            cache.fetch("windows", "x64", self.destination)
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(cache.main(["--platform", "windows", "--arch", "x64",
                                         "--destination", str(self.destination)]), 2)
        self.assertEqual(sentinel.read_text(), "important")
        link = self.root / "link"
        link.symlink_to(self.destination, target_is_directory=True)
        with self.assertRaises(cache.LocalError):
            cache.destination_path(str(link / "cache"))
        with self.assertRaises(cache.LocalError):
            cache.destination_path("")
        with mock.patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as error:
            cache.main(["--platform", "windows", "--arch", "x64", "--destination", str(self.root), "--run-id", "0"])
        self.assertEqual(error.exception.code, 2)

    def test_cli_miss_exit_zero_structured_output_and_foreign_result(self):
        original_load = cache.load_manifest
        self.manifest["sources"]["windows"] = unavailable_source(
            self.root, "windows", self.manifest["sources"]["windows"])
        self.save_manifest()
        for arch in ("x64", "arm64"):
            with mock.patch("sys.stdout", new=io.StringIO()) as output, \
                    mock.patch("sys.stderr", new=io.StringIO()), mock.patch.object(cache, "GitHub") as client, \
                    mock.patch.object(cache, "load_manifest", wraps=lambda p, a, r, root: original_load(p, a, r, root=self.root)):
                code = cache.main(["--platform", "windows", "--arch", arch, "--destination", str(self.destination)])
            self.assertEqual(code, 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "miss")
            self.assertEqual(result["reason"], "source_unavailable")
            self.assertEqual(result["download_bytes"], 0)
            self.assertEqual(json.loads((self.destination / "result.json").read_text()), result)
            client.assert_not_called()
        (self.destination / "result.json").write_text('{"status":"hit"}')
        with self.assertRaises(cache.LocalError):
            cache.destination_path(str(self.destination))

    def test_selected_tar_and_zip_keep_only_toolchains_and_metadata(self):
        llvm = "third_party/llvm-build/Release+Asserts"
        retained = ["BUILD.gn", "chrome/VERSION", "out/Default/args.gn", "tools/clang/scripts/update.py",
                    "tools/rust/update_rust.py", llvm + "/bin/clang", "third_party/rust-toolchain/bin/rustc"]
        excluded = ["chrome/browser/file.cc", "out/Default/obj/file.o", "out/Default/.ninja_log",
                    "out/Default/.ninja_deps", "tools/clang/__pycache__/update.pyc", "tools/rust/old.pyc",
                    "third_party/llvm-build/Debug/bin/clang", "third_party/rust-toolchain-backup/bin/rustc",
                    "download_cache/package.tar.xz"]
        for kind in ("tar", "zip"):
            for source in ("src", "build/src"):
                with self.subTest(kind=kind, source=source):
                    tree = self.root / (kind + source.replace("/", "-"))
                    tree.mkdir()
                    entries = [(source + "/" + name, "file", b"fixture") for name in retained + excluded]
                    entries += [(source + "/" + llvm, "dir", b""),
                                (source + "/" + llvm + "/bin/clang++", "sym", "clang"),
                                ("foreign/src/tools/clang/scripts/update.py", "file", b"excluded")]
                    if kind == "tar":
                        entries.append((source + "/" + llvm + "/bin/clang-hard", "hard",
                                        source + "/" + llvm + "/bin/clang"))
                        result = cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree,
                                                   cache.ToolchainSelection([source]))
                    else:
                        result = cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree,
                                                   cache.ToolchainSelection([source]))
                    for name in retained:
                        path = tree / source / name
                        self.assertEqual(path.read_bytes(), b"fixture")
                        self.assertEqual(path.stat().st_mtime_ns, MTIME)
                        self.assertEqual(path.stat().st_mode & 0o777, 0o755)
                    for name in excluded:
                        self.assertFalse((tree / source / name).exists(), name)
                    self.assertFalse((tree / "foreign").exists())
                    link = tree / source / llvm / "bin/clang++"
                    self.assertEqual(os.readlink(link), "clang")
                    self.assertEqual(link.lstat().st_mtime_ns, MTIME)
                    self.assertEqual((tree / source / llvm).stat().st_mtime_ns, MTIME)
                    self.assertEqual(result["extracted_bytes"], len(retained) * len(b"fixture"))

    def test_source_selection_keeps_full_snapshot_within_pinned_roots(self):
        retained = ["BUILD.gn", "chrome/VERSION", "chrome/browser/file.cc", "base/header.h",
                    "out/Default/args.gn", "out/Default/build.ninja", "out/Default/toolchain.ninja",
                    "out/Default/.ninja_log", "out/Default/.ninja_deps", "out/Default/obj/file.o",
                    "out/Default/gen/generated.h", "tools/clang/scripts/update.py",
                    "third_party/llvm-build/Release+Asserts/bin/clang",
                    "third_party/llvm-build-tools/include/header.h", "third_party/rust-src/library/lib.rs",
                    "third_party/rust-toolchain/lib/rustlib/src/rust/library/lib.rs",
                    "tools/clang/__pycache__/update.pyc", "download_cache/source-fixture"]
        excluded = ["build/download_cache/package.tar.xz", "build/other/data", "download_cache/package.tar.xz",
                    "build/src-backup/base/header.h", "foreign/build/src/base/header.h", "src/base/header.h"]
        source = "build/src"
        selection = cache.SourceSelection([source])
        self.assertTrue(selection("build", "dir"))
        self.assertFalse(selection("build"))
        for kind in ("tar", "zip"):
            with self.subTest(kind=kind):
                tree = self.root / f"full-source-{kind}"
                tree.mkdir()
                entries = [("build", "dir", b""), (source, "dir", b""),
                           (source + "/out/Default/obj", "dir", b""),
                           ("build/download_cache", "dir", b""),
                           (source + "/empty", "dir", b"")]
                entries += [(source + "/" + name, "file", b"fixture") for name in retained]
                entries += [(name, "file", b"excluded") for name in excluded]
                entries += [(source + "/out/Default/gen/header-link.h", "sym", "../../../base/header.h"),
                            (source + "/third_party/llvm-build/Release+Asserts/bin/clang++", "sym", "clang")]
                if kind == "tar":
                    entries.append((source + "/out/Default/obj/hard.o", "hard", source + "/out/Default/obj/file.o"))
                    result = cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
                    self.assertEqual((tree / source / "out/Default/obj/file.o").stat().st_ino,
                                     (tree / source / "out/Default/obj/hard.o").stat().st_ino)
                else:
                    result = cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, selection)
                for name in retained:
                    path = tree / source / name
                    self.assertEqual(path.read_bytes(), b"fixture")
                    self.assertEqual(path.stat().st_mtime_ns, MTIME)
                    self.assertEqual(path.stat().st_mode & 0o777, 0o755)
                for name in excluded + ["build/download_cache", "build/other", "foreign", "build/src-backup"]:
                    self.assertFalse((tree / name).exists(), name)
                for name in ("build", source, source + "/out/Default/obj", source + "/empty"):
                    self.assertEqual((tree / name).stat().st_mtime_ns, MTIME)
                self.assertEqual((tree / source / "out/Default/gen/header-link.h").read_bytes(), b"fixture")
                self.assertEqual(result["extracted_bytes"], len(retained) * len(b"fixture"))

    def test_source_selection_accepts_multiple_roots_and_rejects_unsafe_roots(self):
        selection = cache.SourceSelection(["src", "build/src"])
        for name in ("src", "src/out/Default/obj/file.o", "build/src/base/header.h"):
            self.assertTrue(selection(name), name)
        for name in ("src-backup/file", "build/src-backup/file", "build/download_cache/file", "foreign/src/file"):
            self.assertFalse(selection(name), name)
        for roots in ([], [""], ["."], ["/src"], ["../src"], ["build/../src"]):
            with self.subTest(roots=roots), self.assertRaises(cache.CacheMiss):
                cache.SourceSelection(roots)

    def test_source_selection_skips_absolute_system_links(self):
        names = ["build/src/third_party/node/linux/node-linux-x64/bin/node",
                 "build/src/third_party/llvm-build/Release+Asserts/bin/system-tool",
                 "build/src/tools/clang/system-tool", "build/download_cache/node"]
        entries = [(name, "sym", "/usr/bin/node") for name in names]
        for kind in ("tar", "zip"):
            with self.subTest(kind=kind):
                tree = self.root / f"system-links-{kind}"
                tree.mkdir()
                selection = cache.SourceSelection(["build/src"])
                if kind == "tar":
                    result = cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
                else:
                    result = cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, selection)
                self.assertEqual(result["skipped_external_symlinks"], len(names))
                self.assertEqual(result["extracted_bytes"], 0)
                for name in names:
                    self.assertFalse((tree / name).is_symlink())
                self.assertEqual(list(tree.iterdir()), [])

    def test_source_selection_remaps_known_platform_internal_absolute_links(self):
        roots = [
            ("linux", "build/src", "/repo/build/src"),
            ("macos", "src", "/Users/runner/work/ungoogled-chromium-macos/ungoogled-chromium-macos/build/src"),
            ("windows", "src", r"C:\ungoogled-chromium-windows\build\src"),
            ("windows", "build/src", "C:/ungoogled-chromium-windows/build/src"),
        ]
        rust = "third_party/rust-toolchain"
        llvm = "third_party/llvm-build/Release+Asserts/bin"
        files = {rust + "/rustc/bin/rustc": b"rustc", rust + "/rustfmt-preview/bin/rustfmt": b"rustfmt",
                 rust + "/cargo/bin/cargo": b"cargo", rust + "/rustc/lib/libLLVM.dylib": b"lib",
                 llvm + "/llvm-install-name-tool": b"llvm"}
        links = {rust + "/bin/rustc": rust + "/rustc/bin/rustc",
                 rust + "/bin/rustfmt": rust + "/rustfmt-preview/bin/rustfmt",
                 rust + "/bin/cargo": rust + "/cargo/bin/cargo",
                 rust + "/lib/libLLVM.dylib": rust + "/rustc/lib/libLLVM.dylib",
                 rust + "/lib/libLLVM-current.dylib": rust + "/lib/libLLVM.dylib",
                 rust + "/lib/current": rust + "/lib/runtime",
                 rust + "/lib/runtime": rust + "/rustc/lib",
                 llvm + "/install_name_tool": llvm + "/llvm-install-name-tool"}
        for platform, source, original in roots:
            separator = "\\" if "\\" in original else "/"
            for kind in ("tar", "zip"):
                with self.subTest(platform=platform, source=source, kind=kind):
                    tree = self.root / f"absolute-internal-{platform}-{source.replace('/', '-')}-{kind}"
                    tree.mkdir()
                    entries = [(source + "/" + name, "sym", original + separator + target.replace("/", separator))
                               for name, target in links.items()]
                    entries += [(source + "/" + name, "file", data) for name, data in files.items()]
                    entries.append((source + "/" + rust + "/bin/rustc-relative", "sym", "rustc"))
                    selection = cache.SourceSelection(cache.SOURCES[platform][2], platform=platform)
                    if kind == "tar":
                        result = cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
                    else:
                        result = cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, selection)
                    self.assertEqual(result["remapped_internal_symlinks"], len(links))
                    self.assertEqual(result["skipped_external_symlinks"], 0)
                    self.assertEqual(result["extracted_bytes"], sum(map(len, files.values())))
                    for name, target in links.items():
                        link = tree / source / name
                        relative = os.path.relpath(tree / source / target, link.parent).replace(os.sep, "/")
                        self.assertEqual(os.readlink(link), relative)
                        self.assertTrue(link.resolve().is_relative_to(tree / source))
                        self.assertEqual(link.lstat().st_mtime_ns, MTIME)
                    for name, content in ((rust + "/bin/rustc", b"rustc"), (rust + "/bin/rustfmt", b"rustfmt"),
                                          (rust + "/bin/cargo", b"cargo"), (rust + "/bin/rustc-relative", b"rustc"),
                                          (rust + "/lib/libLLVM-current.dylib", b"lib"),
                                          (rust + "/lib/current/libLLVM.dylib", b"lib"),
                                          (llvm + "/install_name_tool", b"llvm")):
                        self.assertEqual((tree / source / name).read_bytes(), content)

    def test_absolute_external_links_are_omitted_and_unknown_internal_links_reject(self):
        roots = {
            "linux": ("build/src", "/repo/build/src"),
            "macos": ("src", "/Users/runner/work/ungoogled-chromium-macos/ungoogled-chromium-macos/build/src"),
            "windows": ("src", r"C:\ungoogled-chromium-windows\build\src"),
        }
        for platform, (source, original) in roots.items():
            for kind in ("tar", "zip"):
                with self.subTest(platform=platform, kind=kind):
                    tree = self.root / f"absolute-external-{platform}-{kind}"
                    tree.mkdir()
                    entries = [
                        (f"{source}/sdk-link", "sym", "/Applications/Xcode.app/Contents/Developer/SDKs/MacOSX.sdk/usr/include/stdio.h"),
                        (f"{source}/go-link", "sym", "/usr/local/go/bin/go"),
                    ]
                    selection = cache.SourceSelection([source], platform=platform)
                    if kind == "tar":
                        result = cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
                    else:
                        result = cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, selection)
                    self.assertEqual(result["remapped_internal_symlinks"], 0)
                    self.assertEqual(result["skipped_external_symlinks"], 2)
                    self.assertEqual(set(result["external_symlink_paths"]),
                                     {f"{source}/sdk-link", f"{source}/go-link"})
                    self.assertFalse((tree / source / "sdk-link").exists())
                    self.assertFalse((tree / source / "go-link").exists())

                    unknown = [(f"{source}/missing", "sym", original + "/not-in-archive")]
                    bad_tree = self.root / f"absolute-missing-{platform}-{kind}"
                    bad_tree.mkdir()
                    with self.assertRaisesRegex(cache.CacheMiss, "missing_internal_symlink_target"):
                        if kind == "tar":
                            cache.extract_tar(io.BytesIO(tar_bytes(unknown)), bad_tree, selection)
                        else:
                            cache.extract_zip(io.BytesIO(zip_bytes(unknown)), bad_tree, selection)

        tree = self.root / "linux-rejects-macos-root"
        tree.mkdir()
        mac_target = ("/Users/runner/work/ungoogled-chromium-macos/ungoogled-chromium-macos/"
                      "build/src/third_party/rust-toolchain/rustc/bin/rustc")
        result = cache.extract_tar(io.BytesIO(tar_bytes([("build/src/rustc", "sym", mac_target)])), tree,
                                   cache.SourceSelection(["build/src"], platform="linux"))
        self.assertEqual(result["remapped_internal_symlinks"], 0)
        self.assertEqual(result["skipped_external_symlinks"], 1)
        self.assertFalse((tree / "build/src/rustc").exists())

    def test_remapped_absolute_links_still_validate_cycles_escape_case_and_existence(self):
        roots = [("linux", "build/src", "/repo/build/src"),
                 ("macos", "src", "/Users/runner/work/ungoogled-chromium-macos/ungoogled-chromium-macos/build/src"),
                 ("windows", "src", r"C:\ungoogled-chromium-windows\build\src")]
        for platform, source, original in roots:
            cases = [
                [("a", "sym", original + "/a")],
                [("a", "sym", original + "/b"), ("b", "sym", original + "/a")],
                [("a", "sym", original + "/b"), ("b", "sym", "a")],
                [("a", "sym", original + "/../download_cache/file")],
                [("a", "sym", original + "/b"), ("b", "sym", "../../../outside")],
                [("a", "sym", original + "/b"), ("b", "sym", "../download_cache/file")],
                [("a", "sym", original + "/b"), ("b", "sym", "/Applications/Xcode.app/SDK")],
                [("a", "sym", original + "/missing")],
                [("a", "sym", original + "/b"), ("b", "sym", "missing")],
                [("a", "sym", original + "/b"), ("b", "sym", "missing/../file"), ("file", "file", b"data")],
                [("a", "sym", original + "/b"), ("b", "sym", "file/../file"), ("file", "file", b"data")],
                [("a", "sym", original + "/b"), ("b", "file", b"data"), ("a/nested", "file", b"bad")],
                [("a/nested", "file", b"bad"), ("a", "sym", original + "/b"), ("b", "file", b"data")],
            ]
            if platform in ("macos", "windows"):
                cases += [[("a", "sym", original + "/file"), ("File", "file", b"data")],
                          [("a", "sym", original + "/dir/file"), ("Dir/file", "file", b"data")],
                          [("A", "sym", original + "/a")]]
            if platform == "windows":
                cases += [[("a", "sym", original + suffix)] for suffix in
                          (r"\..\outside", r"\file:stream", r"\NUL", r"\trailing.", r"\bad*name")]
            for kind in ("tar", "zip"):
                for index, members in enumerate(cases):
                    entries = [(source + "/" + name, member, target) for name, member, target in members]
                    tree = self.root / f"remap-unsafe-{platform}-{kind}-{index}"
                    tree.mkdir()
                    with self.subTest(platform=platform, kind=kind, entries=entries), self.assertRaises(cache.CacheMiss):
                        selection = cache.SourceSelection(cache.SOURCES[platform][2], platform=platform)
                        if kind == "tar":
                            cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
                        else:
                            cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, selection)

    def test_absolute_root_remapping_is_platform_and_source_scoped(self):
        mac_root = "/Users/runner/work/ungoogled-chromium-macos/ungoogled-chromium-macos/build/src"
        cases = [(cache.SourceSelection(["build/src"], platform="linux"), "build/src", mac_root),
                 (cache.SourceSelection(["src"], platform="windows"), "src", mac_root),
                 (cache.SourceSelection(["src"]), "src", mac_root),
                 (cache.SourceSelection(["foreign/src"], platform="macos"), "foreign/src", mac_root),
                 (cache.SourceSelection(["src"], platform="macos"), "src", mac_root + "-backup"),
                 (cache.SourceSelection(["src"], platform="macos"), "src", "/another/build/src"),
                 (cache.SourceSelection(["src"], platform="macos"), "src", "/repo/build/src")]
        for kind in ("tar", "zip"):
            for index, (selection, source, original) in enumerate(cases):
                tree = self.root / f"remap-scope-{kind}-{index}"
                tree.mkdir()
                entries = [(source + "/file", "file", b"data"), (source + "/link", "sym", original + "/file")]
                with self.subTest(kind=kind, source=source, original=original):
                    if kind == "tar":
                        result = cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
                    else:
                        result = cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, selection)
                    self.assertEqual(result["remapped_internal_symlinks"], 0)
                    self.assertEqual(result["external_symlink_paths"], [source + "/link"])
                    self.assertEqual(result["skipped_external_symlinks"], 1)
                    self.assertFalse((tree / source / "link").is_symlink())
                    self.assertEqual((tree / source / "file").read_bytes(), b"data")

    @unittest.skipUnless(shutil.which("zstd"), "host zstd is unavailable")
    def test_macos_fetch_remaps_rust_chain_and_records_sdk_go_omissions(self):
        original = "/Users/runner/work/ungoogled-chromium-macos/ungoogled-chromium-macos/build/src"
        rust = "third_party/rust-toolchain"
        sdk = "src/out/Default/sdk/xcode_links/MacOSX.sdk"
        go = "src/third_party/go/src"
        extra = [("src/" + rust + "/bin/rustc", "sym", original + "/" + rust + "/rustc/bin/rustc"),
                 ("src/" + rust + "/rustc/bin/rustc", "file", b"rustc"),
                 ("src/" + rust + "/bin/rustc-alias", "sym", original + "/" + rust + "/bin/rustc"),
                 (sdk, "sym", "/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"),
                 (go, "sym", "/usr/local/go/src")]
        for arch in ("x64", "arm64"):
            with self.subTest(arch=arch):
                destination = self.root / f"macos-links-{arch}"
                client = self.fixture_client("macos", arch, "src", extra)
                result = cache.fetch("macos", arch, destination, root=self.root, client=client)
                self.assertEqual(result["status"], "hit", result)
                self.assertEqual(result["remapped_internal_symlinks"], 2)
                self.assertEqual(result["skipped_external_symlinks"], 2)
                self.assertEqual(set(result["external_symlink_paths"]), {sdk, go})
                self.assertEqual((Path(result["source"]) / rust / "bin/rustc-alias").read_bytes(), b"rustc")
                self.assertEqual(json.loads((destination / "result.json").read_text()), result)
                for name in (sdk, go):
                    self.assertFalse((destination / "tree" / name).is_symlink())
        client = self.fixture_client("macos", "x64", "src", extra=[
            ("src/" + rust + "/bin/rustc", "sym", original + "/" + rust + "/rustc/bin/rustc")])
        result = cache.fetch("macos", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["reason"], "missing_internal_symlink_target")
        self.assertEqual({p.name for p in self.destination.iterdir()}, {"result.json"})

    def test_source_selection_rejects_escaping_and_excluded_link_targets(self):
        cases = [
            [("build/src/link", "sym", "../../../outside")],
            [("build/src/link", "sym", "../download_cache/file")],
            [("build/src/link", "sym", "..")],
            [("build/src/node", "sym", "/usr/bin/node"), ("build/src/link", "sym", "node")],
            [("build/src/a", "sym", "b"), ("build/src/b", "sym", "a")],
            [("build/src/node", "sym", "/usr/bin/node"), ("build/src/node/file", "file", b"bad")],
            [("build/src/up", "sym", "."), ("build/src/link", "sym", "up/../../../outside")],
        ]
        for kind in ("tar", "zip"):
            for index, entries in enumerate(cases):
                with self.subTest(kind=kind, entries=entries), self.assertRaises(cache.CacheMiss):
                    tree = self.root / f"source-links-{kind}-{index}"
                    tree.mkdir()
                    selection = cache.SourceSelection(["build/src"])
                    if kind == "tar":
                        cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
                    else:
                        cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, selection)
        entries = [("build/download_cache/file", "file", b"excluded"),
                   ("build/src/link", "hard", "build/download_cache/file")]
        tree = self.root / "source-hardlink"
        tree.mkdir()
        with self.assertRaisesRegex(cache.CacheMiss, "excluded_link_target"):
            cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, cache.SourceSelection(["build/src"]))

    def test_selection_still_validates_skipped_paths_types_and_links(self):
        cases = [
            [("foreign/../escape", "file", b"bad")],
            [("foreign/link", "sym", "../../escape")],
            [("foreign/link", "sym", "/tmp"), ("foreign/link/file", "file", b"bad")],
            [("foreign/file", "file", b"bad"), ("foreign/file/nested", "file", b"bad")],
            [("foreign/a", "sym", "b"), ("foreign/b", "sym", "a")],
        ]
        cases += [[("build/download_cache/../escape", "file", b"bad")],
                  [("build/download_cache/file", "file", b"a"),
                   ("./build/download_cache/file", "file", b"b")]]
        for selection_type in (cache.ToolchainSelection, cache.SourceSelection):
            selection = selection_type(["build/src"])
            for kind in ("tar", "zip"):
                for index, entries in enumerate(cases):
                    tree = self.root / f"skipped-{selection_type.__name__}-{kind}-{index}"
                    tree.mkdir()
                    with self.subTest(selection=selection_type.__name__, kind=kind, entries=entries):
                        with self.assertRaises(cache.CacheMiss):
                            if kind == "tar":
                                cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
                            else:
                                cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, selection)
                        self.assertEqual(list(tree.iterdir()), [])
            for index, entries in enumerate(([("foreign/link", "hard", "../escape")],
                                             [("foreign/fifo", "fifo", b"")])):
                tree = self.root / f"special-{selection_type.__name__}-{index}"
                tree.mkdir()
                with self.assertRaises(cache.CacheMiss):
                    cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, selection)
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as archive:
                info = zipfile.ZipInfo("foreign/fifo")
                info.external_attr = (stat.S_IFIFO | 0o644) << 16
                archive.writestr(info, b"")
            with self.assertRaises(cache.CacheMiss):
                cache.extract_zip(io.BytesIO(output.getvalue()), self.root, selection)

    def test_selected_links_cannot_target_excluded_content(self):
        for kind in ("tar", "zip"):
            targets = [("../../chrome/browser/file.cc", []), ("/usr/bin/node", []),
                       ("__pycache__/update.pyc", []),
                       ("../../foreign-link", [("src/foreign-link", "sym", "tools/clang/update.py")])]
            for index, (target, extra) in enumerate(targets):
                tree = self.root / f"excluded-{kind}-{index}"
                tree.mkdir()
                entries = [("src/tools/clang/link", "sym", target), *extra]
                with self.subTest(kind=kind, target=target), self.assertRaisesRegex(
                        cache.CacheMiss, "excluded_link_target"):
                    if kind == "tar":
                        cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, cache.ToolchainSelection(["src"]))
                    else:
                        cache.extract_zip(io.BytesIO(zip_bytes(entries)), tree, cache.ToolchainSelection(["src"]))
        entries = [("src/out/Default/obj/file.o", "file", b"object"),
                   ("src/tools/clang/link", "hard", "src/out/Default/obj/file.o")]
        tree = self.root / "excluded-hard"
        tree.mkdir()
        with self.assertRaisesRegex(cache.CacheMiss, "excluded_link_target"):
            cache.extract_tar(io.BytesIO(tar_bytes(entries)), tree, cache.ToolchainSelection(["src"]))

    def test_selection_skips_payload_and_enforces_both_byte_limits(self):
        tree = self.root / "limits"
        tree.mkdir()
        extractor = cache.Extractor(tree, cache.ToolchainSelection(["src"]))
        stream = mock.Mock()
        extractor.add("src/out/Default/obj/file.o", "file", 100, 0o644, MTIME, stream)
        stream.read.assert_not_called()
        self.assertEqual(extractor.bytes, 0)
        self.assertEqual(extractor.archive_bytes, 100)
        with mock.patch.object(cache, "MAX_EXTRACTED", 100), self.assertRaisesRegex(
                cache.CacheMiss, "archive_too_large"):
            extractor.add("foreign/file", "file", 1, 0o644, MTIME, stream)
        with mock.patch.object(cache, "MAX_SELECTED", 2), self.assertRaisesRegex(
                cache.CacheMiss, "archive_too_large"):
            extractor.add("src/tools/rust/update.py", "file", 3, 0o644, MTIME, io.BytesIO(b"abc"))
        self.assertEqual(cache.MAX_SELECTED, 30 * 1024**3)
        self.assertEqual(cache.DOWNLOAD_SECONDS, 45 * 60)

    def test_source_selection_uses_full_limit_and_disk_headroom(self):
        for platform in ("linux", "macos", "windows"):
            with self.subTest(platform=platform):
                tree = self.root / f"source-limits-{platform}"
                tree.mkdir()
                extractor = cache.Extractor(tree, cache.SourceSelection(["build/src"], platform=platform))
                stream = mock.Mock()
                self.disk_usage.return_value = mock.Mock(free=1024**4)
                extractor.add("build/download_cache/file", "file", 100, 0o644, MTIME, stream)
                stream.read.assert_not_called()
                self.assertEqual(extractor.archive_bytes, 100)
                self.assertEqual(extractor.bytes, 0)
                with mock.patch.object(cache, "MAX_SELECTED", 2):
                    extractor.add("build/src/out/Default/obj/file.o", "file", 3, 0o644, MTIME, io.BytesIO(b"obj"))
                self.assertEqual(extractor.bytes, 3)
                self.assertEqual((tree / "build/src/out/Default/obj/file.o").read_bytes(), b"obj")
                with mock.patch.object(cache, "MAX_EXTRACTED", 103), self.assertRaisesRegex(
                        cache.CacheMiss, "archive_too_large"):
                    extractor.add("build/src/out/Default/obj/large.o", "file", 1, 0o644, MTIME, stream)
                self.assertEqual(cache.MAX_EXTRACTED, 300 * 1024**3)
                self.assertEqual(cache.DISK_HEADROOM, 4 * 1024**3)
                self.disk_usage.return_value = mock.Mock(free=cache.DISK_HEADROOM + 2)
                with self.assertRaisesRegex(cache.CacheMiss, "insufficient_disk_space"):
                    extractor.add("build/src/base/header.h", "file", 3, 0o644, MTIME, io.BytesIO(b"abc"))
                self.assertEqual((tree / "build/src/base/header.h").stat().st_size, 0)

    def test_low_space_preflight_and_mid_extraction_fall_back(self):
        client = self.fixture_client()
        size = self.manifest["sources"]["windows"]["artifacts"]["x64"]["size_in_bytes"]
        self.disk_usage.return_value = mock.Mock(free=2 * size + cache.DISK_HEADROOM - 1)
        result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["reason"], "insufficient_disk_space")
        client.open.assert_not_called()
        self.assertEqual({p.name for p in self.destination.iterdir()}, {"result.json"})
        self.disk_usage.return_value = mock.Mock(free=1024**4)
        client = self.fixture_client(extra=[("src/tools/clang/data", "file", b"a" * 20)])
        written = []
        original = cache.require_space

        def check_space(path, additional=0):
            if Path(path) == self.destination / "tree" and additional:
                written.append(additional)
                if len(written) == 2:
                    self.disk_usage.return_value = mock.Mock(free=cache.DISK_HEADROOM + additional - 1)
            original(path, additional)

        with mock.patch.object(cache, "require_space", side_effect=check_space), mock.patch.object(cache, "CHUNK", 4):
            result = cache.fetch("windows", "x64", self.destination, root=self.root, client=client)
        self.assertEqual(result["reason"], "insufficient_disk_space")
        self.assertEqual(len(written), 2)
        self.assertEqual(result["extraction_scope"], "source-and-objects")
        self.assertEqual({p.name for p in self.destination.iterdir()}, {"result.json"})

    def test_windows_legacy_scope_hits_are_not_reused(self):
        for old_scope in (None, "toolchains-and-args"):
            with self.subTest(old_scope=old_scope):
                destination = self.root / f"stale-windows-{old_scope}"
                client = self.fixture_client()
                result = cache.fetch("windows", "x64", destination, root=self.root, client=client)
                self.assertEqual(result["status"], "hit", result)
                source = Path(result["source"])
                shutil.rmtree(source / "out/Default/obj")
                (source / "chrome/browser/file.cc").unlink()
                if old_scope is None:
                    result.pop("extraction_scope")
                else:
                    result["extraction_scope"] = old_scope
                (destination / "result.json").write_text(json.dumps(result))
                stale = source / "stale"
                stale.write_text("must not survive")
                client = self.fixture_client()
                self.assertEqual(cache.load_manifest("windows", "x64", root=self.root)[1], result["manifest"])
                result = cache.fetch("windows", "x64", destination, root=self.root, client=client)
                self.assertEqual(result["status"], "hit", result)
                self.assertEqual(result["extraction_scope"], "source-and-objects")
                self.assertEqual((source / "out/Default/obj/file.o").read_bytes(), b"object")
                self.assertEqual((source / "chrome/browser/file.cc").read_bytes(), b"source")
                self.assertFalse(stale.exists())
                client.open.assert_called_once()

    @unittest.skipUnless(shutil.which("zstd"), "host zstd is unavailable")
    def test_posix_legacy_scope_hits_are_not_reused(self):
        for platform, arch in (("linux", "x64"), ("linux", "arm64"), ("macos", "x64"), ("macos", "arm64")):
            source_name = "build/src" if platform == "linux" else "src"
            for old_scope in (None, "toolchains-and-args"):
                with self.subTest(platform=platform, arch=arch, old_scope=old_scope):
                    destination = self.root / f"stale-{platform}-{arch}-{old_scope}"
                    client = self.fixture_client(platform, arch, source_name)
                    result = cache.fetch(platform, arch, destination, root=self.root, client=client)
                    self.assertEqual(result["status"], "hit", result)
                    source = Path(result["source"])
                    shutil.rmtree(source / "out/Default/obj")
                    (source / "chrome/browser/file.cc").unlink()
                    if old_scope is None:
                        result.pop("extraction_scope")
                    else:
                        result["extraction_scope"] = old_scope
                    (destination / "result.json").write_text(json.dumps(result))
                    stale = source / "stale"
                    stale.write_text("must not survive")
                    client = self.fixture_client(platform, arch, source_name)
                    self.assertEqual(cache.load_manifest(platform, arch, root=self.root)[1], result["manifest"])
                    result = cache.fetch(platform, arch, destination, root=self.root, client=client)
                    self.assertEqual(result["status"], "hit", result)
                    self.assertEqual(result["extraction_scope"], "source-and-objects")
                    self.assertEqual((source / "out/Default/obj/file.o").read_bytes(), b"object")
                    self.assertEqual((source / "chrome/browser/file.cc").read_bytes(), b"source")
                    self.assertFalse(stale.exists())
                    client.open.assert_called_once()

    @unittest.skipUnless(shutil.which("zstd"), "host zstd is unavailable")
    def test_posix_low_space_during_source_extraction_cleans_up(self):
        for platform, arch in (("linux", "x64"), ("linux", "arm64"), ("macos", "x64"), ("macos", "arm64")):
            with self.subTest(platform=platform, arch=arch):
                source = "build/src" if platform == "linux" else "src"
                client = self.fixture_client(platform, arch, source)
                self.disk_usage.return_value = mock.Mock(free=1024**4)
                original = cache.require_space

                def check_space(path, additional=0):
                    if Path(path) == self.destination / "tree" and additional:
                        self.disk_usage.return_value = mock.Mock(free=cache.DISK_HEADROOM + additional - 1)
                    original(path, additional)

                with mock.patch.object(cache, "require_space", side_effect=check_space):
                    result = cache.fetch(platform, arch, self.destination, root=self.root, client=client)
                self.assertEqual(result["reason"], "insufficient_disk_space")
                self.assertEqual(result["extraction_scope"], "source-and-objects")
                self.assertEqual({p.name for p in self.destination.iterdir()}, {"result.json"})

    def test_missing_zstd_is_miss_and_never_runs_cached_tool(self):
        with mock.patch.object(cache.shutil, "which", return_value=None):
            result = cache.fetch("linux", "x64", self.destination, root=self.root)
        self.assertEqual(result["reason"], "zstd_unavailable")
        with mock.patch.object(cache.shutil, "which", return_value=str(self.destination / "zstd")):
            result = cache.fetch("linux", "x64", self.destination, root=self.root)
        self.assertEqual(result["reason"], "unsafe_decompressor")


if __name__ == "__main__":
    unittest.main()
