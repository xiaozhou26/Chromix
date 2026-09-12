"""Public flag compatibility and pre-launch IP resolution."""
import pytest

from chromix._fingerprint import normalize_fingerprint_args
from chromix._network import lookup_proxy, network_args, resolve_webrtc_args


@pytest.mark.parametrize('mode', ['off', 'false', '0', 'disable', 'disabled', 'OFF', 'FALSE'])
def test_off_aliases_strip_inherited_platform(mode):
    args = ['--fingerprint-platform=windows', '--fingerprint=' + mode, '--fingerprint-timezone=UTC']
    assert normalize_fingerprint_args(args) == ['--fingerprint=off', '--fingerprint-timezone=UTC']
    assert args[0] == '--fingerprint-platform=windows'


@pytest.mark.parametrize('brand', ['Chrome', 'Edge', 'Opera', 'Vivaldi'])
def test_brands_and_versions(brand):
    args = ['--fingerprint-brand=' + brand.lower(), '--fingerprint-brand-version=152.0.7977.82']
    assert normalize_fingerprint_args(args)[0] == '--fingerprint-brand=' + brand


@pytest.mark.parametrize('flag', [
    '--fingerprint=18446744073709551616', '--fingerprint=-1', '--fingerprint=abc',
    '--fingerprint-brand=Unknown', '--fingerprint-brand-version=1.2.3.4.5',
    '--fingerprint-brand-version=x\nInjected:1', '--fingerprint-hardware-concurrency=129',
    '--fingerprint-device-memory=nan', '--fingerprint-device-memory=inf', '--fingerprint-device-memory=0',
    '--fingerprint-screen-width=-1', '--fingerprint-taskbar-height=-1',
    '--fingerprint-storage-quota=8796093022208', '--fingerprint-noise=maybe',
])
def test_invalid_switches_fail_before_browser_start(flag):
    with pytest.raises(ValueError):
        normalize_fingerprint_args([flag])


@pytest.mark.parametrize('ip', ['198.51.100.7', '2001:db8::7'])
def test_explicit_ip_does_not_call_resolver(ip):
    def forbidden(*args):
        pytest.fail('explicit IP must not trigger an HTTP request')
    args = ['--fingerprint-webrtc-ip=' + ip]
    assert resolve_webrtc_args(args, lookup=forbidden) == args


@pytest.mark.parametrize('ip', ['', 'example.com', '1.2.3.4:80', '[::1]', 'fe80::1%eth0', '127.1', '1.2.3.999', '1.2.3.4\r\n'])
def test_invalid_ip_values(ip):
    with pytest.raises(ValueError, match='IPv4/IPv6'):
        network_args(['--fingerprint-webrtc-ip=' + ip])


def test_auto_resolves_once_using_proxy_credentials_without_changing_region():
    requests = []
    def lookup(proxy):
        requests.append(proxy)
        return 'Asia/Tokyo', 'ja-JP', '203.0.113.9'
    args = ['--fingerprint=42', '--fingerprint-webrtc-ip=auto', '--fingerprint-locale=fr-FR']
    proxy = {'server':'http://proxy.example:8080', 'username':'u@', 'password':'p:', 'bypass':'*'}
    result = resolve_webrtc_args(args, proxy, lookup=lookup)
    assert requests == ['http://u%40:p%3A@proxy.example:8080']
    assert '--fingerprint-webrtc-ip=203.0.113.9' in result
    assert '--fingerprint-locale=fr-FR' in result
    assert not any('timezone' in arg for arg in result)
    assert args[1] == '--fingerprint-webrtc-ip=auto'


def test_geoip_reuses_lookup_and_explicit_native_alias_wins():
    def forbidden(*args):
        pytest.fail('the GeoIP lookup must be reused')
    result = resolve_webrtc_args(['--fingerprint=42'], geoip=True, exit_ip='203.0.113.9', lookup=forbidden)
    assert '--fingerprint-webrtc-ip=203.0.113.9' in result
    args = ['--fingerprint-webrtc-ip=auto', '--uxr-webrtc-ip=198.51.100.8']
    assert resolve_webrtc_args(args, geoip=True, exit_ip='203.0.113.9', lookup=forbidden) == args


def test_off_auto_does_not_make_a_request():
    def forbidden(*args):
        pytest.fail('off must not resolve a fingerprint-only address')
    result = resolve_webrtc_args(['--fingerprint=false', '--fingerprint-webrtc-ip=auto'], lookup=forbidden)
    assert '--fingerprint=off' in result


def test_raw_proxy_flag_is_used_for_auto():
    requests = []
    def lookup(proxy):
        requests.append(proxy)
        return 'UTC', 'en-US', '203.0.113.5'
    result = resolve_webrtc_args(['--proxy-server=http://proxy.example:8080', '--fingerprint-webrtc-ip=auto'], lookup=lookup)
    assert requests == ['http://proxy.example:8080']
    assert '--fingerprint-webrtc-ip=203.0.113.5' in result


def test_auto_failure_has_no_fallback():
    def fail(proxy):
        raise ValueError('lookup failed')
    with pytest.raises(ValueError, match='lookup failed'):
        resolve_webrtc_args(['--fingerprint-webrtc-ip=auto'], 'http://proxy.example:8080', lookup=fail)


@pytest.mark.parametrize('arg', [
    '--proxy-server=', '--proxy-server=http://one:80,direct://',
    '--proxy-server=http=one:80;https=two:80', '--proxy-server=http://u:p@one:80',
    '--proxy-pac-url=https://proxy.example/proxy.pac', '--proxy-auto-detect',
])
@pytest.mark.parametrize('proxy', [None, 'http://one:80'])
def test_ambiguous_auto_routes_fail_before_lookup(arg, proxy):
    def forbidden(*args):
        pytest.fail('ambiguous route must be rejected before lookup')
    with pytest.raises(ValueError, match='GeoIP/auto'):
        resolve_webrtc_args([arg, '--fingerprint-webrtc-ip=auto'], proxy, lookup=forbidden)


def test_proxy_route_conflicts_and_no_proxy_precedence():
    proxy = {'server': 'http://one:8080', 'username': 'u', 'password': 'p'}
    assert lookup_proxy(['--proxy-server=http://one:8080'], proxy) == proxy
    with pytest.raises(ValueError, match='conflicts'):
        lookup_proxy(['--proxy-server=http://two:8080'], proxy)
    with pytest.raises(ValueError, match='conflicts'):
        lookup_proxy(['--no-proxy-server'], proxy)
    args = ['--proxy-server=http://one:8080', '--no-proxy-server']
    assert lookup_proxy(args) is None
    assert network_args(args, proxy) == args
    assert network_args(args[:1])[-1] == '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'
    assert network_args(args[:1] + ['--force-webrtc-ip-handling-policy=default'])[-1].endswith('=default')


@pytest.mark.parametrize('raw, configured', [
    ('http://one:80', 'http://ONE'),
    ('https://one:443', 'https://one'),
    ('socks5://one:1080', 'socks5://one'),
    ('http://[2001:db8:0:0:0:0:0:1]:80', 'http://[2001:db8::1]'),
])
def test_equivalent_proxy_endpoints_keep_high_level_credentials(raw, configured):
    proxy = {'server': configured, 'username': 'u', 'password': 'p'}
    assert lookup_proxy(['--proxy-server=' + raw], proxy) == proxy
