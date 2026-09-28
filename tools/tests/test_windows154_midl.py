"""Verified Windows154 donor pins and isolated, non-native MIDL regression cases."""
import array
import ast
import builtins
from contextlib import redirect_stdout
import filecmp
from functools import reduce
import hashlib
import io
import json
import operator
import os
from pathlib import Path
import posixpath
import re
import shutil
import stat
import struct
import tempfile
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest import mock
import uuid
import warnings

from tools import repair_windows_midl as repair


# The verified 154 artifact's MIDL bytes are identical to this existing fixture.
FIXTURE = Path(__file__).parent / "fixtures/windows153_midl.py"
ORIGINAL_SHA256 = "0c0062cc6b73e1d0874e2a3304498f48ad1cb552d11d49eea223319051dde831"
REPAIRED_SHA256 = "466824620fd2348229c24d477d114e4c7acc0a5e75a8f1b162729809a0dfccde"
GUID = "158428A4-6014-4978-83BA-9FAD0DABE791"
REPLACEMENT = "D0E1CACC-C63C-4192-94AB-BF8EAD0E3B83"
FUNCTIONS = frozenset((
    "ZapTimestamp", "get_tlb_contents", "recreate_guid_hashtable", "overwrite_guids_h",
    "get_uuid_format", "get_uuid_format_iid_file", "overwrite_guids_iid",
    "get_uuid_format_proxy_file", "overwrite_guids_proxy", "getguid", "setguid",
    "overwrite_guids_tlb", "overwrite_guids", "generate_idl_from_template",
    "uuid5_substitutions", "main",
))
ARM_IDENTITY = {
    "chromium_version": "154.0.8037.57",
    "ungoogled_commit": "800d0bb5078472e4442c1fd73373172754a60939",
    "head_sha": "fc387c7527f875ca73c82ed4907fccaa86808c9a",
    "platform": "windows", "arch": "arm64",
    "repository": "ungoogled-software/ungoogled-chromium-windows",
    "repository_id": 177210827,
    "head_branch": "154.0.8037.57-1.1", "event": "push",
    "workflow_path": ".github/workflows/build-arm.yml",
    "run_id": 36093095856,
    "artifact_id": 10939470078, "artifact_name": "build-artifact-arm",
    "artifact_digest": "sha256:4f6e341e4a9dec0b2ffc5e7dab81d8bebc4074b91b55acfa78f6e9b443c25133",
    "artifact_size_in_bytes": 16009896290,
}

IDENTITY = {
    "chromium_version": "154.0.8037.57",
    "ungoogled_commit": "800d0bb5078472e4442c1fd73373172754a60939",
    "head_sha": "fc387c7527f875ca73c82ed4907fccaa86808c9a",
    "platform": "windows", "arch": "x64",
    "repository": "ungoogled-software/ungoogled-chromium-windows",
    "repository_id": 177210827,
    "head_branch": "154.0.8037.57-1.1", "event": "push",
    "workflow_path": ".github/workflows/build-x64.yml",
    "run_id": 36093095228,
    "artifact_id": 10915484727, "artifact_name": "build-artifact",
    "artifact_digest": "sha256:7a6ba27fa2d056759d1e635f486e68cbfed36ef2d73ee201527e1ddb52d0d4a4",
    "artifact_size_in_bytes": 15716545319,
}


def load_functions(payload):
    """Compile only reviewed functions, never archive imports or run_midl."""
    if hashlib.sha256(payload).hexdigest() not in (ORIGINAL_SHA256, REPAIRED_SHA256):
        raise ValueError("unknown MIDL function fixture")
    functions = [node for node in ast.parse(payload).body
                 if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
    if {node.name for node in functions} != FUNCTIONS or len(functions) != len(FUNCTIONS):
        raise ValueError("unexpected MIDL function inventory")
    namespace = {
        "array": array, "filecmp": filecmp, "reduce": reduce, "operator": operator,
        "os": os, "posixpath": posixpath, "re": re, "shutil": shutil, "struct": struct,
        "uuid": uuid, "sys": SimpleNamespace(platform="win32"),
        "run_midl": mock.Mock(side_effect=AssertionError("native execution forbidden")),
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), "reviewed-midl-functions", "exec"), namespace)
    return namespace


