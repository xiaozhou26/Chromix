"""Offline smoke-runner regressions; Node mocks are not browser verification."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from http.client import HTTPConnection
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("fingerprint_smoke", ROOT / "tools/fingerprint_smoke.py")
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)
NODE = Path("/root/.local/share/chromix-ci-node/node-v24.8.0-linux-x64/bin/node")


def options(*extra):
    return smoke.argument_parser().parse_args(["--browser", "/explicit/chrome", *extra])


def scenario(mode="native"):
    return {"name": "sample", "restart": 1, "mode": mode, "seed": 42,
            "platform": "windows", "locale": "de-DE"}


def scope(spec=None, name="window", phase="initial"):
    spec = spec or scenario()
    low = {"brands": [{"brand": "Chromium", "version": "153"}], "mobile": False, "platform": "Linux"}
    high = {**deepcopy(low), "platformVersion": "", "architecture": "x86", "bitness": "64",
            "model": "", "uaFullVersion": "153.0.1.2",
            "fullVersionList": [{"brand": "Chromium", "version": "153.0.1.2"}], "wow64": False}
    headers = {"user-agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/153.0.0.0",
               "accept-language": "en-US,en;q=0.9"}
    for field, header in {"brands": "sec-ch-ua", "mobile": "sec-ch-ua-mobile",
                          "platform": "sec-ch-ua-platform", **smoke.HIGH_HEADERS}.items():
        value = (low if field in low else high)[field]
        if isinstance(value, list):
            headers[header] = ", ".join(f'{json.dumps(item["brand"])};v={json.dumps(item["version"])}' for item in value)
        elif isinstance(value, bool):
            headers[header] = "?1" if value else "?0"
        else:
            headers[header] = json.dumps(value)
    query = {"scenario": spec["name"], "restart": spec["restart"], "scope": name, "phase": phase}
    return {"ua": headers["user-agent"], "language": "en-US", "languages": ["en-US", "en"],
            "intlLocale": "en-US", "environment": environment(),
            "platform": "Linux x86_64", "uaData": {"low": low, "high": high, "highError": None},
            "http": {"path": "/echo?" + urlencode(query), "headers": headers}}


def environment():
    return {"network": {"available": True, "online": True, "effectiveType": "4g",
                        "rtt": 0, "downlink": 10, "saveData": False},
            "storage": {"available": True,
                        "estimate": {"usage": 2048, "quota": 1024, "usageDetails": {"indexedDB": 2048}}}}


@pytest.mark.parametrize("field,value", [
    ("rtt", -1), ("rtt", True), ("rtt", 0.5), ("rtt", 2**32),
    ("downlink", -1), ("downlink", False), ("downlink", float("inf")),
    ("downlink", float("nan")), ("effectiveType", "5g"),
    ("saveData", 0), ("online", "true"),
])
def test_environment_network_invalid_values_fail(field, value):
    sample = environment()
    sample["network"][field] = value
    assert failures(smoke.evaluate_environment(sample, "worker"))


@pytest.mark.parametrize("field,value", [
    ("usage", -1), ("quota", True), ("quota", float("inf")),
    ("usage", float("nan")), ("quota", 1.25), ("quota", 2**65), ("quota", 10**1000),
    ("usageDetails", {"caches": -1}), ("usageDetails", []), ("usageDetails", None),
])
def test_environment_storage_invalid_values_fail(field, value):
    sample = environment()
    sample["storage"]["estimate"][field] = value
    assert failures(smoke.evaluate_environment(sample, "iframe"))


@pytest.mark.parametrize("category", ["network", "storage"])
def test_environment_absence_and_errors_are_not_success(category):
    sample = environment()
    del sample[category]
    assert failures(smoke.evaluate_environment(sample, "window"))
    sample[category] = {"available": False, "reason": "not implemented"}
    checks = smoke.evaluate_environment(sample, "window")
    assert not failures(checks)
    assert any(check["name"] == f"window.{category}" and check["status"] == "not_supported"
               for check in checks)
    sample[category] = {"error": {"name": "UnknownError", "message": "backend failure"}}
    assert failures(smoke.evaluate_environment(sample, "window"))
    assert failures(smoke.evaluate_environment(None, "window"))


def test_environment_accepts_zero_network_and_reduced_storage_quota():
    sample = environment()
    assert not failures(smoke.evaluate_environment(sample, "window"))
    sample["network"]["downlink"] = 0
    sample["storage"]["estimate"] = {"usage": 0, "quota": 0}
    assert not failures(smoke.evaluate_environment(sample, "window"))
    sample["storage"]["estimate"] = {"usage": 2**64, "quota": 2**64}
    assert not failures(smoke.evaluate_environment(sample, "window"))


def test_dynamic_environment_is_excluded_from_stable_identity():
    first = observation()
    changed = deepcopy(first)
    changed["window"]["environment"]["network"]["rtt"] = 200
    changed["worker"]["environment"]["storage"]["estimate"]["quota"] = 8192
    assert smoke.stable_identity(first) == smoke.stable_identity(changed)
    assert not failures(smoke.evaluate_scope(changed["worker"], scenario(), "worker"))
    del changed["worker"]["environment"]
    assert "worker.environment.completed" in failures(
        smoke.evaluate_scope(changed["worker"], scenario(), "worker"))


def signals(marker="a"):
    return {"canvas": {"available": True, "pixelHash": marker * 64, "repeatHash": marker * 64,
                       "dataUrlHash": marker * 64, "dataUrlRepeat": True, "blobHash": marker * 64},
            "audio": {"available": False, "reason": "offline fixture"}}


def surface_absent():
    absent = {"available": False, "reason": "offline unit fixture; not browser evidence"}
    return {name: {key: deepcopy(absent) for key in
                  (("canvas", "webgl1", "webgl2", "webgpu", "audio", "codecs", "network")
                   if name == "window" else ("canvas",))} for name in smoke.SCOPES}


def observation(spec=None, phase="initial", marker="a"):
    return {**{name: scope(spec, name, phase) for name in smoke.SCOPES}, "signals": signals(marker),
            "surfaces": surface_absent()}


def failures(checks):
    return {check["name"] for check in checks if check["status"] == "failed"}


@pytest.mark.parametrize("raw,expected", [("1", 1), ("4294967296", 2**32),
                                            ("0x8000000000000001", 2**63 + 1),
                                            ("18446744073709551615", 2**64 - 1)])
def test_full_uint64_seed_preserved_in_matrix_profile_and_command(raw, expected):
    args = options("--seed", raw)
    spec = smoke.scenario_matrix(args)[2]
    assert args.seed == spec["seed"] == expected
    assert str(expected) in smoke.profile_group(spec)
    assert f"--fingerprint={expected}" in smoke.browser_args(spec, "http://127.0.0.1:9876", False)


@pytest.mark.parametrize("raw", ["0", "-1", "18446744073709551616", "0x10000000000000000", "1.5", "bad"])
def test_seed_outside_nonzero_uint64_rejected(raw):
    with pytest.raises(SystemExit):
        options("--seed", raw)


def test_uint64_seeds_differing_only_in_high_bits_remain_distinct():
    args = options("--seed", "0x100000001", "--other-seed", "0x200000001")
    specs = smoke.scenario_matrix(args)
    first, other = specs[2], specs[4]
    assert first["seed"] & 0xFFFFFFFF == other["seed"] & 0xFFFFFFFF
    assert smoke.profile_group(first) != smoke.profile_group(other)
    assert smoke.browser_args(first, "http://127.0.0.1:9876", False) != smoke.browser_args(other, "http://127.0.0.1:9876", False)


def test_explicit_binary_required_and_cli_preserved():
    with pytest.raises(SystemExit):
        smoke.argument_parser().parse_args([])
    args = options("--seed", "42", "--platform", "windows", "--locale", "de-DE", "--output", "report.json")
    assert args.seed == 42 and args.output == Path("report.json")
    assert len(smoke.scenario_matrix(options())) == 10


def test_native_binary_named_chrome_hash_and_no_name_attestation(tmp_path):
    binary = tmp_path / "chrome"
    binary.write_bytes(b"\x7fELFoffline-test-fixture-not-a-browser")
    binary.chmod(0o700)
    identity = smoke.binary_identity(binary)
    assert identity["sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    args = options("--browser", str(binary), "--expected-sha256", "0" * 64)
    report = smoke.run(args)
    assert report["status"] == "failed"
    assert "binary.expected_sha256" in failures(report["checks"])
    assert report["verification"]["provenance_authenticated"] is False
    assert report["verification"]["runtime_verified"] is False
    binary.write_text("#!/bin/sh\nexit 0\n")
    with pytest.raises(smoke.SmokeError, match="native binary"):
        smoke.binary_identity(binary)


@pytest.mark.parametrize("value", ["", "abc", "g" * 64, "a" * 63])
def test_bad_expected_hash(value):
    with pytest.raises(SystemExit):
        options("--expected-sha256", value)


@pytest.mark.parametrize("value", [
    '"Chromium";v=""', '"";v="153"', '"Chromium";v="x"',
    '"Chromium";v="153",', ',"Chromium";v="153"',
    '"Chromium";v="153" "Other";v="1"', '"Chromium";v="153",,"Other";v="1"',
    '"Chromium";v="153";foo="x"', '"Chromium";v="153", "Chromium";v="153"',
    '"Chromium";v=153', '"Chromium";v="153\\n"',
])
def test_strict_brand_headers(value):
    with pytest.raises(ValueError):
        smoke.parse_ch(value, "brands")


def test_ch_valid_versions_empty_strings_and_booleans():
    assert smoke.parse_ch('"Chromium";v="153", "Not;A Brand";v="99"', "brands") == [
        ("Chromium", "153"), ("Not;A Brand", "99")]
    assert smoke.parse_ch('"Chromium";v="153.0.1.2"', "fullVersionList") == [("Chromium", "153.0.1.2")]
    assert smoke.parse_ch('""', "model") == ""
    assert smoke.parse_ch('""', "platformVersion") == ""
    assert smoke.parse_ch("?0", "mobile") is False
    assert smoke.parse_ch("?1", "wow64") is True


@pytest.mark.parametrize("value,field", [("false", "mobile"), ('"?0"', "mobile"), ("?2", "wow64"),
                                          ("", "model"), ('"\\u0041"', "model"), ("null", "model"),
                                          ('""', "uaFullVersion")])
def test_ch_rejects_non_structured_values(value, field):
    with pytest.raises(ValueError):
        smoke.parse_ch(value, field)


def test_scope_headers_are_negotiated_and_bound_to_exact_request():
    spec = scenario()
    sample = scope(spec)
    assert not failures(smoke.evaluate_scope(sample, spec, "window"))
    for header in ["sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", *smoke.HIGH_HEADERS.values()]:
        broken = deepcopy(sample)
        del broken["http"]["headers"][header]
        assert failures(smoke.evaluate_scope(broken, spec, "window"))
    for key, value in [("name", "other-scenario"), ("restart", 2)]:
        other = {**spec, key: value}
        assert "window.http_request_context" in failures(smoke.evaluate_scope(sample, other, "window"))
    assert "window.http_request_context" in failures(smoke.evaluate_scope(sample, spec, "window", "reload"))
    sample["uaData"]["low"]["mobile"] = 0
    assert "window.ch.mobile" in failures(smoke.evaluate_scope(sample, spec, "window"))


@pytest.mark.parametrize("name", ["iframe", "worker"])
@pytest.mark.parametrize("field", ["ua", "languages", "intlLocale", "low", "high"])
def test_cross_scope_comparison_is_strict(name, field):
    sample = observation()
    if field in ("ua", "intlLocale"):
        sample[name][field] += " changed"
    elif field == "languages":
        sample[name][field] = ["en-US"]
    elif field == "low":
        sample[name]["uaData"][field]["brands"][0]["version"] = "154"
    else:
        sample[name]["uaData"][field]["model"] = "different"
    assert f"{name}.matches_window" in failures(smoke.evaluate_observation(sample, scenario()))


def test_off_does_not_assert_requested_platform_or_locale():
    spec = scenario("off")
    checks = smoke.evaluate_scope(scope(spec), spec, "window")
    assert not failures(checks)
    assert not any("requested_" in check["name"] for check in checks)
    assert failures(smoke.evaluate_scope(scope(), scenario("on"), "window"))


@pytest.mark.parametrize("platform,value", [("windows", "Win32"), ("linux", "Linux x86_64")])
def test_normalized_platform_and_network_flags(platform, value):
    flags = smoke.browser_args({**scenario("on"), "platform": platform}, "http://127.0.0.1:9876", False)
    assert f"--fingerprint-platform={value}" in flags
    assert "--disable-background-networking" in flags
    assert "--proxy-bypass-list=<-loopback>;127.0.0.1:9876" in flags
    assert "--no-sandbox" not in flags


@pytest.mark.parametrize("url", ["http://127.0.0.1:9877/", "http://localhost:9876/", "https://127.0.0.1:9876/",
                                 "http://127.0.0.1:9876.evil/", "http://user@127.0.0.1:9876/", "file:///tmp/x",
                                 "data:text/html,test", "blob:http://127.0.0.1:9876/id", "http://[::1]:9876/"])
def test_exact_origin_rejects_other_network_targets(url):
    assert not smoke.allowed_url(url, "http://127.0.0.1:9876")


def test_server_delivers_plain_js_nonrecursive_frame_and_echo():
    with smoke.local_server() as server:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        bodies = {}
        for path in ["/", "/frame", "/worker.js", "/surface.js", "/surface-worker.js", "/timing",
                     "/echo?scenario=one&restart=2&scope=worker&phase=reload"]:
            conn.request("GET", path)
            response = conn.getresponse()
            assert response.status == 200
            assert response.getheader("Accept-CH") == smoke.ACCEPT_CH
            assert server.origin in response.getheader("Content-Security-Policy")
            bodies[path] = response.read().decode()
        assert bodies["/"].count("<iframe") == 1
        assert "<iframe" not in bodies["/frame"]
        assert bodies["/worker.js"] == smoke.WORKER_SCRIPT
        assert bodies["/surface.js"] == smoke.SURFACE_ASSET.read_text()
        assert bodies["/surface-worker.js"] == smoke.SURFACE_WORKER_SCRIPT
        assert bodies["/timing"] == "chromix-loopback-timing"
        echoed = json.loads(bodies[next(path for path in bodies if path.startswith("/echo"))])
        assert "scenario=one&restart=2" in echoed["path"]
        conn.request("GET", "http://external.invalid/", headers={"Host": "external.invalid"})
        rejected = conn.getresponse()
        assert rejected.status == 403
        rejected.read()
        conn.close()


def test_native_control_explicitly_disables_default_persona():
    flags = smoke.browser_args(scenario(), "http://127.0.0.1:9876", False)
    assert "--fingerprint=off" in flags
    assert not any(flag.startswith(("--fingerprint-platform=", "--fingerprint-locale=")) for flag in flags)


def test_server_idle_connection_does_not_block_shutdown():
    import socket
    import time
    start = time.monotonic()
    with smoke.local_server() as server:
        idle = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
    idle.close()
    assert time.monotonic() - start < 3


def test_canvas_result_requires_real_hashes():
    assert not failures(smoke.evaluate_signals(signals()))
    assert "canvas.completed" in failures(smoke.evaluate_signals({"canvas": {}}))
    broken = signals()
    broken["canvas"]["blobHash"] = None
    assert "canvas.hashes" in failures(smoke.evaluate_signals(broken))


def test_collect_waits_for_frame_and_uses_reload(monkeypatch):
    events = []
    frame = SimpleNamespace(wait_for_load_state=lambda *a, **kw: events.append("frame_loaded"))
    page = SimpleNamespace(goto=lambda *a, **kw: events.append("goto"),
                           reload=lambda **kw: events.append("reload"), frame=lambda **kw: frame)
    def evaluate(target, script, argument, timeout):
        assert "frame_loaded" in events
        events.append(argument)
        return {}
    monkeypatch.setattr(smoke, "evaluate", evaluate)
    smoke.collect_page(page, "http://127.0.0.1:9876", 100, scenario(), "initial")
    assert events[:2] == ["goto", "frame_loaded"]
    assert events[2]["scope"] == "window"
    events.clear()
    smoke.collect_page(page, "http://127.0.0.1:9876", 100, scenario(), "reload")
    assert events[:2] == ["reload", "frame_loaded"]
    page.frame = lambda **kw: None
    with pytest.raises(smoke.SmokeError, match="iframe"):
        smoke.collect_page(page, "http://127.0.0.1:9876", 100, scenario(), "initial")


def matrix():
    records = []
    for spec in smoke.scenario_matrix(options("--platform", "windows")):
        marker = "a" if spec["mode"] != "on" else ("b" if spec["seed"] == options().seed else "c")
        records.append({**spec, "profile": "/tmp/fixture/" + smoke.profile_group(spec),
                        "observation": observation(spec, marker=marker)})
    return records


def test_restart_profile_reuse_seed_stability_and_off_native_comparison():
    records = matrix()
    assert not failures(smoke.evaluate_matrix(records))
    records[3]["profile"] += "-different"
    assert "matrix.windows.de-DE.profile_reused" in failures(smoke.evaluate_matrix(records))
    records = matrix()
    records[3]["observation"]["signals"] = signals("d")
    assert "matrix.windows.de-DE.same_seed_restart" in failures(smoke.evaluate_matrix(records))
    records = matrix()
    records[-1]["observation"]["window"]["ua"] = "wrong-native"
    assert "matrix.windows.de-DE.off_matches_native" in failures(smoke.evaluate_matrix(records))
    assert smoke.profile_group(records[2]) == smoke.profile_group(records[3])
    assert smoke.profile_group(records[2]) != smoke.profile_group(records[4])


def test_launch_exception_is_failed_and_uses_persistent_profile(tmp_path):
    calls = []
    def launch(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("fixture launch failed")
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    server = SimpleNamespace(origin="http://127.0.0.1:9876", snapshot=lambda: [])
    profile = tmp_path / "profile"
    result = smoke.run_scenario(playwright, scenario(), {"path": "/explicit/chrome"}, server, options(), profile)
    assert result["status"] == "failed"
    assert result["failures"][0]["message"] == "fixture launch failed"
    assert calls[0]["user_data_dir"] == str(profile)
    assert calls[0]["executable_path"] == "/explicit/chrome"
    assert calls[0]["service_workers"] == "block"


@pytest.mark.parametrize("fault", [None, "crash", "pageerror", "disconnect", "external", "close"])
def test_persistent_context_without_browser_handle_and_runtime_failures(tmp_path, monkeypatch, fault):
    events, page_events, routes = {}, {}, {}
    network_online, network_events, offline_calls = True, [], []
    frame = SimpleNamespace()
    page = SimpleNamespace(on=lambda name, callback: page_events.update({name: callback}),
                           goto=lambda *a, **kw: None, frame=lambda **kw: frame)
    def set_offline(offline):
        nonlocal network_online
        offline_calls.append(offline)
        network_online = not offline
        network_events.append({"type": "offline" if offline else "online", "online": network_online})
    def mocked_evaluate(target, script, argument, timeout):
        if script == smoke.NETWORK_EVENT_SETUP:
            if target is page:
                network_events.clear()
            return {"online": network_online}
        if script == smoke.NETWORK_EVENT_READ:
            return {"online": network_online, "events": deepcopy(network_events)}
        return {}
    detached = []
    identity = {"path": "/explicit/chrome", "sha256": "a" * 64, "size": 100}
    cdp = SimpleNamespace(send=lambda method: {"arguments": ["/explicit/chrome"]}
                          if method == "Browser.getBrowserCommandLine" else {"product": "mock"},
                          detach=lambda: detached.append(True))
    def close():
        if fault == "close":
            raise RuntimeError("close failed")
        events["close"]()
    def new_page():
        events["page"](page)
        return page
    context = SimpleNamespace(browser=None, pages=[], set_default_timeout=lambda value: None,
                              set_default_navigation_timeout=lambda value: None,
                              route=lambda pattern, callback: routes.update(http=callback),
                              route_web_socket=lambda pattern, callback: routes.update(ws=callback),
                              on=lambda name, callback: events.update({name: callback}),
                              new_page=new_page, new_cdp_session=lambda target: cdp, close=close,
                              set_offline=set_offline)
    launches = []
    def launch(**kwargs):
        launches.append(kwargs)
        Path(kwargs["user_data_dir"]).mkdir(exist_ok=True)
        return context
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    server = SimpleNamespace(origin="http://127.0.0.1:9876", snapshot=lambda: [])
    def collect(page, origin, timeout, spec, phase):
        if fault == "crash":
            page_events["crash"]()
        elif fault == "pageerror":
            page_events["pageerror"](RuntimeError("script failed"))
        elif fault == "disconnect":
            events["close"]()
        elif fault == "external":
            aborted = []
            request = SimpleNamespace(url="https://external.invalid/", redirected_from=None)
            routes["http"](SimpleNamespace(request=request, abort=lambda reason: aborted.append(reason)))
            assert aborted == ["blockedbyclient"]
        return observation(spec, phase)
    monkeypatch.setattr(smoke, "binary_identity", lambda path: identity)
    monkeypatch.setattr(smoke, "collect_page", collect)
    monkeypatch.setattr(smoke, "evaluate", mocked_evaluate)
    monkeypatch.setattr(smoke, "evaluate_observation", lambda observation, *_: smoke.evaluate_network_events(observation["network_events"]))
    profile = tmp_path / "profile"
    first = smoke.run_scenario(playwright, scenario(), identity, server, options(), profile)
    second = smoke.run_scenario(playwright, {**scenario(), "restart": 2}, identity, server, options(), profile)
    assert first["status"] == ("passed" if fault is None else "failed")
    assert second["status"] == first["status"]
    assert first["profile_fresh"] is True and second["profile_fresh"] is False
    assert launches[0]["user_data_dir"] == launches[1]["user_data_dir"]
    assert detached == [True, True]
    assert offline_calls == [True, False, True, False]
    assert network_online is True


def test_failed_json_and_exit_without_runtime(tmp_path, capsys, monkeypatch):
    output = tmp_path / "report.json"
    assert smoke.main(["--browser", str(tmp_path / "missing"), "--output", str(output)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report == json.loads(output.read_text())
    assert report["status"] == "failed" and not report["verification"]["runtime_verified"]
    monkeypatch.setattr(smoke, "binary_identity", lambda path: {"path": str(path), "sha256": "a" * 64, "size": 100})
    def missing_playwright():
        raise smoke.SmokeError("Python Playwright package is not installed")
    monkeypatch.setattr(smoke, "load_playwright", missing_playwright)
    report = smoke.run(options())
    assert report["status"] == "failed" and not report["scenarios"]
    assert "Playwright" in report["failures"][0]["message"]


def test_output_cannot_overwrite_binary_hardlink(tmp_path, capsys):
    binary = tmp_path / "chrome"
    binary.write_bytes(b"\x7fELFfixture")
    binary.chmod(0o700)
    alias = tmp_path / "report.json"
    alias.hardlink_to(binary)
    assert smoke.main(["--browser", str(binary), "--output", str(alias)]) == 1
    assert binary.read_bytes() == b"\x7fELFfixture"
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_node_js_syntax_and_mocked_worker_canvas_are_not_browser_evidence():
    if not NODE.is_file():
        pytest.skip("explicit Node executable unavailable; no browser verification")
    scripts = {name: getattr(smoke, name) for name in ["NAV_PROBE", "WORKER_SCRIPT", "WORKER_PROBE", "SIGNAL_PROBE", "MEDIA_PROBE",
                                                      "SURFACE_PROBE", "SURFACE_WORKER_SCRIPT", "NETWORK_EVENT_SETUP", "NETWORK_EVENT_READ"]}
    source = "const scripts = " + json.dumps(scripts) + ";\n" + r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
(async () => {
  for (const [name, script] of Object.entries(scripts))
    new vm.Script(name.endsWith('WORKER_SCRIPT') ? script : '(' + script + ')');
  const low = {brands:[{brand:'Chromium',version:'153'}], mobile:false, platform:'Linux'};
  let posted, fetched;
  const context = {URLSearchParams, Intl, location:{search:'?scenario=one&restart=2&phase=reload'},
    navigator:{userAgent:'mock UA',language:'en-US',languages:['en-US'],platform:'Linux x86_64',
      onLine:true, connection:{effectiveType:'4g',rtt:0,downlink:10,saveData:false},
      storage:{estimate:async()=>({usage:2048,quota:1024,usageDetails:{indexedDB:2048}})},
      userAgentData:{toJSON:()=>low,getHighEntropyValues:async()=>({...low,model:''})}},
    fetch:async (url, options)=>{ fetched = {url,options}; return {ok:true,json:async()=>({path:url})}; },
    postMessage:value=>{assert.equal(typeof value.then,'undefined'); posted = structuredClone(value);}};
  vm.runInNewContext(scripts.WORKER_SCRIPT, context);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(posted.value.ua, 'mock UA');
  assert.equal(posted.value.uaData.high.model, '');
  assert.equal(posted.value.environment.network.rtt, 0);
  assert.equal(posted.value.environment.network.saveData, false);
  assert.equal(posted.value.environment.storage.estimate.usage, 2048);
  assert.equal(posted.value.environment.storage.estimate.quota, 1024);
  const probe = vm.runInNewContext('(' + scripts.NAV_PROBE + ')', context);
  delete context.navigator.connection;
  delete context.navigator.storage;
  let missing = await probe({scope:'window'});
  assert.equal(missing.environment.network.available, false);
  assert.equal(missing.environment.storage.available, false);
  context.navigator.storage = {estimate:async()=>{throw new Error('backend failure');}};
  missing = await probe({scenario:'one',restart:'2',scope:'worker',phase:'reload'});
  assert.equal(missing.environment.storage.error.message, 'backend failure');
  const query = new URL('http://127.0.0.1' + fetched.url).searchParams;
  assert.equal(query.get('scenario'), 'one');
  assert.equal(query.get('restart'), '2');
  assert.equal(query.get('scope'), 'worker');
  assert.equal(fetched.options.redirect, 'error');
  assert.equal(fetched.options.mode, 'same-origin');
  const canvasContext = {crypto:require('node:crypto').webcrypto,TextEncoder,Uint8Array,
    document:{createElement:()=>({getContext:()=>({fillRect(){},
      getImageData:()=>({data:new Uint8Array([1,2,3,4])})}),
      toDataURL:()=> 'data:image/png;base64,AAAA',
      toBlob:callback=>callback(new Blob(['mock pixels']))})}};
  const result = await vm.runInNewContext('(' + scripts.SIGNAL_PROBE + ')()', canvasContext);
  assert.equal(result.canvas.available, true);
  assert.match(result.canvas.pixelHash, /^[a-f0-9]{64}$/);
  assert.equal(result.canvas.pixelHash, result.canvas.repeatHash);
  assert.match(result.canvas.blobHash, /^[a-f0-9]{64}$/);
  assert.equal(result.audio.available, false);
  console.log('Node syntax/mock execution only; not browser runtime evidence');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([str(NODE), "-"], input=source, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not browser runtime evidence" in result.stdout
