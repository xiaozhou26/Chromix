#!/usr/bin/env python3
"""Admit the original Windows157 ARM64 stage6 producer, never its failed successor."""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re

# A separate module instance keeps the x64 defaults unchanged in the same process.
spec = importlib.util.spec_from_file_location("windows157_arm64_shared", Path(__file__).with_name("windows157_compiled_resume.py"))
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
files = recovery.files
tools = recovery.tools
MANIFEST = "build/windows/windows157-arm64-stage6-checkpoint.json"
MANIFEST_SHA256 = "b19cd67c45bc584aaa21210b058ce527a7be52e054d930201909a4b0c9905572"
WORKFLOW = "build-win-arm64-157-compiled-resume"
WORKFLOW_SHA256 = "283b8f458e8594407569e242347641a806d97f35111626c53f5ccda0688941d9"
BASE_CHECKOUT = recovery.checkout


def load(repo):
    raw = files._file(repo, MANIFEST).read_bytes()
    if recovery.digest(raw) != MANIFEST_SHA256:
        raise ValueError("ARM stage6 checkpoint manifest changed")
    return json.loads(raw)


def metadata(client, m, now=None):
    now = now or datetime.now(timezone.utc)
    run = client.get(f'/actions/runs/{m["run_id"]}')
    want = dict(id=m["run_id"], head_sha=m["head_sha"], head_branch="main", event="workflow_dispatch",
                status="completed", conclusion="failure", run_attempt=1,
                workflow_id=m["workflow_id"], path=m["workflow_path"])
    if (any(type(run.get(k)) is not type(v) or run.get(k) != v for k, v in want.items())
            or any(run.get(k, {}).get("id") != m["repository_id"]
                   or run.get(k, {}).get("full_name") != m["repository"] for k in ("repository", "head_repository"))):
        raise ValueError("ARM compiled donor run mismatch")
    jobs = client.items(f'/actions/runs/{m["run_id"]}/attempts/1/jobs', "jobs")
    def job(number, identifier, conclusion):
        name = f"stage {number} (resume compile)"
        found = [j for j in jobs if j.get("id") == identifier or j.get("name") == name]
        expected = dict(id=identifier, name=name, run_id=m["run_id"], head_sha=m["head_sha"],
                        run_attempt=1, status="completed", conclusion=conclusion)
        if len(found) != 1 or any(found[0].get(k) != v for k, v in expected.items()):
            raise ValueError("ARM producer/successor job mismatch")
        return found[0]
    producer = job(6, m["job_id"], "success")
    successor = job(7, m["failed_successor"]["job_id"], "failure")
    def steps(j, expected):
        for name, conclusion in expected.items():
            found = [s for s in j["steps"] if s.get("name") == name]
            if len(found) != 1 or found[0].get("status") != "completed" or found[0].get("conclusion") != conclusion:
                raise ValueError("ARM producer step mismatch: " + name)
    steps(producer, {"Run stage 6": "success", "Download tree from previous stage": "success",
                    "Ensure build tree snapshot": "skipped", "Upload fingerprint diagnostics": "success",
                    "Upload restored reuse evidence": "success", "Upload final bundle": "skipped",
                    **{f"Upload tree part {i}": "success" for i in range(1, 5)}})
    steps(successor, {"Run stage 7": "failure", "Ensure build tree snapshot": "failure",
                      "Download tree from previous stage": "success", "Upload final bundle": "skipped"})
    for j in jobs:
        match = re.fullmatch(r"stage (\d+) \(resume compile\)", j.get("name", ""))
        if match and int(match[1]) > 7 and j.get("conclusion") != "skipped":
            raise ValueError("later ARM compiled producer exists")
    listed = client.items(f'/actions/runs/{m["run_id"]}/artifacts', "artifacts")
    trees = [a for a in listed if re.fullmatch(r"win-arm64-tree-s6-.*", a.get("name", ""))]
    if (len(trees) != 2 or any(re.search(r"tree-s(?:[7-9]|1[0-2])-", a.get("name", "")) for a in listed)):
        raise ValueError("ARM latest valid tree set changed; never accept stage7 reuploads")
    for pin in m["artifacts"] + m["evidence_artifacts"]:
        found = [a for a in listed if a.get("id") == pin["id"] or a.get("name") == pin["name"]]
        if (len(found) != 1 or any(found[0].get(k) != v for k, v in pin.items())
                or found[0].get("expired") is not False
                or datetime.fromisoformat(pin["expires_at"].replace("Z", "+00:00")) <= now
                or any(found[0].get("workflow_run", {}).get(k) != v for k, v in
                       dict(id=m["run_id"], head_sha=m["head_sha"], head_branch="main",
                            repository_id=m["repository_id"], head_repository_id=m["repository_id"]).items())):
            raise ValueError("ARM artifact identity/expiry mismatch")
        if pin in m["artifacts"] and not (producer["started_at"] <= found[0]["created_at"] <= producer["completed_at"]):
            raise ValueError("ARM tree was not uploaded by the original stage6 producer")
    return recovery.expected(m)


