"""Bounded TLS ClientHello decoding and native protocol comparison (not spoofing)."""
from __future__ import annotations
import hashlib
import json


class Reader:
    def __init__(self, data):
        self.data = memoryview(data)
        self.position = 0

    def take(self, count):
        if count < 0 or self.position + count > len(self.data):
            raise ValueError('truncated TLS vector')
        result = bytes(self.data[self.position:self.position + count])
        self.position += count
        return result

    def number(self, count):
        return int.from_bytes(self.take(count), 'big')

    def vector(self, width):
        return self.take(self.number(width))

    def done(self):
        if self.position != len(self.data):
            raise ValueError('trailing TLS bytes')


def words(data):
    if len(data) % 2:
        raise ValueError('odd TLS integer vector')
    return [int.from_bytes(data[i:i + 2], 'big') for i in range(0, len(data), 2)]


def parse_client_hello(data):
    """Accept one complete handshake message, not a TLS record or decrypted key."""
    if not isinstance(data, bytes) or not 42 <= len(data) <= 131072:
        raise ValueError('invalid ClientHello size')
    handshake = Reader(data)
    if handshake.number(1) != 1 or handshake.number(3) != len(data) - 4:
        raise ValueError('not a complete ClientHello')
    body = Reader(handshake.take(len(data) - 4))
    version = body.number(2); body.take(32)
    session = body.vector(1)
    if len(session) > 32:
        raise ValueError('oversized TLS session id')
    ciphers = words(body.vector(2)); compression = list(body.vector(1))
    if not ciphers or not compression:
        raise ValueError('empty required ClientHello vector')
    extensions = Reader(body.vector(2)); body.done()
    result = {'legacy_version': version, 'ciphers': ciphers, 'compression': compression,
              'extensions': [], 'groups': [], 'signature_algorithms': [],
              'supported_versions': [], 'alpn': [], 'key_share_groups': [],
              'message_sha256': hashlib.sha256(data).hexdigest()}
    while extensions.position < len(extensions.data):
        kind = extensions.number(2); payload = extensions.vector(2)
        if kind in result['extensions']:
            raise ValueError('duplicate TLS extension')
        result['extensions'].append(kind)
        inner = Reader(payload)
        if kind in (10, 13):
            values = words(inner.vector(2))
            if not values:
                raise ValueError('empty TLS group/signature vector')
            result['groups' if kind == 10 else 'signature_algorithms'] = values
        elif kind == 43:
            result['supported_versions'] = words(inner.vector(1))
            if not result['supported_versions']:
                raise ValueError('empty supported versions')
        elif kind == 16:
            protocols = Reader(inner.vector(2))
            while protocols.position < len(protocols.data):
                value = protocols.vector(1)
                if not value:
                    raise ValueError('empty ALPN name')
                result['alpn'].append(value.decode('ascii'))
            if not result['alpn']:
                raise ValueError('empty ALPN list')
        elif kind == 51:
            shares = Reader(inner.vector(2))
            while shares.position < len(shares.data):
                group = shares.number(2)
                if group in result['key_share_groups']:
                    raise ValueError('duplicate TLS key share group')
                result['key_share_groups'].append(group)
                if not shares.vector(2):
                    raise ValueError('empty TLS key share')
        else:
            inner.take(len(payload))
        inner.done()
    return result


def grease(value):
    return type(value) is int and 0 <= value <= 65535 and value & 0x0F0F == 0x0A0A and value >> 8 == value & 255


def tls_identity(hello):
    fields = ('legacy_version', 'ciphers', 'compression', 'extensions', 'groups',
              'signature_algorithms', 'supported_versions', 'alpn', 'key_share_groups')
    if not isinstance(hello, dict) or any(k not in hello for k in fields):
        raise ValueError('incomplete ClientHello profile')
    result = {}
    for key in fields:
        value = hello[key]
        if key == 'legacy_version':
            if type(value) is not int or not 0 <= value <= 65535:
                raise ValueError('invalid TLS version')
            result[key] = value
        elif key == 'alpn':
            if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x for x in value):
                raise ValueError('invalid ALPN list')
            result[key] = value
        else:
            maximum = 255 if key == 'compression' else 65535
            if not isinstance(value, list) or any(type(x) is not int or not 0 <= x <= maximum for x in value):
                raise ValueError('invalid TLS numeric list: ' + key)
            if key in ('ciphers', 'compression') and not value:
                raise ValueError('empty required TLS list: ' + key)
            # GREASE values and extension order deliberately vary in Chromium.
            # Keep their count/positions in ordered vectors, not just presence.
            normalized = [0x0A0A if grease(x) else x for x in value]
            result[key] = sorted(normalized) if key == 'extensions' else normalized
    return result


def compare(reference, observed):
    left, right = tls_identity(reference), tls_identity(observed)
    differences = {key: {'expected': left[key], 'observed': right[key]}
                   for key in left if left[key] != right[key]}
    return {'status': 'mismatch' if differences else 'observed_match', 'differences': differences,
            'canonical_sha256': hashlib.sha256(json.dumps(right, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
