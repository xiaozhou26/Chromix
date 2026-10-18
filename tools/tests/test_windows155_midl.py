"""Windows155 dual-arch pins, controlled MIDL runtime, and once-only invalidation."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from types import MappingProxyType
import unittest

from tools import repair_windows_midl as repair
from tools.tests import test_windows154_midl as previous

IDENTITY = {
    "chromium_version": "155.0.8059.39",
    "ungoogled_commit": "ffeb45a2bb88bc56a1dfed31410aeb10e9efcd5a",
    "head_sha": "989b4382364baaddb8b3180e1eb1030ca96a7273",
    "platform": "windows", "arch": "x64",
    "repository": "ungoogled-software/ungoogled-chromium-windows", "repository_id": 177210827,
    "head_branch": "155.0.8059.39-1.1", "event": "push",
    "workflow_path": ".github/workflows/build-x64.yml", "run_id": 37831345909,
    "artifact_id": 11670281902, "artifact_name": "build-artifact",
    "artifact_digest": "sha256:4648e4426093106521986dc5a4dc66b47d861250710b3391f5ed7cf73d11db5c",
    "artifact_size_in_bytes": 15705197543,
}

ARM_IDENTITY = {
    **IDENTITY, "arch": "arm64",
    "workflow_path": ".github/workflows/build-arm.yml", "run_id": 37831345827,
    "artifact_id": 11702070420, "artifact_name": "build-artifact-arm",
    "artifact_digest": "sha256:08244eb203c3fc6551c761f70bcb4744ec55735c4e1d1a6f51c67a77c12d4f50",
    "artifact_size_in_bytes": 16004417072,
}


IDENTITY15579 = {
    "chromium_version": "155.0.8059.79",
    "ungoogled_commit": "9f145d8815e6e50cc4a2537fcac40966d911e027",
    "head_sha": "a1e3123c8d543b5c9a1eecfaf0f06cf242c0db37",
    "platform": "windows", "arch": "x64",
    "repository": "ungoogled-software/ungoogled-chromium-windows", "repository_id": 177210827,
    "head_branch": "155.0.8059.79-1.1", "event": "push",
    "workflow_path": ".github/workflows/build-x64.yml", "run_id": 38678495080,
    "artifact_id": 12099451094, "artifact_name": "build-artifact",
    "artifact_digest": "sha256:6cdf0ed9773532f172cbc9999ed3589ef16c81b14629f36da55d8f2d0a4dd768",
    "artifact_size_in_bytes": 15695268453,
}

ARM_IDENTITY15579 = {
    **IDENTITY15579, "arch": "arm64",
    "workflow_path": ".github/workflows/build-arm.yml", "run_id": 38678494961,
    "artifact_id": 12107338297, "artifact_name": "build-artifact-arm",
    "artifact_digest": "sha256:fa6e16ffd91dc85a1dcf945e8eea338a9a497da159c5cb36301aff9b841faa88",
    "artifact_size_in_bytes": 15984393421,
}


class Windows155MidlTest(unittest.TestCase):
    identity = IDENTITY
    arm_identity = ARM_IDENTITY
    records = repair.WINDOWS155_RECORDS
    original_sha256 = repair.WINDOWS155_ORIGINAL_SHA256
    repaired_sha256 = repair.WINDOWS155_REPAIRED_SHA256
    transform = staticmethod(repair.transform155)
    records_argument = "records155"

    def restore_repository(self):
        from tools.fetch_upstream_cache import ROOT
        from tools.platform_pins import load_shared_pins

        repo = self.src / "historical-repo"
        (repo / "build").mkdir(parents=True)
        for name in ("CHROMIUM_VERSION", "CHROMIUM_LINUX_VERSION", "CHROMIUM_MACOS_VERSION",
                     "CHROMIUM_WINDOWS_VERSION", "build/ungoogled-revisions.psd1", "build/upstream-cache.json"):
            shutil.copyfile(ROOT / name, repo / name)
        pins = load_shared_pins(repo)
        pins.update(WindowsChromiumVersion=IDENTITY["chromium_version"],
                    WindowsUngoogledVersion=IDENTITY["chromium_version"] + "-1",
                    WindowsUngoogledCommit=IDENTITY["ungoogled_commit"],
                    UngoogledWindowsVersion=IDENTITY["head_branch"],
                    UngoogledWindowsCommit=IDENTITY["head_sha"])
        (repo / "build/ungoogled-revisions.psd1").write_text(
            "@{\n" + "".join(f'  {key} = "{value}"\n' for key, value in pins.items()) + "}\n")
        (repo / "CHROMIUM_WINDOWS_VERSION").write_text(IDENTITY["chromium_version"] + "\n")
        manifest = json.loads((repo / "build/upstream-cache.json").read_text())
        source = {key: value for key, value in IDENTITY.items()
                  if key not in ("platform", "arch") and not key.startswith("artifact_")}
        source.update(source_roots=["src", "build/src"], artifacts={})
        for identity in (IDENTITY, ARM_IDENTITY):
            source["artifacts"][identity["arch"]] = {
                "id": identity["artifact_id"], "name": identity["artifact_name"],
                "digest": identity["artifact_digest"], "size_in_bytes": identity["artifact_size_in_bytes"],
                **({"run_id": identity["run_id"], "workflow_path": identity["workflow_path"]}
                   if identity["arch"] == "arm64" else {}),
                "expires_at": "2026-10-14T12:44:25Z" if identity["arch"] == "x64" else "2026-10-15T11:49:46Z",
                "inner_archive": "artifacts.zip",
            }
        manifest["sources"]["windows"] = source
        (repo / "build/upstream-cache.json").write_text(json.dumps(manifest))
        return repo

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.src = Path(temporary.name).resolve()
        self.script = self.src / repair.SCRIPT
        self.script.parent.mkdir(parents=True)
        self.original = previous.FIXTURE.read_bytes()
        self.script.write_bytes(self.original)
        self.out = self.src / "out/Default"
        self.out.mkdir(parents=True)
        self.output = self.out / "gen/midl/a.tlb"
        self.output.parent.mkdir(parents=True)
        self.output.write_bytes(b"stale")
        (self.out / "build.ninja").write_text(previous.graph(["gen/midl/a.tlb"]))

    def apply(self, identity=None, arch="x64", **kwargs):
        return repair.apply(self.src, "windows", arch, self.identity if identity is None else identity, **kwargs)

    def test_verified_donor_transform_and_idempotent_output_invalidation(self):
        self.assertEqual(dict(self.records[0]["identity"]), self.identity)
        self.assertEqual(hashlib.sha256(self.original).hexdigest(), self.original_sha256)
        source = self.src / "third_party/win_build_output/midl/x64/source.tlb"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"")
        unrelated = self.out / "gen/unrelated.obj"
        unrelated.write_bytes(b"retain")
        first = self.apply()
        self.assertEqual(first["status"], "repaired")
        self.assertEqual(first["invalidated_outputs"], ["gen/midl/a.tlb"])
        self.assertEqual(self.script.read_bytes(), self.transform(self.original))
        self.assertEqual(hashlib.sha256(self.script.read_bytes()).hexdigest(), self.repaired_sha256)
        self.output.write_bytes(b"regenerated")
        states = {p: previous.snapshot(p) for p in (source, unrelated, self.script, self.output)}
        self.assertEqual(self.apply()["status"], "verified")
        for path, state in states.items():
            self.assertEqual(previous.snapshot(path), state)
        self.assertEqual(json.loads((self.src / repair.RECORD).read_text())["identity"], self.identity)

    def test_all_identity_fields_and_types_fail_before_deletion(self):
        for identity in (self.identity, self.arm_identity):
            for key, value in identity.items():
                bad = "155.0.8059.40" if key == "chromium_version" else "unknown"
                for alternate in ((bad, float(value)) if type(value) is int else (bad,)):
                    with self.subTest(arch=identity["arch"], key=key), self.assertRaisesRegex(
                            ValueError, "identity"):
                        self.apply(dict(identity, **{key: alternate}), arch=identity["arch"])
        for identity in ({"chromium_version": self.identity["chromium_version"]}, dict(self.identity, extra=True),
                         dict(self.identity, arch="arm64")):
            with self.assertRaisesRegex(ValueError, "identity"):
                self.apply(identity)
        with self.assertRaisesRegex(ValueError, "identity"):
            self.apply(dict(self.identity, arch="arm64"), arch="arm64")
        self.assertEqual(self.output.read_bytes(), b"stale")
        self.assertEqual(self.script.read_bytes(), self.original)

    def test_unknown_partial_or_crlf_bytes_cannot_delete_outputs(self):
        for payload in (b"", self.original + b"# unknown", self.original.replace(b"\n", b"\r\n"),
                        self.original.replace(repair.BEFORE, repair.AFTER)):
            self.script.write_bytes(payload)
            for identity in (self.identity, self.arm_identity):
                with self.subTest(arch=identity["arch"]), self.assertRaises(ValueError):
                    self.apply(identity, arch=identity["arch"])
            self.assertEqual(self.output.read_bytes(), b"stale")
            self.assertEqual(self.script.read_bytes(), payload)

    def test_immutable_records_and_cross_version_proofs(self):
        record = self.records[0]
        with self.assertRaises(TypeError):
            record["identity"]["artifact_id"] = 1
        for records in ((), [record], (record, record), (dict(record),),
                        (MappingProxyType(dict(record, before_sha256="0" * 64)),)):
            with self.assertRaises(ValueError):
                self.apply(**{self.records_argument: records})
        repair.apply(self.src, "windows", "x64", previous.IDENTITY)
        self.output.write_bytes(b"regenerated")
        with self.assertRaisesRegex(ValueError, "invalidation record"):
            self.apply()
        self.assertEqual(self.output.read_bytes(), b"regenerated")

    def test_arm64_exact_restore_pin_repairs_once_without_touching_source(self):
        from tools.restore_upstream_cache import identities

        identity, _, _ = identities(self.restore_repository(), "windows", "arm64")
        self.assertEqual(identity, self.arm_identity)
        self.assertEqual(len(self.records), 2)
        self.assertEqual(dict(self.records[1]["identity"]), self.arm_identity)
        (self.out / "build.ninja").write_text(
            previous.graph(["gen/midl/a.tlb"]).replace("x64", "arm64"))
        source = self.src / "third_party/win_build_output/midl/arm64/source.tlb"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"")
        before = previous.snapshot(source)
        first = self.apply(identity, arch="arm64")
        self.assertEqual(first["status"], "repaired")
        self.assertEqual(first["declared_outputs"], 1)
        self.assertEqual(first["invalidated_outputs"], ["gen/midl/a.tlb"])
        self.assertEqual(hashlib.sha256(self.script.read_bytes()).hexdigest(), self.repaired_sha256)
        self.output.write_bytes(b"regenerated")
        second = self.apply(identity, arch="arm64")
        self.assertEqual(second["status"], "verified")
        self.assertEqual(second["invalidated_outputs"], [])
        self.assertEqual(self.output.read_bytes(), b"regenerated")
        self.assertEqual(previous.snapshot(source), before)
        self.assertEqual(json.loads((self.src / repair.RECORD).read_text())["identity"], self.arm_identity)
        with self.assertRaisesRegex(ValueError, "invalidation record"):
            self.apply()
        self.assertEqual(self.output.read_bytes(), b"regenerated")

    def test_arm64_rejects_mixed_x64_and_historical_donor_fields(self):
        variants = [dict(self.arm_identity, **{key: value}) for key, value in self.identity.items()
                    if value != self.arm_identity[key]]
        variants += [dict(self.arm_identity, **{key: value}) for key, value in previous.ARM_IDENTITY.items()
                     if value != self.arm_identity[key] and key != "chromium_version"]
        variants += [dict(self.arm_identity, extra=True),
                     {key: value for key, value in self.arm_identity.items() if key != "artifact_digest"}]
        for identity in variants:
            with self.subTest(identity=identity), self.assertRaisesRegex(ValueError, "identity"):
                self.apply(identity, arch="arm64")
        with self.assertRaisesRegex(ValueError, "identity"):
            self.apply(self.arm_identity, arch="x64")
        self.assertEqual(self.script.read_bytes(), self.original)
        self.assertEqual(self.output.read_bytes(), b"stale")
        self.assertFalse((self.src / repair.RECORD).exists())

    def test_existing154_arm64_and_nonwindows_remain_separate(self):
        self.assertEqual(dict(repair.WINDOWS154_RECORDS[1]["identity"]), previous.ARM_IDENTITY)
        for platform in ("linux", "macos", "off"):
            self.assertEqual(repair.apply(self.src, platform, "x64", self.identity)["status"], "not_applicable")
        result = repair.apply(self.src, "windows", "arm64", previous.ARM_IDENTITY)
        self.assertEqual(result["status"], "repaired")
        self.assertEqual(json.loads((self.src / repair.RECORD).read_text())["identity"], previous.ARM_IDENTITY)


class Windows15579MidlTest(Windows155MidlTest):
    identity = IDENTITY15579
    arm_identity = ARM_IDENTITY15579
    records = repair.WINDOWS15579_RECORDS
    original_sha256 = repair.WINDOWS15579_ORIGINAL_SHA256
    repaired_sha256 = repair.WINDOWS15579_REPAIRED_SHA256
    transform = staticmethod(repair.transform15579)
    records_argument = "records15579"

    def restore_repository(self):
        from tools.fetch_upstream_cache import ROOT
        return ROOT

    def test_dualarch_production_restore_identities_are_exact(self):
        from tools.restore_upstream_cache import identities

        self.assertEqual(len(self.records), 2)
        for identity in (self.identity, self.arm_identity):
            restored, _, _ = identities(self.restore_repository(), "windows", identity["arch"])
            self.assertEqual(restored, identity)
        self.assertEqual(self.original_sha256,
                         "0c0062cc6b73e1d0874e2a3304498f48ad1cb552d11d49eea223319051dde831")
        self.assertEqual(self.repaired_sha256,
                         "466824620fd2348229c24d477d114e4c7acc0a5e75a8f1b162729809a0dfccde")
        repaired = self.transform(self.original)
        self.assertIs(self.transform(repaired), repaired)

    def test_15539_fields_cannot_authorize_15579_outputs(self):
        for current, historical in ((self.identity, IDENTITY), (self.arm_identity, ARM_IDENTITY)):
            for key, value in historical.items():
                if value == current[key]:
                    continue
                with self.subTest(arch=current["arch"], field=key), self.assertRaisesRegex(ValueError, "identity"):
                    self.apply(dict(current, **{key: value}), arch=current["arch"])
        self.assertEqual(self.script.read_bytes(), self.original)
        self.assertEqual(self.output.read_bytes(), b"stale")
        self.assertFalse((self.src / repair.RECORD).exists())

    def test_15539_proof_does_not_authorize_15579_or_repeat_invalidation(self):
        repair.apply(self.src, "windows", "x64", IDENTITY)
        self.output.write_bytes(b"regenerated")
        before = previous.snapshot(self.script)
        with self.assertRaisesRegex(ValueError, "invalidation record"):
            self.apply()
        self.assertEqual(previous.snapshot(self.script), before)
        self.assertEqual(self.output.read_bytes(), b"regenerated")
        self.assertEqual(json.loads((self.src / repair.RECORD).read_text())["identity"], IDENTITY)


class Windows155Arm64MidlFunctionsTest(previous.Windows154MidlFunctionsTest):
    transform = staticmethod(repair.transform155)

    def setUp(self):
        super().setUp()
        self.midl = previous.load_functions(self.transform(self.original))
        arm = self.source / "arm64"
        self.source_arch.rename(arm)
        self.source_arch = arm
        self.tlb = arm / "example.tlb"

    def invoke(self, **kwargs):
        main = self.midl["main"]

        def arm_main(environment, *args):
            self.assertEqual(environment, "environment.x64")
            self.assertEqual(args[-2:], ("/env", "x64"))
            return main(str(self.env), *args[:-1], "arm64")

        self.env = self.root / "environment.arm64"
        self.env.write_bytes(b"PATH=fixture\0\0")
        self.midl["main"] = arm_main
        try:
            result = super().invoke(**kwargs)
        finally:
            self.midl["main"] = main
        for args, _ in self.native_calls:
            self.assertEqual(args[args.index("/env") + 1], "arm64")
        return result


class Windows15579Arm64MidlFunctionsTest(Windows155Arm64MidlFunctionsTest):
    transform = staticmethod(repair.transform15579)


class Windows15579X64MidlFunctionsTest(previous.Windows154MidlFunctionsTest):
    def setUp(self):
        super().setUp()
        self.midl = previous.load_functions(repair.transform15579(self.original))


if __name__ == "__main__":
    unittest.main()
