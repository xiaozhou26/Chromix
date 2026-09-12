#!/usr/bin/env python3
"""Local browser capability audit; no proxy/wire or physical identity claims."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sdk/python'))
from chromix import _device_launch as launch
from chromix._device_headers import header_errors

ATOMIC = launch.bounded('''async () => {
  const memory = new SharedArrayBuffer(4), worker = new Worker('/atomic.js');
  try {
    const result = await new Promise((resolve,reject)=>{
      worker.onmessage=e=>resolve(e.data);worker.onerror=e=>reject(Error(e.message));
      worker.postMessage(memory);
    });
    return {worker:result, parent:Atomics.load(new Int32Array(memory),0)};
  } finally { worker.terminate(); }
}''')

FONT_NODES = '''() => {
  const container=document.createElement('div');container.id='p0-fonts';
  const texts=['Aa09','\\u4e2d\\u6587','\\u{1f600}','\\u2211','\\u0378'];
  for(const family of ['serif','sans-serif','monospace','system-ui']) {
    for(const text of texts){
      const span=document.createElement('span');span.textContent=text;
      span.style.font=`16px ${family}`;span.style.display='block';
      container.appendChild(span);
    }
  }
  document.body.appendChild(container);
  return Array.from(container.children).map(e=>({text:e.textContent,family:e.style.fontFamily}));
}'''


def font_sources(context, page):
    samples = page.evaluate(FONT_NODES)
    session = context.new_cdp_session(page)
    try:
        session.send('DOM.enable')
        session.send('CSS.enable')
        root = session.send('DOM.getDocument')['root']['nodeId']
        nodes = session.send('DOM.querySelectorAll', {'nodeId':root, 'selector':'#p0-fonts > span'})['nodeIds']
        for sample, node in zip(samples, nodes):
            sample['platformFonts'] = session.send('CSS.getPlatformFontsForNode', {'nodeId':node})['fonts']
        if len(nodes) != len(samples):
            raise ValueError('font source node count differs')
        return samples
    finally:
        session.detach()
        page.evaluate("document.querySelector('#p0-fonts')?.remove()")


def evaluate(observation, isolated):
    errors = launch.pool.observation_errors(observation)
    for scope in launch.pool.SCOPES:
        value = observation.get(scope, {})
        if value.get('probeVersion') != 2:
            errors.append(f'{scope}: requires probe v2')
        errors.extend(f'{scope}: {error}' for error in header_errors(value,
                      require_hints=scope in ('window', 'iframe')))
        execution = value.get('execution', {})
        if scope in ('window', 'iframe', 'worker') and (
                execution.get('isolated') is not isolated or execution.get('sab') is not isolated):
            errors.append(f'{scope}: isolation/SAB mismatch')
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--headed', action='store_true')
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('output must not already exist')
    report = {'browser_sha256':launch.pool.file_hash(args.browser),
              'collected_at':datetime.now(timezone.utc).isoformat(), 'runs':[],
              'errors':[], 'wire':'not_collected', 'physical_gpu_equivalence':'not_verified',
              'font_file_binding':'not_verified'}
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=str(args.browser.resolve()),
                headless=not args.headed, args=launch.NATIVE_ARGS, chromium_sandbox=True)
            try:
                report['browser_version'] = browser.version
                report['probe_sha256'] = launch.pool.file_hash(launch.PROBE)
                for isolated in (False, True):
                    context = browser.new_context(no_viewport=True)
                    try:
                        with launch.probe_server(isolated=isolated) as origin:
                            observation = launch.collect_live(context, origin)
                            errors = evaluate(observation, isolated)
                            page = context.new_page()
                            try:
                                page.goto(origin)
                                fonts = font_sources(context, page)
                                if any(not sample['platformFonts'] for sample in fonts):
                                    errors.append('font sample has no platform font evidence')
                                atomics = page.evaluate(ATOMIC) if isolated else None
                                if isolated and atomics != {'worker':7, 'parent':7}:
                                    errors.append('shared worker memory did not round-trip')
                            finally:
                                page.close()
                            report['runs'].append({'isolated':isolated, 'observation':observation,
                                                  'font_sources':fonts, 'atomics':atomics, 'errors':errors})
                            report['errors'].extend(errors)
                    finally:
                        context.close()
            finally:
                browser.close()
        if launch.pool.file_hash(args.browser) != report['browser_sha256']:
            report['errors'].append('browser executable changed')
    except Exception as error:
        report['errors'].append(str(error))
    report['status'] = 'failed' if report['errors'] else 'passed'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, ensure_ascii=True)
    print(json.dumps({'status':report['status'], 'errors':report['errors'], 'output':str(args.output)}))
    return int(bool(report['errors']))


if __name__ == '__main__':
    raise SystemExit(main())
