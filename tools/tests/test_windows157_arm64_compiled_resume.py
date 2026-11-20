"""Original ARM stage6 producer admission and isolated compiled232-to235 continuation."""
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import windows157_arm64_compiled_resume as arm
import windows157_compiled_resume as x64
import gen_windows_arm64_workflow as generator

NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)
FIXTURE = json.loads(gzip.decompress((ROOT / "tools/tests/fixtures/windows157_arm64_stage6.json.gz").read_bytes()))


class Client:
    def __init__(self):
        self.data = deepcopy(FIXTURE)

    def get(self, path):
        return self.data["run"]

    def items(self, path, key):
        return self.data[key][key]


def test_real_stage6_producer_not_stage7_upload_steps():
    m = arm.load(ROOT)
    origin = arm.metadata(Client(), m, NOW)
    assert (origin["run_id"], origin["job_id"], origin["stage"], origin["attempt"], origin["arch"]) == (
        44541436609, 136332986647, 6, 1, "arm64")
    assert [a["id"] for a in origin["artifacts"]] == [14658294686, 14659132255]
    assert m["source_receipt"]["patch_count"] == 232
    assert m["failed_successor"] == dict(job_id=136501397575, stage=7, restored=False, new_source_tree=False)
    assert m["reuse_receipts"]["result.json"]["retention_proven"] is False
    assert m["source_receipt"]["outputs"] != x64.load(ROOT)["source_receipt"]["outputs"]
    arm.recovery.validate_selection(ROOT, m)


@pytest.mark.parametrize("mutation", ["sha", "attempt", "job", "upload", "digest", "size", "expiry",
                                     "missing", "extra", "late_upload", "stage7_tree", "later", "snapshot"])
def test_fail_closed_metadata(mutation):
    m = arm.load(ROOT)
    c = Client()
    producer = next(j for j in c.data["jobs"]["jobs"] if j["id"] == m["job_id"])
    successor = next(j for j in c.data["jobs"]["jobs"] if j["id"] == 136501397575)
    artifact = next(a for a in c.data["artifacts"]["artifacts"] if a["id"] == 14658294686)
    if mutation == "sha": c.data["run"]["head_sha"] = "0" * 40
    if mutation == "attempt": c.data["run"]["run_attempt"] = 2
    if mutation == "job": producer["id"] += 1
    if mutation == "upload": next(s for s in producer["steps"] if s["name"] == "Upload tree part 4")["conclusion"] = "failure"
    if mutation == "snapshot": next(s for s in successor["steps"] if s["name"] == "Ensure build tree snapshot")["conclusion"] = "success"
    if mutation == "digest": artifact["digest"] = "sha256:" + "0" * 64
    if mutation == "size": artifact["size_in_bytes"] -= 1
    if mutation == "expiry": artifact["expired"] = True
    if mutation == "missing": c.data["artifacts"]["artifacts"].remove(artifact)
    if mutation == "extra": c.data["artifacts"]["artifacts"].append(dict(artifact, id=1))
    if mutation == "late_upload": artifact["created_at"] = successor["started_at"]
    if mutation == "stage7_tree": c.data["artifacts"]["artifacts"].append(dict(artifact, id=1, name="win-arm64-tree-s7-attempt-1-part1"))
    if mutation == "later": next(j for j in c.data["jobs"]["jobs"] if j["name"] == "stage 8 (resume compile)")["conclusion"] = "success"
    with pytest.raises(ValueError): arm.metadata(c, m, NOW)


def test_expired_donor_cannot_fall_back():
    with pytest.raises(ValueError, match="expiry"):
        arm.metadata(Client(), arm.load(ROOT), datetime(2027, 1, 1, tzinfo=timezone.utc))


def test_profile_does_not_mutate_x64_defaults():
    assert arm.recovery is not x64
    assert (x64.ARCH, x64.OUTPUT, x64.FIRST_STAGE, x64.WORKFLOW) == (
        "x64", "out/Chromix", 4, "build-win-x64-157-compiled-resume")
    assert (arm.recovery.ARCH, arm.recovery.OUTPUT, arm.recovery.FIRST_STAGE) == ("arm64", "out/Default", 7)
    assert x64.expected(x64.load(ROOT))["stage"] == 3
    assert (ROOT / ".github/workflows/build-win-x64-157-compiled-resume.yml").read_bytes() == subprocess.check_output(
        ["git", "-C", str(ROOT), "show", "f679ecc:.github/workflows/build-win-x64-157-compiled-resume.yml"])