def checkout(repo):
    git, head = BASE_CHECKOUT(repo)
    if recovery.digest(files._file(repo, f".github/workflows/{WORKFLOW}.yml").read_bytes()) != WORKFLOW_SHA256:
        raise ValueError("ARM recovery workflow bytes changed")
    base = recovery.origin.git_tree(git, repo, recovery.RECIPE_BASE)
    current = recovery.origin.git_tree(git, repo, head)
    names = {"build/args.windows.arm64.gn", "build/upstream-cache.json",
             "tools/restore_upstream_cache.py", "tools/import_upstream_cache.py",
             "tools/fetch_upstream_cache.py", "tools/restored_reuse_evidence.py",
             "tools/upstream_object_cache.py"}
    names.update(n for n in base if n.startswith("assets/"))
    for name in names:
        if current.get(name) != base.get(name):
            raise ValueError("ARM Windows recipe input changed: " + name)
    return git, head


def retained_gn_contract(view, work):
    if os.name == "nt" and work.resolve().as_posix().casefold() != "c:/c/chromix":
        raise ValueError("ARM compiled checkpoint must remain at C:/c/chromix")
    m = load(Path(__file__).resolve().parents[1])
    if files._file(work / "src", "out/Default/args.gn").read_text() != m["args_gn"]:
        raise ValueError("ARM retained GN arguments differ from the real cache recipe")
    if (work / "src/out/Chromix").exists():
        raise ValueError("ARM cache cannot contain cold out/Chromix")


def compiled_contract(work, m, *, migrated=False):
    src = work / "src"
    for name in (recovery.PENDING, recovery.BLOCKER, ".chromix-patch-in-progress",
                 ".chromix-restored-patches-in-progress", ".chromix-domain-substitution-in-progress"):
        if (src / name).exists():
            raise ValueError("interrupted ARM compiled source: " + name)
    if files._file(work, ".chromix-target-arch").read_text().strip() != "arm64":
        raise ValueError("ARM target marker mismatch")
    if (src / "out/Chromix").exists():
        raise ValueError("ARM cache cannot contain cold out/Chromix")
    for name, expected in {".chromix-source-unpacked": m["version"], ".chromix-ungoogled-core": m["core"],
                           ".chromix-ungoogled-windows": m["windows"], ".chromix-binaries-pruned": m["core"],
                           ".chromix-domain-substituted": m["core"]}.items():
        if files._file(src, name).read_text().strip() != expected:
            raise ValueError("ARM source layer mismatch: " + name)
    if json.loads(files._file(src, ".chromix-upstream-restored.json").read_text()) != m["upstream_receipt"]:
        raise ValueError("original ARM upstream restore receipt mismatch")
    if json.loads(files._file(src, ".chromix-restored-patches.json").read_text()) != m["restored_patches"]:
        raise ValueError("original ARM restored232 receipt mismatch")
    if not migrated:
        for name in (".chromix-patches", ".chromix-patch-selection"):
            if files._file(src, name).read_text().strip() != m["old_preparation_key"]:
                raise ValueError("ARM old232 preparation key mismatch")
        if files._file(src, ".chromix-source-ready").read_text().strip() != "|".join(
                (m["version"], m["core"], m["windows"], m["old_preparation_key"])):
            raise ValueError("ARM donor ready marker mismatch")
        for name, expected in m["reuse_receipts"].items():
            if json.loads(files._file(work / "upstream-reuse", name).read_text()) != expected:
                raise ValueError("ARM original reuse evidence mismatch")
        if json.loads(files._file(work, "upstream-cache-preparation.json").read_text()) != m["preparation_receipt"]:
            raise ValueError("ARM original preparation evidence mismatch")
    if tools.sha(src / "build/toolchain/win/midl.py") != "466824620fd2348229c24d477d114e4c7acc0a5e75a8f1b162729809a0dfccde":
        raise ValueError("ARM repaired MIDL mismatch")
    tools.metagen.validate_sources(src, m["version"])
    out = files._safe(src / "out/Default", directory=True)
    for name in ("args.gn", "build.ninja", "gn.exe", ".ninja_log", ".ninja_deps"):
        if not files._file(out, name).stat().st_size:
            raise ValueError("ARM compiled checkpoint lacks retained state")
    recovery.ninja_state.validate(out, files._file(src, "third_party/ninja/ninja.exe"))
    for name in ("out/Default/gn.exe", "third_party/ninja/ninja.exe", "third_party/rust-toolchain/bin/bindgen.exe",
                 tools.CRUBIT_ROOT + "/bin/cc_bindings_from_rs.exe"):
        tools.pe_executable(src / name)
    if files._file(src, "chrome/build/win-arm64.pgo.txt").read_text().strip() != tools.PGO["arm64"]:
        raise ValueError("ARM exact PGO selector mismatch")
    tools.file(src / "chrome/build/pgo_profiles" / tools.PGO["arm64"])
    for entry in m["preparation_receipt"]["generator_fingerprint"].values():
        if tools.sha(src / entry["path"]) != entry["sha256"]:
            raise ValueError("ARM retained host generator changed: " + entry["path"])
    for name, expected in m["preparation_receipt"]["rust_libraries"].items():
        if tools.sha(src / name) != expected:
            raise ValueError("ARM target/host Rust library changed: " + name)
    return out


