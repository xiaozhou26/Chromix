#!/usr/bin/env python3
"""Read-only measured-corpus integrity, review-expiry and cohort checks.

Review metadata is explicitly supplied, never invented from a browser run.
Passing these checks neither authenticates the reviewer nor attests hardware,
font-file-to-glyph binding, network routes or cross-device emulation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

import apply_restored_patches as paths
import device_pool as pool
from fingerprint_acceptance import read_json

PROBE = Path(__file__).resolve().parents[1] / 'sdk/python/chromix/device_probe.js'
SHA256 = re.compile('[0-9a-f]{64}')


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('timestamp must be an ISO-8601 string with timezone')
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('timestamp requires timezone')
    return result.astimezone(timezone.utc)


def label(value):
    return isinstance(value, str) and 0 < len(value) <= 128 and not any(ord(c) < 32 for c in value)


def review(manifest_path, *, now=None):
    manifest_path = Path(manifest_path).resolve()
    root = manifest_path.parent
    now = datetime.now(timezone.utc) if now is None else now
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('review clock requires timezone')
    manifest_hash, probe_hash = pool.file_hash(manifest_path), pool.file_hash(PROBE)
    manifest = read_json(manifest_path)
    if (not isinstance(manifest, dict) or set(manifest) != {
            'schema_version', 'browser_version', 'probe_sha256', 'max_age_days', 'cohorts', 'samples'} or
            type(manifest['schema_version']) is not int or manifest['schema_version'] != 1):
        raise ValueError('requires a complete schema v1 review manifest')
    version, age = manifest['browser_version'], manifest['max_age_days']
    if not isinstance(version, str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+){3}', version):
        raise ValueError('manifest must pin a full browser version')
    if manifest['probe_sha256'] != probe_hash:
        raise ValueError('manifest was reviewed with a different device probe')
    if type(age) is not int or not 1 <= age <= 365:
        raise ValueError('max_age_days must be in [1, 365]')
    groups, samples = manifest['cohorts'], manifest['samples']
    if not isinstance(groups, list) or not 1 <= len(groups) <= 100:
        raise ValueError('requires 1..100 explicit cohorts')
    if not isinstance(samples, list) or not 1 <= len(samples) <= 1000:
        raise ValueError('requires 1..1000 reviewed samples')
    cohorts = {}
    for group in groups:
        if (not isinstance(group, dict) or set(group) != {'id', 'os', 'architecture', 'min_devices'} or
                not all(label(group.get(k)) for k in ('id', 'os', 'architecture')) or
                type(group['min_devices']) is not int or not 1 <= group['min_devices'] <= 1000 or
                group['id'] in cohorts):
            raise ValueError('invalid, empty or duplicate cohort definition')
        cohorts[group['id']] = {**group, 'devices': set()}
    report = {'schema_version': 1, 'manifest_sha256': manifest_hash, 'probe_sha256': probe_hash,
              'reviewed_at': now.isoformat(), 'browser_version': version, 'samples': [], 'errors': [],
              'qualification': {'scope': 'bundle integrity and supplied curation metadata only',
                                'reviewer_authenticated': False, 'physical_backend_equivalence': False,
                                'font_file_to_glyph_binding': False, 'routes_verified': False}}
    seen_ids, seen_paths, seen_devices = set(), set(), set()
    fingerprints = {}
    for index, sample in enumerate(samples):
        row = {'index': index, 'status': 'rejected', 'errors': []}
        report['samples'].append(row)
        try:
            if (not isinstance(sample, dict) or set(sample) != {
                    'path', 'record_id', 'record_sha256', 'device_id', 'cohort_id',
                    'reviewer', 'reviewed_at', 'expires_at', 'sample_kind'} or
                    not all(label(sample.get(k)) for k in ('device_id', 'cohort_id', 'reviewer')) or
                    sample['sample_kind'] not in ('physical', 'virtual', 'control', 'fixture') or
                    not all(isinstance(sample.get(k), str) and SHA256.fullmatch(sample[k])
                            for k in ('record_id', 'record_sha256'))):
                raise ValueError('invalid or incomplete sample review metadata')
            if sample['cohort_id'] not in cohorts:
                raise ValueError('unknown cohort')
            path = paths._path(root, sample['path'])
            if path in seen_paths or sample['record_id'] in seen_ids:
                raise ValueError('duplicate record path or record ID')
            seen_paths.add(path); seen_ids.add(sample['record_id'])
            if pool.file_hash(path) != sample['record_sha256']:
                raise ValueError('reviewed record checksum changed')
            fingerprints[path] = sample['record_sha256']
            record = read_json(path)
            # Check containment, regular files and size before the shared bundle
            # validator reads evidence. Never follow a symlink out of a bundle.
            if not isinstance(record, dict) or not isinstance(record.get('evidence'), dict):
                raise ValueError('missing bundle evidence')
            evidence = {}
            for kind, entry in record['evidence'].items():
                evidence_path = paths._path(path.parent, entry['path'])
                fingerprints[evidence_path] = entry['sha256']
                if pool.file_hash(evidence_path) != entry['sha256']:
                    raise ValueError('evidence checksum changed before review')
                evidence[kind] = read_json(evidence_path)
            record = pool.validate_record(record, path.parent)
            if record['record_id'] != sample['record_id']:
                raise ValueError('review belongs to a different record')
            gaps = pool.backend_gaps(record)
            if gaps:
                raise ValueError('; '.join(gaps))
            provenance, browser = record['provenance'], evidence['browser']
            if (provenance.get('browser_version') != version or browser.get('browser_versions') != [version] * 3 or
                    provenance.get('probe_sha256') != probe_hash or browser.get('probe_sha256') != probe_hash):
                raise ValueError('missing or mismatched browser-version/probe provenance; recollect the bundle')
            if any(scope.get('probeVersion') != 2 for observation in browser['observations']
                   for scope in observation.values()):
                raise ValueError('every context requires probe v2 evidence')
            collected = timestamp(provenance['collected_at'])
            reviewed, expires = timestamp(sample['reviewed_at']), timestamp(sample['expires_at'])
            if not collected <= reviewed <= now < expires:
                raise ValueError('future, expired or incorrectly ordered collection/review timestamps')
            if now - collected > timedelta(days=age):
                raise ValueError('collection exceeded maximum evidence age')
            host, cohort = record['device']['host'], cohorts[sample['cohort_id']]
            if (host['os']['system'], host['os']['architecture']) != (cohort['os'], cohort['architecture']):
                raise ValueError('observed OS/architecture does not match cohort')
            device = (sample['cohort_id'], sample['device_id'])
            if device in seen_devices:
                raise ValueError('one physical device cannot count twice in a cohort')
            seen_devices.add(device)
            row.update(record_id=record['record_id'], cohort_id=sample['cohort_id'], device_id=sample['device_id'],
                       reviewer=sample['reviewer'], sample_kind=sample['sample_kind'],
                       gpu_inventory_sha256=pool.digest(host['gpu']), font_inventory_sha256=pool.digest(host['fonts']))
            # Controls and templates may accompany a review for calibration, but
            # never inflate the physical-device count or synthetic pool weights.
            if sample['sample_kind'] != 'physical':
                row.update(status='excluded', reason='not a physical-device sample')
            else:
                cohort['devices'].add(sample['device_id'])
                row['status'] = 'review_checks_passed'
        except (ValueError, OSError, KeyError, TypeError, AttributeError, paths.ApplyError) as error:
            row['errors'].append(str(error))
            report['errors'].append(f'sample {index}: {error}')
    report['cohorts'] = [{**{k: v for k, v in group.items() if k != 'devices'},
                          'observed_devices': len(group['devices'])} for group in cohorts.values()]
    for group in report['cohorts']:
        if group['observed_devices'] < group['min_devices']:
            report['errors'].append(f'cohort {group["id"]}: insufficient distinct reviewed physical devices')
    for path, before in fingerprints.items():
        if pool.file_hash(path) != before:
            report['errors'].append('input changed during review: ' + str(path))
    if pool.file_hash(manifest_path) != manifest_hash or pool.file_hash(PROBE) != probe_hash:
        report['errors'].append('manifest or device probe changed during review')
    report['status'] = 'failed' if report['errors'] else 'passed'
    return report


def main(argv=None):
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('output must be a new file')
    try:
        report = review(args.manifest)
    except (ValueError, OSError, TypeError, paths.ApplyError) as error:
        report = {'schema_version': 1, 'status': 'failed', 'errors': [str(error)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, ensure_ascii=True)
    print(json.dumps({'status': report['status'], 'errors': report['errors'], 'output': str(args.output)}))
    return int(report['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