def test_arm_workflow_stage7_to12_and_original_native_acceptance():
    w = generator.arm64_compiled_resume_workflow()
    assert set(w["jobs"]) == {*(f"build-{i}" for i in range(7, 13)), "complete", "verify-arm64"}
    assert w["on"] == {"workflow_dispatch": {"inputs": {"compile_jobs": {"type": "string", "default": "auto"}}}}
    assert w["env"]["CHROMIX_USE_UPSTREAM_CACHE"] == "1"
    assert w["jobs"]["complete"]["needs"] == [f"build-{i}" for i in range(7, 13)]
    assert w["jobs"]["verify-arm64"] == generator.workflow()["jobs"]["verify-arm64"]
    for i in range(7, 13):
        j = w["jobs"][f"build-{i}"]
        assert j["runs-on"] == "windows-2022"
        if i == 7:
            assert "needs" not in j and "refs/heads/main" in j["if"]
            restore = next(s for s in j["steps"] if s.get("name") == "Restore and migrate original ARM64 stage6 checkpoint")
            assert "windows157_arm64_compiled_resume.py" in restore["run"] and "--phase migrate" in restore["run"]
        else:
            assert j["needs"] == f"build-{i-1}"
        stage = next(s for s in j["steps"] if s.get("id") == "stage")
        assert stage["run"] == f"build\\windows\\ci-stage.ps1 -StageIndex {i} -MaxStages 12 -FromSynced -CompiledRecovery -Arch arm64"
        assert any(s.get("name") == "Upload final source receipt" for s in j["steps"])
        for step in j["steps"]:
            if step.get("name", "").startswith("Upload tree part"):
                assert "snapshot_safe" in step["if"] and f"arm64-157-compiled-tree-s{i}" in step["with"]["name"]
    assert (ROOT / f".github/workflows/{arm.WORKFLOW}.yml").read_text() == generator.render_arm64_compiled_resume()


def test_arm_consumer_requires_new_main_run_stage7_to12():
    env = dict(GITHUB_REPOSITORY="xiaozhou26/Chromix", GITHUB_REF="refs/heads/main", GITHUB_EVENT_NAME="workflow_dispatch",
               GITHUB_WORKFLOW_REF=f"xiaozhou26/Chromix/.github/workflows/{arm.WORKFLOW}.yml@refs/heads/main",
               GITHUB_JOB="build-7", GITHUB_SHA="a" * 40, GITHUB_RUN_ID="999", GITHUB_RUN_ATTEMPT="1")
    with mock.patch.dict(os.environ, env): arm.consumer()
    for key, value in [("GITHUB_JOB", "build-6"), ("GITHUB_JOB", "build-13"), ("GITHUB_RUN_ID", "44541436609"),
                       ("GITHUB_EVENT_NAME", "push"), ("GITHUB_REF", "refs/heads/arm")]:
        with mock.patch.dict(os.environ, dict(env, **{key: value})), pytest.raises(ValueError): arm.consumer()


def test_arm_gn_exact_no_cold_output_and_no_x64_profile(tmp_path):
    work = tmp_path.resolve() / "chromix"
    out = work / "src/out/Default"
    out.mkdir(parents=True)
    args = out / "args.gn"
    args.write_text(arm.load(ROOT)["args_gn"])
    arm.retained_gn_contract(ROOT, work)
    args.write_text(args.read_text().replace('target_cpu = "arm64"', 'target_cpu = "x64"'))
    with pytest.raises(ValueError, match="GN"): arm.retained_gn_contract(ROOT, work)
    args.write_text(arm.load(ROOT)["args_gn"])
    (work / "src/out/Chromix").mkdir()
    with pytest.raises(ValueError, match="cold"): arm.retained_gn_contract(ROOT, work)


def test_no_sdk_installer_changes_or_cold_reprepare():
    path = "build/windows/ensure-windows-sdk.ps1"
    assert (ROOT / path).read_bytes().replace(b"\r\n", b"\n") == subprocess.check_output(
        ["git", "-C", str(ROOT), "show", "f679ecc:" + path]).replace(b"\r\n", b"\n")
    ci = (ROOT / "build/windows/ci-stage.ps1").read_text()
    assert 'if ($RestoredUpstream -and -not $CompiledRecovery)' in ci
    assert 'if (-not $CompiledRecovery)' in ci
    assert 'windows157-arm64-compiled-recipe' in ci