def additional_protected(work, m):
    return {".chromix-upstream-restored.json", ".chromix-restored-patches.json",
            *m["preparation_receipt"]["rust_libraries"],
            *(e["path"] for e in m["preparation_receipt"]["generator_fingerprint"].values())}


def receipt_binding(work, m):
    return {"profile": "windows157-arm64-stage6-cache232-to235",
            "checkpoint_manifest_sha256": MANIFEST_SHA256,
            "profile_helper_sha256": recovery.digest(Path(__file__).read_bytes().replace(b"\r\n", b"\n")),
            "shared_helper_sha256": recovery.digest(Path(recovery.__file__).read_bytes().replace(b"\r\n", b"\n")),
            "workflow_sha256": WORKFLOW_SHA256,
            "recipe_base": recovery.RECIPE_BASE,
            "original_source_receipt_sha256": recovery.digest(json.dumps(m["source_receipt"], sort_keys=True, separators=(",", ":")).encode()),
            "windows_recipe_sha256": recovery.scoped_recipe(Path(__file__).resolve().parents[1], "git"),
            "original_producer_job_id": m["job_id"], "original_producer_stage": 6,
            "failed_successor_restored": False, "failed_successor_new_source_tree": False,
            "upstream_identity": m["upstream_receipt"]["identity"],
            "upstream_receipt_sha256": tools.sha(work / "src/.chromix-upstream-restored.json"),
            "original_restored232_sha256": tools.sha(work / "src/.chromix-restored-patches.json"),
            "pgo_sha256": tools.sha(work / "src/chrome/build/pgo_profiles" / tools.PGO["arm64"]),
            "args_gn_sha256": tools.sha(work / "src/out/Default/args.gn"),
            "upstream_object_reuse_proven": False}


def consumer():
    if (os.environ.get("GITHUB_REPOSITORY") != "xiaozhou26/Chromix"
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("GITHUB_WORKFLOW_REF") != f"xiaozhou26/Chromix/.github/workflows/{WORKFLOW}.yml@refs/heads/main"
            or not re.fullmatch(r"build-(?:[7-9]|1[0-2])", os.environ.get("GITHUB_JOB", ""))
            or not re.fullmatch("[0-9a-f]{40}", os.environ.get("GITHUB_SHA", ""))
            or not re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ID", ""))
            or not re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ATTEMPT", ""))
            or os.environ.get("GITHUB_RUN_ID") == "44541436609"):
        raise ValueError("ARM compiled recovery requires its main-only stage7..12 new run")


recovery.ARCH = "arm64"
recovery.OUTPUT = "out/Default"
recovery.PGO_SELECTOR = "win-arm64"
recovery.FIRST_STAGE = 7
recovery.DONOR_RUN = "44541436609"
recovery.VIEW = "windows157-arm64-compiled-recipe"
recovery.WORKFLOW = WORKFLOW
recovery.load = load
recovery.metadata = metadata
recovery.checkout = checkout
recovery.retained_gn_contract = retained_gn_contract
recovery.compiled_contract = compiled_contract
recovery.consumer = consumer
recovery.additional_protected = additional_protected
recovery.receipt_binding = receipt_binding

if __name__ == "__main__":
    raise SystemExit(recovery.main())
