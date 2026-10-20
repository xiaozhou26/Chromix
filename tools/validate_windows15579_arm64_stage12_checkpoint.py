#!/usr/bin/env python3
"""Admit only the unchanged Windows155.79 ARM64 stage12 compiled checkpoint."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import apply_restored_patches as arp
import merge_gn_args
import migrate_windows_snapshot as safe
import patch_selection
import prepare_restored_build as prepared
import restore_upstream_cache as upstream
import verify_patch_stack as stack
from validate_windows_snapshot import Client
from validate_windows155_stage8_api_checkpoint import exact, require

REPO = Path(__file__).resolve().parents[1]
REPOSITORY = "xiaozhou26/Chromix"
RUN = 39104889810
JOB = 118608015312
SHA = "8454c240f86343ec283bdd10d1b5d778f6624e48"
TREE = "b699059af377aaa38d8c01177130ca1e93debedd"
BRANCH = "build/windows15579-donors"
VERSION = "155.0.8059.79"
CORE = "9f145d8815e6e50cc4a2537fcac40966d911e027"
WINDOWS = "a1e3123c8d543b5c9a1eecfaf0f06cf242c0db37"
PATCH_KEY = "bfea4512b09671df8ccce0a523395da545a7fda3eb2d0faf1f3f95ddc83a4ce6"
KEY = "|".join((VERSION, CORE, WINDOWS, PATCH_KEY))
SOURCE_IDENTITY = "0c11c2b9ffdfaf8465dba86861838fb991a33c4800aff046a612f7f1c2bdfede"
SOURCE_OUTPUTS = "11b1388812bd33a8229259e0104cb4528882a2936b35bf198a31285a55a475cb"
PREPARATION = "36ffa453991505c56f7a3d5a21304578c95bae3f60d07726b8b3ef91fe2a61c9"
UPSTREAM_RECEIPT = "66889e702bbab30c7fac90026875ba1358fd0bf3c0d0011a887bccfa38bcd436"
UPSTREAM_IDENTITY = "dfed87e386d299ece07fbc50b1832846775ca749cacbdd59fbc4bbb6c70b2348"
SOURCE_REPORT = "fingerprint-diagnostics/source-5a11481bd060472ea21648181aeb0e9f.json"
SOURCE_REPORT_HASH = "2c815c958e4baa3c5e9c3f928b97a5563b91cf78fc871d503875559adbe94ea4"
LOG_HASH = "530da3b8a27f0f2c50773ac660be87352280f40882d7dad17c498976d2d265ce"
LOG_SIZE = 255543
PREFIX = "win-arm64-tree-s12-attempt-1-part"
ARTIFACTS = [
    dict(id=12281582560, name=PREFIX + "1", size_in_bytes=9663676664, expired=False,
         digest="sha256:f990b005ad29b65a1d97d6616e2b61d319244bc2ea4ec7a043b71e056a9e4bb9"),
    dict(id=12281343067, name=PREFIX + "2", size_in_bytes=2948094979, expired=False,
         digest="sha256:b699f57b13dd7f5a099e5d33f9f695be765ffd616286ed5b4953fab0b1b54a27"),
]
ADMISSION = "fingerprint-diagnostics/windows15579-arm64-stage12-admission.json"
ALLOWED = frozenset({
    "build/windows/ci-stage.ps1",
    ".github/workflows/build-win15579-arm64-stage12-resume.yml",
    "tools/gen_windows15579_arm64_stage12_workflow.py",
    "tools/validate_windows15579_arm64_stage12_checkpoint.py",
    "tools/tests/test_windows15579_arm64_stage12_resume.py",
    "tools/tests/test_windows_upstream_cache.py",
    "tools/tests/fixtures/windows15579_arm64_stage12_receipts.json.gz",
    "docs/windows15579-arm64-stage12-resume.md",
})


def sha(data):
    return hashlib.sha256(data).hexdigest()


def identity_hash(data):
    return sha(arp._json(data))


def read(root, name):
    return safe._file(root, name).read_bytes()


def producer_log():
    return subprocess.run(["gh", "api", "--allow-escape-sequences",
                           f"repos/{REPOSITORY}/actions/jobs/{JOB}/logs"],
                          capture_output=True, check=True, timeout=120).stdout


def manifest():
    return dict(repository=REPOSITORY, platform="windows", arch="arm64",
                workflow="build-win-arm64-github", run_id=RUN, job_id=JOB,
                attempt=1, stage=12, head_sha=SHA, pattern=PREFIX + "*", artifacts=ARTIFACTS)


def validate_metadata(client, *, log_loader=producer_log):
    run = client.get(f"/actions/runs/{RUN}")
    exact(run, dict(id=RUN, head_sha=SHA, head_branch=BRANCH, run_attempt=1,
                    path=".github/workflows/build-win-arm64-github.yml", event="workflow_dispatch",
                    workflow_id=357619747, status="completed", conclusion="failure"), "donor run")
    for field in ("repository", "head_repository"):
        exact(run.get(field), dict(full_name=REPOSITORY, id=1342691290), field)
    job = client.get(f"/actions/jobs/{JOB}")
    exact(job, dict(id=JOB, run_id=RUN, run_attempt=1, head_sha=SHA, head_branch=BRANCH,
                    name="stage 12 (resume compile)", status="completed", conclusion="failure",
                    labels=["windows-2022"]), "donor job")
    for name, conclusion in {"Run stage 12": "failure", "Ensure build tree snapshot": "success",
                             **{f"Upload tree part {i}": "success" for i in range(1, 5)}}.items():
        matches = [step for step in job["steps"] if step.get("name") == name]
        require(len(matches) == 1, "ambiguous producer step: " + name)
        exact(matches[0], dict(status="completed", conclusion=conclusion), name)
    items = client.items(f"/actions/runs/{RUN}/artifacts", "artifacts")
    selected = sorted((a for a in items if a.get("name", "").startswith(PREFIX)
                       or a.get("id") in {a["id"] for a in ARTIFACTS}), key=lambda a: a["name"])
    require(len(selected) == 2, "complete exact stage12 attempt1 artifact set required")
    for actual, expected in zip(selected, ARTIFACTS):
        exact(actual, expected, "donor artifact")
        exact(actual.get("workflow_run"), dict(id=RUN, head_sha=SHA, head_branch=BRANCH,
                                               repository_id=1342691290, head_repository_id=1342691290),
              "artifact producer")
    raw = log_loader()
    require(len(raw) == LOG_SIZE and sha(raw) == LOG_HASH, "producer log digest/size mismatch")
    require(b"build did not finish within 12 stages" in raw, "missing budget exhaustion evidence")
    for part in ARTIFACTS:
        require((f"Artifact {part['name']}.zip successfully finalized. Artifact ID {part['id']}").encode()
                in raw, "missing finalized upload evidence")
    return manifest()


def validate_listing(listing):
    members = {}
    for block in re.split(r"\r?\n\s*\r?\n", listing.strip()):
        fields = [line.split(" = ", 1) for line in block.splitlines() if " = " in line]
        record = dict(fields)
        require(len(record) == len(fields) and "Path" in record, "ambiguous archive listing")
        name = record["Path"].replace("\\", "/")
        parts = name.split("/")
        require(parts[0] == "chromix" and all(part not in ("", ".", "..")
                and not any(ord(c) < 32 or c in ':<>"|?*' for c in part)
                and not part.endswith((" ", "."))
                and not re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
                for part in parts), "unsafe archive path")
        require(not any(k in record for k in ("Symbolic Link", "Hard Link", "Reparse"))
                and not re.search(r"(?:^|\s)l[rwx-]{9}(?:\s|$)", record.get("Attributes", ""))
                and record.get("Anti", "-") == "-", "archive links/anti-items forbidden")
        folded = name.casefold()
        require(folded not in members and not folded.startswith("chromix/src/out/chromix")
                and not any(p.casefold().startswith(".chromix") and
                            (re.search(r"in[-_]?progress|migration", p.casefold())
                             or p.casefold().startswith(".chromix-upstream-restore-")) for p in parts),
                "duplicate/cold/interrupted/migrated archive")
        members[folded] = record
    for name in (".chromix-target-arch", "src/.chromix-source-ready", "src/.chromix-restored-patches.json",
                 "src/.chromix-upstream-restored.json", "src/.chromix-restored-build-prepared.json",
                 "src/out/Default/args.gn", "src/out/Default/build.ninja",
                 "src/out/Default/.ninja_log", "src/out/Default/.ninja_deps", SOURCE_REPORT):
        record = members.get("chromix/" + name.lower(), {})
        require(record.get("Folder", "-") == "-" and record.get("Size", "").isdigit()
                and int(record["Size"]) > 0, "missing compiled checkpoint member: " + name)
    return members


def check_archive(path):
    command = shutil.which("7z.exe") or shutil.which("7zz") or shutil.which("7z")
    require(bool(command), "7z required")
    listing = subprocess.run([command, "l", "-slt", "-ba", "-sccUTF-8", str(path)],
                             check=True, capture_output=True).stdout.decode("utf-8")
    validate_listing(listing)
    return dict(status="verified", archive=str(path))


def repository_identity(repo, donor, target_sha):
    git = safe._host_git((repo, donor))
    _, old = safe._tree(git, donor, SHA)
    require(safe._git(git, donor, "rev-parse", "HEAD^{tree}").decode().strip() == TREE,
            "donor source tree mismatch")
    head, new = safe._tree(git, repo, target_sha)
    changed = {name for name in old.keys() | new.keys() if old.get(name) != new.get(name)}
    require(not changed - ALLOWED, "source/build inputs differ outside controller allowlist")
    for root, entries in ((donor, old), (repo, new)):
        safe._tracked(root, entries)
        require(not safe._git(git, root, "ls-files", "--others", "--exclude-standard").strip(),
                "uncommitted checkout inputs")
    require(patch_selection.preparation_key(repo, "windows", "windows") == PATCH_KEY
            and patch_selection.preparation_key(donor, "windows", "windows") == PATCH_KEY,
            "original preparation key changed")
    return head, sorted(changed)


def source_report(data):
    exact(data, dict(status="verified", patch_count=231, domain_substituted=True), "source receipt")
    require(identity_hash(data.get("identity")) == SOURCE_IDENTITY
            and identity_hash(data.get("outputs")) == SOURCE_OUTPUTS, "expected source identity mismatch")


def source_state(work, repo):
    src = work / "src"
    require(read(work, ".chromix-target-arch").decode().strip() == "arm64", "ARM64 marker required")
    require(not (src / "out/Chromix").exists(), "cold output forbidden")
    for root in (work, src):
        for path in root.iterdir():
            name = path.name.casefold()
            require(not (name.startswith(".chromix") and
                         (re.search(r"in[-_]?progress|migration", name)
                          or name.startswith(".chromix-upstream-restore-"))), "interrupted/migrated tree")
    markers = {".chromix-source-ready": KEY, ".chromix-patches": PATCH_KEY,
               ".chromix-patch-selection": PATCH_KEY,
               ".chromix-source-unpacked": VERSION, ".chromix-ungoogled-core": CORE,
               ".chromix-ungoogled-windows": WINDOWS, ".chromix-binaries-pruned": CORE,
               ".chromix-domain-substituted": CORE, ".chromix-toolchain-ready": CORE + "|" + WINDOWS}
    for name, value in markers.items():
        require(read(src, name).decode("utf-8-sig").strip() == value, "original layer marker mismatch: " + name)
    require(patch_selection.source_version(src) == VERSION, "Chromium version mismatch")
    receipt = upstream.verify_restored(work, "windows", "arm64", repo=repo)
    normalized = dict(receipt, manifest={k: v for k, v in receipt["manifest"].items() if k != "path"})
    require(sha((json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False)
                 + "\n").encode()) == UPSTREAM_RECEIPT, "upstream receipt changed")
    require(identity_hash(receipt["identity"]) == UPSTREAM_IDENTITY, "upstream identity mismatch")
    require(sha(read(work, SOURCE_REPORT)) == SOURCE_REPORT_HASH, "donor source report changed")
    source_report(json.loads(read(work, SOURCE_REPORT)))
    core, windows = work / "tooling/ungoogled-chromium", work / "tooling/ungoogled-chromium-windows"
    git = safe._host_git((work, repo))
    for root, pin, submodule in ((core, CORE, None), (windows, WINDOWS, CORE)):
        _, tree = safe._tree(git, root, pin)
        safe._tracked(root, tree, core_pin=submodule)
    identity, patches, lite = arp._load(repo, core, windows, "windows", src=src)
    require(identity_hash(identity) == SOURCE_IDENTITY and len(patches) == 231, "live source recipe mismatch")
    names = set(lite) | {entry[0] for _, _, entries in patches for entry in entries}
    require(arp._completed(src, identity, names), "source completion receipt missing")
    completion = json.loads(read(src, arp.MARKER))
    # Completion includes the lite payload, while verify_patch_stack reports patched files only.
    patch_names = {entry[0] for _, _, entries in patches for entry in entries}
    require(identity_hash({name: completion["outputs"][name] for name in patch_names}) == SOURCE_OUTPUTS,
            "source output hashes differ from the donor")
    expected = dict(receipt["original_args"]["assignments"])
    for root, name in ((core, "flags.gn"), (windows, "flags.windows.gn"),
                       (repo, "build/args.windows.gn"), (repo, "build/args.windows.arm64.gn")):
        _, assignments = merge_gn_args.parse(safe._file(root, name))
        expected.update(upstream.parse_gn_assignments("\n".join(assignments.values())))
    expected["chrome_pgo_phase"] = receipt["original_args"]["assignments"]["chrome_pgo_phase"]
    if "v8_metagen_libclang_so" in expected:
        expected["v8_metagen_libclang_so"] = expected["v8_metagen_libclang_so"].replace(
            "C:/ungoogled-chromium-windows/build/src/", src.as_posix() + "/")
    args = read(src, "out/Default/args.gn").decode()
    keys = re.findall(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", args)
    values = upstream.parse_gn_assignments(args)
    require(len(keys) == len(set(keys)) and values == expected and values.get("chrome_pgo_phase") == "2"
            and values.get("target_cpu") == '"arm64"' and values.get("target_os") == '"win"',
            "original native ARM64 GN/PGO configuration changed")
    for name in ("upstream-cache-preparation.json", "src/.chromix-restored-build-prepared.json"):
        raw = read(work, name)
        require(sha(raw) == PREPARATION, "original preparation receipt changed: " + name)
        data = json.loads(raw)
        exact(data, dict(platform="windows", arch="arm64", ready_for_gn=True,
                         native_tools=True, compilers_native=True, toolchains_native=True,
                         removed_gn=False, removed_final_products=[]), "prepared toolchain")
        require(identity_hash(data["source_identity"]) == UPSTREAM_IDENTITY, "prepared source identity mismatch")
        require(all(data["counters"][key] == 0 for key in
                    ("first_finish", "tool_swap_invalidations", "toolchain_invalidated_outputs")),
                "full preparation/invalidation forbidden")
    state_names = set(markers) | {upstream.MARKER, arp.MARKER, "out/Default/args.gn",
                                  "out/Default/build.ninja", "out/Default/.ninja_log", "out/Default/.ninja_deps"}
    return safe._capture(src, state_names)


def host_tools(work):
    src = work / "src"
    data = json.loads(read(work, "upstream-cache-preparation.json"))
    require(prepared.tool_fingerprint(src, "windows", "arm64") == data["tool_fingerprint"]
            and prepared.generator_fingerprint(src, "windows", "arm64", host_arch="x64")
            == data["generator_fingerprint"], "toolchain/generator replacement forbidden")
    require(prepared.validate_native_tools(src, "windows", "arm64")["native_tools"], "native host tools missing")
    previous = data["environment"]["environment"]
    for name in ("ImageOS", "VCToolsVersion", "WindowsSDKVersion", "UCRTVersion", "INCLUDE", "LIB"):
        require(os.environ.get(name, "") == previous[name], "host SDK/build environment drift: " + name)


def verify(work, donor, repo, stage, target_sha, source_output):
    require(13 <= stage <= 24 and os.environ.get("GITHUB_JOB") == f"build-{stage - 12}",
            "exact continuation stage/job required")
    require(re.fullmatch(r"[0-9a-f]{40}", target_sha or "") is not None
            and target_sha == os.environ.get("GITHUB_SHA"), "real target SHA required")
    require(os.environ.get("GITHUB_RUN_ID") != str(RUN)
            and all(re.fullmatch(r"[1-9][0-9]*", os.environ.get(k, "")) for k in
                    ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")), "new consumer run/attempt required")
    repo, donor, work = (safe._safe(p.resolve(), directory=True) for p in (repo, donor, work))
    require(repo == REPO.resolve() and len({repo, donor, work}) == 3
            and not any(a.is_relative_to(b) for a in (repo, donor, work)
                        for b in (repo, donor, work) if a != b), "independent trusted checkouts required")
    head, changed = repository_identity(repo, donor, target_sha)
    state = source_state(work, repo)
    stable = {name: value[0] for name, value in state.items() if not name.startswith("out/Default/.ninja")
              and name != "out/Default/build.ninja"}
    admission = work / ADMISSION
    consumer = {k: os.environ[k] for k in ("GITHUB_RUN_ID", "GITHUB_SHA")}
    expected = dict(status="verified", donor=manifest(), source_tree=TREE, preparation_key=KEY,
                    source_identity=SOURCE_IDENTITY, target_sha=head, consumer=consumer,
                    unchanged_state=stable, changed_files=[])
    if stage == 13:
        require(not admission.exists(), "initial donor already admitted; restore exact stage12")
        temporary = Path(os.environ["RUNNER_TEMP"]).resolve()
        require(json.loads(read(temporary, "windows-snapshot.json")) == manifest(), "fixed restore manifest mismatch")
        download = json.loads(read(temporary, "windows-snapshot-download.json"))
        exact(download, dict(status="success", phase="complete", publication="published",
                             repository=REPOSITORY, head_sha=SHA, run_id=RUN), "verified snapshot download")
        require([{k: a.get(k) for k in ARTIFACTS[0]} for a in download.get("artifacts", [])] == ARTIFACTS,
                "downloaded artifact identities differ")
    else:
        exact(json.loads(read(work, ADMISSION)), expected, "continuation admission")
    host_tools(work)
    require(not source_output.exists() and source_output.resolve().is_relative_to(
            (work / "fingerprint-diagnostics").resolve()), "new diagnostics source report required")
    proof = stack.verify(work / "src", repo, core=work / "tooling/ungoogled-chromium",
                         tooling=work / "tooling/ungoogled-chromium-windows", platform="windows")
    source_report(proof)
    safe._unchanged(work / "src", state)
    source_output.parent.mkdir(parents=True, exist_ok=True)
    source_output.write_bytes(arp._json(proof))
    if stage == 13:
        admission.write_bytes(arp._json(dict(expected, repository_changed_files=changed)))
    return dict(expected, stage=stage, attempt=os.environ["GITHUB_RUN_ATTEMPT"],
                operation="unchanged-source-admission", original_prepare_receipt=PREPARATION,
                incremental_state={name: value for name, value in state.items()
                                   if name.startswith("out/Default/")})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--metadata", action="store_true")
    mode.add_argument("--archive", type=Path)
    mode.add_argument("--workdir", type=Path)
    parser.add_argument("--donor-repo", type=Path)
    parser.add_argument("--stage", type=int)
    parser.add_argument("--source-output", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    require(not args.report.exists(), "report already exists")
    if args.metadata:
        result = validate_metadata(Client(REPOSITORY, os.environ.get("GH_TOKEN", "")))
    elif args.archive:
        result = check_archive(args.archive)
    else:
        require(args.donor_repo is not None and args.stage is not None and args.source_output is not None,
                "admission requires donor-repo/stage/source-output")
        result = verify(args.workdir, args.donor_repo, REPO, args.stage,
                        os.environ.get("GITHUB_SHA"), args.source_output)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