def arm_sources(root):
    donor = json.loads(gzip.decompress((ROOT / "tools/tests/fixtures/windows157_compiled_stage3.json.gz").read_bytes()))
    m = arm.load(ROOT)
    for name, text in donor["sources"].items():
        raw = text.encode()
        expected = m["source_receipt"]["outputs"].get(name)
        if expected and hashlib.sha256(raw).hexdigest() != expected:
            raw = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            assert hashlib.sha256(raw).hexdigest() == expected
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    return root


def test_real_arm_receipt_all232_preimages_and235_roundtrip_preserve_crlf(tmp_path):
    from test_windows157_compiled_resume import real_stack
    src = arm_sources(tmp_path.resolve() / "src")
    m = arm.load(ROOT)
    arm.recovery.verify_hashes(src, m["source_receipt"]["outputs"])
    before, revised = arm.recovery.private_migration(src, real_stack())
    assert {name for name in revised if before[name][0] != revised[name]} == set(arm.recovery.TARGETS)
    assert revised["third_party/blink/public/devtools_protocol/domains/CSS.pdl"].count(b"\r\n") > 100
    for pin in m["additions"]:
        assert hashlib.sha256(revised[pin["target"]]).hexdigest() == pin["after"]
    (src / arm.recovery.PENDING).write_text("pending")
    (src / arm.recovery.BLOCKER).write_text("pending")
    arm.recovery.publish_sources(src, before, revised)
    arm.recovery.verify_hashes(src, {n: hashlib.sha256(raw).hexdigest() if raw is not None else None
                                     for n, raw in revised.items()})


def test_scoped_windows_recipe_rejects_windows_change_but_not_linux_metadata():
    m = json.loads((ROOT / "build/patch-selection-157.json").read_bytes())
    baseline = arm.recovery.windows_recipe_projection(m)
    changed = deepcopy(m)
    changed["platforms"]["linux"]["selection_status"] = "unrelated-platform"
    assert arm.recovery.windows_recipe_projection(changed) == baseline
    changed["platforms"]["windows"]["additions"][0]["sha256"] = "0" * 64
    assert arm.recovery.windows_recipe_projection(changed) != baseline
    assert arm.recovery.scoped_recipe(ROOT, "git") == "397ac53ac7e816cc2e0ece888288c524407b534d69624f1741a8f8fb51f25b00"