def msft_tlb(guid=GUID):
    header = bytearray(0x54 + 15 * 16)
    header[:8] = b"MSFT\x02\x00\x01\x00"
    hash_offset = len(header)
    guid_offset = hash_offset + 0x80
    custom_offset = guid_offset + 24
    struct.pack_into("<II", header, 0x54 + 4 * 16, hash_offset, 0x80)
    struct.pack_into("<II", header, 0x54 + 5 * 16, guid_offset, 24)
    struct.pack_into("<II", header, 0x54 + 11 * 16, custom_offset, 0x54)
    custom = (b"\x08\x00\x3e\x00\x00\x00"
              b"Created by MIDL version 8.01.0622 at Tue Jan 19 03:14:07 2038\n"
              b"\x13\x00\xff\xff\xff\x7fWW\x13\x00\x6e\x02\x01\x08WW")
    return bytes(header) + b"\xff" * 0x80 + uuid.UUID(guid).bytes_le + b"\xff" * 8 + custom


def snapshot(path):
    return path.read_bytes(), repair._identity(path.lstat())


def graph(names):
    rule = "__chrome_elevation_service_elevation_service_idl_idl_action___build_toolchain_win_win_clang_x64__rule"
    return (f"rule {rule}\n"
            "  command = python3 ../../build/toolchain/win/midl.py environment.x64 source gen/midl\n"
            "  description = ACTION //chrome/elevation_service:elevation_service_idl_idl_action"
            "(//build/toolchain/win:win_clang_x64)\n"
            "  restat = 1\n  pool = build_toolchain_action_pool\n"
            f"build {' '.join(names)}: {rule} | ../../build/toolchain/win/midl.py\n")


