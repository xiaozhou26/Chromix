import assert from 'node:assert/strict';
import test from 'node:test';
import { ensurePersonaGeometry } from '../_persona.js';
import { buildContextOptions } from '../index.js';

const vectors = [
  ['1', 1920, 1200, 48], ['42', 1680, 1050, 40], ['101', 1600, 900, 40],
  ['4294967295', 1920, 1080, 40], ['4294967297', 1920, 1080, 48],
  ['9223372036854775809', 1366, 768, 48], ['18446744073709551615', 1920, 1080, 48],
];
for (const [seed, width, height, taskbar] of vectors) {
  test(`full uint64 seed is deterministic: ${seed}`, () => {
    const args = [`--fingerprint=${seed}`];
    const first = ensurePersonaGeometry(args);
    assert.deepEqual(first, ensurePersonaGeometry(args));
    assert.deepEqual(first.geometry, ensurePersonaGeometry(first.args).geometry);
    assert.deepEqual([first.geometry.width, first.geometry.height, first.geometry.taskbar], [width, height, taskbar]);
    assert.deepEqual(args, [`--fingerprint=${seed}`]);
  });
}

for (const value of ['0', '-1', '+1', '1.5', '0x42', '1e3', ' 42', '42 ', 'NaN',
                      'Infinity', '18446744073709551616', '１']) {
  test(`reject malformed synthetic seed: ${JSON.stringify(value)}`, () => {
    assert.throws(() => ensurePersonaGeometry([`--fingerprint=${value}`]), /uint64/);
    assert.throws(() => ensurePersonaGeometry([`--fingerprint=${value}`], () => 0.1), /uint64/);
  });
}

const invalid = [
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
];
for (const args of invalid) {
  test(`reject inconsistent geometry: ${args.join(' ')}`, () => {
    assert.throws(() => ensurePersonaGeometry(['--fingerprint=42', ...args]));
  });
}

test('aliases, zero taskbar and explicit viewport form one idempotent record', () => {
  const args = ['--fingerprint=42', '--fingerprint-screen-width=1920', '--fingerprint-screen-height=1080',
    '--fingerprint-taskbar-height=0', '--uxr-screen-avail-width=1800', '--uxr-screen-avail-height=1080',
    '--uxr-device-pixel-ratio=1', '--uxr-viewport-width=800', '--uxr-viewport-height=600'];
  const first = ensurePersonaGeometry(args);
  assert.equal(first.geometry.availWidth, 1800);
  assert.equal(first.geometry.taskbar, 0);
  assert.deepEqual([first.geometry.viewportWidth, first.geometry.viewportHeight], [800, 600]);
  assert.deepEqual(ensurePersonaGeometry(first.args).args, first.args);
  assert.ok(!first.switches.some(a => a.startsWith('--uxr-screen-width=')));
});

for (const dpr of [0.25, 1, 1.25, 8]) {
  test(`context sends screen/viewport and explicit DPR ${dpr} together`, () => {
    const options = {args: ['--uxr-synthetic-device-tests=true', '--fingerprint=42',
      `--uxr-device-pixel-ratio=${dpr}`, '--uxr-viewport-width=800', '--uxr-viewport-height=600']};
    const value = buildContextOptions(options);
    assert.deepEqual(value.viewport, {width: 800, height: 600});
    assert.deepEqual(value.screen, {width: 1680, height: 1050});
    assert.equal(value.deviceScaleFactor, dpr);
    assert.equal(buildContextOptions({...options, viewport: null}).screen, undefined);
    assert.equal(buildContextOptions({...options, headless: false}).deviceScaleFactor, undefined);
  });
}

test('native geometry remains default; explicit context geometry wins', () => {
  assert.equal(buildContextOptions({args: ['--fingerprint=42']}).viewport, null);
  const value = buildContextOptions({args: ['--uxr-synthetic-device-tests=true', '--fingerprint=42'],
    contextOptions: {viewport: {width: 640, height: 480}, screen: {width: 1024, height: 768}, deviceScaleFactor: 2}});
  assert.deepEqual(value.viewport, {width: 640, height: 480});
  assert.deepEqual(value.screen, {width: 1024, height: 768});
  assert.equal(value.deviceScaleFactor, 2);
});
