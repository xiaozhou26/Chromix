#!/usr/bin/env python3
"""Admit only the uploaded Windows157 stage1 pre-GN Crubit failure checkpoint."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import zipfile

import apply_restored_patches as patches
import merge_gn_args
import migrate_windows_snapshot as files
import patch_selection
import windows157_fresh as fresh
import windows157_tools as tools
from verify_windows_snapshot_source import _inventory

REPOSITORY = "xiaozhou26/Chromix"
REPOSITORY_ID = 1342691290
DONOR_SHA = "f2d0021993567111b0dd740d61b45bcf774a4f29"
MAIN_BASE = "e2fb7d2129c47d941e341ae8e25f503b07527bfe"
RUN = 44541444866
JOB = 135620566911
WORKFLOW_ID = 423920583
WORKFLOW = "build-win-x64-157-crubit-resume"
PREPARE_KEY = "8e9638c040847fe4f70831ff2fbf7d26854d029b6a8305bbab0dbc5d8b4bd4dc"
MAIN_KEYS = {"31107da7fc8154c7bb327d307cb241335d2f9aff9eca4e36b034c62717dd8e18",
             "47681faa4e50148eba141aea36765df3fa406f20a9a70b0635c15402af5d01ea"}
HELPER = "tools/windows157_tools.py"
HELPER_TUPLE = (
    "0a503874fe18c3d3fa2ed6ff5ebd098f9120a759b926356f7566cbf4f333d351",
    "464733ae07ca98c457ff8e6b1482a90185d8a2bc0639ac06eb433619a95b2e41",
)
PROOF = "build/windows/windows157-crubit-source.json"
PROOF_SHA256 = "3cf8be13ba9af51e87894f904a38f3186a9d60255842c3f51d704871439f4eb2"
RECEIPT = ".chromix-windows157-crubit-resume.json"
PARTS = (
    (14541953586, 9663676664, "b6a5d4721bdd3a56b96d528676f32e027c065130c5677db0c26843cc114f82ee", "2026-12-03T15:00:23Z"),
    (14541992786, 758018904, "c79f7a1520766b77b7004fcbc517e05c64af42363b177086f5131b4d492bfb14", "2026-12-03T15:01:51Z"),
)
RECOVERY_FILES = frozenset({HELPER, PROOF, "build/windows/ci-stage.ps1", "tools/windows157_crubit_resume.py",
    "tools/tests/test_windows157_crubit_resume.py", "tools/tests/fixtures/windows157_crubit_checkpoint.json.gz",
    "tools/tests/fixtures/windows157_crubit_source.json.gz", "tools/gen_windows_arm64_workflow.py",
    f".github/workflows/{WORKFLOW}.yml"})
VIEW = "windows157-crubit-recipe"
RECOVERY_CODE = {
    "build/windows/ci-stage.ps1": "73ade18522a3336f3ff2a7df28283ed6c54d37ab51920c983e04f329f4b8ffb9",
    "tools/gen_windows_arm64_workflow.py": "db6f83852ba4892d234092851eef665e189027bcd3849498e70637eadceab38d",
    ".github/workflows/build-win-x64-157-crubit-resume.yml": "47b6eeb9833b6c758d1d68e03d9ff23222b7154fdbdb45d0441c0087604a1d35"
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest() -> dict:
    return dict(repository=REPOSITORY, head_sha=DONOR_SHA, run_id=RUN, stage=1, attempt=1,
                job_id=JOB, arch="x64", workflow="build-win-x64-157-fresh", artifacts=[
                    dict(id=identifier, name=f"win-x64-157-tree-s1-attempt-1-part{index}",
                         size_in_bytes=size, digest="sha256:" + sha, expired=False)
                    for index, (identifier, size, sha, _) in enumerate(PARTS, 1)])


def metadata(client, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    run = client.get(f"/actions/runs/{RUN}")
    required = dict(id=RUN, head_sha=DONOR_SHA, head_branch="main", run_attempt=1,
                    status="completed", conclusion="failure", event="workflow_dispatch",
                    workflow_id=WORKFLOW_ID, path=".github/workflows/build-win-x64-157-fresh.yml")
    if (any(type(run.get(k)) is not type(v) or run.get(k) != v for k, v in required.items())
            or any(run.get(k, {}).get("full_name") != REPOSITORY or run.get(k, {}).get("id") != REPOSITORY_ID
                   for k in ("repository", "head_repository"))):
        raise ValueError("fixed Crubit donor run mismatch")
    jobs = client.items(f"/actions/runs/{RUN}/attempts/1/jobs", "jobs")
    selected = [j for j in jobs if j.get("id") == JOB or j.get("name") == "Windows157 x64 stage 1"]
    expected = dict(id=JOB, run_id=RUN, run_attempt=1, head_sha=DONOR_SHA,
                    name="Windows157 x64 stage 1", status="completed", conclusion="failure")
    if len(selected) != 1 or any(type(selected[0].get(k)) is not type(v) or selected[0].get(k) != v for k, v in expected.items()):
        raise ValueError("fixed Crubit donor job mismatch")
    if any(j.get("name", "").startswith("Windows157 x64 stage ") and j.get("id") != JOB
           and j.get("conclusion") != "skipped" for j in jobs):
        raise ValueError("a later donor stage exists")
    steps = selected[0].get("steps", [])
    for name, conclusion in {"Run stage 1": "failure", "Restore latest valid preserved snapshot": "skipped",
            "Ensure build tree snapshot": "success", "Upload final bundle": "skipped",
            **{f"Upload tree part {i}": "success" for i in range(1, 5)}}.items():
        found = [s for s in steps if s.get("name") == name]
        if len(found) != 1 or found[0].get("status") != "completed" or found[0].get("conclusion") != conclusion:
            raise ValueError("Crubit checkpoint producer step mismatch: " + name)
    listed = client.items(f"/actions/runs/{RUN}/artifacts", "artifacts")
    trees = [a for a in listed if "tree-" in a.get("name", "")]
    expected = manifest()
    if len(trees) != len(PARTS):
        raise ValueError("latest donor tree set changed")
    for pin, (_, _, _, expiry) in zip(expected["artifacts"], PARTS):
        found = [a for a in trees if a.get("id") == pin["id"]]
        if (len(found) != 1 or any(type(found[0].get(k)) is not type(v) or found[0].get(k) != v for k, v in pin.items())
                or found[0].get("expires_at") != expiry
                or datetime.fromisoformat(expiry.replace("Z", "+00:00")) <= now
                or any(found[0].get("workflow_run", {}).get(k) != v for k, v in
                       dict(id=RUN, head_sha=DONOR_SHA, head_branch="main", repository_id=REPOSITORY_ID,
                            head_repository_id=REPOSITORY_ID).items())):
            raise ValueError("Crubit checkpoint artifact mismatch or expiry")
    return expected


def checked_download(origin: dict, download: dict) -> None:
    if origin != manifest():
        raise ValueError("not the fixed Crubit origin")
    required = dict(status="success", phase="complete", publication="published",
                    repository=REPOSITORY, head_sha=DONOR_SHA, run_id=RUN)
    if any(download.get(k) != v for k, v in required.items()):
        raise ValueError("Crubit tree download was not authenticated and published")
    records = download.get("artifacts", [])
    if [{k: a.get(k) for k in origin["artifacts"][0]} for a in records] != origin["artifacts"]:
        raise ValueError("Crubit download artifact set mismatch")
    volumes = download.get("volumes", [])
    if (len(volumes) != 2 or [v.get("name") for v in volumes] != ["tree.7z.001", "tree.7z.002"]
            or [v.get("artifact_id") for v in volumes] != [p[0] for p in PARTS]
            or any(type(v.get("size_in_bytes")) is not int or v["size_in_bytes"] <= 0
                   or not re.fullmatch("[0-9a-f]{64}", v.get("sha256", "")) for v in volumes)
            or download.get("total_size_in_bytes") != sum(v["size_in_bytes"] for v in volumes)):
        raise ValueError("Crubit download volume receipt mismatch")


def git_tree(git: str, repo: Path, sha: str) -> dict:
    result = {}
    for record in files._git(git, repo, "ls-tree", "-rz", "--full-tree", sha).split(b"\0"):
        if record:
            entry, name = record.split(b"\t", 1)
            result[name.decode()] = tuple(entry.decode().split())
    return result


def blob(git, repo, sha, path):
    return files._git(git, repo, "cat-file", "blob", f"{sha}:{path}")


def checkout_identity(repo: Path, expected_sha: str) -> tuple[str, dict]:
    git = files._host_git((repo,))
    head, tree = files._tree(git, repo, expected_sha)
    files._git(git, repo, "merge-base", "--is-ancestor", MAIN_BASE, head)
    base = git_tree(git, repo, MAIN_BASE)
    changed = {n for n in set(base) | set(tree) if base.get(n) != tree.get(n)}
    if changed - RECOVERY_FILES:
        raise ValueError("consumer differs outside reviewed Crubit recovery: " + str(sorted(changed - RECOVERY_FILES)))
    if _inventory(repo) != set(tree):
        raise ValueError("consumer checkout has untracked/missing inputs")
    files._tracked(repo, tree)
    if (digest(blob(git, repo, DONOR_SHA, HELPER)), digest(blob(git, repo, head, HELPER))) != HELPER_TUPLE:
        raise ValueError("Crubit helper transition is not the exact reviewed tuple")
    if digest(files._file(repo, PROOF).read_bytes()) != PROOF_SHA256:
        raise ValueError("Crubit full-source proof changed")
    for name, expected in RECOVERY_CODE.items():
        if digest(files._file(repo, name).read_bytes().replace(b"\r\n", b"\n")) != expected:
            raise ValueError("unreviewed Crubit orchestration helper: " + name)
    return git, tree


def windows_recipe(manifest: dict) -> dict:
    return {**{k: v for k, v in manifest.items() if k != "platforms"},
            "platforms": {"windows": manifest["platforms"]["windows"]}}


def recipe_view(repo: Path, destination: Path, git: str) -> None:
    """Use frozen donor source inputs with only the reviewed host helper installed."""
    current = json.loads(files._file(repo, patch_selection.MANIFEST157).read_bytes())
    old = json.loads(blob(git, repo, DONOR_SHA, patch_selection.MANIFEST157))
    if windows_recipe(current) != windows_recipe(old):
        raise ValueError("Windows platform recipe changed")
    _, current_patches = patch_selection.select(repo, "windows")
    for name, data in current_patches:
        if data != blob(git, repo, DONOR_SHA, name):
            raise ValueError("selected Windows patch changed: " + name)
    if patch_selection.preparation_key(repo, "windows", "windows") not in MAIN_KEYS:
        raise ValueError("unreviewed main platform preparation identity")
    old_tree = git_tree(git, repo, DONOR_SHA)
    current_tree = git_tree(git, repo, "HEAD")
    protected = [n for n in old_tree if n.startswith(("build/windows/", "assets/"))
                 and n != "build/windows/ci-stage.ps1"]
    protected += ["build/args.windows.gn", "build/args.windows.arm64.gn", "build/ungoogled-revisions.psd1",
                  "CHROMIUM_WINDOWS_VERSION", "UNGOOGLED_WINDOWS_VERSION", "tools/windows157_fresh.py",
                  "tools/verify_patch_stack.py", "tools/apply_restored_patches.py", "tools/merge_gn_args.py"]
    if any(old_tree.get(n) != current_tree.get(n) for n in protected):
        raise ValueError("Windows preparation inputs changed outside host helper")
    if destination.exists():
        raise ValueError("recipe view already exists")
    destination.mkdir(parents=True)
    archive = files._git(git, repo, "archive", "--format=zip", DONOR_SHA)
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        for item in source.infolist():
            if item.is_dir():
                continue
            relative = patches._relative(item.filename)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            data = source.read(item)
            if old_tree[relative] == current_tree.get(relative):
                candidate = files._file(repo, relative).read_bytes()
                oid = old_tree[relative][2]
                normalized = candidate.replace(b"\r\n", b"\n")
                if oid in {hashlib.sha1(f"blob {len(b)}\0".encode() + b).hexdigest()
                           for b in (candidate, normalized)}:
                    data = candidate
            if relative == "build/windows/lite-tarball-files/v8/test/torque/test-torque.tq":
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            target.write_bytes(data)
            if os.name != "nt":
                target.chmod(int(old_tree[relative][0], 8) & 0o777)
    (destination / HELPER).write_bytes(files._file(repo, HELPER).read_bytes())
    result = subprocess.run([sys.executable, str(destination / "tools/patch_selection.py"), "--repo", str(destination),
                             "--platform", "windows", "--key", "windows"], check=True, capture_output=True, text=True)
    if result.stdout.strip() != PREPARE_KEY:
        raise ValueError("frozen donor Windows prepare key mismatch")


def view_identity(repo: Path, view: Path, git: str) -> None:
    tree = git_tree(git, repo, DONOR_SHA)
    if _inventory(view) != set(tree):
        raise ValueError("frozen recipe view inventory changed")
    for name, (mode, kind, oid) in tree.items():
        raw = files._file(view, name).read_bytes()
        if name == HELPER:
            if digest(raw) != HELPER_TUPLE[1]:
                raise ValueError("frozen recipe helper changed")
            continue
        if kind != "blob" or mode not in ("100644", "100755"):
            raise ValueError("unsafe donor recipe Git entry")
        candidates = [raw, raw.replace(b"\r\n", b"\n")]
        if oid not in {hashlib.sha1(f"blob {len(b)}\0".encode() + b).hexdigest() for b in candidates}:
            raise ValueError("frozen donor recipe file changed: " + name)


def tree_state(root: Path) -> dict:
    result = {}
    if not root.exists():
        return result
    files._safe(root, directory=True)
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs:
            files._safe(Path(directory) / name, directory=True)
        for name in names:
            path = files._file(Path(directory), name)
            result[path.relative_to(root).as_posix()] = files._fingerprint(path)
    return result


def phase_contract(work: Path) -> dict:
    src = work / "src"
    fresh.initialize(work.parent / VIEW, work, "x64", resume=True)
    markers = {".chromix-source-unpacked": fresh.VERSION, ".chromix-ungoogled-core": fresh.CORE,
               ".chromix-ungoogled-windows": fresh.WINDOWS, ".chromix-binaries-pruned": fresh.CORE,
               ".chromix-patches": PREPARE_KEY, ".chromix-patch-selection": PREPARE_KEY,
               ".chromix-source-ready": "|".join((fresh.VERSION, fresh.CORE, fresh.WINDOWS, PREPARE_KEY))}
    for name, value in markers.items():
        if files._file(src, name).read_text().strip() != value:
            raise ValueError("pre-GN donor source marker mismatch: " + name)
    for name in (".chromix-domain-substituted", ".chromix-upstream-restored.json", ".chromix-domain-substitution-in-progress",
                 ".chromix-layer-in-progress", ".chromix-patch-in-progress"):
        if (src / name).exists():
            raise ValueError("donor is not the exact pre-domain Crubit failure phase: " + name)
    out = src / "out/Chromix"
    if {p.name for p in out.iterdir()} != {"args.gn"}:
        raise ValueError("donor already has GN/Ninja outputs; pre-GN checkpoint required")
    tools.pe_executable(src / "third_party/rust-toolchain/bin/bindgen.exe")
    import install_windows_metagen_bindings as bindings
    bindings.validate_sources(src, fresh.VERSION)
    bindings.source_contract(src, out, fresh.VERSION)
    if tools.sha(src / "build/toolchain/win/midl.py") != "f113aabeee53f6ba0fce6d364d45d294b094b656afc03e24029bf7503fb99b3a":
        raise ValueError("pre-domain repaired MIDL mismatch")
    if tools.file(src / "build/util/MINIBRANCH").read_bytes() != tools.MINIBRANCH:
        raise ValueError("MINIBRANCH mismatch")
    if (src / tools.CRUBIT_ROOT / "bin/cc_bindings_from_rs.exe").exists():
        raise ValueError("donor Crubit was already installed")
    if not (src / "out/crubit").is_dir():
        raise ValueError("preserved Cargo output is missing")
    if tools.file(src / "chrome/build/win64.pgo.txt").read_text().strip() != tools.PGO["x64"]:
        raise ValueError("donor PGO selector changed")
    return markers


def gn_contract(view: Path, work: Path) -> None:
    src = work / "src"
    overlay = ('# Windows157 fresh native build; never disable PGO for tarball sources.\n'
               'chrome_pgo_phase = 2\nhost_cpu = "x64"\n'
               f'v8_metagen_libclang_so = {json.dumps((src / "third_party/llvm-build/Release+Asserts/bin/libclang.dll").as_posix())}\n'
               f'v8_metagen_libclang_bindings_dir = {json.dumps((src / "third_party/llvm-build/Release+Asserts/bindings/python").as_posix())}\n')
    if files._file(work, "windows157-native.gn").read_text() != overlay:
        raise ValueError("donor native PGO2 overlay changed")
    inputs = [work / "tooling/ungoogled-chromium/flags.gn", work / "tooling/ungoogled-chromium-windows/flags.windows.gn",
              view / "build/args.windows.gn", work / "windows157-native.gn"]
    keys, values = [], {}
    for path in inputs:
        order, parsed = merge_gn_args.parse(path)
        keys.extend(k for k in order if k not in keys)
        values.update(parsed)
    expected = "\n".join([merge_gn_args.GENERATED_HEADER, *[values[k] for k in keys]]) + "\n"
    if files._file(src, "out/Chromix/args.gn").read_text() != expected:
        raise ValueError("donor GN arguments differ from exact preparation inputs")


def full_source(repo: Path, src: Path) -> dict:
    raw = files._file(repo, PROOF).read_bytes()
    if digest(raw) != PROOF_SHA256:
        raise ValueError("full source proof digest mismatch")
    proof = json.loads(raw)
    for name, expected in proof["outputs"].items():
        path = files._file(src, name, missing=expected is None)
        actual = digest(path.read_bytes().replace(b"\r\n", b"\n")) if path.exists() else None
        if actual != expected:
            raise ValueError("complete affected-source file mismatch: " + name)
    return proof["outputs"]


def verify_source(repo: Path, view: Path, work: Path) -> dict:
    src = files._safe(work / "src", directory=True)
    before = tree_state(src / "out")
    pgo_before = tree_state(src / "chrome/build/pgo_profiles")
    markers = phase_contract(work)
    git = files._host_git((repo, view, work))
    states = []
    for relative, pin, core in (("tooling/ungoogled-chromium", fresh.CORE, None),
                                ("tooling/ungoogled-chromium-windows", fresh.WINDOWS, fresh.CORE)):
        root = work / relative
        head, tree = files._tree(git, root, pin)
        states.append((root, files._tracked(root, tree, core_pin=core)))
    gn_contract(view, work)
    outputs = full_source(repo, src)
    lite = view / "build/windows/lite-tarball-files"
    for path in lite.rglob("*"):
        if path.is_file() and files._file(src, path.relative_to(lite).as_posix()).read_bytes() != path.read_bytes():
            raise ValueError("complete lite source differs from donor recipe")
    with tempfile.TemporaryDirectory(prefix="windows157-crubit-proof-") as temporary:
        report = Path(temporary) / "source.json"
        subprocess.run([sys.executable, str(view / "tools/verify_patch_stack.py"), "--src", str(src),
                        "--repo", str(view), "--platform", "windows", "--output", str(report)],
                       check=True, timeout=600)
        proof = json.loads(report.read_text())
    if proof.get("status") != "verified" or proof["patch_count"] != 232:
        raise ValueError("incomplete donor patch proof")
    if full_source(repo, src) != outputs:
        raise ValueError("full source changed during verification")
    if tree_state(src / "out") != before or tree_state(src / "chrome/build/pgo_profiles") != pgo_before:
        raise ValueError("preserved Cargo/GN/PGO outputs changed during verification")
    for root, state in states:
        files._unchanged(root, state)
    for name, expected in markers.items():
        if files._file(src, name).read_text().strip() != expected:
            raise ValueError("source markers changed during verification")
    return dict(status="verified", phase="source-ready-bindgen-installed-crubit-built-pre-gn-pre-domain",
                preparation_key=PREPARE_KEY, source=proof, full_source=outputs,
                output_files=len(before), pgo_files=len(pgo_before), changed_source_files=[])


def consumer() -> str:
    sha = os.environ.get("GITHUB_SHA", "")
    if (os.environ.get("GITHUB_REPOSITORY") != REPOSITORY or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("GITHUB_WORKFLOW_REF") != f"{REPOSITORY}/.github/workflows/{WORKFLOW}.yml@refs/heads/main"
            or os.environ.get("GITHUB_RUN_ID") == str(RUN) or sha == DONOR_SHA
            or not re.fullmatch("[0-9a-f]{40}", sha)
            or not re.fullmatch(r"build-(?:[1-9]|1[0-2])", os.environ.get("GITHUB_JOB", ""))
            or not re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ID", ""))
            or not re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ATTEMPT", ""))):
        raise ValueError("Crubit recovery requires its main-only fixed consumer")
    return sha


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("metadata", "admit", "view"), required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--origin", type=Path)
    parser.add_argument("--download", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        sha = consumer()
        repo = args.repo.resolve()
        git, tree = checkout_identity(repo, sha)
        if args.phase == "metadata":
            from validate_windows_snapshot import Client
            result = metadata(Client(REPOSITORY, os.environ.get("GH_TOKEN", "")))
        else:
            if not args.workdir:
                raise ValueError("workdir required")
            work = args.workdir.resolve()
            view = work.parent / VIEW
            if view.exists():
                if args.phase == "admit":
                    raise ValueError("admission recipe already exists")
            else:
                recipe_view(repo, view, git)
            view_identity(repo, view, git)
            receipt = work / RECEIPT
            if args.phase == "admit":
                if (not args.origin or not args.download or receipt.exists()
                        or os.environ.get("GITHUB_JOB") != "build-1"):
                    raise ValueError("fixed downloaded donor and unused admission required")
                origin = json.loads(files._file(args.origin.parent, args.origin.name).read_text())
                download = json.loads(files._file(args.download.parent, args.download.name).read_text())
                checked_download(origin, download)
                result = verify_source(repo, view, work)
                result.update(donor_sha=DONOR_SHA, target_sha=sha, run_id=RUN, job_id=JOB, attempt=1,
                              helper_tuple=HELPER_TUPLE, origin=origin,
                              consumer_run_id=os.environ["GITHUB_RUN_ID"],
                              consumer_attempt=os.environ["GITHUB_RUN_ATTEMPT"])
                with receipt.open("x", encoding="utf-8") as stream:
                    json.dump(result, stream, sort_keys=True)
            else:
                result = json.loads(files._file(work, RECEIPT).read_text())
                if (result.get("status") != "verified" or result.get("target_sha") != sha
                        or result.get("donor_sha") != DONOR_SHA or result.get("origin") != manifest()
                        or result.get("helper_tuple") != list(HELPER_TUPLE)
                        or result.get("preparation_key") != PREPARE_KEY
                        or result.get("consumer_run_id") != os.environ["GITHUB_RUN_ID"]
                        or result.get("consumer_attempt") != os.environ["GITHUB_RUN_ATTEMPT"]):
                    raise ValueError("continuation lacks same-consumer fixed Crubit admission")
            view_identity(repo, view, git)
        files._tracked(repo, tree)
        with args.report.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2, sort_keys=True)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Windows157 fixed Crubit resume failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