class Windows154MidlIdentityTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="windows154-midl-")
        self.addCleanup(temporary.cleanup)
        self.src = Path(temporary.name)
        self.script = self.src / repair.SCRIPT
        self.script.parent.mkdir(parents=True)
        self.original = FIXTURE.read_bytes()
        self.script.write_bytes(self.original)
        self.identity = dict(IDENTITY)

    def apply(self, **kwargs):
        return repair.apply(self.src, "windows", kwargs.pop("arch", "x64"),
                            kwargs.pop("identity", self.identity), **kwargs)

    def outputs(self):
        out = self.src / "out/Default"
        names = ["gen/midl/a.h", "gen/midl/a_i.c", "gen/midl/a.tlb"]
        out.mkdir(parents=True)
        (out / "build.ninja").write_text(graph(names))
        for name in names:
            path = out / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"stale")
            os.utime(path, ns=(4102444800000000000, 4102444800000000000))
        return out, [out / name for name in names]

    def test_exact_transform_is_shared_without_changing_153_hashes(self):
        self.assertEqual(hashlib.sha256(self.original).hexdigest(), ORIGINAL_SHA256)
        self.assertEqual(repair.ORIGINAL_SHA256, ORIGINAL_SHA256)
        self.assertEqual(repair.REPAIRED_SHA256, REPAIRED_SHA256)
        repaired = repair.transform154(self.original)
        self.assertEqual(repaired, repair.transform(self.original))
        self.assertEqual(hashlib.sha256(repaired).hexdigest(), REPAIRED_SHA256)
        self.assertIs(repair.transform154(repaired), repaired)
        self.assertEqual(dict(repair.WINDOWS154_RECORDS[0]["identity"]), IDENTITY)
        self.assertEqual(dict(repair.WINDOWS154_RECORDS[1]["identity"]), ARM_IDENTITY)

    def test_arm64_identity_is_explicit_and_repaired_without_x64_substitution(self):
        result = self.apply(arch="arm64", identity=ARM_IDENTITY)
        self.assertEqual(result["status"], "repaired")
        self.assertEqual(json.loads((self.src / repair.RECORD).read_text())["identity"], ARM_IDENTITY)

    def test_unknown_partial_crlf_and_corrupted_pin_rejected_before_output_deletion(self):
        _, outputs = self.outputs()
        for raw in (b"", self.original + b"# changed\n", self.original.replace(b"\n", b"\r\n"),
                    self.original.replace(repair.BEFORE, repair.AFTER, 1)):
            self.script.write_bytes(raw)
            before = snapshot(self.script)
            with self.subTest(raw_hash=hashlib.sha256(raw).hexdigest()), self.assertRaises(ValueError):
                self.apply()
            self.assertEqual(snapshot(self.script), before)
            self.assertTrue(all(path.read_bytes() == b"stale" for path in outputs))
        self.script.write_bytes(self.original)
        for key in ("WINDOWS154_ORIGINAL_SHA256", "WINDOWS154_REPAIRED_SHA256"):
            with self.subTest(key=key), mock.patch.object(repair, key, "0" * 64), self.assertRaises(ValueError):
                self.apply()
        self.assertEqual(self.script.read_bytes(), self.original)

    def test_every_identity_field_types_unknown_version_and_arm_fail_closed(self):
        for key, value in self.identity.items():
            bad = "154.0.8037.58" if key == "chromium_version" else "wrong"
            alternatives = (bad, float(value)) if type(value) is int else (bad,)
            for alternative in alternatives:
                with self.subTest(key=key, alternative=alternative), self.assertRaisesRegex(ValueError, "identity"):
                    self.apply(identity=dict(self.identity, **{key: alternative}))
        for identity in ({"chromium_version": "154.0.8037.57"}, dict(self.identity, extra=True),
                         dict(self.identity, arch="arm64")):
            with self.assertRaisesRegex(ValueError, "identity"):
                self.apply(identity=identity)
        with self.assertRaisesRegex(ValueError, "identity"):
            self.apply(arch="arm64", identity=dict(self.identity, arch="arm64"))
        self.assertEqual(self.script.read_bytes(), self.original)

    def test_records_are_explicit_immutable_and_known_hash_only(self):
        record = repair.WINDOWS154_RECORDS[0]
        with self.assertRaises(TypeError):
            record["identity"]["artifact_id"] = 1
        with self.assertRaises(TypeError):
            record["before_sha256"] = "0" * 64
        for records in ((), [record], (record, record), (dict(record),),
                        (MappingProxyType(dict(record, identity=dict(IDENTITY))),),
                        (MappingProxyType(dict(record, before_sha256="0" * 64)),),
                        (MappingProxyType(dict(record, after_sha256="0" * 64)),)):
            with self.subTest(records=records), self.assertRaises(ValueError):
                self.apply(records=records)
        self.assertEqual(self.apply(records=(record,))["status"], "repaired")

    def test_once_only_proof_keeps_source_tlb_and_unrelated_future_outputs(self):
        out, outputs = self.outputs()
        source = self.src / "third_party/win_build_output/midl/x64/source.tlb"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"")
        other = out / "gen/midl/not_declared.tlb"
        other.write_bytes(b"unrelated")
        self.script.chmod(0o755)
        baseline = {path: snapshot(path) for path in (source, other)}
        first = self.apply()
        self.assertEqual(first["invalidated_outputs"], sorted(path.relative_to(out).as_posix() for path in outputs))
        self.assertEqual(first["before_sha256"], ORIGINAL_SHA256)
        self.assertEqual(first["after_sha256"], REPAIRED_SHA256)
        self.assertEqual(stat.S_IMODE(self.script.stat().st_mode), 0o755)
        self.assertTrue(all(not path.exists() for path in outputs))
        proof_path = self.src / repair.RECORD
        proof = json.loads(proof_path.read_text())
        self.assertEqual(set(proof), {"schema_version", "identity", "before_sha256", "after_sha256", "outputs", "graphs"})
        self.assertEqual(proof["schema_version"], 1)
        self.assertEqual(proof["identity"], IDENTITY)
        for path in outputs:
            path.write_bytes(b"regenerated")
        baseline.update({path: snapshot(path) for path in (*outputs, self.script, proof_path)})
        second = self.apply()
        self.assertEqual(second["status"], "verified")
        self.assertEqual(second["invalidated_outputs"], [])
        self.assertEqual(second["before_sha256"], REPAIRED_SHA256)
        for path, state in baseline.items():
            self.assertEqual(snapshot(path), state)

    def test_153_proof_cannot_authorize_154_or_repeat_invalidation(self):
        _, outputs = self.outputs()
        repair.apply(self.src, "windows", "x64", dict(repair.IDENTITIES["x64"]))
        for path in outputs:
            path.write_bytes(b"regenerated")
        with self.assertRaisesRegex(ValueError, "invalidation record"):
            self.apply()
        self.assertTrue(all(path.read_bytes() == b"regenerated" for path in outputs))

    def test_missing_corrupted_or_reverted_proof_never_reinvalidates(self):
        _, outputs = self.outputs()
        self.apply()
        record = self.src / repair.RECORD
        good = record.read_bytes()
        for path in outputs:
            path.write_bytes(b"keep")
        record.unlink()
        with self.assertRaisesRegex(ValueError, "lacks one-time"):
            self.apply()
        value = json.loads(good)
        for change in ({"before_sha256": "0" * 64}, {"after_sha256": "0" * 64},
                       {"outputs": ["../../source.tlb"]},
                       {"identity": dict(IDENTITY, artifact_id=float(IDENTITY["artifact_id"]))}):
            record.write_text(json.dumps(dict(value, **change)))
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.apply()
        record.write_bytes(good)
        self.script.write_bytes(self.original)
        with self.assertRaisesRegex(ValueError, "reverted"):
            self.apply()
        self.assertTrue(all(path.read_bytes() == b"keep" for path in outputs))

    def test_unverified_graphs_cannot_touch_source_or_declared_outputs(self):
        out, outputs = self.outputs()
        graph_path = out / "build.ninja"
        for text in (graph(["../../third_party/win_build_output/midl/source.tlb"]),
                     graph(["obj/not_midl.obj"]), graph(["gen/midl/a.tlb"]).replace("python3 ", "echo "),
                     graph(["gen/midl/a.tlb"]).replace(" | ../../", " ../../")):
            graph_path.write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.apply()
            self.assertTrue(all(path.read_bytes() == b"stale" for path in outputs))
            self.assertEqual(self.script.read_bytes(), self.original)

    def test_linked_script_and_output_fail_before_mutation(self):
        _, outputs = self.outputs()
        external = self.src / "external"
        external.write_bytes(b"outside")
        for path in (self.script, outputs[-1]):
            content = path.read_bytes()
            for hard in (False, True):
                path.unlink()
                os.link(external, path) if hard else path.symlink_to(external)
                with self.subTest(path=path, hard=hard), self.assertRaisesRegex(ValueError, "linked or unsafe"):
                    self.apply()
                self.assertEqual(external.read_bytes(), b"outside")
                self.assertTrue(outputs[0].exists())
                path.unlink()
                path.write_bytes(content)


