"""Synthetic handshake fixtures exercise the parser, not real wire provenance."""
from copy import deepcopy
import random
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fingerprint_protocols as protocols
import fingerprint_transport_audit as transport
from test_device_p0 import http_sample


def vector(data, width):
    return len(data).to_bytes(width, 'big') + data


def words(*values):
    return b''.join(v.to_bytes(2, 'big') for v in values)


def extensions():
    return [(10, vector(words(0x0A0A, 29, 23), 2)),
            (13, vector(words(0x0403, 0x0804), 2)),
            (43, vector(words(0x0A0A, 0x0304, 0x0303), 1)),
            (16, vector(vector(b'h2', 1) + vector(b'http/1.1', 1), 2)),
            (51, vector(words(29) + vector(b'fixture-not-a-real-key', 2), 2)),
            (0x1A1A, b'')]


def hello(ext=None):
    ext = extensions() if ext is None else ext
    body = (words(0x0303) + bytes(32) + b'\0' + vector(words(0x2A2A, 0x1301, 0x1302), 2) + b'\x01\0' +
            vector(b''.join(words(kind) + vector(payload, 2) for kind, payload in ext), 2))
    return b'\x01' + vector(body, 3)


def test_bounded_clienthello_decode_and_normalization():
    value = protocols.parse_client_hello(hello())
    assert value['alpn'] == ['h2', 'http/1.1']
    assert value['groups'] == [0x0A0A, 29, 23]
    assert value['key_share_groups'] == [29]
    changed = protocols.parse_client_hello(hello(list(reversed(extensions()))))
    changed['ciphers'][0] = 0xFAFA
    changed['groups'][0] = 0x6A6A
    assert protocols.compare(value, changed)['status'] == 'observed_match'
    changed['ciphers'] = changed['ciphers'][1:] + changed['ciphers'][:1]
    assert protocols.compare(value, changed)['status'] == 'mismatch'


@pytest.mark.parametrize('field', ['ciphers', 'groups', 'supported_versions', 'alpn', 'signature_algorithms'])
def test_ordered_tls_parameters_are_not_sorted_away(field):
    value = protocols.parse_client_hello(hello())
    other = deepcopy(value); other[field].reverse()
    assert protocols.compare(value, other)['status'] == 'mismatch'


@pytest.mark.parametrize('cut', range(len(hello())))
def test_every_truncated_handshake_is_rejected(cut):
    with pytest.raises(ValueError):
        protocols.parse_client_hello(hello()[:cut])


@pytest.mark.parametrize('ext', [extensions() * 2, [(10, b'\0\0')], [(13, b'\0\x01\xff')],
    [(16, b'\0\0')], [(16, b'\0\x01\0')], [(43, b'\0')],
    [(51, vector(words(29) + b'\0\0', 2))],
    [(51, vector((words(29) + vector(b'x', 2)) * 2, 2))]])
def test_malformed_extension_vectors_fail(ext):
    with pytest.raises(ValueError):
        protocols.parse_client_hello(hello(ext))


def test_malformed_sizes_types_and_numeric_profiles_fail():
    for data in (None, 'hello', bytearray(hello()), hello() + b'x', b'\x01' * 131073):
        with pytest.raises(ValueError):
            protocols.parse_client_hello(data)
    rng = random.Random(42)
    for _ in range(100):
        with pytest.raises(ValueError):
            protocols.parse_client_hello(rng.randbytes(rng.randrange(1024)))
    for field, value in (('ciphers', []), ('compression', [256]), ('groups', [True]), ('alpn', [])):
        profile = protocols.parse_client_hello(hello()); profile[field] = value
        with pytest.raises(ValueError):
            protocols.tls_identity(profile)


def transport_report():
    sample = http_sample()
    identity = sample['identity']['value']
    observation = {'userAgent': identity['ua'], 'languages': identity['languages'],
                   'userAgentData': identity['uaData'], 'wire': sample['http']['value']}
    headers = [[key, value] for key, value in [(':method','GET'), (':authority','localhost'),
                                              (':scheme','https'), (':path','/echo')]]
    connection = {'alpn':'h2', 'settings': [[[1, 65536], [4, 6291456]]],
                  'requests': [{'headers': headers, 'pseudo_order':[k for k, _ in headers]}]}
    return {'errors': [], 'client_hellos':[protocols.parse_client_hello(hello()) for _ in range(2)],
            'connections':[deepcopy(connection) for _ in range(2)],
            'observations':[deepcopy(observation) for _ in range(2)]}


def test_http2_and_http_js_coherence_are_required():
    assert transport.assess(transport_report())[0] == []
    for mutate in (
        lambda r: r['connections'][1].update(alpn='http/1.1'),
        lambda r: r['connections'][1].update(settings=[[[1, 1]]]),
        lambda r: r['connections'][1]['requests'][0]['pseudo_order'].reverse(),
        lambda r: r['observations'][0]['wire']['headers'].update({'sec-ch-ua-arch':'"arm"'}),
        lambda r: r.update(client_hellos=[]),
        lambda r: r.update(observations=[]),
        lambda r: r.update(connections=None),
    ):
        report = transport_report(); mutate(report)
        assert transport.assess(report)[0]


def test_resumption_is_not_silently_normalized_into_full_handshake():
    report = transport_report()
    for hello in report['client_hellos']:
        hello['extensions'].append(41)
    assert 'PSK resumption' in str(transport.assess(report)[0])


def test_owned_tls_fixture_never_issues_tickets(tmp_path):
    import ssl
    with transport.endpoint(tmp_path) as (server, origin):
        assert server.tls.num_tickets == 0
        assert server.tls.options & ssl.OP_NO_TICKET
        assert origin.startswith('https://127.0.0.1:')
