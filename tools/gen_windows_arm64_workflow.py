#!/usr/bin/env python3
"""Generate the independent Windows ARM64 build from the Windows stage template."""
from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / '.github/workflows/build-win-x64-github.yml'
OUTPUT = REPO / '.github/workflows/build-win-arm64-github.yml'


def replace_target(value):
    if isinstance(value, str):
        return value.replace('win-x64', 'win-arm64').replace('tree-s', 'win-arm64-tree-s')
    if isinstance(value, list):
        return [replace_target(item) for item in value]
    if isinstance(value, dict):
        return {key: replace_target(item) for key, item in value.items()}
    return value


def workflow():
    source = yaml.safe_load(TEMPLATE.read_text(encoding='utf-8'))
    result = replace_target(deepcopy(source))
    events = result.pop(True) if True in result else result.pop('on')
    result = {'name': result.pop('name'), 'on': events, **result}
    for event in ('workflow_call', 'workflow_dispatch'):
        events[event]['inputs'] = {name: value for name, value in events[event]['inputs'].items()
                                   if name in ('build_profile', 'compile_jobs', 'use_upstream_cache', 'upstream_run_id')}
        events[event]['inputs']['use_upstream_cache']['default'] = False
    events['push']['paths'] += [
        '.github/workflows/build-win-x64-github.yml',
        'tools/gen_windows_arm64_workflow.py',
        'tools/verify_windows_bundle.py',
        'tools/tests/test_windows_arm64_workflow.py',
        'tools/tests/test_windows_arm64_build.py',
        'tools/tests/test_windows_upstream_cache.py',
        'tools/tests/test_verify_windows_bundle.py',
    ]
    result['env'].pop('CHROMIX_WINDOWS_MIGRATION_PROFILE', None)
    result['env'].update(CHROMIX_TARGET_ARCH='arm64', CHROMIX_PREFER_UPSTREAM_CACHE='0')
    for index in range(1, 13):
        job = result['jobs'][f'build-{index}']
        if index == 1:
            job.pop('if', None)
        else:
            previous = f'build-{index - 1}'
            job['if'] = ("${{ always() && needs." + previous + ".result == 'success' && needs."
                         + previous + ".outputs.finished != 'true' }}")
        steps = []
        for step in job['steps']:
            if step.get('name') in ('Download tree from previous run', 'Check explicit snapshot migration inputs',
                                     'Restore exact source-migration snapshot'):
                continue
            if (step.get('name') == 'Ensure build tree snapshot'
                    or step.get('name', '').startswith('Upload tree part')):
                step['if'] = step['if'].replace(' }}', " && steps.stage.outputs.finished != 'true' }}")
            if step.get('name') == 'Download tree from previous stage':
                step.pop('if', None)
            if step.get('id') == 'stage' and index > 1:
                step['run'] = f'build\\windows\\ci-stage.ps1 -StageIndex {index} -MaxStages 12 -FromArtifact'
            steps.append(step)
            if step.get('name') == 'Verify native process-tree cleanup':
                steps.append({
                    'name': 'Verify generated Windows ARM64 workflow',
                    'shell': 'powershell', 'timeout-minutes': 2,
                    'run': 'python tools/gen_windows_arm64_workflow.py --check\n'
                           'if ($LASTEXITCODE -ne 0) { throw "Windows ARM64 workflow generation is stale" }',
                })
            if step.get('name') == 'Upload final bundle':
                step['with']['name'] = 'win-arm64'
                steps.append({
                    'name': 'Upload final source receipt',
                    'if': "steps.stage.outputs.finished == 'true'",
                    'uses': 'actions/upload-artifact@v4',
                    'with': {'name': 'chromix-win-arm64-source-receipt',
                             'path': 'C:\\c\\dist\\source-verification.json',
                             'if-no-files-found': 'error', 'retention-days': 7},
                })
        job['steps'] = steps
    complete_step = result['jobs']['complete']['steps'][0]
    complete_step['run'] = complete_step['run'].replace('chromix-win-arm64 artifact', 'win-arm64 artifact')
    result['jobs']['verify-arm64'] = {
        'name': 'native Windows ARM64 bundle and fingerprint verification',
        'needs': 'complete',
        'if': "${{ !cancelled() && needs.complete.result == 'success' }}",
        'runs-on': 'windows-11-arm',
        'timeout-minutes': 60,
        'steps': [
            {'uses': 'actions/checkout@v4'},
            {'uses': 'actions/setup-python@v5', 'with': {'python-version': '3.13', 'architecture': 'x64'}},
            {'name': 'Download this run\'s compiled ARM64 bundle',
             'uses': 'actions/download-artifact@v4',
             'with': {'name': 'win-arm64', 'path': 'dist'}},
            {'name': 'Download this run\'s source receipt',
             'uses': 'actions/download-artifact@v4',
             'with': {'name': 'chromix-win-arm64-source-receipt', 'path': 'source-receipt'}},
            {'name': 'Verify checksum, PE architecture, version and native headless exit',
             'shell': 'pwsh', 'timeout-minutes': 5,
             'run': 'python tools/verify_windows_bundle.py --archive dist/chromix-win-arm64.zip '
                    '--sha256-file dist/SHA256SUMS --dest smoke --arch arm64 --native '
                    '--report diagnostics/native-smoke.json\n'
                    'if ($LASTEXITCODE -ne 0) { throw "native ARM64 bundle verification failed" }'},
            {'name': 'Install fingerprint verification dependencies', 'shell': 'pwsh', 'timeout-minutes': 10,
             'run': 'python -m pip install --disable-pip-version-check --timeout 30 --retries 1 '
                    '-r tools/fingerprint-requirements.txt\n'
                    'if ($LASTEXITCODE -ne 0) { throw "fingerprint dependency installation failed" }'},
            {'name': 'Verify native ARM64 fingerprint behavior', 'shell': 'pwsh', 'timeout-minutes': 40,
             'run': '$browser = (Resolve-Path smoke/chromix/chrome.exe).Path\n'
                    '$hash = (Get-FileHash -LiteralPath $browser -Algorithm SHA256).Hash.ToLowerInvariant()\n'
                    '$pins = & build/windows/read-platform-pins.ps1 -Repo $env:GITHUB_WORKSPACE\n'
                    '$version = $pins.ChromiumVersion\n'
                    'python -X utf8 tools/fingerprint_acceptance.py --browser $browser '
                    '--expected-sha256 $hash --expected-version $version '
                    '--source-report source-receipt/source-verification.json '
                    '--output-dir diagnostics/fingerprint\n'
                    'if ($LASTEXITCODE -ne 0) { throw "native ARM64 fingerprint acceptance failed" }'},
            {'name': 'Upload native verification diagnostics', 'if': '${{ always() }}',
             'uses': 'actions/upload-artifact@v4',
             'with': {'name': 'native-win-arm64-attempt-${{ github.run_attempt }}',
                      'path': 'diagnostics/', 'if-no-files-found': 'warn', 'retention-days': 7}},
        ],
    }
    return result


def render():
    return ('# Generated by tools/gen_windows_arm64_workflow.py; edit the generator or x64 template.\n'
            '# x64-host cross-build; success requires the separate native ARM64 verification job.\n'
            + yaml.safe_dump(workflow(), sort_keys=False, width=120))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    expected = render()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding='utf-8') != expected:
            parser.exit(1, 'Windows ARM64 workflow is stale; run tools/gen_windows_arm64_workflow.py\n')
    else:
        OUTPUT.write_text(expected, encoding='utf-8')


if __name__ == '__main__':
    main()