def test_full_arm_transaction_keeps_cache_receipts_pgo_outputs_and_three_source_edits(tmp_path):
    import struct
    from test_windows157_compiled_resume import real_stack
    from test_windows157 import put, sources
    from test_windows_metagen_bindings import pe_dll
    import repair_windows_midl as midl
    import base64
    import io
    import zipfile
    m = arm.load(ROOT)
    work = tmp_path.resolve() / "chromix"
    src = arm_sources(work / "src")
    sources(src, domain=True)
    payload = (ROOT / "tools/tests/fixtures/windows153_midl.py").read_bytes()
    put(src / "build/toolchain/win/midl.py", payload.replace(midl.BEFORE, midl.AFTER).replace(midl.COMPARE_BEFORE, midl.COMPARE_AFTER))
    view = tmp_path.resolve() / arm.recovery.VIEW
    arm.recovery.recipe_view(ROOT, view, "git")
    markers = {".chromix-source-unpacked": m["version"], ".chromix-ungoogled-core": m["core"],
               ".chromix-ungoogled-windows": m["windows"], ".chromix-binaries-pruned": m["core"],
               ".chromix-domain-substituted": m["core"], ".chromix-patches": m["old_preparation_key"],
               ".chromix-patch-selection": m["old_preparation_key"],
               ".chromix-source-ready": "|".join((m["version"], m["core"], m["windows"], m["old_preparation_key"]))}
    for name, value in markers.items(): put(src / name, value + "\n")
    put(work / ".chromix-target-arch", "arm64\n")
    put(src / ".chromix-upstream-restored.json", json.dumps(m["upstream_receipt"]))
    put(src / ".chromix-restored-patches.json", json.dumps(m["restored_patches"]))
    put(work / "upstream-cache-preparation.json", json.dumps(m["preparation_receipt"]))
    for name, value in m["reuse_receipts"].items(): put(work / "upstream-reuse" / name, json.dumps(value))
    put(work / "fingerprint-diagnostics/source-stage6.json", json.dumps(m["source_receipt"]))
    put(src / "out/Default/args.gn", m["args_gn"])
    put(src / "out/Default/build.ninja", "build chrome: phony\n")
    pe = bytearray(pe_dll())
    struct.pack_into("<H", pe, 86, 2)
    for name in ("out/Default/gn.exe", "third_party/rust-toolchain/bin/bindgen.exe",
                 arm.tools.CRUBIT_ROOT + "/bin/cc_bindings_from_rs.exe",
                 "third_party/llvm-build/Release+Asserts/bin/clang-cl.exe",
                 "third_party/llvm-build/Release+Asserts/bin/llvm-profdata.exe"):
        put(src / name, pe)
    actual = json.loads(gzip.decompress((ROOT / "tools/tests/fixtures/windows157_ninja_donor.json.gz").read_bytes()))
    ninja = zipfile.ZipFile(io.BytesIO(base64.b64decode(actual["official_ninja_zip_base64"]))).read("ninja.exe")
    put(src / "third_party/ninja/ninja.exe", ninja)
    put(src / "out/Default/.ninja_log", actual["log"]["text"])
    put(src / "out/Default/.ninja_deps", base64.b64decode(actual["deps"]["prefix_base64"]))
    put(src / "out/Default/obj/third_party/abseil-cpp/absl/base/base/casts.obj", b"retained ARM object")
    put(src / "chrome/build/win-arm64.pgo.txt", arm.tools.PGO["arm64"] + "\n")
    put(src / "chrome/build/pgo_profiles" / arm.tools.PGO["arm64"], b"ARM PGO fixture")
    # Tool binaries are fixture payloads; source receipts and both complete patch replays are real.
    original_sha = arm.tools.sha
    fixture_hashes = {}
    for name, expected in m["preparation_receipt"]["rust_libraries"].items():
        put(src / name, b"retained Rust library")
        fixture_hashes[src / name] = expected
    for entry in m["preparation_receipt"]["generator_fingerprint"].values():
        path = src / entry["path"]
        if not path.exists(): put(path, b"retained generator")
        fixture_hashes[path] = entry["sha256"]
    def sha(path): return fixture_hashes.get(path) or original_sha(path)
    selected, _ = arm.recovery.selected_recipe(view)
    identity = deepcopy(m["source_receipt"]["identity"])
    identity["selection"] = selected["selection"]
    identity["series"] = dict(path="patches/series", sha256=selected["series_sha256"], patches=selected["patches"])
    state = arm.recovery.output_state(src / "out")
    receipt_bytes = (src / ".chromix-restored-patches.json").read_bytes()
    with mock.patch.object(arm.tools, "sha", side_effect=sha), mock.patch.object(
            arm.recovery, "source_stack", return_value=(identity, real_stack(), work / "core", work / "windows")):
        result = arm.recovery.migrate(ROOT, view, work, m, "a" * 40)
    assert result["arch"] == "arm64" and result["output_directory"] == "out/Default"
    assert result["source"]["patch_count"] == 235
    assert result["binding"]["original_producer_job_id"] == 136332986647
    assert result["binding"]["upstream_object_reuse_proven"] is False
    assert arm.recovery.output_state(src / "out") == state
    assert (src / ".chromix-restored-patches.json").read_bytes() == receipt_bytes
    assert (src / "chrome/build/pgo_profiles" / arm.tools.PGO["arm64"]).read_bytes() == b"ARM PGO fixture"
    assert not (src / arm.recovery.PENDING).exists() and not (src / arm.recovery.BLOCKER).exists()
    arm.recovery.verify_hashes(src, result["source"]["outputs"])


def test_legacy_x64_receipt_has_no_new_arm_binding_requirement(tmp_path):
    work = tmp_path.resolve() / "chromix"
    (work / "src").mkdir(parents=True)
    m = x64.load(ROOT)
    receipt = dict(status="migrated", target_sha="a" * 40, donor=x64.expected(m), preparation_key="key",
                   changed_source_files=list(x64.TARGETS), source={"patch_count": 235}, arch="x64",
                   consumer_run_id="999", consumer_attempt="1")
    (work / x64.RECEIPT).write_text(json.dumps(receipt))
    # The historical receipt passes identity admission and reaches the unchanged marker checks.
    with mock.patch.dict(os.environ, GITHUB_RUN_ID="999", GITHUB_RUN_ATTEMPT="1"), \
            mock.patch.object(x64, "compiled_contract"), mock.patch.object(x64, "retained_gn_contract"), \
            mock.patch.object(x64, "preparation_key", return_value="key"):
        with pytest.raises(Exception) as error:
            x64.resume(ROOT, ROOT, work, m, "a" * 40)
        assert "continuation identity mismatch" not in str(error.value)
        assert ".chromix-patches" in str(error.value)
