#!/usr/bin/env python3
"""Generate the fixed Windows155.79 ARM64 stage12 continuation controller."""
from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path

import yaml
import gen_windows_arm64_workflow as arm
from validate_windows15579_arm64_stage12_checkpoint import SHA

NAME = 'build-win15579-arm64-stage12-resume'
OUTPUT = Path(__file__).resolve().parents[1] / '.github/workflows' / (NAME + '.yml')


def ps(name, run, **kwargs):
    return dict(name=name, shell='powershell', run="$ErrorActionPreference = 'Stop'\n" + run, **kwargs)


def workflow():
    source = arm.workflow()
    result = {key: deepcopy(value) for key, value in source.items() if key != 'jobs'}
    result.update(name=NAME, **{'run-name': 'Windows155.79 ARM64 fixed stage12 → stages13–24'})
    result['on'] = {'workflow_dispatch': {'inputs': {
        'compile_jobs': {'description': 'Ninja jobs (auto or positive integer)', 'type': 'string', 'default': 'auto'}}}}
    result['concurrency']['group'] = NAME + '-${{ github.ref }}'
    result['env'].update(CHROMIX_BUILD_PROFILE='native', CHROMIX_USE_UPSTREAM_CACHE='1')
    jobs = {}
    for index in range(1, 13):
        logical = index + 12
        job = deepcopy(source['jobs']['build-2'])
        job['name'] = f'stage {logical} (unchanged stage12 continuation)'
        job['defaults'] = {'run': {'working-directory': 'target'}}
        if index == 1:
            job.pop('needs')
            job.pop('if')
        else:
            previous = f'build-{index - 1}'
            job['needs'] = previous
            job['if'] = ("${{ always() && needs." + previous + ".result == 'success' && needs."
                         + previous + ".outputs.finished != 'true' }}")
        steps = [ps('Record snapshot producer attempt',
                    'Add-Content -LiteralPath $env:GITHUB_OUTPUT -Value "attempt=$env:GITHUB_RUN_ATTEMPT"\n'
                    'Add-Content -LiteralPath $env:GITHUB_ENV -Value "CHROMIX_RESUME_STARTED=$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"',
                    id='snapshot_origin', **{'working-directory': '${{ github.workspace }}'}),
                 {'uses': 'actions/checkout@v4', 'with': {'path': 'target', 'fetch-depth': 0,
                                                        'persist-credentials': False}},
                 {'name': 'Checkout exact donor as data', 'uses': 'actions/checkout@v4',
                  'with': {'ref': SHA, 'path': 'donor', 'fetch-depth': 0,
                           'persist-credentials': False, 'submodules': False}},
                 ps('Verify generated continuation controller',
                    'python -m pip install --disable-pip-version-check PyYAML==6.0.2\n'
                    'if ($LASTEXITCODE -ne 0) { throw "workflow validation dependency installation failed" }\n'
                    'python tools/gen_windows15579_arm64_stage12_workflow.py --check\n'
                    'if ($LASTEXITCODE -ne 0) { throw "stale stage12 continuation workflow" }'),
                 ps('Free host cache space before snapshot download',
                    "foreach ($path in @('C:\\Android', 'C:\\Program Files\\Android', 'C:\\Program Files (x86)\\Android', 'C:\\ghcup', 'C:\\Miniconda3', 'C:\\ProgramData\\chocolatey\\cache')) {\n"
                    '  if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue }\n}')]
        if index == 1:
            steps.append(deepcopy(next(step for step in source['jobs']['build-1']['steps']
                                       if step.get('name') == 'Verify native process-tree cleanup')))
            steps += [ps('Validate fixed stage12 producer and artifact metadata',
                         'python tools/validate_windows15579_arm64_stage12_checkpoint.py --metadata --report "$env:RUNNER_TEMP\\windows-snapshot.json"\n'
                         'if ($LASTEXITCODE -ne 0) { throw "fixed stage12 donor rejected" }',
                         env={'GH_TOKEN': '${{ secrets.UPSTREAM_ACTIONS_TOKEN || github.token }}'}),
                      ps('Download only digest-pinned stage12 volumes',
                         'python tools/download_windows_snapshot.py --manifest "$env:RUNNER_TEMP\\windows-snapshot.json" --destination C:\\restore --report "$env:RUNNER_TEMP\\windows-snapshot-download.json"\n'
                         'if ($LASTEXITCODE -ne 0) { throw "fixed stage12 download failed; no fallback" }',
                         env={'GH_TOKEN': '${{ secrets.UPSTREAM_ACTIONS_TOKEN || github.token }}'})]
        else:
            steps.append({'name': 'Download tree from exact previous producer attempt',
                          'uses': 'actions/download-artifact@v4', 'with': {
                              'pattern': f'win-arm64-tree-s{logical - 1}-attempt-${{{{ needs.build-{index - 1}.outputs.snapshot_attempt }}}}-part*',
                              'merge-multiple': True, 'path': 'C:\\restore'}})
        steps.append(ps('Select compile parallelism', 'python tools/build_resources.py --github-env\n'
                        'if ($LASTEXITCODE -ne 0) { throw "invalid compile parallelism" }'))
        steps.append(ps(f'Run stage {logical}',
                        '$elapsed = ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [long]$env:CHROMIX_RESUME_STARTED) / 60\n'
                        '$budget = [Math]::Min(300, [Math]::Floor(325 - $elapsed))\n'
                        'if ($budget -lt 60) { throw "insufficient restore/compile/snapshot budget" }\n'
                        f'build\\windows\\ci-stage.ps1 -StageIndex {logical} -MaxStages 24 -FromArtifact -UseUpstreamCache -Arch arm64 '
                        '-StageBudgetMinutes $budget -ResumeStage12DonorRepo "$env:GITHUB_WORKSPACE\\donor"', id='stage'))
        for original in source['jobs']['build-2']['steps'][5:]:
            step = deepcopy(original)
            if (step.get('name') == 'Ensure build tree snapshot'
                    or step.get('name', '').startswith('Upload tree part')):
                step['if'] = step['if'].replace(" && steps.stage.outputs.finished != 'true'", '')
            if step.get('name', '').startswith('Upload tree part'):
                step['with']['name'] = step['with']['name'].replace('tree-s2-', f'tree-s{logical}-')
            if step.get('uses') == 'actions/upload-artifact@v4':
                step['with']['retention-days'] = 7
            steps.append(step)
        steps.append({'name': 'Upload fixed restore proof', 'if': '${{ always() }}',
                      'uses': 'actions/upload-artifact@v4', 'with': {
                          'name': 'stage12-restore-proof-${{ github.job }}-attempt-${{ github.run_attempt }}',
                          'path': '${{ runner.temp }}/windows-snapshot*.json\n${{ runner.temp }}/stage12-archive.json',
                          'if-no-files-found': 'warn', 'retention-days': 7}})
        job['steps'] = steps
        jobs[f'build-{index}'] = job
    jobs['complete'] = deepcopy(source['jobs']['complete'])
    jobs['verify-arm64'] = deepcopy(source['jobs']['verify-arm64'])
    result['jobs'] = jobs
    return result


def render():
    return ('# Generated by tools/gen_windows15579_arm64_stage12_workflow.py.\n'
            '# Fixed donor only; twelve additional stages; native Windows11 ARM acceptance required.\n'
            + yaml.safe_dump(workflow(), sort_keys=False, width=120))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text() != render():
            parser.exit(1, 'stage12 continuation workflow is stale\n')
    else:
        OUTPUT.write_text(render())


if __name__ == '__main__':
    main()
