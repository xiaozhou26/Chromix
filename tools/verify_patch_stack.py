#!/usr/bin/env python3
"""Read-only freshness check: reverse/reapply the current patch stack in scratch.

Readiness stamps identify intended inputs, not actual restored source contents.
This checks every current hunk and new file without modifying SRC or timestamps.
It is structural patch evidence, not an attestation of all upstream source.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import apply_restored_patches as arp


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_stack(repo, core=None, tooling=None, platform=None, substituted=False):
    if substituted:
        if core is None or tooling is None or platform not in ('windows', 'linux', 'macos'):
            raise ValueError('domain-substituted sources require core, platform-tooling and platform')
        identity, patches, _ = arp._load(repo, core, tooling, platform)
        return identity, patches
    series = arp._read(repo, 'patches/series')
    names = [line.split('#', 1)[0].strip() for line in series.decode('utf-8').splitlines()]
    names = [name for name in names if name]
    if not names or len(names) != len(set(names)):
        raise ValueError('empty or duplicate patch series')
    patches, identities = [], []
    for name in names:
        raw = arp._read(repo, name)
        data, entries = arp.transform_patch(raw, set(), [])
        patches.append((name, data, entries))
        identities.append({'path': name, 'sha256': digest(raw)})
    return {'series_sha256': digest(series), 'patches': identities}, patches


def verify(src, repo, *, core=None, tooling=None, platform=None):
    src, repo = Path(src).resolve(), Path(repo).resolve()
    if not src.is_dir() or not repo.is_dir() or src == repo:
        raise ValueError('distinct existing source and repository directories required')
    for name in (arp.IN_PROGRESS, '.chromix-layer-in-progress', '.chromix-patch-in-progress',
                 '.chromix-domain-substitution-in-progress'):
        if (src / name).exists():
            raise ValueError('source has unfinished preparation: ' + name)
    substituted = (src / '.chromix-domain-substituted').exists()
    identity, patches = load_stack(repo, core, tooling, platform, substituted)
    names = {entry[0] for _, _, entries in patches for entry in entries}
    before = arp._snapshot(src, names)
    program = shutil.which('git')
    if not program:
        raise ValueError('git is required for read-only source verification')
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    with tempfile.TemporaryDirectory(prefix='chromix-patch-verify-') as temporary:
        root = Path(temporary)
        stage = root / 'src'
        stage.mkdir()
        # TMPDIR may itself be inside a developer checkout. Without a ceiling,
        # git apply can discover that parent repository and silently skip paths
        # outside its cwd prefix, even with --no-index and a zero exit status.
        env['GIT_CEILING_DIRECTORIES'] = str(root)
        # CRLF is a source checkout representation, not a semantic migration.
        # Only the disposable copy is normalized; original bytes/mtime are checked below.
        for name, (data, _) in before.items():
            if data is not None:
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data.replace(b'\r\n', b'\n'))
        for reverse in (True, False):
            sequence = reversed(patches) if reverse else patches
            for name, data, entries in sequence:
                patch = root / 'current.patch'
                patch.write_bytes(data)
                args = [program, '-c', 'core.autocrlf=false', 'apply', '--no-index', '--whitespace=nowarn']
                if reverse:
                    args.append('--reverse')
                result = subprocess.run([*args, str(patch)], cwd=stage, env=env,
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
                if result.returncode:
                    detail = result.stderr.decode('utf-8', errors='replace').strip()
                    raise ValueError(f'stale/incompatible source at {name} ({"reverse" if reverse else "forward"}): '
                                     f'{detail}; restore a clean matching source; do not rewrite readiness stamps')
                for target, action, _ in entries:
                    if (action == 'create' and reverse or action == 'delete' and not reverse) and (stage / target).exists():
                        raise ValueError('patch did not remove the complete expected file: ' + target)
        for name, (data, info) in before.items():
            target = arp._path(stage, name)
            observed = target.read_bytes() if target.exists() else None
            if observed != (data.replace(b'\r\n', b'\n') if data is not None else None):
                raise ValueError('patch roundtrip changed source: ' + name)
            original = arp._path(src, name)
            if ((original.read_bytes() if original.exists() else None) != data or
                    (info and original.stat().st_mtime_ns != info.st_mtime_ns)):
                raise ValueError('source changed concurrently: ' + name)
    # Detect a concurrent repository edit too; don't certify a mixed patch set.
    if load_stack(repo, core, tooling, platform, substituted)[0] != identity:
        raise ValueError('patch inputs changed during verification')
    return {'schema_version': 1, 'status': 'verified', 'method': 'reverse-forward-in-scratch',
            'qualification': 'current patch hunks/new files, not full upstream attestation',
            'domain_substituted': substituted, 'patch_count': len(patches), 'identity': identity,
            'outputs': {name: digest(data) if data is not None else None for name, (data, _) in before.items()}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--src', type=Path, required=True)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--core', type=Path)
    parser.add_argument('--platform-tooling', type=Path)
    parser.add_argument('--platform', choices=('windows', 'linux', 'macos'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.resolve().is_relative_to(args.src.resolve()):
        parser.error('output must be a new file outside SRC')
    try:
        report = verify(args.src, args.repo, core=args.core, tooling=args.platform_tooling, platform=args.platform)
    except (ValueError, OSError, arp.ApplyError, subprocess.SubprocessError) as error:
        report = {'schema_version': 1, 'status': 'failed', 'error': str(error)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps({'status': report['status'], 'error': report.get('error'), 'output': str(args.output)}))
    return int(report['status'] != 'verified')


if __name__ == '__main__':
    raise SystemExit(main())
