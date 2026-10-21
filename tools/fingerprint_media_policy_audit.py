#!/usr/bin/env python3
"""Compare codec queries with encoding, decoding, recording, MSE and local RTC.

Native recordings are replayed after disabling their codec, so rejection cannot
be inferred solely from an unsupported query. Unavailable native codecs retain
explicit gaps in operation coverage.
"""
from __future__ import annotations
import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from fingerprint_backend_audit import BASE_ARGS, PROBE, launch, server

FAMILIES = ('h264', 'vp8', 'vp9', 'av1', 'hevc')
CASES = {'native': (), 'disabled': FAMILIES, 'vp8_only': ('h264', 'vp9', 'av1', 'hevc')}
API_KEYS = ('encoderSupported', 'decoderSupported', 'recorderSupported', 'mseSupported')


def case_args(name):
    # Empty ICE servers need host UDP; the ungoogled default disables it.
    return [*BASE_ARGS, '--fingerprint-audio-render=native',
            '--webrtc-ip-handling-policy=default_public_and_private_interfaces',
            *['--fingerprint-codec-' + family + '=disabled' for family in CASES[name]]]


def payload(text):
    if not isinstance(text, str) or not 0 < len(text) <= 1400000:
        raise ValueError('missing or oversized media payload')
    data = base64.b64decode(text, validate=True)
    if not 0 < len(data) <= 1048576:
        raise ValueError('invalid media payload size')
    return data


def encoded_errors(value):
    if value['status'] != 'encoded' or not 1 <= len(value['chunks']) <= 16 or value.get('error'):
        return ['supported encoder produced no usable chunks']
    if not value['config'].get('codec') or not any(c['type'] == 'key' for c in value['chunks']):
        return ['encoded fixture lacks codec configuration or a keyframe']
    if any(not payload(c['data']) or c['timestamp'] != 0 for c in value['chunks']):
        return ['invalid encoded fixture']
    if value['config'].get('description'):
        payload(value['config']['description'])
    return []


def recorded_errors(value):
    if value['status'] != 'recorded':
        return ['supported recorder failed to record']
    if (type(value['bytes']) is not int or value['bytes'] != len(payload(value['data'])) or
            not isinstance(value['mime'], str) or not re.fullmatch(
                r'video/(?:webm|mp4|x-matroska|matroska);codecs=[^;]+', value['mime'], re.I)):
        return ['invalid recording payload or MIME type']
    errors = []
    if value.get('mimeSupported') is False:
        errors.append('recorder rejected its serialized MIME type')
    for mime in value.get('chunkMimes', []):
        if mime.lower() != value['mime'].lower():
            errors.append('recording chunk MIME differs from recorder MIME')
    return errors


def decoded_errors(value, frames=False):
    if value['status'] != 'decoded':
        return ['supported decoder did not decode the native fixture']
    rows = value['frames'] if frames else [value]
    if not rows or any((row['width'], row['height']) != (64, 64) for row in rows):
        return ['decoded frame dimensions mismatch']
    return []


def rejected(value):
    return (value.get('status') == 'rejected' and not value.get('chunks') and not value.get('frames') and
            not value.get('bytes') and isinstance(value.get('error'), str) and
            value['error'].startswith(('NotSupportedError', 'EncodingError', 'OperationError',
                'Error:MediaError:3', 'Error:MediaError:4', 'Error:MSE append failed')))


def family_in_mime(value):
    text = value.lower()
    for family, pattern in (('h264', r'\b(?:h264|avc1|avc3)\b'), ('vp8', r'\bvp8\b'),
            ('vp9', r'\b(?:vp9|vp09)\b'), ('av1', r'\b(?:av1|av01)\b'),
            ('hevc', r'\b(?:h265|hevc|hvc1|hev1)\b')):
        if re.search(pattern, text):
            return family
    return None


def assess(report):
    try:
        return _assess(report)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError) as error:
        return ['malformed media policy evidence: ' + str(error)], []


