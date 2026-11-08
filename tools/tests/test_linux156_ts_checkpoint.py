"""Exact donor metadata is required before any Linux156 recovery download."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

from tools import validate_linux156_ts_checkpoint as gate


class Client:
    def __init__(self, arch):
        donor = gate.DONORS[arch]
        self.run = {"id": donor["run_id"], "path": f".github/workflows/build-linux-{arch}.yml",
                    "head_branch": "main", "head_sha": gate.HEAD, "event": "workflow_dispatch",
                    "repository": {"full_name": gate.REPOSITORY},
                    "head_repository": {"full_name": gate.REPOSITORY},
                    "status": "completed", "conclusion": "failure", "run_attempt": 1}
        self.jobs = [{"id": donor["job_id"], "name": f"Linux / linux-{arch} stage 1 (prepare + first compile)",
                      "status": "completed", "conclusion": "failure", "steps": [
                          {"name": "Run stage 1", "conclusion": "failure"},
                          {"name": "Preserve latest failed Linux source/output checkpoint", "conclusion": "success"},
                          {"name": "Verify handoff snapshot", "conclusion": "success"},
                          *[{"name": f"Upload tree part {i}", "conclusion": "success"} for i in range(1, 5)]]}]
        self.artifacts = [{**gate.expected(arch)["artifacts"][0],
                           "workflow_run": {"id": donor["run_id"], "head_sha": gate.HEAD}}]

    def get(self, path):
        assert path == f"/actions/runs/{self.run['id']}"
        return self.run

    def items(self, path, key):
        return self.jobs if key == "jobs" else self.artifacts


class Linux156DonorTest(unittest.TestCase):
    def test_both_exact_failed_stage1_donors(self):
        for arch in gate.DONORS:
            with self.subTest(arch=arch):
                self.assertEqual(gate.validate(Client(arch), arch), gate.expected(arch))

    def test_changed_run_job_artifact_or_preservation_is_rejected(self):
        for arch in gate.DONORS:
            cases = (("run", "head_sha", "a" * 40), ("run", "head_branch", "other"),
                     ("run", "run_attempt", 2), ("run", "conclusion", "success"),
                     ("run", "status", "in_progress"), ("job", "id", 99),
                     ("job", "conclusion", "success"), ("artifact", "id", 99),
                     ("artifact", "size_in_bytes", 123), ("artifact", "digest", "sha256:" + "a" * 64),
                     ("artifact", "expired", True), ("artifact", "name", "chromix-linux-x64-tree-s2-attempt-1-part1"))
            for kind, key, value in cases:
                with self.subTest(arch=arch, kind=kind, key=key):
                    client = Client(arch)
                    target = client.run if kind == "run" else client.jobs[0] if kind == "job" else client.artifacts[0]
                    target[key] = value
                    with self.assertRaises(ValueError):
                        gate.validate(client, arch)
            for name in ("Run stage 1", "Preserve latest failed Linux source/output checkpoint",
                         "Verify handoff snapshot", "Upload tree part 1", "Upload tree part 4"):
                with self.subTest(arch=arch, step=name):
                    client = Client(arch)
                    next(s for s in client.jobs[0]["steps"] if s["name"] == name)["conclusion"] = "skipped"
                    with self.assertRaises(ValueError):
                        gate.validate(client, arch)

    def test_no_other_origin_or_partial_artifact_set(self):
        for arch in gate.DONORS:
            client = Client(arch)
            with self.assertRaises(ValueError):
                gate.validate(client, arch, "someone/fork")
            client.artifacts.append({**client.artifacts[0], "id": 123,
                                     "name": client.artifacts[0]["name"].replace("part1", "part2")})
            with self.assertRaises(ValueError):
                gate.validate(client, arch)
            for key, value in (("attempt", True), ("stage", 2), ("head_sha", "a" * 40), ("arch", "other")):
                manifest = copy.deepcopy(gate.expected(arch))
                manifest[key] = value
                with self.assertRaises(ValueError):
                    gate.check_manifest(manifest, arch)


class Linux156ResumeWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[2]
        self.reusable = yaml.safe_load((self.root / '.github/workflows/build-posix-github.yml').read_text())

    def test_exact_both_manual_entries_reuse_eight_stages_and_native_gate(self):
        for arch, runner in (('x64', 'ubuntu-22.04'), ('arm64', 'ubuntu-24.04')):
            entry = yaml.safe_load((self.root / f'.github/workflows/build-linux156-{arch}-ts-resume.yml').read_text())
            events = entry.get('on', entry.get(True))
            self.assertEqual(set(events), {'workflow_dispatch'})
            self.assertEqual(set(events['workflow_dispatch']['inputs']), {'compile_jobs'})
            job = entry['jobs']['resume']
            self.assertEqual(job['uses'], './.github/workflows/build-posix-github.yml')
            inputs = job['with']
            self.assertEqual(inputs['resume_run_id'], str(gate.DONORS[arch]['run_id']))
            self.assertEqual(inputs['resume_artifact_ids'], str(gate.DONORS[arch]['artifact_id']))
            self.assertEqual(inputs['resume_tree_stage'], '1')
            self.assertEqual(inputs['resume_attempt'], '1')
            self.assertEqual(inputs['resume_migration'], 'linux156-ts')
            self.assertEqual(inputs['runner'], runner)
            self.assertEqual(inputs['max-stages'], 8)
            self.assertEqual(inputs['build_profile'], 'fast')
            self.assertIs(inputs['use_upstream_cache'], True)
        baseline = yaml.safe_load(subprocess.check_output(
            ['git', 'show', gate.HEAD + ':.github/workflows/build-posix-github.yml'], cwd=self.root, text=True))
        self.assertEqual(self.reusable['jobs']['verify-linux-arm64'], baseline['jobs']['verify-linux-arm64'])
        for number in range(1, 9):
            old = baseline['jobs'][f'posix-{number}']['steps']
            new = self.reusable['jobs'][f'posix-{number}']['steps']
            for identifier in ('stage', 'linux_runtime_checkpoint', 'checkpoint'):
                self.assertEqual(next(s for s in old if s.get('id') == identifier),
                                 next(s for s in new if s.get('id') == identifier))
            self.assertEqual([s for s in old if s.get('name', '').startswith('Upload tree part')],
                             [s for s in new if s.get('name', '').startswith('Upload tree part')])

    def test_guard_rejects_empty_donor_old_upstream_wrong_arch_or_mode(self):
        steps = self.reusable['jobs']['posix-1']['steps']
        guard = next(s for s in steps if s.get('name') == 'Require exact Linux156 recovery inputs')
        self.assertLess(steps.index(guard), next(i for i,s in enumerate(steps) if s.get('id') == 'stage'))
        for arch in gate.DONORS:
            valid = {'resume_migration': 'linux156-ts', 'platform': 'linux', 'arch': arch,
                     'use_upstream_cache': 'true', 'build_profile': 'fast', 'resume_tree_stage': '1',
                     'resume_attempt': '1', 'resume_run_id': str(gate.DONORS[arch]['run_id']),
                     'resume_artifact_ids': str(gate.DONORS[arch]['artifact_id'])}
            for change in ({}, {'resume_run_id': ''}, {'resume_run_id': '42455171946'},
                           {'resume_artifact_ids': ''}, {'resume_tree_stage': '2'}, {'resume_attempt': '2'},
                           {'use_upstream_cache': 'false'}, {'build_profile': 'release'}, {'platform': 'macos'},
                           {'arch': 'arm64' if arch == 'x64' else 'x64'}, {'resume_migration': 'other'}):
                script = guard['run']
                for key, value in {**valid, **change}.items():
                    script = script.replace('${{ inputs.' + key + ' }}', value)
                result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, not change, (change, result.stderr))

    def test_migration_failure_stops_without_generic_or_cold_fallback(self):
        step = next(s for s in self.reusable['jobs']['posix-1']['steps']
                    if s.get('name') == 'Restore and migrate selected POSIX checkpoint')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'build/posix').mkdir(parents=True)
            (root / 'tools').mkdir()
            (root / 'chromix-logs').mkdir()
            (root / 'build/posix/restore-snapshot.sh').write_text('#!/bin/sh\nmkdir -p "$2/src"\n')
            (root / 'tools/migrate_linux156_ts_snapshot.py').write_text('raise SystemExit(23)\n')
            (root / 'tools/migrate_restored_snapshot.py').write_text('raise RuntimeError("must not fall back")\n')
            script = step['run'].replace('${{ inputs.resume_migration }}', 'linux156-ts')
            script = script.replace('${{ inputs.arch }}', 'x64').replace('${{ inputs.platform }}', 'linux')
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=root,
                                    env={**os.environ, 'RUNNER_TEMP': str(root), 'GITHUB_WORKSPACE': str(root)},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
            self.assertNotIn('must not fall back', result.stdout + result.stderr)
            self.assertTrue((root / 'chromix-build/src').is_dir())


if __name__ == "__main__":
    unittest.main()
