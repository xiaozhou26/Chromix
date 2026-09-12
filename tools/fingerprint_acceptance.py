#!/usr/bin/env python3
"""CI fingerprint regression gate on a pinned, explicitly supplied browser.

All probes run; failures, timeouts and missing reports fail the gate. Optional
capabilities stay incomplete, never hardware/route/font-file qualification.
Reports and logs belong in a separate diagnostic artifact, not the release ZIP.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from fingerprint_smoke import binary_identity, sha256_file
from fingerprint_subprocess import run_command
from verify_patch_stack import load_stack

REPO = Path(__file__).resolve().parents[1]
SUITES = (
    ('identity', 'fingerprint_smoke.py', ()),
    ('device', 'device_p0_audit.py', ()),
    ('canvas', 'canvas_chain_audit.py', ()),
    ('runtime', 'fingerprint_runtime_audit.py', ()),
    ('display_backend', 'fingerprint_runtime_audit.py', ('--persona-backend',)),
    ('transport', 'fingerprint_transport_audit.py', ()),
    ('render', 'fingerprint_render_audit.py', ()),
)


def read_json(path):
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('report exceeds 64 MiB')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result
    def constant(value):
        raise ValueError('nonfinite JSON constant: ' + value)
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique, parse_constant=constant)


def provenance(repo=REPO):
    identity, patches = load_stack(repo)
    paths = [repo / 'CHROMIUM_VERSION', repo / 'tools/fingerprint-requirements.txt',
             *sorted((repo / 'tools').glob('*.py')), *sorted((repo / 'tools').glob('*.js')),
             *sorted((repo / 'sdk/python/chromix').glob('*.py')),
             *sorted((repo / 'sdk/python/chromix').glob('*.js'))]
    result = {'patch_inputs': identity,
              'patch_targets': sorted({entry[0] for _, _, entries in patches for entry in entries}),
              'runner_files': {p.relative_to(repo).as_posix(): sha256_file(p) for p in paths}}
    try:
        result['commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo,
            timeout=10, text=True, encoding='utf-8').strip()
        result['dirty'] = bool(subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'],
            cwd=repo, timeout=10, text=True, encoding='utf-8').strip())
    except (OSError, subprocess.SubprocessError):
        result.update(commit=None, dirty=None)
    result['packages'] = {name: importlib.metadata.version(name)
                          for name in ('playwright', 'Pillow', 'cryptography', 'h2', 'psutil')}
    return result


def check_source(path, source_root, inputs, expected_targets=None):
    report = read_json(path)
    if (not isinstance(report, dict) or type(report.get('schema_version')) is not int or
            report.get('schema_version') != 1 or report.get('status') != 'verified' or
            report.get('method') != 'reverse-forward-in-scratch'):
        raise ValueError('a successful source patch-stack verification report is required')
    identity = report.get('identity', {})
    if not isinstance(identity, dict):
        raise ValueError('malformed source identity')
    series = identity.get('series', {})
    if not isinstance(series, dict):
        raise ValueError('malformed source series identity')
    patches = identity.get('patches', series.get('patches'))
    series_hash = identity.get('series_sha256', series.get('sha256'))
    if (patches != inputs['patches'] or series_hash != inputs['series_sha256'] or
            type(report.get('patch_count')) is not int or report.get('patch_count') != len(patches)):
        raise ValueError('source receipt was produced for a different patch stack')
    outputs = report.get('outputs')
    if not isinstance(outputs, dict) or not outputs:
        raise ValueError('source receipt contains no source output hashes')
    if expected_targets is None:
        _, current_patches = load_stack(REPO)
        expected_targets = {entry[0] for _, _, entries in current_patches for entry in entries}
    if set(outputs) != set(expected_targets):
        raise ValueError('source receipt does not cover exactly the current patch targets')
    from apply_restored_patches import _path
    for name, expected in outputs.items():
        if expected is not None and (not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected)):
            raise ValueError('invalid source output hash')
        if source_root:
            target = _path(source_root, name)
            if (sha256_file(target) if target.is_file() else None) != expected:
                raise ValueError('source changed since its freshness check: ' + name)
    return {'path': str(path.resolve()), 'sha256': sha256_file(path),
            'check': 'live-source-hashes' if source_root else 'producer-receipt-only',
            'qualification': 'local build provenance, not signed binary/source attestation'}


def optional_gaps(value, path=''):
    gaps = []
    if isinstance(value, dict):
        if value.get('status') in ('unavailable', 'not_supported', 'partial', 'incomplete'):
            gaps.append(path or 'report')
        for key, item in value.items():
            if key in ('unavailable', 'skipped', 'not_supported') and isinstance(item, list) and item:
                gaps.append((path + '.' + key).strip('.'))
            gaps.extend(optional_gaps(item, (path + '.' + key).strip('.')))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            gaps.extend(optional_gaps(item, f'{path}[{i}]'))
    return sorted(set(gaps))


def assess_suite(name, report, expected_hash, expected_version):
    """Revalidate raw observations; do not trust a saved top-level 'passed'."""
    errors = []
    if not isinstance(report, dict):
        return ['suite report must be an object'], []
    browser = report.get('browser', {})
    if not isinstance(browser, dict):
        return ['malformed browser identity'], []
    observed_hash = report.get('browser_sha256', browser.get('sha256'))
    if observed_hash != expected_hash:
        errors.append('suite used a different browser executable')
    version = report.get('browser_version')
    # The legacy identity matrix predates per-report version evidence. Its
    # executable hash is still pinned and the separate preflight checks version.
    if (version is not None or name != 'identity') and version != expected_version:
        errors.append('suite used a different browser version')
    if report.get('status') not in ('passed', 'incomplete') or report.get('errors') or report.get('failures'):
        errors.append('suite reported failure')
    try:
        if name == 'identity':
            import fingerprint_smoke as smoke
            scenarios = report['scenarios']
            expected = smoke.scenario_matrix(smoke.argument_parser().parse_args(['--browser', 'placeholder']))
            if [s['name'] for s in scenarios] != [s['name'] for s in expected]:
                errors.append('identity matrix did not complete')
            checks = smoke.evaluate_matrix(scenarios)
            for scenario in scenarios:
                checks.extend(smoke.evaluate_observation(scenario['observation'], scenario))
                if scenario.get('failures') or scenario.get('blocked_requests'):
                    errors.append('identity scenario failure or unexpected request')
            errors.extend(c['name'] for c in checks if c['status'] == 'failed')
        elif name == 'device':
            import device_p0_audit as device
            if [r['isolated'] for r in report['runs']] != [False, True]:
                errors.append('isolation matrix did not complete')
            for run in report['runs']:
                errors.extend(device.evaluate(run['observation'], run['isolated']))
                if len(run['font_sources']) != 20 or any(not f.get('platformFonts') for f in run['font_sources']):
                    errors.append('missing font source samples')
                if run['isolated'] and run.get('atomics') != {'worker': 7, 'parent': 7}:
                    errors.append('missing shared-memory roundtrip')
        elif name == 'canvas':
            import canvas_chain_audit as canvas
            if [r['profile'] for r in report['runs']] != ['a', 'a', 'b']:
                errors.append('canvas restart/profile matrix did not complete')
            for run in report['runs']:
                errors.extend(canvas.evaluate(run['observation'])['errors'])
            if len({canvas.signature(r['observation']) for r in report['runs']}) != 1:
                errors.append('canvas changed across restart/profiles')
        elif name in ('runtime', 'display_backend'):
            from fingerprint_runtime_audit import assess
            errors.extend(assess(report))
            expected_mode = 'launch-backend' if name == 'display_backend' else 'native-cdp'
            if report.get('configuration_mode') != expected_mode:
                errors.append('wrong display configuration mode')
        elif name == 'transport':
            from fingerprint_transport_audit import assess
            errors.extend(assess(report)[0])
        elif name == 'render':
            from fingerprint_render_audit import assess
            if len(report['runs']) != 2:
                errors.append('render page matrix did not complete')
            for run in report['runs']:
                errors.extend(assess(run)[0])
        else:
            errors.append('unknown suite')
    except (KeyError, TypeError, ValueError, AttributeError, IndexError) as error:
        errors.append('invalid or missing raw observation: ' + str(error))
    return errors, optional_gaps(report)


def run(args):
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report = {'schema_version': 1, 'collected_at': datetime.now(timezone.utc).isoformat(),
              'status': 'failed', 'ci_gate_passed': False, 'full_acceptance': False,
              'errors': [], 'gaps': [], 'suites': [], 'control': args.control,
              'qualification': {'physical_devices': 'not_attested', 'external_routes': 'not_tested',
                                'font_file_to_glyph_binding': 'not_tested', 'quic': 'not_tested'}}
    try:
        original = binary_identity(args.browser)
        report['browser'] = original
        if original['sha256'] != args.expected_sha256:
            raise ValueError('executable SHA-256 does not match the expected build')
        inputs = provenance()
        report['provenance'] = inputs
        if args.source_report:
            report['source'] = check_source(args.source_report, args.source_root,
                                            inputs['patch_inputs'], inputs['patch_targets'])
        elif not args.control:
            raise ValueError('--source-report is required for the CI gate (use --control only for probe calibration)')
        preflight_path = args.output_dir / 'browser.json'
        preflight = run_command([sys.executable, '-X', 'utf8', str(REPO / 'tools/fingerprint_browser_identity.py'),
            '--browser', original['path'], '--output', str(preflight_path)], timeout=60,
            log=args.output_dir / 'browser.log', cwd=REPO)
        report['preflight'] = preflight
        if preflight['timed_out'] or preflight['cleanup_errors'] or preflight['exit_code'] != 0:
            raise ValueError('browser identification failed or timed out')
        identified = read_json(preflight_path)
        if identified.get('version') != args.expected_version or identified.get('sha256') != original['sha256']:
            raise ValueError('running browser version/hash does not match the expected build')
        report['browser']['version'] = identified['version']
        for name, script, flags in SUITES:
            path = args.output_dir / (name + '.json')
            command = [sys.executable, '-X', 'utf8', str(REPO / 'tools' / script), '--browser', original['path'],
                       '--output', str(path), *flags]
            if name == 'identity':
                command += ['--expected-sha256', args.expected_sha256]
            failures, gaps = [], []
            result = {'command': command, 'timed_out': False, 'exit_code': None, 'cleanup_errors': []}
            try:
                with tempfile.TemporaryDirectory(prefix='chromix-audit-') as temporary:
                    environment = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8',
                                   'TEMP': temporary, 'TMP': temporary, 'TMPDIR': temporary}
                    result = run_command(command, timeout=args.suite_timeout, log=args.output_dir / (name + '.log'),
                                         cwd=REPO, env=environment)
            except Exception as error:
                # One launcher, report or cleanup failure must not hide the
                # diagnostics from the other independent suites.
                failures.append('suite execution failed: ' + type(error).__name__ + ': ' + str(error))
            result.update(name=name, report=path.name)
            if result['timed_out'] or result['cleanup_errors']:
                failures.append('suite timed out or descendant cleanup failed')
            try:
                observation = read_json(path)
                raw_errors, gaps = assess_suite(name, observation, args.expected_sha256, args.expected_version)
                failures.extend(raw_errors)
                incomplete_exit = (result['exit_code'] == 1 and isinstance(observation, dict) and
                                   observation.get('status') == 'incomplete' and bool(gaps) and not raw_errors)
                if result['exit_code'] != 0 and not incomplete_exit:
                    failures.append('suite exited nonzero')
                result['report_sha256'] = sha256_file(path)
            except (OSError, ValueError) as error:
                failures.append('missing/invalid suite report: ' + str(error))
            result.update(errors=failures, gaps=gaps)
            report['suites'].append(result)
            report['errors'].extend(f'{name}: {e}' for e in failures)
            report['gaps'].extend(f'{name}: {g}' for g in gaps)
            print(json.dumps({'suite': name, 'errors': len(failures), 'gaps': len(gaps)}), flush=True)
        if binary_identity(args.browser) != {k: original[k] for k in ('path', 'sha256', 'size')}:
            report['errors'].append('browser executable changed during acceptance')
        if provenance() != inputs:
            report['errors'].append('runner or patch inputs changed during acceptance')
        if args.source_report and check_source(args.source_report, args.source_root,
                inputs['patch_inputs'], inputs['patch_targets']) != report['source']:
            report['errors'].append('source receipt changed during acceptance')
    except Exception as error:
        report['errors'].append(type(error).__name__ + ': ' + str(error))
    report['ci_gate_passed'] = not args.control and not report['errors'] and len(report['suites']) == len(SUITES)
    report['status'] = 'failed' if report['errors'] else 'incomplete' if report['gaps'] else 'passed'
    with (args.output_dir / 'acceptance.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, ensure_ascii=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', type=Path, required=True)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--expected-version', default=(REPO / 'CHROMIUM_VERSION').read_text().strip())
    parser.add_argument('--source-report', type=Path)
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--suite-timeout', type=int, default=240)
    parser.add_argument('--control', action='store_true', help='calibration only; can never pass the CI build gate')
    args = parser.parse_args(argv)
    if (not re.fullmatch('[0-9a-fA-F]{64}', args.expected_sha256) or
            not re.fullmatch(r'[0-9]+(?:\.[0-9]+){3}', args.expected_version) or not 30 <= args.suite_timeout <= 1800):
        parser.error('supply an executable hash, full version and timeout in [30, 1800] seconds')
    if args.output_dir.exists() or (args.source_root and not args.source_report):
        parser.error('output directory must be new; source-root requires source-report')
    args.expected_sha256 = args.expected_sha256.lower()
    args.output_dir = args.output_dir.resolve()
    report = run(args)
    print(json.dumps({'status': report['status'], 'ci_gate_passed': report['ci_gate_passed'],
                      'errors': len(report['errors']), 'output': str(args.output_dir)}))
    return int(bool(report['errors']) if args.control else not report['ci_gate_passed'])


if __name__ == '__main__':
    raise SystemExit(main())
