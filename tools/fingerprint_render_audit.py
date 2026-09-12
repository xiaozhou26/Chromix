#!/usr/bin/env python3
"""Extended bitmap/export/ownership/GPU integration audit on an explicit browser."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import math
from fingerprint_runtime_audit import launch, server

PROBE = Path(__file__).with_name('fingerprint_render_probe.js')


def _assess(observation):
    if not isinstance(observation, dict) or observation.get('schema_version') != 1:
        return ['missing render observation'], []
    errors = list(observation.get('errors', []))
    unavailable = list(observation.get('unavailable', []))
    seen = observation.get('observed', {})
    for name in ('bitmap', 'exports', 'workerOwnership', 'webgl', 'webgl2', 'webgpu'):
        if name not in seen:
            errors.append('missing render path: ' + name)
    if seen.get('bitmap') != dict.fromkeys(('crop', 'resize', 'flip', 'bitmaprenderer', 'ownership'), True):
        errors.append('bitmap operation/ownership evidence missing')
    if seen.get('workerOwnership') != {'detached': True, 'pixels': True}:
        errors.append('worker transfer evidence missing')
    if seen.get('exports') != [{'kind': kind, 'snapshots': 3, 'sourceMutation': True} for kind in ('html', 'offscreen')]:
        errors.append('concurrent export snapshot evidence missing')
    for api in ('webgl', 'webgl2'):
        value = seen.get(api, {})
        if value.get('status') in ('unavailable', 'partial'):
            unavailable.append(api)
            continue
        if value.get('status') != 'observed' or value.get('contextRestored') is not True:
            errors.append(api + ': context restoration unverified')
        for phase in ('before', 'after'):
            pixels = value.get(phase, [])
            if len(pixels) != 16 or any(type(v) is not int or abs(v - (64,128,191,255)[i % 4]) > 1 for i, v in enumerate(pixels)):
                errors.append(api + ': native clear/readback pixels disagree')
    gpu = seen.get('webgpu', {})
    if gpu.get('status') == 'unavailable':
        unavailable.append('webgpu')
    else:
        limits, boundaries = gpu.get('limits', {}), gpu.get('boundaries', [])
        features, enabled = gpu.get('features'), gpu.get('enabledFeatures')
        if (gpu.get('status') != 'observed' or not isinstance(features, list) or not isinstance(enabled, list) or
                not set(features) <= set(enabled) or not limits or not boundaries):
            errors.append('WebGPU advertised feature/limit requests not verified')
        if len(boundaries) != len({b['name'] for b in boundaries}) or {b['name'] for b in boundaries} != set(limits):
            errors.append('WebGPU boundary matrix incomplete')
        for b in boundaries:
            value, invalid, accepted = b['advertised'], b['requested'], b.get('accepted')
            if (limits.get(b['name']) != value or type(value) is not int or
                    type(accepted) not in (int, float) or not math.isfinite(accepted) or b.get('rejected') is not True or
                    (not invalid < value or not accepted <= value if b['name'].startswith('min')
                     else not invalid > value or not accepted >= value)):
                errors.append('WebGPU valid/invalid boundary mismatch: ' + b['name'])
    return errors, unavailable


def assess(observation):
    try:
        return _assess(observation)
    except (TypeError, ValueError, KeyError, AttributeError, IndexError) as error:
        return ['malformed render evidence: ' + str(error)], []


def run(browser, headed=False):
    report = {'schema_version': 1, 'browser_sha256': launch.pool.file_hash(browser),
              'probe_sha256': launch.pool.file_hash(PROBE), 'runs': [], 'errors': [], 'unavailable': [],
              'qualification': 'native API integration; not physical GPU or profile-distinct rendering attestation'}
    try:
        from playwright.sync_api import sync_playwright
        with server() as origin, sync_playwright() as pw:
            instance = pw.chromium.launch(executable_path=str(browser.resolve()), headless=not headed,
                args=launch.NATIVE_ARGS, chromium_sandbox=True)
            try:
                report['browser_version'] = instance.version
                context = instance.new_context(no_viewport=True)
                context.add_init_script(path=str(PROBE))
                for _ in range(2):
                    page = context.new_page()
                    try:
                        page.goto(origin)
                        observation = page.evaluate('''async () => {
                          let timer;
                          try { return await Promise.race([chromixRenderProbe(), new Promise((_, reject) => {
                            timer = setTimeout(() => reject(Error('render suite timed out')), 120000);
                          })]); } finally { clearTimeout(timer); }
                        }''')
                        errors, unavailable = assess(observation)
                        report['runs'].append(observation)
                        report['errors'].extend(errors); report['unavailable'].extend(unavailable)
                    finally:
                        page.close()
                context.close()
            finally:
                instance.close()
    except Exception as error:
        report['errors'].append(str(error))
    if launch.pool.file_hash(browser) != report['browser_sha256']:
        report['errors'].append('browser executable changed')
    if len(report['runs']) != 2:
        report['errors'].append('not all render launches completed')
    report['status'] = 'failed' if report['errors'] else 'incomplete' if report['unavailable'] else 'passed'
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--headed', action='store_true')
    args = parser.parse_args(argv)
    if not args.browser.is_file() or args.output.exists():
        parser.error('use an existing executable and a new report path')
    report = run(args.browser, args.headed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, ensure_ascii=True)
    print(json.dumps({'status': report['status'], 'errors': report['errors'], 'unavailable': report['unavailable']}))
    return int(report['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
