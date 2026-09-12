"""Synthetic geometry validation and real Python/Node algorithm parity."""
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chromix._persona import ensure_persona_geometry
from chromix import api

VECTORS = [
    ('1', 1920, 1200, 48), ('42', 1680, 1050, 40), ('101', 1600, 900, 40),
    ('4294967295', 1920, 1080, 40), ('4294967297', 1920, 1080, 48),
    ('9223372036854775809', 1366, 768, 48), ('18446744073709551615', 1920, 1080, 48),
]
BAD_SEEDS = ['0', '-1', '+1', '1.5', '0x42', '1e3', ' 42', '42 ', 'NaN',
             'Infinity', '18446744073709551616', '１']
INVALID = [
    ['--uxr-screen-width=0'], ['--uxr-screen-width=32769'], ['--uxr-screen-width=10.5'],
    ['--uxr-screen-width=1e3'], ['--uxr-screen-height=NaN'], ['--uxr-screen-width= 800'],
    ['--uxr-device-pixel-ratio=0.24'], ['--uxr-device-pixel-ratio=8.1'], ['--uxr-device-pixel-ratio=Infinity'],
    ['--uxr-device-pixel-ratio=0x2'], ['--uxr-viewport-width=800'], ['--uxr-viewport-height=600'],
    ['--uxr-screen-width=800', '--uxr-screen-avail-width=801'],
    ['--uxr-screen-height=600', '--uxr-screen-avail-height=601'],
    ['--uxr-screen-height=600', '--uxr-taskbar-height=601'],
    ['--uxr-screen-height=600', '--uxr-taskbar-height=40', '--uxr-screen-avail-height=570'],
    ['--uxr-screen-width=800', '--fingerprint-screen-width=801'],
    ['--uxr-taskbar-height=40', '--fingerprint-taskbar-height=48'],
    ['--window-size=800,600', '--uxr-outer-width=900'], ['--window-size=800,600,700'],
    ['--window-size=800, 600'], ['--window-size=32769,600'], ['--uxr-outer-height=85'],
]
EXPLICIT = ['--fingerprint=42', '--fingerprint-screen-width=1920', '--fingerprint-screen-height=1080',
            '--fingerprint-taskbar-height=0', '--uxr-screen-avail-width=1800', '--uxr-screen-avail-height=1080',
            '--uxr-device-pixel-ratio=1', '--uxr-viewport-width=800', '--uxr-viewport-height=600']


@pytest.mark.parametrize('seed,width,height,taskbar', VECTORS)
def test_full_uint64_determinism(seed, width, height, taskbar):
    original = [f'--fingerprint={seed}']
    args, geometry = ensure_persona_geometry(original)
    assert ensure_persona_geometry(original) == (args, geometry)
    assert ensure_persona_geometry(args) == (args, geometry)
    assert (geometry['width'], geometry['height'], geometry['taskbar']) == (width, height, taskbar)
    assert original == [f'--fingerprint={seed}']


@pytest.mark.parametrize('seed', BAD_SEEDS)
def test_invalid_seed_does_not_randomize(seed):
    for generator in (None, random.Random(42)):
        with pytest.raises(ValueError, match='uint64'):
            ensure_persona_geometry([f'--fingerprint={seed}'], generator)


@pytest.mark.parametrize('extra', INVALID)
def test_invalid_geometry(extra):
    with pytest.raises(ValueError):
        ensure_persona_geometry(['--fingerprint=42', *extra])


def test_alias_workarea_and_explicit_viewport():
    args, geometry = ensure_persona_geometry(EXPLICIT)
    assert geometry['taskbar'] == 0 and geometry['avail_width'] == 1800
    assert (geometry['viewport_width'], geometry['viewport_height']) == (800, 600)
    assert not any(a.startswith('--uxr-screen-width=') for a in args)
    assert ensure_persona_geometry(args) == (args, geometry)


@pytest.mark.parametrize('dpr', [0.25, 1, 1.25, 8])
def test_context_passes_screen_dpr_and_viewport_together(dpr):
    _, geometry = ensure_persona_geometry(['--fingerprint=42', f'--uxr-device-pixel-ratio={dpr}',
        '--uxr-viewport-width=800', '--uxr-viewport-height=600'])
    value = api._split_context_kwargs(api._VIEWPORT_UNSET, None, None, None, {}, geometry)
    assert value == {'viewport': {'width': 800, 'height': 600}, 'screen': {'width': 1680, 'height': 1050},
                     'device_scale_factor': dpr}
    native = api._split_context_kwargs(None, None, None, None, {}, geometry)
    assert native == {'viewport': None, 'no_viewport': True}
    explicit = {'screen': {'width': 1024, 'height': 768}, 'device_scale_factor': 2}
    value = api._split_context_kwargs({'width': 640, 'height': 480}, None, None, None, explicit, geometry)
    assert value == {**explicit, 'viewport': {'width': 640, 'height': 480}}


def test_node_python_parity_for_valid_and_invalid_records():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node executable unavailable for cross-SDK parity')
    module = Path(__file__).resolve().parents[2] / 'node/_persona.js'
    script = '''
import {readFileSync} from 'node:fs';
const {ensurePersonaGeometry} = await import(process.argv[1]);
console.log(JSON.stringify(JSON.parse(readFileSync(0, 'utf8')).map(args => {
  try {const result = ensurePersonaGeometry(args); return {args: result.args,
    geometry: Object.fromEntries(Object.entries(result.geometry).map(([k,v]) => [k.replace(/[A-Z]/g, c=>'_'+c.toLowerCase()),v]))};}
  catch {return {invalid:true};}
})));
'''
    cases = [[f'--fingerprint={seed}'] for seed, *_ in VECTORS] + [EXPLICIT]
    cases += [['--fingerprint=42', *extra] for extra in INVALID]
    cases += [[f'--fingerprint={seed}'] for seed in BAD_SEEDS]
    cases += [['--fingerprint=42', 'xxuxr-screen-width=900', '--uxr-window-x=-1920']]
    result = subprocess.run([node, '--input-type=module', '-e', script, module.as_uri()],
                            input=json.dumps(cases), capture_output=True, encoding='utf-8', timeout=20, check=True)
    python = []
    for args in cases:
        try:
            completed, geometry = ensure_persona_geometry(args)
            python.append({'args': completed, 'geometry': geometry})
        except ValueError:
            python.append({'invalid': True})
    assert json.loads(result.stdout) == python
