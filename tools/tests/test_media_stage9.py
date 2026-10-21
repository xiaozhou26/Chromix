"""Stage9 media validator/probe contracts, not Windows browser acceptance."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fingerprint_media_policy_audit as media

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / 'tools/fingerprint_backend_probe.js'
PATCH = ROOT / 'patches/0182-mediarecorder-codec-policy.patch'
EVIDENCE = json.loads((Path(__file__).parent / 'fixtures' / 'backend_completion_sources.json').read_text())
MIMES = {'h264': 'video/mp4;codecs=avc1.42001E', 'vp8': 'video/webm;codecs=vp8',
         'vp9': 'video/webm;codecs=vp9', 'av1': 'video/webm;codecs=av01.0.04M.08',
         'hevc': 'video/mp4;codecs=hvc1.1.6.L93.B0'}


def recording(family):
    mime = 'video/webm;codecs=av01' if family == 'av1' else MIMES[family]
    return {'status': 'recorded', 'data': 'eHg=', 'bytes': 2, 'requestedMime': MIMES[family],
            'mime': mime, 'mimeSupported': True, 'chunkMimes': [mime]}


def capability(kind, family, allowed):
    content_type = ('video/' + ('h265' if family == 'hevc' else family)
                    if kind == 'encoding' else MIMES[family])
    return {'status': 'resolved', 'configuration': {'type': 'webrtc' if kind == 'encoding' else 'file',
            'video': {'contentType': content_type, 'width': 64, 'height': 64,
                      'bitrate': 200000, 'framerate': 30}},
            **dict.fromkeys(('supported', 'smooth', 'powerEfficient'), allowed)}


def stage9_report():
    report = {'schema_version': 1, 'status': 'passed', 'errors': [], 'runs': [],
              'browser_sha256': 'a' * 64, 'browser_version': '152.0.7977.82',
              'probe_sha256': media.launch.pool.file_hash(media.PROBE)}
    for name, disabled in media.CASES.items():
        families = {}
        for family, mime in MIMES.items():
            allowed = family not in disabled
            rejection = {'status': 'rejected', 'error': 'NotSupportedError:codec disabled'}
            dimensions = {'width': 64, 'height': 64}
            playback = {'status': 'decoded', **dimensions} if allowed else rejection
            recorded = recording(family)
            families[family] = {
                **dict.fromkeys(media.API_KEYS, allowed), 'rtcSend': int(allowed),
                'rtcReceive': int(allowed), 'canPlay': 'probably' if allowed else '', 'mime': mime,
                **{key: capability(key, family, allowed) for key in ('encoding', 'decoding')},
                'encoded': {'status': 'encoded', 'config': {'codec': family},
                            'chunks': [{'data': 'eHg=', 'type': 'key', 'timestamp': 0}]} if allowed else rejection,
                'recorded': recorded if allowed else rejection,
                'decoded': {'status': 'decoded', 'frames': [dimensions]} if allowed else rejection,
                'playback': {**playback, 'mime': recorded['mime'], 'recorderMime': recorded['mime']},
                'mse': {**playback, 'mime': mime, 'recorderMime': recorded['mime'], 'mimeSupported': allowed},
            }
        default = ({**recording('vp8'), 'playback': {'status': 'decoded', 'width': 64, 'height': 64}}
                   if name != 'disabled' else {'status': 'rejected', 'error': 'NotSupportedError'})
        rtc = ({'status': 'decoded', 'frames': 3, 'bytes': 456, 'codec': 'video/VP8', 'errors': [],
                'formats': ['vp8', 'rtx', 'red', 'ulpfec']}
               if name != 'disabled' else {'status': 'offered', 'formats': []})
        observation = {'families': families,
                       'apis': dict.fromkeys(('encoder', 'decoder', 'recorder', 'mse'), True),
                       'defaultRecorder': default, 'rtc': rtc,
                       'audio': {'status': 'recorded', 'bytes': 123, 'mime': 'audio/webm;codecs=opus'}}
        report['runs'].append({'name': name, 'launch_args': media.case_args(name), 'observation': observation})
    return report


def test_valid_rtp_queries_and_full_mse_mime_keep_bare_recorder_mime_independent():
    assert media.assess(stage9_report()) == ([], [])


@pytest.mark.parametrize('mutation,expected', [
    (lambda row: row['encoding']['configuration'].update(type='record'), 'RTP MIME'),
    (lambda row: row['encoding']['configuration']['video'].update(contentType=MIMES['av1']), 'RTP MIME'),
    (lambda row: row['mse'].update(mime='video/webm;codecs=av01'), 'MSE did not use'),
    (lambda row: row['recorded'].update(mimeSupported=False), 'serialized MIME'),
    (lambda row: row['recorded'].update(chunkMimes=[';codecs=av01']), 'chunk MIME'),
    (lambda row: row['playback'].update(mime=MIMES['av1']), 'playback replaced'),
])
def test_mime_and_configuration_mismatches_cannot_pass(mutation, expected):
    report = stage9_report()
    mutation(report['runs'][0]['observation']['families']['av1'])
    assert any(expected in error for error in media.assess(report)[0])


def test_disabled_mse_rejection_must_use_the_verified_native_mime():
    report = stage9_report()
    report['runs'][1]['observation']['families']['av1']['mse']['mime'] = 'video/webm;codecs=av01'
    assert any('MSE did not use' in error for error in media.assess(report)[0])


def test_rejection_details_do_not_hide_other_codec_failures():
    report = stage9_report()
    row = report['runs'][1]['observation']['families']['vp8']
    row['encoding'] = {'status': 'rejected', 'error': {'name': 'TypeError', 'message': 'bad configuration'}}
    row['recorded'] = recording('vp8')
    errors, _ = media.assess(report)
    assert any('TypeError' in error and 'bad configuration' in error for error in errors)
    assert any('disabled recorded operation was not rejected' in error for error in errors)


def test_empty_default_container_fails_even_if_playback_decodes():
    report = stage9_report()
    report['runs'][2]['observation']['defaultRecorder']['mime'] = ';codecs=vp8'
    assert any('invalid recording payload or MIME type' in error for error in media.assess(report)[0])


@pytest.mark.parametrize('changes', [{'status': 'failed'}, {'frames': 0}, {'bytes': 0},
                                     {'codec': 'video/H264'}, {'errors': ['AbortError']}])
def test_diagnostics_never_replace_required_rtc_transfer_and_decode(changes):
    report = stage9_report()
    rtc = report['runs'][2]['observation']['rtc']
    rtc.update(changes, diagnostics={'cleanupErrors': [{'name': 'AbortError', 'phase': 'cleanup'}]})
    assert any('allowed RTC codec did not transfer and decode video' in error for error in media.assess(report)[0])


def test_media_launch_explicitly_enables_host_ice_without_changing_codec_restrictions():
    for name, disabled in media.CASES.items():
        assert media.case_args(name) == [
            *media.BASE_ARGS, '--fingerprint-audio-render=native',
            '--webrtc-ip-handling-policy=default_public_and_private_interfaces',
            *['--fingerprint-codec-' + family + '=disabled' for family in disabled],
        ]
    assert not any('webrtc-ip-handling-policy' in arg for arg in media.BASE_ARGS)


@pytest.mark.parametrize('replacement', [None, '--force-webrtc-ip-handling-policy=default',
                                        '--webrtc-ip-handling-policy=disable_non_proxied_udp'])
def test_missing_or_wrong_chrome_ice_policy_cannot_pass(replacement):
    report = stage9_report()
    args = report['runs'][0]['launch_args']
    args.remove('--webrtc-ip-handling-policy=default_public_and_private_interfaces')
    if replacement:
        args.append(replacement)
    assert any('unexpected codec launch arguments' in error for error in media.assess(report)[0])


@pytest.mark.parametrize('blocked', [False, True])
def test_local_chrome152_rtc_policy_control(tmp_path, blocked):
    executable = os.environ.get('CHROMIX_MEDIA_RTC_BROWSER')
    if not executable:
        pytest.skip('optional local Chrome 152 RTC control; not Windows acceptance')
    from playwright.sync_api import sync_playwright

    script = PROBE.read_text().replace('chromixBackendProbe.codecs = codecs;',
        'chromixBackendProbe.codecs = codecs; chromixBackendProbe.rtc = rtc;')
    args = media.case_args('native')
    if blocked:
        args[args.index('--webrtc-ip-handling-policy=default_public_and_private_interfaces')] = (
            '--webrtc-ip-handling-policy=disable_non_proxied_udp')
    with media.server() as origin, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=executable, headless=True, args=args)
        try:
            page = browser.new_page(no_viewport=True)
            page.goto(origin)
            page.evaluate(script)
            result = page.evaluate('async () => await chromixBackendProbe.bounded('
                                   'chromixBackendProbe.rtc(false), 20000)')
            (tmp_path / 'rtc.json').write_text(json.dumps({
                'qualification': 'local unpatched Chrome harness control; not Windows acceptance',
                'version': browser.version, 'launch_args': args, 'result': result}, indent=2))
            assert browser.version == '152.0.7977.82'
        finally:
            browser.close()
    diagnostics = result['diagnostics']
    before = diagnostics['beforeCleanup']
    assert result['errors'] == []
    assert all(before[peer]['signaling'] == 'stable' for peer in ('send', 'receive'))
    assert before['source'][0]['enabled'] and before['source'][0]['readyState'] == 'live'
    assert diagnostics['drawRequests'] > 0
    assert any(event['event'] == 'track' for event in diagnostics['events'])
    final = diagnostics['stats'][-1]
    if blocked:
        assert result['status'] == 'failed'
        assert diagnostics['candidates'] and all(row['candidate'] is None for row in diagnostics['candidates'])
        assert all(before[peer]['iceGathering'] == 'complete' for peer in ('send', 'receive'))
        assert all(before[peer]['iceConnection'] == 'new' for peer in ('send', 'receive'))
        assert any(row['type'] == 'media-source' and row.get('frames', 0) > 0 for row in final['send'])
        outbound = [row for row in final['send'] if row['type'] == 'outbound-rtp']
        assert outbound and all(row['bytesSent'] == 0 for row in outbound)
        assert not any(row['type'] == 'inbound-rtp' for row in final['receive'])
        assert before['play']['status'] == 'pending'
        assert before['remote'][0]['muted']
        assert diagnostics['cleanupErrors'][0]['name'] == 'AbortError'
        assert diagnostics['cleanupErrors'][0]['phase'] == 'cleanup'
    else:
        assert result['status'] == 'decoded' and result['frames'] > 0 and result['bytes'] > 0
        assert result['codec'].lower() == 'video/vp8'
        assert all(before[peer]['connection'] == 'connected' for peer in ('send', 'receive'))
        for peer in ('send', 'receive'):
            transport = next(row for row in final[peer] if row['type'] == 'transport')
            selected = next(row for row in final[peer] if row['id'] == transport['selectedCandidatePairId'])
            assert transport['dtlsState'] == 'connected'
            assert transport['bytesSent'] > 0 and transport['bytesReceived'] > 0
            assert selected['type'] == 'candidate-pair' and selected['state'] == 'succeeded'
        assert before['play']['status'] == 'resolved'
        assert diagnostics['cleanupErrors'] == []


def apply_patch(directory, reverse=False):
    command = shutil.which('gpatch') or shutil.which('patch')
    if not command:
        pytest.skip('GNU patch is required')
    result = subprocess.run([command, '-p1', '--fuzz=0', '--batch', '--binary', '--get=0',
                             '--reject-file=-', '--reverse' if reverse else '--forward', '-i', str(PATCH)],
                            cwd=directory, capture_output=True, text=True,
                            env={**os.environ, 'LC_ALL': 'C', 'PATCH_GET': '0'}, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any(token in (result.stdout + result.stderr).lower() for token in ('fuzz', 'failed', 'offset'))


def test_0182_strict_pinned_source_roundtrip(tmp_path):
    assert hashlib.sha256(PATCH.read_bytes()).hexdigest() == EVIDENCE['patches']['0182']['patch_sha256']
    lines = []
    for section in EVIDENCE['patches']['0182']['sections']:
        first, text = section['line'], section['text']
        lines.extend('// unrelated pinned source line\n' for _ in range(first - 1 - len(lines)))
        lines.extend(text.splitlines(True))
    target = tmp_path / EVIDENCE['patches']['0182']['target']
    target.parent.mkdir(parents=True)
    original = (''.join(lines) + '// not EOF\n').encode()
    target.write_bytes(original)
    apply_patch(tmp_path)
    assert target.read_bytes() != original
    apply_patch(tmp_path, reverse=True)
    assert target.read_bytes() == original


def test_0182_full_chromium152_preimage_roundtrip(tmp_path):
    root = os.environ.get('CHROMIX_MEDIA_UPSTREAM_ROOT')
    if not root:
        pytest.skip('exact Chromium 152 upstream source root required')
    receipt = EVIDENCE['patches']['0182']
    original = (Path(root) / receipt['target']).read_bytes()
    assert hashlib.sha256(original).hexdigest() == receipt['preimage_sha256']
    target = tmp_path / receipt['target']
    target.parent.mkdir(parents=True)
    target.write_bytes(original)
    apply_patch(tmp_path)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == receipt['output_sha256']
    apply_patch(tmp_path, reverse=True)
    assert target.read_bytes() == original
    assert (Path(root) / receipt['target']).read_bytes() == original


def test_default_recorder_selects_container_without_reenabling_passthrough(tmp_path):
    compiler = shutil.which('clang++') or shutil.which('g++')
    if not compiler:
        pytest.skip('C++ compiler required')
    additions = '\n'.join(line[1:] for line in PATCH.read_text().splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
    start = additions.index('  passthrough_enabled_ =')
    selection = additions[start:additions.index('\n\n', start)]
    code = r'''
#include <cassert>
#include <string>
namespace media { bool restricted; bool HasUxrVideoCodecRestrictions() {return restricted;} }
struct Stream {bool video; int NumberOfVideoComponents() {return video;}};
int main() {
  for (bool restricted : {false, true}) for (bool video : {false, true}) {
    for (std::string initial : {"", "video/mp4", "audio/webm", "video/webm"}) {
      media::restricted = restricted;
      Stream stream{video}; auto* media_stream = &stream;
      std::string type_ = initial; bool passthrough_enabled_ = false;
''' + selection + r'''
      assert(passthrough_enabled_ == (initial.empty() && !restricted));
      assert(type_ == (initial.empty() && restricted ? (video ? "video/webm" : "audio/webm") : initial));
    }
  }
}
'''
    source = tmp_path / 'container.cc'
    source.write_text(code)
    binary = tmp_path / 'container'
    subprocess.run([compiler, '-std=c++20', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(binary)],
                   check=True, capture_output=True, timeout=30)
    subprocess.run([str(binary)], check=True, timeout=10)


@pytest.fixture(scope='module')
def node():
    executable = os.environ.get('CHROMIX_NODE') or shutil.which('node')
    if not executable:
        pytest.skip('Node required for executable probe contracts')
    subprocess.run([executable, '--check', str(PROBE)], check=True, capture_output=True, timeout=10)
    return executable


def run_js(node, body):
    bootstrap = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
globalThis.chromixBackendProbe = {bounded:promise => promise, delay:async () => {}};
const media = source.slice(source.indexOf("(() => {\n  'use strict';\n  const {bounded, delay}"));
vm.runInThisContext(media.replace('chromixBackendProbe.codecs = codecs;',
  'globalThis.testMedia = {specs, capability, play, record, rtc};'));
(async () => {
'''
    result = subprocess.run([node, '-e', bootstrap + body + '\n})().catch(e => {console.error(e);process.exit(1)});',
                             str(PROBE)], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_capability_js_keeps_rejection_details(node):
    source = PROBE.read_text()
    assert "capability('encodingInfo', {type:'webrtc'," in source
    assert 'contentType:spec.rtpMime' in source
    assert 'MediaSource.isTypeSupported(spec.mime)' in source
    assert 'play(recording, spec.mime)' in source
    run_js(node, r'''
Object.defineProperty(globalThis, 'navigator', {value:{mediaCapabilities:{
  encodingInfo:async configuration => {
    assert.equal(configuration.type, 'webrtc');
    assert.match(configuration.video.contentType, /^video\/(VP8|VP9|AV1|H264|H265)$/);
    return {supported:true, smooth:true, powerEfficient:false};
  }
}}});
for (const spec of Object.values(testMedia.specs)) {
  const configuration = {type:'webrtc', video:{contentType:spec.rtpMime}};
  const result = await testMedia.capability('encodingInfo', configuration);
  assert.equal(result.status, 'resolved'); assert.equal(result.supported, true);
  assert.deepEqual(result.configuration, configuration);
}
navigator.mediaCapabilities.encodingInfo = async () => {throw new TypeError('specific bad configuration')};
const result = await testMedia.capability('encodingInfo', {type:'record'});
assert.equal(result.status, 'rejected');
assert.deepEqual(result.error, {name:'TypeError', message:'specific bad configuration'});
assert.equal(result.supported, undefined);
''')


def test_mse_js_uses_verified_mime_without_rewriting_recorder_mime(node):
    run_js(node, r'''
let sourceBufferMime, blobMime, video, mediaSource;
globalThis.document = {createElement:() => video = {load(){}, removeAttribute(){}}};
globalThis.MediaSource = class {
  constructor() {mediaSource = this;}
  static isTypeSupported(mime) {return mime === testMedia.specs.av1.mime;}
  addEventListener(name, callback) {queueMicrotask(callback);}
  addSourceBuffer(mime) {
    sourceBufferMime = mime;
    assert.equal(mime, testMedia.specs.av1.mime);
    return {addEventListener(name, callback) {if (name === 'updateend') this.done = callback;},
      appendBuffer() {this.done();}};
  }
  endOfStream() {video.videoWidth = video.videoHeight = 64; video.onloadeddata();}
};
globalThis.URL = {createObjectURL(value) {
  if (value instanceof Blob) {blobMime = value.type; queueMicrotask(() => video.onloadeddata());}
  return 'blob:owned';
}, revokeObjectURL(){}};
const fixture = {status:'recorded', bytes:2, data:'eHg=', mime:'video/webm;codecs=av01'};
const result = await testMedia.play(fixture, testMedia.specs.av1.mime);
assert.equal(result.status, 'decoded'); assert.equal(result.mimeSupported, true);
assert.equal(sourceBufferMime, testMedia.specs.av1.mime);
assert.equal(result.recorderMime, fixture.mime);
await testMedia.play(fixture); assert.equal(blobMime, fixture.mime);
assert.equal(fixture.mime, 'video/webm;codecs=av01');
''')


def test_recorder_js_keeps_actual_default_mime_and_independent_queries(node):
    run_js(node, r'''
let options, stopped = false;
const queried = [], track = {requestFrame(){}, stop(){stopped=true}};
const stream = {getVideoTracks:() => [track],getTracks:() => [track]};
globalThis.document = {createElement:tag => tag === 'canvas' ?
  {getContext:() => ({fillRect(){}}),captureStream:() => stream} :
  {canPlayType:mime => {queried.push(['play',mime]);return '';}}};
globalThis.MediaSource = {isTypeSupported:mime => {queried.push(['mse',mime]);return false;}};
globalThis.MediaRecorder = class {
  constructor(input, settings) {assert.equal(input,stream);options=settings;this.mimeType=';codecs=vp8';}
  static isTypeSupported(mime) {queried.push(['recorder',mime]);return false;}
  start() {this.state='recording';}
  stop() {this.state='inactive';this.ondataavailable({data:new Blob(['xx'],{type:this.mimeType})});this.onstop();}
};
const result = await testMedia.record('');
assert.deepEqual(options, {});assert.equal(result.status,'recorded');
assert.equal(result.mime,';codecs=vp8');assert.equal(result.mimeSupported,false);
assert.deepEqual(result.chunkMimes,[';codecs=vp8']);
assert.deepEqual(queried,[['recorder',';codecs=vp8'],['play',';codecs=vp8'],['mse',';codecs=vp8']]);
assert.equal(result.bytes,2);assert.equal(stopped,true);
''')


@pytest.mark.parametrize('failure', ['construct', 'start', 'empty', 'success'])
def test_recorder_diagnostics_preserve_failure_phase_and_fixed_workload(node, failure):
    run_js(node, 'const failure = ' + json.dumps(failure) + ';\n' + r'''
let constructions = 0, draws = 0, stops = 0, cleaned = false;
const track = {kind:'video', enabled:true, muted:false, readyState:'live',
  getSettings:() => ({width:64,height:64}), requestFrame(){draws++}, stop(){cleaned=true}};
const stream = {getVideoTracks:() => [track], getTracks:() => [track]};
globalThis.document = {createElement:tag => tag === 'canvas' ?
  {getContext:() => ({fillRect(){}}), captureStream:fps => {assert.equal(fps,30);return stream}} :
  {canPlayType:() => 'probably'}};
globalThis.MediaSource = {isTypeSupported:() => true};
globalThis.MediaRecorder = class {
  constructor(input, options) {
    constructions++; assert.equal(input,stream); assert.equal(options.mimeType,testMedia.specs.hevc.mime);
    if (failure === 'construct') throw new DOMException('native initialize rejected','NotSupportedError');
    this.state='inactive';this.mimeType=options.mimeType;
  }
  static isTypeSupported() {return true}
  start() {
    if (failure === 'start') throw new DOMException('encoder start rejected','NotSupportedError');
    this.state='recording';this.onstart();
  }
  stop() {
    stops++;this.state='inactive';
    this.ondataavailable({data:new Blob([failure === 'empty' ? '' : 'xx'],{type:this.mimeType})});
    this.onstop();
  }
};
const result = await testMedia.record(testMedia.specs.hevc.mime);
assert.equal(constructions,1);assert.equal(cleaned,true);
assert.equal(result.requestedMime,testMedia.specs.hevc.mime);
assert.deepEqual(result.diagnostics.beforeCleanup.tracks[0].settings,{width:64,height:64});
if (failure === 'construct' || failure === 'start') {
  assert.equal(result.status,'rejected');assert.match(result.error,/^NotSupportedError:/);
  assert.equal(result.bytes,0);assert.equal(result.diagnostics.failure.phase,failure);
  assert.equal(draws,0);assert.equal(stops,0);
} else {
  assert.equal(result.status,'recorded');assert.equal(result.bytes,failure === 'empty' ? 0 : 2);
  assert.equal(draws,3);assert.equal(stops,1);assert.equal(result.diagnostics.drawRequests,3);
  assert.deepEqual(result.diagnostics.events.map(e => e.event),['constructed','start','dataavailable','stop']);
}
''')


@pytest.mark.parametrize('advertised,before,after', [(True, False, False), (True, True, True)])
def test_hevc_diagnostics_cannot_replace_advertisement_or_accept_failed_recording(advertised, before, after):
    report = stage9_report()
    row = report['runs'][0]['observation']['families']['hevc']
    row['recorderSupported'] = advertised
    row['recorderQueries'] = {'advertised': advertised, 'beforeRecord': before, 'afterRecord': after}
    row['recorded'] = {'status': 'rejected', 'bytes': 0, 'error': 'NotSupportedError:initialize',
                       'diagnostics': {'failure': {'phase': 'construct'}}}
    assert 'native: hevc: supported recorder failed to record' in media.assess(report)[0]


@pytest.mark.parametrize('advertised,forbidden', [(True, False), (False, True), (False, False)])
def test_recorder_query_changes_never_skip_advertised_or_disabled_attempt(node, advertised, forbidden):
    run_js(node, 'const advertised = ' + json.dumps(advertised) + ', forbidden = ' + json.dumps(forbidden) + ';\n' + r'''
const start = source.indexOf('      const recorderSupported = MediaRecorder.isTypeSupported(spec.mime);');
const end = source.indexOf('      const recording = forbidden ?',start);
assert.ok(start > 0 && end > start);
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const sample = new AsyncFunction('MediaRecorder','MediaSource','spec','capability','record','forbidden',
  source.slice(start,end) + '\nreturn {recorderSupported,recorderQueries,recorded};');
let queries = 0, attempts = 0;
const result = await sample({isTypeSupported(mime){
  assert.equal(mime,testMedia.specs.hevc.mime);return queries++ === 0 ? advertised : false;
}}, {isTypeSupported:() => false}, testMedia.specs.hevc, async () => ({supported:false}),
async mime => {attempts++;assert.equal(mime,testMedia.specs.hevc.mime);
  return {status:'rejected',error:'NotSupportedError',bytes:0};}, forbidden);
assert.equal(result.recorderSupported,advertised);
assert.deepEqual(result.recorderQueries,{advertised,beforeRecord:false,afterRecord:false});
assert.equal(attempts,advertised || forbidden ? 1 : 0);
assert.equal(result.recorded.status,advertised || forbidden ? 'rejected' : 'unavailable');
''')


def test_empty_hevc_recording_remains_a_failure():
    report = stage9_report()
    report['runs'][0]['observation']['families']['hevc']['recorded'].update(bytes=0, data='')
    assert media.assess(report)[0]


@pytest.mark.parametrize('unavailable', [False, True])
def test_runtime_diagnostics_capture_real_flags_and_gpu_without_launch_changes(unavailable):
    class Session:
        detached = False

        def send(self, method):
            if unavailable:
                raise RuntimeError('CDP unavailable')
            return {'arguments': ['--use-angle']} if method == 'Browser.getBrowserCommandLine' else {
                'gpu': {'featureStatus': {'video_encode': 'disabled_software'}}}

        def detach(self):
            self.detached = True

    session = Session()
    browser = type('Browser', (), {'new_browser_cdp_session': lambda self: session})()
    result = media.runtime_diagnostics(browser)
    assert session.detached
    assert result['collected'] == 'after_media_operations'
    if unavailable:
        assert len(result['errors']) == 2
    else:
        assert result['command_line']['arguments'] == ['--use-angle']
        assert result['system_info']['gpu']['featureStatus']['video_encode'] == 'disabled_software'
        assert not result['errors']


@pytest.mark.parametrize('decoded_frames', [0, 3])
def test_rtc_js_retains_transfer_stats_and_classifies_cleanup_abort(node, decoded_frames):
    run_js(node, 'const decodedFrames = ' + str(decoded_frames) + ';\n' + r'''
let video, playReject, peerCount = 0, draws = 0, cleared = false;
const peers = [], addedCandidates = [];
const track = {id:'track', kind:'video', get enabled(){return true},
  set enabled(value){assert.fail('capture track must stay enabled')}, muted:false, readyState:'live',
  getSettings:() => ({width:64,height:64}), requestFrame(){
    assert.equal(peers[0].remoteDescription.type, 'answer'); draws++;
  }, stop(){}, addEventListener(){}};
const stream = {id:'stream', getTracks:() => [track], getVideoTracks:() => [track]};
globalThis.document = {createElement:tag => {
  if (tag === 'canvas') return {getContext:() => ({fillRect(){}}), captureStream:() => stream};
  return video = {addEventListener(){}, play:() => new Promise((resolve,reject) => {playReject=reject}),
    set srcObject(value) {if (value === null && playReject) playReject(new DOMException('play interrupted by detach', 'AbortError'));},
    getVideoPlaybackQuality:() => ({totalVideoFrames:0, droppedVideoFrames:0})};
}};
globalThis.setInterval = callback => {callback();return 1};
globalThis.clearInterval = handle => {assert.equal(handle, 1);cleared=true;};
globalThis.RTCPeerConnection = class {
  constructor(settings) {
    assert.deepEqual(settings, {iceServers:[]});peers.push(this);
    this.index = peerCount++;this.connectionState = 'connected';this.iceConnectionState = 'connected';
  }
  addEventListener() {} addTrack() {} close() {assert.equal(cleared, true);this.closed=true;}
  async createOffer() {return {type:'offer',sdp:'a=rtpmap:96 VP8/90000\r\n'};}
  async createAnswer() {
    assert.ok(playReject);assert.equal(this.remoteDescription.type, 'offer');
    return {type:'answer',sdp:'a=rtpmap:96 VP8/90000\r\n'};
  }
  async setLocalDescription(value) {
    this.localDescription=value;
    this.onicecandidate({candidate:{id:this.index, toJSON:() => ({candidate:'owned candidate'})}});
  }
  async setRemoteDescription(value) {
    this.remoteDescription=value;
    if (this.index === 1) this.ontrack({track,streams:[stream]});
  }
  async addIceCandidate(value) {assert.ok(this.remoteDescription);addedCandidates.push(value.id);}
  getReceivers() {return this.index === 1 ? [{track}] : [];}
  async getStats() {
    const rows = [{id:'codec',type:'codec',mimeType:'video/VP8'},
      {id:'transport',type:'transport',selectedCandidatePairId:'pair'},
      {id:'pair',type:'candidate-pair',state:'succeeded',nominated:true},
      this.index === 0 ? {id:'out',type:'outbound-rtp',kind:'video',framesEncoded:3,bytesSent:456} :
        {id:'in',type:'inbound-rtp',kind:'video',framesDecoded:decodedFrames,bytesReceived:456,codecId:'codec'}];
    return new Map(rows.map(row => [row.id,row]));
  }
};
const result = await testMedia.rtc(false);
assert.equal(result.status, decodedFrames ? 'decoded' : 'failed');
assert.deepEqual(result.errors, []);
assert.equal(result.diagnostics.beforeCleanup.play.status, 'pending');
assert.equal(result.diagnostics.play.status, 'rejected');
assert.equal(result.diagnostics.cleanupErrors[0].name, 'AbortError');
assert.equal(result.diagnostics.cleanupErrors[0].phase, 'cleanup');
assert.ok(result.diagnostics.cleanupErrors[0].at >= result.diagnostics.cleanupStartedAt);
assert.equal(result.diagnostics.stats.at(-1).label, 'before_cleanup');
assert.ok(result.diagnostics.stats[0].send.some(row => row.type === 'outbound-rtp'));
assert.ok(result.diagnostics.stats[0].receive.some(row => row.type === 'candidate-pair'));
assert.ok(result.diagnostics.sdp.receive.remote.includes('VP8'));
assert.equal(result.diagnostics.candidates.length, 2);
assert.deepEqual(addedCandidates.sort(), [0, 1]);
assert.ok(peers.every(peer => peer.closed));
assert.ok(draws > 0);assert.equal(result.diagnostics.drawRequests, draws);
if (decodedFrames) {assert.equal(result.frames, 3);assert.equal(result.bytes, 456);}
else assert.ok(result.diagnostics.failure.includes('framesDecoded > 0'));
''')