class Windows154MidlFunctionsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="windows154-functions-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.original = FIXTURE.read_bytes()
        self.midl = load_functions(repair.transform154(self.original))
        self.source = self.root / "third_party/win_build_output/midl/example"
        self.source_arch = self.source / "x64"
        self.source_arch.mkdir(parents=True)
        self.names = ("example.h", "example_i.c", "example_p.c", "example.tlb")
        for name in self.names:
            (self.source_arch / name).write_bytes(msft_tlb() if name.endswith(".tlb") else GUID.encode())
        self.tlb = self.source_arch / "example.tlb"
        self.idl = self.root / "example.idl.template"
        self.idl.write_text("PLACEHOLDER-GUID-" + GUID)
        self.env = self.root / "environment.x64"
        self.env.write_bytes(b"PATH=fixture\0\0")
        self.native_calls = []
        self.parser_inputs = []
        warning_context = warnings.catch_warnings()
        warning_context.__enter__()
        self.addCleanup(warning_context.__exit__, None, None, None)
        warnings.simplefilter("ignore", ResourceWarning)

    def invoke(self, *, dynamic=True, tlb=True, ignored=False, returncode=0, missing=None,
               equal_times=False, dynamic_spec=None):
        output = self.root / "out/Default/gen/example"
        output.mkdir(parents=True, exist_ok=True)
        self.midl["open"] = lambda path, *args, **kwargs: builtins.open(
            self.env if path == "environment.x64" else path, *args, **kwargs)
        original_parser = self.midl["get_tlb_contents"]
        def parser(path):
            self.parser_inputs.append(Path(path).read_bytes())
            return original_parser(path)
        self.midl["get_tlb_contents"] = parser

        def generate(args, env):
            self.native_calls.append((args, env))
            self.assertEqual(env, {"PATH": "fixture"})
            native = self.root / "fake-native"
            native.mkdir(exist_ok=True)
            if returncode:
                return returncode, str(native)
            guid = Path(args[-1]).read_text()
            self.assertNotIn("PLACEHOLDER-GUID-", guid)
            for option in ("/h", "/iid", "/proxy", "/tlb"):
                if option not in args:
                    continue
                name = args[args.index(option) + 1]
                if name == missing:
                    continue
                path = native / name
                path.write_bytes(msft_tlb(guid) if option == "/tlb" else guid.encode())
                self.midl["ZapTimestamp"](str(path))
                if equal_times:
                    for target in (path, output / name):
                        os.utime(target, ns=(123456789000, 123456789000))
            return 0, str(native)

        self.midl["run_midl"] = generate
        with redirect_stdout(io.StringIO()):
            result = self.midl["main"](
                "environment.x64", str(self.source), str(output),
                dynamic_spec if dynamic_spec is not None else
                ((("ignore_proxy_stub," if ignored else "") + f"PLACEHOLDER-GUID-{GUID}={REPLACEMENT}")
                 if dynamic else "none"), "example.tlb" if tlb else "none", "example.h", "none",
                "example_i.c", "example_p.c", "never-executed-clang", str(self.idl), "/env", "x64")
        return result, output

    def assert_generated(self, output):
        self.assertEqual((output / "example.h").read_bytes(), REPLACEMENT.encode())
        self.assertEqual((output / "example_i.c").read_bytes(), REPLACEMENT.encode())
        contents, _, _, offset, _ = self.midl["get_tlb_contents"](str(output / "example.tlb"))
        self.assertEqual(self.midl["getguid"](contents, offset), REPLACEMENT.encode())
        self.assertIn(b"at a redacted point in time", (output / "example.tlb").read_bytes())

    def test_loader_excludes_all_archive_top_level_code_and_native_function(self):
        self.assertNotIn("subprocess", self.midl)
        self.assertNotIn("tempfile", self.midl)
        self.assertIsInstance(self.midl["run_midl"], mock.Mock)
        with self.assertRaisesRegex(ValueError, "unknown MIDL"):
            load_functions(self.original + b"raise RuntimeError('unreviewed')\n")

    def test_original_existing_empty_tlb_asserts_before_native_generation(self):
        self.midl = load_functions(self.original)
        self.tlb.write_bytes(b"")
        before = snapshot(self.tlb)
        with self.assertRaises(AssertionError):
            self.invoke()
        self.assertEqual(self.parser_inputs, [b""])
        self.assertEqual(self.native_calls, [])
        self.assertEqual(snapshot(self.tlb), before)

    def test_patched_empty_tlb_skips_guid_parser_and_preserves_source_identity(self):
        self.tlb.write_bytes(b"")
        before = snapshot(self.tlb)
        result, output = self.invoke()
        self.assertEqual(result, 0)
        self.assertEqual(self.parser_inputs, [])
        self.assertEqual(len(self.native_calls), 1)
        self.assert_generated(output)
        self.assertEqual(snapshot(self.tlb), before)

    def test_original_equal_size_mtime_silently_keeps_stale_header(self):
        self.midl = load_functions(self.original)
        self.idl.write_text(REPLACEMENT)
        self.tlb.write_bytes(b"")
        filecmp.clear_cache()
        result, output = self.invoke(dynamic=False, equal_times=True)
        self.assertEqual(result, 0)
        self.assertEqual((output / "example.h").read_bytes(), GUID.encode())
        self.assertNotEqual((output / "example.h").read_bytes(), REPLACEMENT.encode())

    def test_patched_equal_size_mtime_uses_content_comparison(self):
        self.idl.write_text(REPLACEMENT)
        self.tlb.write_bytes(b"")
        filecmp.clear_cache()
        compare = filecmp.cmpfiles
        with mock.patch.object(filecmp, "cmpfiles", wraps=compare) as call:
            result, output = self.invoke(dynamic=False, equal_times=True)
        self.assertEqual(call.call_args.kwargs, {"shallow": False})
        self.assertEqual(result, 0)
        self.assert_generated(output)

    def test_nonempty_valid_tlb_retains_guid_replacement(self):
        before = snapshot(self.tlb)
        result, output = self.invoke()
        self.assertEqual(result, 0)
        self.assertEqual(self.parser_inputs, [before[0]])
        self.assert_generated(output)
        self.assertEqual(snapshot(self.tlb), before)

    def test_nonempty_invalid_tlb_still_fails_without_native_calls(self):
        self.tlb.write_bytes(b"not a TLB")
        before = snapshot(self.tlb)
        with self.assertRaises(AssertionError):
            self.invoke()
        self.assertEqual(self.native_calls, [])
        self.assertEqual(snapshot(self.tlb), before)

    def test_missing_tlb_keeps_upstream_placeholder_creation(self):
        self.tlb.unlink()
        result, output = self.invoke()
        self.assertEqual(result, 0)
        self.assertEqual(self.parser_inputs, [])
        self.assertEqual(self.tlb.read_bytes(), b"")
        self.assert_generated(output)

    def test_ignored_proxy_only_copies_included_native_outputs(self):
        self.tlb.write_bytes(b"")
        result, output = self.invoke(ignored=True)
        self.assertEqual(result, 0)
        self.assert_generated(output)
        self.assertEqual((output / "example_p.c").read_bytes(), GUID.encode())

    def test_native_failure_is_not_reported_as_success(self):
        self.tlb.write_bytes(b"")
        result, output = self.invoke(returncode=7)
        self.assertEqual(result, 7)
        self.assertEqual(self.parser_inputs, [])
        self.assertEqual((output / "example.tlb").read_bytes(), b"")

    def test_missing_native_output_remains_error(self):
        self.tlb.write_bytes(b"")
        with self.assertRaises(AssertionError):
            self.invoke(missing="example.h")

    def test_no_tlb_still_runs_native_path_without_tlb_parser(self):
        result, _ = self.invoke(tlb=False)
        self.assertEqual(result, 0)
        self.assertEqual(self.parser_inputs, [])
        self.assertNotIn("/tlb", self.native_calls[0][0])

    def test_nonwindows_empty_tlb_behavior_is_unchanged(self):
        self.midl["sys"].platform = "linux"
        self.tlb.write_bytes(b"")
        before = snapshot(self.tlb)
        with self.assertRaises(AssertionError):
            self.invoke()
        self.assertEqual(self.native_calls, [])
        self.assertEqual(snapshot(self.tlb), before)