def _assess(report):
    errors, gaps = list(report.get('errors', [])), []
    if type(report.get('schema_version')) is not int or report.get('schema_version') != 1 or report.get('probe_sha256') != launch.pool.file_hash(PROBE):
        errors.append('media probe identity mismatch')
    runs = report['runs']
    if [r['name'] for r in runs] != list(CASES):
        return [*errors, 'native / disabled / allowed codec matrix did not complete'], gaps
    native = runs[0]['observation']['families']
    for run in runs:
        name, observed = run['name'], run['observation']
        prefix = lambda rows: errors.extend(name + ': ' + row for row in rows)
        if run['launch_args'] != case_args(name):
            prefix(['unexpected codec launch arguments'])
        if observed['apis'] != dict.fromkeys(('encoder', 'decoder', 'recorder', 'mse'), True):
            prefix(['required media APIs unavailable'])
        if set(observed['families']) != set(FAMILIES):
            prefix(['missing codec family queries'])
        for family in FAMILIES:
            row, control = observed['families'][family], native[family]
            if any(type(row[key]) is not bool for key in API_KEYS) or row['canPlay'] not in ('', 'maybe', 'probably'):
                prefix([family + ': malformed codec queries'])
            if any(type(row[key]) is not int or row[key] < 0 for key in ('rtcSend', 'rtcReceive')):
                prefix([family + ': malformed RTC capabilities'])
            capability_results = {}
            for key in ('encoding', 'decoding'):
                result = row.get(key)
                valid = isinstance(result, dict) and all(
                    type(result.get(field)) is bool
                    for field in ('supported', 'smooth', 'powerEfficient'))
                if not valid:
                    detail = result.get('error') if isinstance(result, dict) else None
                    prefix([family + ': missing MediaCapabilities result: ' + key +
                            (': ' + json.dumps(detail, sort_keys=True) if detail else '')])
                elif result.get('error') or result.get('status', 'resolved') != 'resolved':
                    prefix([family + ': inconsistent MediaCapabilities result: ' + key])
                configuration = result.get('configuration') if isinstance(result, dict) else None
                if configuration is not None:
                    content_type = configuration.get('video', {}).get('contentType', '')
                    expected = 'video/' + ('h265' if family == 'hevc' else family)
                    if key == 'encoding' and (configuration.get('type') != 'webrtc' or
                                              content_type.lower() != expected):
                        prefix([family + ': encodingInfo did not query the RTP MIME with type webrtc'])
                    if key == 'decoding' and (configuration.get('type') != 'file' or
                                              content_type != row.get('mime')):
                        prefix([family + ': decodingInfo MIME differs from the file query'])
                capability_results[key] = result if valid else None
            if row.get('mime') is not None:
                recording = control['recorded'] if family in CASES[name] else row['recorded']
                mse = row['mse']
                if mse['status'] != 'unavailable' and (
                        mse.get('mime') != row['mime'] or mse.get('mimeSupported') != row['mseSupported'] or
                        mse.get('recorderMime') != recording.get('mime')):
                    prefix([family + ': MSE did not use the queried codec MIME and original recording'])
                if row['recorded']['status'] == 'recorded' and (
                        row['recorded'].get('requestedMime') != row['mime'] or
                        family_in_mime(row['recorded']['mime']) != family):
                    prefix([family + ': recorder MIME does not match the requested codec'])
                playback = row['playback']
                if playback['status'] != 'unavailable' and playback.get('mime') != recording.get('mime'):
                    prefix([family + ': playback replaced the serialized recorder MIME'])
            if family in CASES[name]:
                advertised_capabilities = any(
                    capability_results[key] is not None and any(capability_results[key][field]
                        for field in ('supported', 'smooth', 'powerEfficient'))
                    for key in ('encoding', 'decoding'))
                if (any(row[key] for key in API_KEYS) or row['canPlay'] or row['rtcSend'] or row['rtcReceive'] or
                        advertised_capabilities):
                    prefix([family + ': disabled codec still advertised'])
                for key in ('encoded', 'recorded'):
                    if not rejected(row[key]):
                        prefix([family + ': disabled ' + key + ' operation was not rejected'])
                for key, fixture_key in (('decoded', 'encoded'), ('playback', 'recorded'), ('mse', 'recorded')):
                    if control[fixture_key]['status'] in ('encoded', 'recorded'):
                        if not rejected(row[key]):
                            prefix([family + ': native fixture still accepted by disabled ' + key])
                        if control[key]['status'] != 'decoded':
                            gaps.append(name + '.' + family + '.' + key + '.no_native_decode_control')
                    else:
                        gaps.append(name + '.' + family + '.' + key + '.no_native_fixture')
                        if row[key]['status'] != 'unavailable':
                            prefix([family + ': operation claims a missing fixture'])
            else:
                if name != 'native' and any(row[key] != control[key] for key in (*API_KEYS, 'canPlay', 'rtcSend', 'rtcReceive')):
                    prefix([family + ': allowed codec capabilities changed'])
                if row['encoderSupported']:
                    prefix([family + ': ' + e for e in encoded_errors(row['encoded'])])
                    if row['decoderSupported']:
                        prefix([family + ': ' + e for e in decoded_errors(row['decoded'], frames=True)])
                else:
                    gaps.append(name + '.' + family + '.native_encoder')
                if row['recorderSupported']:
                    prefix([family + ': ' + e for e in recorded_errors(row['recorded'])])
                    if row['canPlay']:
                        prefix([family + ': ' + e for e in decoded_errors(row['playback'])])
                    if row['mseSupported']:
                        prefix([family + ': ' + e for e in decoded_errors(row['mse'])])
                else:
                    gaps.append(name + '.' + family + '.native_recorder')
        rtc = observed['rtc']
        default = observed['defaultRecorder']
        if name == 'disabled':
            if not rejected(default):
                prefix(['default video recorder bypassed the disabled pool'])
            if rtc['status'] not in ('offered', 'rejected') or rtc.get('frames'):
                prefix(['RTC decoded with no allowed primary video codec'])
            if rtc['status'] == 'rejected' and not rejected(rtc):
                prefix(['RTC failed for an unrelated reason'])
        else:
            prefix(recorded_errors(default))
            prefix(decoded_errors(default['playback']))
            selected = family_in_mime(default['mime'])
            if selected is None or selected in CASES[name]:
                prefix(['default recorder selected an unknown or forbidden codec'])
            if (rtc['status'] != 'decoded' or type(rtc['frames']) is not int or rtc['frames'] < 1 or
                    type(rtc['bytes']) is not int or rtc['bytes'] < 1 or rtc['errors'] or
                    family_in_mime(rtc['codec']) in (*CASES[name], None)):
                prefix(['allowed RTC codec did not transfer and decode video'])
        if any(family_in_mime(codec) in CASES[name] for codec in rtc.get('formats', [])):
            prefix(['RTC offer contains a disabled codec'])
        audio = observed['audio']
        if (audio['status'] != 'recorded' or type(audio['bytes']) is not int or audio['bytes'] <= 0 or
                'opus' not in audio['mime'].lower()):
            prefix(['video codec policy broke audio-only recording'])
    # VP8 is the required portable control; optional hardware codecs cannot
    # make a suite with no successful encode/decode operations pass.
    if not all(native['vp8'][key] for key in API_KEYS) or not native['vp8']['canPlay']:
        errors.append('portable VP8 encode/decode/record/MSE control unavailable')
    return sorted(set(errors)), sorted(set(gaps))


