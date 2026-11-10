#!/usr/bin/env python3
"""Resume the exact failed Intel continuation stage2, preserving its original admission."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

try:
    from . import validate_macos156_x64_stage8_checkpoint as intel
except ImportError:
    import validate_macos156_x64_stage8_checkpoint as intel

RUN = 42683707985
JOB = 129524021495
ATTEMPT = 1
SHA = "d885c638aa3188c59162c376b38939c57a1f2076"
TREE = "0f7728a96c3ae6271d1a9e3afc5b7c80683cd2ee"
PREFIX = "chromix-mac156-x64-stage8-resume-tree-s2-attempt-1-part"
ARTIFACTS = [
    dict(id=13724617338, name=PREFIX + "1", size_in_bytes=8599926529,
         digest="sha256:b87fde3fa628aa6ddbabc1279fc7bee4925238c68c07841b0386124a3bb8e8cd", expired=False),
]
VOLUME_SIZES = [8599926271]
VOLUME_HASH = "130a106933b6bce26f1a55a11e59a465f885e99c190fa9f5a9ac05105053bfb5"
LOG_HASH = "63678d2874ad2771574af531604eb33873a295173ee6ed60c626a88d527379a7"
LOG_SIZE = 135365
RECEIPT = "fingerprint-diagnostics/mac156-x64-stage2-resume.json"
CONTINUATION = "mac156-x64-stage2-continuation"
HANDOFF = "fingerprint-diagnostics/mac156-x64-stage8-continuation-s1-after-attempt-1.json"
DONOR_RECEIPTS = {
    intel.RECEIPT: "cb84d8d3e7aebee471da88c6a281d4662c45a6cc4b1eee4737c6d829f9607fd8",
    HANDOFF: "076420cdf4520e08c6a2b1a7e49ca1a3bb7fada1ba9eb0236cf2b07978e7c782",
    HANDOFF.replace(".json", "-source.json"):
        "afebdcf64f7a79524d20aa942feb0e826fa427a35f1953e0d960323096afc421",
}
require = intel.require


def expected_manifest():
    return dict(repository=intel.REPOSITORY, platform="macos", arch="x64",
                workflow="build-mac156-x64-stage8-resume", run_id=RUN, attempt=ATTEMPT,
                stage=2, head_sha=SHA, job_id=JOB, pattern=PREFIX + "*", artifacts=ARTIFACTS)


def producer_log():
    return subprocess.run(["gh", "api", "--allow-escape-sequences",
                           f"repos/{intel.REPOSITORY}/actions/jobs/{JOB}/logs"],
                          check=True, capture_output=True, timeout=120).stdout


def validate_metadata(client, repository, artifact_ids, *, log_loader=producer_log):
    require(repository == intel.REPOSITORY and artifact_ids == [a["id"] for a in ARTIFACTS],
            "exact latest Intel stage2 repository/artifacts required")
    run = client.get(f"/actions/runs/{RUN}")
    expected = dict(id=RUN, head_sha=SHA, head_branch="main", event="workflow_dispatch",
                    path=".github/workflows/build-mac156-x64-stage8-resume.yml",
                    run_attempt=ATTEMPT, status="completed", conclusion="failure")
    require(type(run.get("run_attempt")) is int and all(run.get(k) == v for k, v in expected.items())
            and run.get("repository", {}).get("full_name") == repository
            and run.get("head_repository", {}).get("full_name") == repository,
            "exact failed continuation run required")
    job = client.get(f"/actions/jobs/{JOB}")
    expected = dict(id=JOB, run_id=RUN, head_sha=SHA, head_branch="main", run_attempt=ATTEMPT,
                    status="completed", conclusion="failure", labels=["macos-26-intel"],
                    name="macOS156 Intel (read-only stage8 admission, logical 9..16) / "
                         "macos-x64 stage 2 (resume compile)")
    require(type(job.get("run_attempt")) is int and all(job.get(k) == v for k, v in expected.items()),
            "exact failed stage2 producer job required")
    steps = {s["name"]: s for s in job.get("steps", [])}
    required_steps = {"Run stage 2": "failure", "Verify handoff snapshot": "success",
                      "Download tree from previous stage": "success",
                      "Preserve latest macOS compiled checkpoint": "success",
                      "Upload failed macOS runtime bundle": "skipped",
                      **{f"Upload tree part {i}": "success" for i in range(1, 5)}}
    for name, conclusion in required_steps.items():
        require((steps.get(name, {}).get("status"), steps.get(name, {}).get("conclusion")) ==
                ("completed", conclusion), "incomplete producer step: " + name)
    raw = log_loader()
    require(len(raw) == LOG_SIZE and intel.sha(raw) == LOG_HASH, "exact stage2 producer log required")
    for text in ("mac156 x64-stage8 continuation rejected: exact previous continuation handoff required",
                 "packed 8599926271 bytes as 1 volumes (streaming)",
                 *(f"Final size is {a['size_in_bytes']} bytes. Artifact ID is {a['id']}" for a in ARTIFACTS),
                 *(f".snapshot-stage-2/p{i}/. No artifacts will be uploaded." for i in (2, 3, 4))):
        require(text.encode() in raw, "stage2 snapshot/upload evidence missing")
    artifacts = sorted((a for a in client.items(f"/actions/runs/{RUN}/artifacts", "artifacts")
                        if a.get("name", "").startswith(PREFIX)), key=lambda a: a["name"])
    require(len(artifacts) == len(ARTIFACTS), "complete exact stage2 artifact set required")
    for actual, expected in zip(artifacts, ARTIFACTS):
        require(all(actual.get(k) == v for k, v in expected.items()) and actual.get("expired") is False,
                "stage2 artifact ID/name/size/digest/expiry mismatch")
        origin = actual.get("workflow_run", {})
        require(all(origin.get(k) == v for k, v in dict(id=RUN, head_sha=SHA, head_branch="main").items()),
                "stage2 artifact origin mismatch")
    return expected_manifest()


def download_identity(origin, report):
    manifest = json.loads(origin.read_bytes())
    require(manifest == expected_manifest(), "exact latest stage2 manifest required")
    data = json.loads(report.read_bytes())
    require((data.get("status"), data.get("publication"), data.get("phase")) ==
            ("success", "published", "complete"), "verified published download required")
    require(all(data.get(k) == manifest[k] for k in ("repository", "head_sha", "run_id")),
            "stage2 download origin mismatch")
    artifacts, volumes = data.get("artifacts", []), data.get("volumes", [])
    require(len(artifacts) == len(volumes) == 1, "single latest stage2 part required")
    for index, (actual, expected, volume, size) in enumerate(zip(artifacts, ARTIFACTS, volumes, VOLUME_SIZES), 1):
        require(all(actual.get(k) == v for k, v in expected.items()), "stage2 downloaded artifact mismatch")
        attempts = actual.get("attempts", [])
        require(attempts and attempts[-1].get("status") == "success"
                and attempts[-1].get("size_in_bytes") == expected["size_in_bytes"]
                and "sha256:" + attempts[-1].get("sha256", "") == expected["digest"],
                "stage2 verified ZIP digest/size required")
        require(volume.get("name") == f"tree.tar.zst.{index:03d}"
                and volume.get("artifact_id") == expected["id"] and volume.get("size_in_bytes") == size
                and volume.get("sha256") == VOLUME_HASH,
                "stage2 packed volume mismatch")
    require(data.get("total_size_in_bytes") == sum(VOLUME_SIZES), "stage2 packed total mismatch")
    return dict(manifest=manifest, volumes=volumes, producer_log_sha256=LOG_HASH, producer_log_size=LOG_SIZE)


def continuation_identity():
    return dict(status="verified", operation="unchanged-mac156-x64-stage2-continuation",
                repository=intel.REPOSITORY, run_id=RUN, attempt=ATTEMPT, stage=2, job_id=JOB,
                head_sha=SHA, tree=TREE, artifacts=ARTIFACTS, version=intel.VERSION, platform="macos", arch="x64",
                preparation_key=intel.KEY, source_identity=intel.SOURCE_IDENTITY, producer_log_sha256=LOG_HASH,
                completed_logical_stage=9,
                continuation_stages="1..8 after failed stage2; stage1 handoff (logical 10..17)", changed_files=[])


def donor_identity(work):
    for name, digest in DONOR_RECEIPTS.items():
        require(intel.sha(intel.read(work, name)) == digest, "exact packed donor receipt required: " + name)
    receipt = json.loads(intel.read(work, intel.RECEIPT))
    require(receipt.get("continuation_run") == str(RUN)
            and receipt.get("provenance", {}).get("manifest") == intel.expected_manifest(),
            "original stage8 admission provenance changed")
    directory = work / "fingerprint-diagnostics"
    pattern = re.compile(r"mac156-x64-stage8-continuation-s1-after-attempt-([1-9][0-9]*)\.json")
    require([path.name for path in directory.iterdir() if pattern.fullmatch(path.name)] == [Path(HANDOFF).name],
            "exact single donor stage1 handoff required")
    handoff = json.loads(intel.read(work, HANDOFF))
    expected = {key: value for key, value in receipt.items()
                if key not in ("recipes", "state", "provenance")}
    expected.update(continuation_attempt="1", continuation_stage=1, logical_stage=intel.STAGE + 1, phase="after")
    require(all(handoff.get(k) == v for k, v in expected.items()), "exact stage1 handoff provenance required")
    require(handoff["state"]["graph"] == receipt["state"]["graph"], "original admitted graph changed")
    # Stage2 failed before its gate wrote a receipt or invoked Ninja.
    require(not list((work / "fingerprint-diagnostics").glob("mac156-x64-stage8-continuation-s2-*.json")),
            "unexpected donor stage2 compile evidence")
    return receipt, handoff


def admit(work, donor, repo, origin, download_report):
    require(re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ID", "")), "continuation run required")
    provenance = download_identity(origin, download_report)
    require(intel.migration._donor_git(donor, repo, "rev-parse", "HEAD").decode().strip() == SHA
            and intel.migration._donor_git(donor, repo, "rev-parse", "HEAD^{tree}").decode().strip() == TREE
            and not intel.migration._donor_git(donor, repo, "status", "--porcelain", "--untracked-files=all"),
            "clean exact stage2 producer checkout required")
    old, handoff = donor_identity(work)
    recipes = intel.recipes(repo)
    require(recipes == old.get("recipes") == intel.recipes(donor), "stage2 admitted recipes changed")
    require(intel.arp.patch_selection.select(repo, "macos") == intel.arp.patch_selection.select(donor, "macos"),
            "stage2 selected patches changed")
    state = intel.source_state(repo, work)
    require(state == handoff.get("state") and state["graph"] == old["state"]["graph"],
            "latest stage2 must retain the actual stage1 handoff Ninja state/graph")
    inventory = intel.output_inventory(work / "src/out/Default")
    intel.native_state(repo, work)
    intel.migration.restore.verify_restored(work, "macos", "x64", repo=repo)
    intel.migration._verify_tooling(work, repo, (work, donor, repo), "macos")
    output = work / RECEIPT
    require(not output.exists(), "stage2 recovery already admitted")
    intel.live_source(repo, work, output.with_name("mac156-x64-stage2-source-admission.json"))
    require(intel.source_state(repo, work) == state and intel.output_inventory(work / "src/out/Default") == inventory,
            "stage2 admission modified existing build state")
    result = dict(continuation_identity(), provenance=provenance, recipes=recipes, state=state,
                  continuation_run=os.environ["GITHUB_RUN_ID"], donor_receipts=DONOR_RECEIPTS)
    output.write_bytes(intel.arp._json(result))
    return result


def continuation(repo, work, stage, phase):
    _, handoff = donor_identity(work)
    base = handoff["logical_stage"]
    receipt_name, prefix = RECEIPT, CONTINUATION
    expected, manifest = continuation_identity(), expected_manifest()
    require(phase in ("before", "after"), "invalid continuation phase")
    require(re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ID", "")), "continuation run ID required")
    require(re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ATTEMPT", "")), "continuation attempt required")
    require(1 <= stage <= 8, "continuation stage must be 1..8")
    require(os.environ.get("CHROMIX_BUILD_PROFILE") == "fast"
            and os.environ.get("CHROMIX_USE_UPSTREAM_CACHE") == "1",
            "continuation requires fast profile and mandatory restored cache")
    receipt = json.loads(intel.read(work, receipt_name))
    require(all(receipt.get(key) == value for key, value in expected.items()),
            "missing or changed x64-stage2 continuation provenance")
    require(receipt.get("continuation_run") == os.environ.get("GITHUB_RUN_ID")
            and receipt.get("provenance", {}).get("manifest") == manifest,
            "continuation run/donor isolation mismatch")
    require(intel.recipes(repo) == receipt.get("recipes"), "admitted build recipes changed")
    state = intel.source_state(repo, work)
    require(state["graph"] == receipt["state"]["graph"],
            "GN graph changed; regeneration forbidden")
    if phase == "before":
        intel.native_state(repo, work)
    if stage == 1 and phase == "before":
        require(state == receipt.get("state"), "initial Ninja state changed before continuation")
    if phase == "before" and stage > 1:
        intel.previous_handoff(work, prefix, stage, base, expected, state)
    output = work / "fingerprint-diagnostics" / (
        f"{prefix}-s{stage}-{phase}-attempt-{os.environ['GITHUB_RUN_ATTEMPT']}.json")
    intel.live_source(repo, work, output.with_name(output.stem + "-source.json"))
    result = dict(expected, continuation_stage=stage, logical_stage=base + stage, phase=phase,
                  continuation_run=os.environ.get("GITHUB_RUN_ID"),
                  continuation_attempt=os.environ.get("GITHUB_RUN_ATTEMPT"), state=state)
    output.write_bytes(intel.arp._json(result))
    return result


def compile_tree(repo, work, stage):
    _, handoff = donor_identity(work)
    base = handoff["logical_stage"]
    receipt_name, prefix, expected = RECEIPT, CONTINUATION, continuation_identity()
    receipt = json.loads(intel.read(work, receipt_name))
    require(all(receipt.get(key) == value for key, value in expected.items())
            and receipt.get("continuation_run") == os.environ.get("GITHUB_RUN_ID"), "unadmitted compile")
    require(stage is not None and 1 <= stage <= 8, "compile stage required")
    before = json.loads(intel.read(work, f"fingerprint-diagnostics/{prefix}-s{stage}"
                            f"-before-attempt-{os.environ.get('GITHUB_RUN_ATTEMPT', 'local')}.json"))
    require(all(before.get(key) == value for key, value in expected.items())
            and before.get("continuation_run") == os.environ.get("GITHUB_RUN_ID")
            and before.get("continuation_stage") == stage and before.get("phase") == "before"
            and before.get("logical_stage") == base + stage
            and before.get("continuation_attempt") == os.environ.get("GITHUB_RUN_ATTEMPT")
            and before.get("state") == intel.source_state(repo, work), "compile requires same-attempt read-only gate")
    ninja = Path(shutil.which("ninja") or "missing").resolve()
    require(intel.restore_ninja.host_identity() == ("macos", "x64")
            and intel.restore_ninja.binary_architectures(ninja, "macos") == {"x64"}
            and intel.restore_ninja.probe_version(ninja, work) == "1.13.2", "exact native Ninja 1.13.2 required")
    jobs = os.environ.get("CHROMIX_JOBS", "")
    require(re.fullmatch(r"[1-9][0-9]*", jobs), "positive compile job count required")
    env = {key: value for key, value in os.environ.items() if not key.startswith("DYLD_")}
    return intel.frozen_ninja(ninja, work, jobs, env)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("metadata", "admit", "check", "compile"), required=True)
    for flag in ("repo", "workdir", "previous-repo", "origin", "download-report", "report"):
        parser.add_argument("--" + flag, type=Path)
    parser.add_argument("--stage", type=int)
    parser.add_argument("--phase", choices=("before", "after"))
    args = parser.parse_args(argv)
    try:
        if args.mode == "metadata":
            require(args.report is not None, "metadata report required")
            repository = os.environ.get("GITHUB_REPOSITORY", intel.REPOSITORY)
            result = validate_metadata(intel.snapshot.Client(repository, os.environ.get("GH_TOKEN", "")),
                                       repository, [a["id"] for a in ARTIFACTS])
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_bytes(intel.arp._json(result))
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                    output.write(f"head_sha={SHA}\npattern={result['pattern']}\n")
        else:
            require(args.workdir is not None, "workdir required")
            work = args.workdir.resolve()
            repo = (args.repo or Path(__file__).resolve().parents[1]).resolve()
            if args.mode == "admit":
                require(all((args.previous_repo, args.origin, args.download_report)), "admission provenance required")
                admit(work, args.previous_repo.resolve(), repo, args.origin, args.download_report)
            elif args.mode == "check":
                require(args.stage is not None and args.phase is not None, "stage and phase required")
                continuation(repo, work, args.stage, args.phase)
            else:
                return compile_tree(repo, work, args.stage)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"mac156 Intel stage2 recovery rejected: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
