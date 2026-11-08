"""Windows155 dual-arch pins, controlled MIDL runtime, and once-only invalidation."""
import hashlib
import json
from pathlib import Path
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


def install_historical_repository(repo):
    fixture = json.loads((Path(__file__).parent / "fixtures/windows155_repository.json").read_text())
    for name, content in fixture["files"].items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return repo


class Windows155MidlTest(unittest.TestCase):
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

    def apply(self, identity=IDENTITY, arch="x64", **kwargs):
        return repair.apply(self.src, "windows", arch, identity, **kwargs)

    def test_verified_donor_transform_and_idempotent_output_invalidation(self):
        self.assertEqual(dict(repair.WINDOWS155_RECORDS[0]["identity"]), IDENTITY)
        self.assertEqual(hashlib.sha256(self.original).hexdigest(), repair.WINDOWS155_ORIGINAL_SHA256)
        source = self.src / "third_party/win_build_output/midl/x64/source.tlb"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"")
        unrelated = self.out / "gen/unrelated.obj"
        unrelated.write_bytes(b"retain")
        first = self.apply()
        self.assertEqual(first["status"], "repaired")
        self.assertEqual(first["invalidated_outputs"], ["gen/midl/a.tlb"])
        self.assertEqual(self.script.read_bytes(), repair.transform155(self.original))
        self.assertEqual(hashlib.sha256(self.script.read_bytes()).hexdigest(), repair.WINDOWS155_REPAIRED_SHA256)
        self.output.write_bytes(b"regenerated")
        states = {p: previous.snapshot(p) for p in (source, unrelated, self.script, self.output)}
        self.assertEqual(self.apply()["status"], "verified")
        for path, state in states.items():
            self.assertEqual(previous.snapshot(path), state)
        self.assertEqual(json.loads((self.src / repair.RECORD).read_text())["identity"], IDENTITY)

    def test_all_identity_fields_and_types_fail_before_deletion(self):
        for identity in (IDENTITY, ARM_IDENTITY):
            for key, value in identity.items():
                bad = "155.0.8059.40" if key == "chromium_version" else "unknown"
                for alternate in ((bad, float(value)) if type(value) is int else (bad,)):
                    with self.subTest(arch=identity["arch"], key=key), self.assertRaisesRegex(
                            ValueError, "identity"):
                        self.apply(dict(identity, **{key: alternate}), arch=identity["arch"])
        for identity in ({"chromium_version": "155.0.8059.39"}, dict(IDENTITY, extra=True),
                         dict(IDENTITY, arch="arm64")):
            with self.assertRaisesRegex(ValueError, "identity"):
                self.apply(identity)
        with self.assertRaisesRegex(ValueError, "identity"):
            self.apply(dict(IDENTITY, arch="arm64"), arch="arm64")
        self.assertEqual(self.output.read_bytes(), b"stale")
        self.assertEqual(self.script.read_bytes(), self.original)

    def test_unknown_partial_or_crlf_bytes_cannot_delete_outputs(self):
        for payload in (b"", self.original + b"# unknown", self.original.replace(b"\n", b"\r\n"),
                        self.original.replace(repair.BEFORE, repair.AFTER)):
            self.script.write_bytes(payload)
            for identity in (IDENTITY, ARM_IDENTITY):
                with self.subTest(arch=identity["arch"]), self.assertRaises(ValueError):
                    self.apply(identity, arch=identity["arch"])
            self.assertEqual(self.output.read_bytes(), b"stale")
            self.assertEqual(self.script.read_bytes(), payload)

    def test_immutable_records_and_cross_version_proofs(self):
        record = repair.WINDOWS155_RECORDS[0]
        with self.assertRaises(TypeError):
            record["identity"]["artifact_id"] = 1
        for records in ((), [record], (record, record), (dict(record),),
                        (MappingProxyType(dict(record, before_sha256="0" * 64)),)):
            with self.assertRaises(ValueError):
                self.apply(records155=records)
        repair.apply(self.src, "windows", "x64", previous.IDENTITY)
        self.output.write_bytes(b"regenerated")
        with self.assertRaisesRegex(ValueError, "invalidation record"):
            self.apply()
        self.assertEqual(self.output.read_bytes(), b"regenerated")

    def test_arm64_exact_restore_pin_repairs_once_without_touching_source(self):
        from tools.restore_upstream_cache import identities

        repo = install_historical_repository(self.src / "historical-repository")
        identity, _, _ = identities(repo, "windows", "arm64")
        self.assertEqual(identity, ARM_IDENTITY)
        self.assertEqual(len(repair.WINDOWS155_RECORDS), 2)
        self.assertEqual(dict(repair.WINDOWS155_RECORDS[1]["identity"]), ARM_IDENTITY)
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
        self.assertEqual(hashlib.sha256(self.script.read_bytes()).hexdigest(), repair.WINDOWS155_REPAIRED_SHA256)
        self.output.write_bytes(b"regenerated")
        second = self.apply(identity, arch="arm64")
        self.assertEqual(second["status"], "verified")
        self.assertEqual(second["invalidated_outputs"], [])
        self.assertEqual(self.output.read_bytes(), b"regenerated")
        self.assertEqual(previous.snapshot(source), before)
        self.assertEqual(json.loads((self.src / repair.RECORD).read_text())["identity"], ARM_IDENTITY)
        with self.assertRaisesRegex(ValueError, "invalidation record"):
            self.apply()
        self.assertEqual(self.output.read_bytes(), b"regenerated")

    def test_arm64_rejects_mixed_x64_and_historical_donor_fields(self):
        variants = [dict(ARM_IDENTITY, **{key: value}) for key, value in IDENTITY.items()
                    if value != ARM_IDENTITY[key]]
        variants += [dict(ARM_IDENTITY, **{key: value}) for key, value in previous.ARM_IDENTITY.items()
                     if value != ARM_IDENTITY[key] and key != "chromium_version"]
        variants += [dict(ARM_IDENTITY, extra=True),
                     {key: value for key, value in ARM_IDENTITY.items() if key != "artifact_digest"}]
        for identity in variants:
            with self.subTest(identity=identity), self.assertRaisesRegex(ValueError, "identity"):
                self.apply(identity, arch="arm64")
        with self.assertRaisesRegex(ValueError, "identity"):
            self.apply(ARM_IDENTITY, arch="x64")
        self.assertEqual(self.script.read_bytes(), self.original)
        self.assertEqual(self.output.read_bytes(), b"stale")
        self.assertFalse((self.src / repair.RECORD).exists())

    def test_existing154_arm64_and_nonwindows_remain_separate(self):
        self.assertEqual(dict(repair.WINDOWS154_RECORDS[1]["identity"]), previous.ARM_IDENTITY)
        for platform in ("linux", "macos", "off"):
            self.assertEqual(repair.apply(self.src, platform, "x64", IDENTITY)["status"], "not_applicable")
        result = repair.apply(self.src, "windows", "arm64", previous.ARM_IDENTITY)
        self.assertEqual(result["status"], "repaired")
        self.assertEqual(json.loads((self.src / repair.RECORD).read_text())["identity"], previous.ARM_IDENTITY)


class Windows155Arm64MidlFunctionsTest(previous.Windows154MidlFunctionsTest):
    def setUp(self):
        super().setUp()
        self.midl = previous.load_functions(repair.transform155(self.original))
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


if __name__ == "__main__":
    unittest.main()
