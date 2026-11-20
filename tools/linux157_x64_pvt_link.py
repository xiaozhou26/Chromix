#!/usr/bin/env python3
"""Run the fixed x64 stage2 PVT link profile without changing the ARM recipe."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import sys
from pathlib import Path

try:
    from . import linux157_storage as shared
    from . import linux157_pvt_link as engine
except ImportError:
    import linux157_storage as shared
    import linux157_pvt_link as engine

source = engine.source
MANIFEST = 'build/linux/157.0.8103.78/x64-pvt-link-checkpoint.json'
MANIFEST_SHA256 = '8f8a04894ad625d490364a663ac421da21ad3b5e3de44e6ce2bc07fe34fbceea'
HELPER = 'tools/linux157_x64_pvt_link.py'
BINDING = 'x64_pvt_link_profile'


def load(repo=source.ROOT, arch='x64'):
    if arch != 'x64':
        raise ValueError('x64 PVT link profile cannot admit another architecture')
    raw = source.regular(repo, MANIFEST).read_bytes()
    if source.digest(raw) != MANIFEST_SHA256:
        raise ValueError('unreviewed x64 PVT link checkpoint manifest')
    return json.loads(raw)


def binding(repo=source.ROOT):
    manifest = load(repo)
    return {'manifest': {'path': MANIFEST, 'sha256': MANIFEST_SHA256},
            'helper': {'path': HELPER, 'sha256': engine.tools.file_hash(source.regular(repo, HELPER))},
            'engine': {'path': 'tools/linux157_pvt_link.py',
                       'sha256': engine.tools.file_hash(source.regular(repo, 'tools/linux157_pvt_link.py'))},
            'route_inputs': {name: engine.tools.file_hash(source.regular(repo, name)) for name in (
                'build/posix/ci-stage.sh', 'tools/gen_linux157_x64_pvt_link_workflow.py',
                '.github/workflows/build-linux157-x64-pvt-link-resume.yml',
                '.github/workflows/build-linux157-x64-pvt-link-stages.yml')},
            'recipe_sha256': source.recipe(repo)['sha256'],
            'run_id': manifest['run_id'], 'job_id': manifest['job_id'],
            'head_sha': manifest['head_sha'], 'arch': 'x64', 'stage': 2,
            'attempt': 1, 'continuation_stage': 3, 'old_patch_count': 232, 'new_patch_count': 233}


def authenticate(repo=source.ROOT):
    engine.review(load(repo))
    selected, _ = engine.patch_selection.select(repo, 'linux')
    integration = selected['selection']['downstream']['validation']['recipe_integration']
    if integration.get(BINDING) != binding(repo):
        raise ValueError('x64 PVT link helper/profile requires exact parent selector review')
    if source.recipe(repo) != load(repo)['target_recipe']:
        raise ValueError('x64 PVT link cannot change the existing global recipe')
    return selected


@contextmanager
def profile(repo=source.ROOT):
    """Bind only this process's explicit x64 route; restore ARM defaults on exit."""
    authenticate(repo)
    originals = engine.load, shared.verify_current, shared.verify_current_ninja
    def verify_current(work, arch, repo=source.ROOT):
        load(repo, arch)
        return engine.verify(work, arch, repo)
    def verify_ninja(work, repo=source.ROOT):
        receipt = json.loads(source.regular(work / 'src', source.RECEIPT).read_bytes())
        load(repo, receipt['target_arch'])
        return engine.verify_ninja(work, repo)
    engine.load = load
    shared.verify_current = verify_current
    shared.verify_current_ninja = verify_ninja
    try:
        yield engine
        authenticate(repo)
    finally:
        engine.load, shared.verify_current, shared.verify_current_ninja = originals


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--command', choices=('migration', 'source', 'build', 'upload'), required=True)
    parser.add_argument('--repo', type=Path, default=source.ROOT)
    options, remaining = parser.parse_known_args(argv)
    repo = options.repo.resolve()
    try:
        if options.command == 'migration':
            args_parser = argparse.ArgumentParser()
            args_parser.add_argument('--phase', choices=('metadata', 'restore', 'admit', 'verify'), required=True)
            args_parser.add_argument('--arch', choices=('x64',), required=True)
            args_parser.add_argument('--workdir', type=Path)
            args_parser.add_argument('--metadata', type=Path)
            args = args_parser.parse_args(remaining)
            if args.phase == 'metadata':
                result = engine.validate_metadata(engine.snapshot.Client(load(repo)['repository'], engine.os.environ.get('GH_TOKEN', '')), 'x64', repo)
                print(json.dumps(result, sort_keys=True))
                return 0
            with profile(repo) as active:
                if args.phase == 'metadata':
                    result = active.validate_metadata(active.snapshot.Client(load(repo)['repository'], active.os.environ.get('GH_TOKEN', '')), 'x64', repo)
                elif args.phase == 'restore' and args.workdir:
                    result = active.restore(args.workdir.absolute(), 'x64', repo)
                elif args.phase == 'admit' and args.workdir and args.metadata:
                    result = active.admit(args.workdir.resolve(), 'x64', json.loads(args.metadata.read_bytes()), repo)
                elif args.phase == 'verify' and args.workdir:
                    result = active.verify(args.workdir.resolve(), 'x64', repo)
                else:
                    args_parser.error('missing phase-specific arguments')
            print(json.dumps(result, sort_keys=True))
            return 0
        if '--archive-report' not in remaining:
            if '--arch' not in remaining or remaining[remaining.index('--arch') + 1] != 'x64':
                raise ValueError('x64 PVT link wrapper requires explicit --arch x64')
        with profile(repo):
            if options.command == 'source':
                return source.main(['--repo', str(repo), *remaining])
            if options.command == 'build':
                try:
                    from . import linux157_build
                except ImportError:
                    import linux157_build
                return linux157_build.main(['--repo', str(repo), *remaining])
            # The uploader uses script imports; share the bound engine/source instances.
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            sys.modules['linux157_source'] = source
            sys.modules['linux157_storage'] = shared
            import verify_linux157_upload
            return verify_linux157_upload.main(['--repo', str(repo), *remaining])
    except (OSError, ValueError, KeyError, engine.subprocess.SubprocessError) as error:
        print('Linux157 x64 PVT link recovery: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