def runtime_diagnostics(browser):
    result = {'collected': 'after_media_operations', 'errors': []}
    session = None
    try:
        session = browser.new_browser_cdp_session()
        for key, method in (('command_line', 'Browser.getBrowserCommandLine'),
                            ('system_info', 'SystemInfo.getInfo')):
            try:
                result[key] = session.send(method)
            except Exception as error:
                result['errors'].append(method + ': ' + type(error).__name__ + ': ' + str(error))
    except Exception as error:
        result['errors'].append(type(error).__name__ + ': ' + str(error))
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception as error:
                result['errors'].append('detach: ' + type(error).__name__ + ': ' + str(error))
    return result


def run(browser, headed=False):
    report = {'schema_version': 1, 'collected_at': datetime.now(timezone.utc).isoformat(),
        'browser_sha256': launch.pool.file_hash(browser), 'probe_sha256': launch.pool.file_hash(PROBE),
        'runs': [], 'errors': [], 'qualification': {'rtc': 'owned local peer pair',
            'rtc_transport': 'host UDP on default interfaces; no STUN/TURN servers',
            'default_or_proxy_network_policy': 'not_tested',
            'physical_media': 'not_attested', 'drm_remote_decoders': 'not_tested',
            'codec_fixtures': 'generated by native control; missing codecs remain gaps'}}
    try:
        from playwright.sync_api import sync_playwright
        with server() as origin, sync_playwright() as pw:
            fixtures = {}
            for name in CASES:
                args = case_args(name)
                instance = pw.chromium.launch(executable_path=str(browser.resolve()), headless=not headed,
                    chromium_sandbox=True, args=args)
                try:
                    if report.setdefault('browser_version', instance.version) != instance.version:
                        raise ValueError('browser version changed across launches')
                    context = instance.new_context(no_viewport=True)
                    try:
                        context.add_init_script(path=str(PROBE))
                        page = context.new_page()
                        page.goto(origin, timeout=30000)
                        observed = page.evaluate('async args => await chromixBackendProbe.codecs(args)',
                                                {'fixtures': fixtures, 'disabled': list(CASES[name])})
                        report['runs'].append({'name': name, 'launch_args': args, 'observation': observed,
                                               'runtime_diagnostics': runtime_diagnostics(instance)})
                        if name == 'native':
                            fixtures = observed['families']
                    finally:
                        context.close()
                finally:
                    instance.close()
    except Exception as error:
        report['errors'].append(type(error).__name__ + ': ' + str(error))
    if launch.pool.file_hash(browser) != report['browser_sha256']:
        report['errors'].append('browser executable changed')
    report['errors'], report['gaps'] = assess(report)
    report['status'] = 'failed' if report['errors'] else 'incomplete' if report['gaps'] else 'passed'
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
    print(json.dumps({'status': report['status'], 'errors': report['errors'], 'gaps': report['gaps']}))
    return int(report['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