class Windows154MidlPrepareTest(unittest.TestCase):
    def setUp(self):
        from tools import prepare_restored_build as prepare
        from tools import restore_upstream_cache as restore
        from tools.tests.test_prepare_restored_build import PrepareRestoredBuildTest

        self.prepare, self.restore = prepare, restore
        support = self.support = PrepareRestoredBuildTest()
        support.setUp()
        self.addCleanup(support.doCleanups)
        receipt = support.fixture("windows", "x64")
        repo = support.fixture_repo
        path = repo / "build/upstream-cache.json"
        manifest = json.loads(path.read_text())
        source = {key: value for key, value in IDENTITY.items()
                  if key not in ("platform", "arch") and not key.startswith("artifact_")}
        source.update(source_roots=["src", "build/src"], artifacts={"x64": {
            "id": IDENTITY["artifact_id"], "name": IDENTITY["artifact_name"],
            "digest": IDENTITY["artifact_digest"], "size_in_bytes": IDENTITY["artifact_size_in_bytes"],
            "expires_at": "2026-09-30T21:34:19Z", "inner_archive": "artifacts.zip",
        }})
        manifest["sources"]["windows"] = source
        support.write(path, json.dumps(manifest))
        identity, _, provenance = restore.identities(repo, "windows", "x64")
        self.assertEqual(identity, IDENTITY)
        receipt.update(identity=identity, manifest=provenance,
                       original_args=restore.source_args(support.src, identity))
        support.write(support.src / restore.MARKER, json.dumps(receipt))
        self.script = support.write(support.src / repair.SCRIPT, FIXTURE.read_bytes())
        self.outputs = [support.write(support.out / name, b"stale") for name in (
            "gen/midl/a.h", "gen/midl/a_i.c", "gen/midl/a.tlb")]
        support.write(support.out / "build.ninja", graph(
            [path.relative_to(support.out).as_posix() for path in self.outputs]))

    def test_actual_strict_receipt_precedes_repair_and_inspect_finish_do_not_repeat(self):
        support = self.support
        with support.native_context("windows", "x64"):
            for index, phase in enumerate(("inspect", "finish", "inspect", "finish")):
                result = self.prepare.prepare(support.work, "windows", "x64", phase=phase,
                                              repo=support.fixture_repo)
                self.assertEqual(result["windows_midl"]["status"], "repaired" if index == 0 else "verified")
                if index == 0:
                    self.assertTrue(all(not path.exists() for path in self.outputs))
                    for path in self.outputs:
                        path.write_bytes(b"regenerated")
                else:
                    self.assertEqual(result["windows_midl"]["invalidated_outputs"], [])
                    self.assertTrue(all(path.read_bytes() == b"regenerated" for path in self.outputs))
        self.assertEqual(hashlib.sha256(self.script.read_bytes()).hexdigest(), REPAIRED_SHA256)

    def test_corrupt_restore_receipt_fails_before_repair(self):
        marker = self.support.src / self.restore.MARKER
        value = json.loads(marker.read_text())
        value["identity"]["artifact_digest"] = "sha256:" + "0" * 64
        marker.write_text(json.dumps(value))
        with self.support.native_context("windows", "x64"), \
                mock.patch.object(self.prepare, "repair_windows_midl") as apply, self.assertRaises(ValueError):
            self.prepare.prepare(self.support.work, "windows", "x64", repo=self.support.fixture_repo)
        apply.assert_not_called()
        self.assertEqual(self.script.read_bytes(), FIXTURE.read_bytes())
        self.assertTrue(all(path.read_bytes() == b"stale" for path in self.outputs))


if __name__ == "__main__":
    unittest.main()
