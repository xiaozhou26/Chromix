"""Exact Windows157 source payloads, ARM cache provenance and x64-only fresh entry."""
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import fetch_upstream_cache as cache
import gen_windows_arm64_workflow as generator
import import_upstream_cache as importer
import install_windows_metagen_bindings as metagen
import repair_windows_midl as midl
import windows157_fresh as fresh
import windows157_tools as tools
from tools.tests.test_windows154_midl import graph
from tools.tests.test_windows_metagen_bindings import pe_dll

AUDIT = json.loads(gzip.decompress((ROOT / "tools/tests/fixtures/windows157_sources.json.gz").read_bytes()))
FILES = AUDIT["files"]
NOW = datetime(2026, 11, 19, 12, 47, tzinfo=timezone.utc)


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


def substitute(text):
    for line in FILES["core/domain_regex.list"]["text"].splitlines():
        if line:
            pattern, replacement = line.split("#")
            text = re.sub(pattern, replacement, text)
    return text


def sources(src, *, domain=False):
    put(src / "chrome/VERSION", FILES["chromium/chrome/VERSION"]["text"])
    put(src / "DEPS", FILES["chromium/DEPS"]["text"])
    names = FILES["windows/domain_substitution.list"]["text"].splitlines()
    for relative, item in FILES.items():
        if relative.startswith("v8/tools/metagen/"):
            data = substitute(item["text"]) if domain and relative in names else item["text"]
            put(src / relative, data)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path.resolve() / "repo"
    for name in ("build/upstream-cache.json", "build/ungoogled-revisions.psd1", "CHROMIUM_VERSION",
                 "CHROMIUM_WINDOWS_VERSION", "CHROMIUM_LINUX_VERSION", "CHROMIUM_MACOS_VERSION"):
        put(path / name, (ROOT / name).read_bytes())
    return path


def test_payload_urls_and_all_actual_hashes():
    assert (AUDIT["chromium_version"], AUDIT["core"], AUDIT["windows"], AUDIT["v8"]) == (
        fresh.VERSION, fresh.CORE, fresh.WINDOWS, metagen.V8_157_REVISION)
    for relative, item in FILES.items():
        assert item["url"].startswith("https://raw.githubusercontent.com/")
        assert hashlib.sha256(item["text"].encode()).hexdigest() == item["sha256"], relative
    for name, digest in metagen.V8_157_HASHES.items():
        assert FILES["v8/tools/metagen/" + name]["sha256"] == digest
    assert set(metagen.V8_157_HASHES) == set(metagen.V8_156_HASHES) | {"layout_ir.py", "layout_extract.py"}
    for name, digest in tools.CRUBIT_INPUTS.items():
        assert FILES["crubit/" + name]["sha256"] == digest
    for name, digest in tools.TOOLING.items():
        assert FILES["windows/" + name]["sha256"] == digest
    assert 'refs/branch-heads/8103@' in AUDIT['chromium_commit']['message']
    assert not re.search(r'refs/branch-heads/\d+_\d+', AUDIT['chromium_commit']['message'])
    assert FILES['chromium/chrome/build/win64.pgo.txt']['text'].strip() == fresh.PROFILES['x64']
    assert FILES['chromium/chrome/build/win-arm64.pgo.txt']['text'].strip() == tools.PGO['arm64']
    assert "SDK_VERSION = '10.0.28000.0'" in FILES['chromium/build/vs_toolchain.py']['text']


def test_arm_only_manifest_and_exact_live_metadata(repo):
    pin, identity = cache.load_manifest("windows", "arm64", root=repo)
    assert pin["run_id"] == 43476895318
    assert pin["run_attempt"] == 1 and pin["producer_job_id"] == 132865547833
    assert pin["artifact"]["id"] == 14178413147
    assert pin["artifact"]["size_in_bytes"] == 16438090945
    meta = AUDIT["metadata"]
    cache.validate_metadata(pin, meta["arm_run"], meta["arm_artifact"], NOW, meta["arm_job"])
    with pytest.raises(cache.CacheMiss, match="source_unavailable") as error:
        cache.load_manifest("windows", "x64", root=repo)
    assert error.value.details["manifest"]["available"] is False
    assert "artifact_id" not in error.value.details["manifest"]
    actual, _ = importer.repository_identity(repo, "windows", "arm64")
    assert actual == dict(midl.WINDOWS157_RECORDS[0]["identity"])
    assert identity["artifact_digest"] == actual["artifact_digest"]


@pytest.mark.parametrize("field,value", [("run_attempt", 2), ("id", 1), ("conclusion", "failure"),
                                          ("head_sha", "f" * 40)])
def test_wrong_producer_is_not_a_fallback(repo, field, value):
    pin, _ = cache.load_manifest("windows", "arm64", root=repo)
    meta = deepcopy(AUDIT["metadata"])
    meta["arm_job"][field] = value
    with pytest.raises(cache.CacheMiss):
        cache.validate_metadata(pin, meta["arm_run"], meta["arm_artifact"], NOW, meta["arm_job"])


def test_expiry_is_fail_closed_and_missing_x64_never_downloads(repo, tmp_path):
    pin, _ = cache.load_manifest("windows", "arm64", root=repo)
    meta = AUDIT["metadata"]
    with pytest.raises(cache.CacheMiss, match="artifact_expired"):
        cache.validate_metadata(pin, meta["arm_run"], meta["arm_artifact"],
                                datetime(2026, 11, 19, 20, 14, 30, tzinfo=timezone.utc), meta["arm_job"])
    client = mock.Mock()
    result = cache.fetch("windows", "x64", tmp_path.resolve() / "cache", root=repo, client=client)
    assert result["status"] == "miss" and result["reason"] == "source_unavailable"
    client.json.assert_not_called()
    client.download.assert_not_called()


@pytest.mark.parametrize("change", ["omit", "overlap", "reason", "fake", "other-platform"])
def test_unavailable_arch_manifest_cannot_hide_or_invent_artifacts(repo, change):
    path = repo / "build/upstream-cache.json"
    manifest = json.loads(path.read_text())
    source = manifest["sources"]["windows"]
    if change == "omit":
        del source["unavailable_arches"]
    elif change == "overlap":
        source["unavailable_arches"] = {"arm64": "artifact_unavailable"}
    elif change == "reason":
        source["unavailable_arches"]["x64"] = "download_allowed"
    elif change == "fake":
        source["artifacts"]["x64"] = dict(source["artifacts"]["arm64"])
    else:
        manifest["sources"]["linux"]["unavailable_arches"] = {"x64": "artifact_unavailable"}
    path.write_text(json.dumps(manifest))
    with pytest.raises(cache.CacheMiss):
        cache.load_manifest("windows", "arm64", root=repo)


@pytest.mark.parametrize("domain", [False, True])
def test_exact_metagen_sources_and_domain_transformation(tmp_path, domain):
    src = tmp_path.resolve()
    sources(src, domain=domain)
    expected = metagen.V8_157_DOMAIN_HASHES if domain else metagen.V8_157_HASHES
    assert metagen.validate_sources(src, fresh.VERSION) == expected
    raw = FILES["v8/tools/metagen/compile_flags.py"]["text"]
    assert hashlib.sha256(substitute(raw).encode()).hexdigest() == metagen.V8_157_DOMAIN_HASHES["compile_flags.py"]
    for name in expected:
        path = src / "v8/tools/metagen" / name
        original = path.read_bytes()
        path.write_bytes(original + b"# mutation\n")
        with pytest.raises(ValueError, match="SHA256"):
            metagen.validate_sources(src, fresh.VERSION)
        path.write_bytes(original)
    put(src / "DEPS", "'v8_revision': '" + metagen.V8_156_REVISION + "'")
    with pytest.raises(ValueError, match="DEPS"):
        metagen.validate_sources(src, fresh.VERSION)


def command(src):
    library = src / 'third_party/llvm-build/Release+Asserts/bin/libclang.dll'
    bindings = src / 'third_party/llvm-build/Release+Asserts/bindings/python'
    resource = src / 'third_party/llvm-build/Release+Asserts/lib/clang/23'
    return (f'"{sys.executable}" ../../v8/tools/metagen/metagen.py --driver-mode=cl '
            f'--clang-resource-dir "{resource}" --libclang-so "{library}" '
            f'--libclang-bindings-dir "{bindings}" --expect-libclang-major 23')


def test_actual_157_cli_requires_cl_and_all_layout_modules(tmp_path):
    src = tmp_path.resolve()
    out = src / "out/Default"
    good = command(src)
    assert metagen.action_contract(good, out, fresh.VERSION)["script"] == src / 'v8/tools/metagen/metagen.py'
    for bad in (good.replace('--driver-mode=cl', ''), good.replace('--driver-mode=cl', '--driver-mode=gcc'),
                good + ' --driver-mode=cl', good + ' --clang-builtin-headers-dir old',
                good.replace('clang/23', 'clang/24'), good + '\n' + good):
        with pytest.raises(ValueError):
            metagen.action_contract(bad, out, fresh.VERSION)
    text = FILES['v8/BUILD.gn']['text']
    assert '"--driver-mode=cl"' in text and '"$_metagen_out_dir/layouts.json"' in text
    assert '"tools/metagen/layout_ir.py"' in text and '"tools/metagen/layout_extract.py"' in text


def test_relocate_actual_donor_bindings_without_pgo_change(tmp_path):
    src = tmp_path.resolve()
    sources(src, domain=True)
    out = src / 'out/Default'
    relative = 'third_party/llvm-build/Release+Asserts/bin/libclang.dll'
    put(src / relative, pe_dll())
    path = put(out / 'args.gn', 'target_cpu="arm64"\nhost_cpu="x64"\nchrome_pgo_phase=2\n'
               f'v8_metagen_libclang_so="C:/ungoogled-chromium-windows/build/src/{relative}"\n'
               'v8_metagen_libclang_bindings_dir="C:/hostedtoolcache/windows/Python/3.12.10/x64/Lib/site-packages"\n')
    metagen.configure(src, out, fresh.VERSION)
    assert 'chrome_pgo_phase=2' in path.read_text()
    assert 'hostedtoolcache' not in path.read_text()
    assert str(src) in path.read_text()
    before = path.read_bytes(), path.stat().st_mtime_ns
    metagen.configure(src, out, fresh.VERSION)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_midl_reconstructed_from_real_157_patch_and_domain_rules(tmp_path):
    src = tmp_path.resolve()
    path = put(src / midl.SCRIPT, FILES['chromium/build/toolchain/win/midl.py']['text'])
    patch = FILES['windows/patches/ungoogled-chromium/windows/windows-disable-win-build-output.patch']['text']
    patch = patch.split('--- a/build/win/message_compiler.py')[0]
    subprocess.run(['patch', '-p1', '--fuzz=0', '--batch'], input=patch, text=True, cwd=src, check=True, capture_output=True)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == '501ad1740d960ab1f2afeb8278162b05163e3772edb6ea167554f67ef82ca99c'
    fresh.midl(src)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == 'f113aabeee53f6ba0fce6d364d45d294b094b656afc03e24029bf7503fb99b3a'
    # Forward-domain donor bytes and repaired bytes are independently reproduced.
    path.write_text(substitute(path.read_text()))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == midl.WINDOWS157_REPAIRED_SHA256
    before = path.read_bytes().replace(midl.AFTER, midl.BEFORE).replace(midl.COMPARE_AFTER, midl.COMPARE_BEFORE)
    assert hashlib.sha256(before).hexdigest() == midl.WINDOWS157_ORIGINAL_SHA256
    path.write_bytes(before)
    out = src / 'out/Default'
    put(out / 'build.ninja', graph(['gen/midl/a.tlb']))
    output = put(out / 'gen/midl/a.tlb', b'stale')
    untouched = put(out / 'gen/retain.obj', b'keep')
    source_tlb = put(src / 'third_party/win_build_output/midl/arm64/source.tlb', b'')
    identity = dict(midl.WINDOWS157_RECORDS[0]['identity'])
    result = midl.apply(src, 'windows', 'arm64', identity)
    assert result['invalidated_outputs'] == ['gen/midl/a.tlb']
    assert not output.exists() and untouched.read_bytes() == b'keep' and source_tlb.read_bytes() == b''
    output.write_bytes(b'regenerated')
    assert midl.apply(src, 'windows', 'arm64', identity)['status'] == 'verified'
    assert output.read_bytes() == b'regenerated'
    for key, value in identity.items():
        alternate = value + 1 if type(value) is int else 'invalid'
        if key == 'chromium_version':
            alternate = '157.0.8103.50'
        with pytest.raises(ValueError):
            midl.apply(src, 'windows', 'arm64', dict(identity, **{key: alternate}))
        assert output.read_bytes() == b'regenerated'
    with pytest.raises(ValueError):
        midl.apply(src, 'windows', 'x64', dict(identity, arch='x64'))


def test_fresh_x64_only_identity_and_no_156_profile_reuse(repo, tmp_path):
    work = tmp_path.resolve() / 'work'
    with pytest.raises(ValueError, match='x64 only'):
        fresh.initialize(repo, work, 'arm64', resume=False)
    assert not work.exists()
    fresh.initialize(repo, work, 'x64', resume=False)
    assert json.loads((work / fresh.RECEIPT).read_text())['chromium_version'] == fresh.VERSION
    put(work / 'src/.chromix-source-ready', fresh.VERSION)
    fresh.overlay(repo, work, 'x64')
    assert 'chrome_pgo_phase = 2' in (work / 'windows157-native.gn').read_text()
    put(work / 'src/chrome/build/win64.pgo.txt', 'wrong156.profdata')
    with mock.patch.object(fresh.urllib.request, 'urlopen') as download:
        with pytest.raises(ValueError, match='exact Chromium157'):
            fresh.profile(work / 'src', 'x64')
        download.assert_not_called()
    with pytest.raises(ValueError, match='receipt missing'):
        fresh.initialize(repo, tmp_path.resolve() / 'absent', 'x64', resume=True)


def test_generated_x64_only_fresh_and_arm_no_cold_contract():
    workflow = generator.fresh_workflow('x64', 157)
    assert set(workflow['on']) == {'workflow_dispatch'}
    assert workflow['on']['workflow_dispatch']['inputs']['use_upstream_cache']['default'] is True
    assert (ROOT / '.github/workflows/build-win-x64-157-fresh.yml').read_text() == generator.render_fresh('x64', 157)
    with pytest.raises(ValueError):
        generator.fresh_workflow('arm64', 157)
    for i in range(1, 13):
        job = workflow['jobs'][f'build-{i}']
        assert job['name'] == f'Windows157 x64 stage {i}' and job['runs-on'] == 'windows-2022'
        stage = next(s for s in job['steps'] if s.get('id') == 'stage')
        assert '-FreshNative' in stage['run'] and '-FromArtifact' not in stage['run']
        assert any(s.get('name') == 'Require explicit Windows157 fresh authorization' for s in job['steps'])
    arm = generator.workflow()
    assert arm['jobs']['verify-arm64']['runs-on'] == 'windows-11-arm'
    ci = (ROOT / 'build/windows/ci-stage.ps1').read_text()
    guard = ci.split('if ($Revisions.ChromiumVersion -eq "157.0.8103.78" -and $Arch -eq "arm64")')[1].split('\n}', 1)[0]
    assert '$RequireUpstreamCache = $true' in guard and '$FreshNative' in guard and 'native' in guard
    assert 'verify_patch_stack.py' in ci and 'windows157_tools.py' in ci
    assert 'historical fresh entries cannot change versions' in ci
    assert 'build-win-x64-157-fresh[.]yml@refs/heads/' in ci
    assert ci.index('Windows157 Crubit/MINIBRANCH') < ci.index('& $gn gen')
    assert ci.index('& $gn gen') < ci.index('Windows157 native host generators/PGO')


def test_host_pe_rejects_wrong_arch_and_missing_dlls(tmp_path):
    payload = bytearray(pe_dll())
    struct.pack_into('<H', payload, 86, 2)
    executable = put(tmp_path.resolve() / 'tool.exe', payload)
    tools.pe_executable(executable)
    struct.pack_into('<H', payload, 68, 0xAA64)
    executable.write_bytes(payload)
    with pytest.raises(ValueError, match='AMD64'):
        tools.pe_executable(executable)
    executable.write_bytes(pe_dll())
    with pytest.raises(ValueError, match='executable'):
        tools.pe_executable(executable)


def host_tree(src):
    sources(src, domain=True)
    payload = bytearray(pe_dll())
    struct.pack_into('<H', payload, 86, 2)
    for relative in ('third_party/node/win/node.exe', 'third_party/ninja/ninja.exe',
                     'third_party/llvm-build/Release+Asserts/bin/clang-cl.exe',
                     'third_party/llvm-build/Release+Asserts/bin/llvm-profdata.exe',
                     'third_party/dawn/tools/golang/windows-amd64/bin/go.exe',
                     'third_party/devtools-frontend/src/third_party/esbuild/esbuild.exe',
                     'third_party/typescript/windows-amd64/src/lib/tsc.exe',
                     tools.CRUBIT_ROOT + '/bin/cc_bindings_from_rs.exe'):
        put(src / relative, payload)
    for name in tools.CRUBIT_INPUTS:
        put(src / tools.CRUBIT_ROOT / 'lib/third_party/crubit' / name, FILES['crubit/' + name]['text'])
    put(src / tools.CRUBIT_ROOT / 'bin/rustc_driver-157.dll', pe_dll())
    put(src / 'third_party/rust-toolchain/bin/rustc_driver-157.dll', pe_dll())
    put(src / 'build/util/MINIBRANCH', tools.MINIBRANCH)
    put(src / 'third_party/typescript/windows-amd64/src/lib/lib.dom.d.ts',
        'set innerHTML(html: string|TrustedHTML);\n' * 2 +
        'set src(url: string|TrustedScriptURL);\nhidden: boolean;\n')
    return src


@pytest.mark.parametrize('arch', ['x64', 'arm64'])
def test_host_generators_validate_real_input_pins_and_pgo_without_download(tmp_path, arch):
    src = host_tree(tmp_path.resolve())
    out = src / 'out/Default'
    put(out / 'args.gn', f'target_cpu="{arch}"\nhost_cpu="x64"\nchrome_pgo_phase=2\n')
    target = 'win64' if arch == 'x64' else 'win-arm64'
    put(src / f'chrome/build/{target}.pgo.txt', tools.PGO[arch])
    put(src / 'chrome/build/pgo_profiles' / tools.PGO[arch], b'synthetic profile, not executable')
    runtime = dict(platform='win32', bits=64, process_machine=0x8664, native_machine=0x8664)
    with mock.patch.object(metagen, 'runtime_info', return_value=runtime), \
            mock.patch.object(tools.subprocess, 'run', side_effect=[SimpleNamespace(stdout=value) for value in
                              ['v24.12.0', '0.28.2', 'Version 7.0.2', '', '']]) as run:
        tools.probe(src, out, arch)
        assert run.call_count == 5
        assert run.call_args_list[2].args[0][0].endswith('windows-amd64/src/lib/tsc.exe')
        assert run.call_args_list[3].args[0][-1].endswith(tools.PGO[arch])
    for name in ('chrome_pgo_phase', 'target_cpu', 'host_cpu'):
        original = (out / 'args.gn').read_text()
        bad = original + f'{name}="untrusted"\n'
        (out / 'args.gn').write_text(bad)
        with mock.patch.object(metagen, 'runtime_info', return_value=runtime), \
                mock.patch.object(tools.subprocess, 'run') as run:
            with pytest.raises(ValueError):
                tools.probe(src, out, arch)
            run.assert_not_called()
        (out / 'args.gn').write_text(original)


@pytest.mark.parametrize('mutation', ['cargo', 'runtime', 'minibranch', 'typescript', 'layout'])
def test_157_dependency_mutations_fail_before_native_execution(tmp_path, mutation):
    src = host_tree(tmp_path.resolve())
    paths = {
        'cargo': src / tools.CRUBIT_ROOT / 'lib/third_party/crubit/Cargo.toml',
        'runtime': src / tools.CRUBIT_ROOT / 'bin/rustc_driver-157.dll',
        'minibranch': src / 'build/util/MINIBRANCH',
        'typescript': src / 'third_party/typescript/windows-amd64/src/lib/lib.dom.d.ts',
        'layout': src / 'v8/tools/metagen/layout_ir.py',
    }
    tools.dependencies(src)
    paths[mutation].write_bytes(b'changed')
    with mock.patch.object(tools.subprocess, 'run') as run:
        with pytest.raises(ValueError):
            tools.dependencies(src)
        run.assert_not_called()


def test_metagen_install_checks_both_actual_outputs_and_rejects_late_changes(tmp_path):
    src = tmp_path.resolve()
    sources(src)
    out = src / 'out/Default'
    put(out / 'args.gn', 'target_cpu="arm64"\nhost_cpu="x64"\nchrome_pgo_phase=2\n')
    library = put(src / 'third_party/llvm-build/Release+Asserts/bin/libclang.dll', pe_dll())
    put(src / 'third_party/llvm-build/Release+Asserts/lib/clang/23/include/stddef.h', 'header')
    runtime = dict(platform='win32', bits=64, process_machine=0x8664, native_machine=0x8664, site_packages=[])
    with mock.patch.object(metagen.subprocess, 'run', return_value=SimpleNamespace(stdout=command(src))) as run, \
            mock.patch.object(metagen, 'run_json', side_effect=[runtime, {'api_preflight': 'passed'}]), \
            mock.patch.object(metagen, 'provision', return_value='pinned'):
        result = metagen.install(src, out, Path('ninja'), fresh.VERSION, download=False)
        assert result['scripts_sha256'] == metagen.V8_157_HASHES
        assert run.call_args_list[0].args[0][-1] == 'gen/v8/metagen/instance-types.h'
        assert run.call_args_list[1].args[0][-1] == 'gen/v8/metagen/layouts.json'
    def late(argv, *, cwd):
        if '--probe' in argv:
            library.write_bytes(b'mutated DLL')
            return {'api_preflight': 'passed'}
        return runtime
    with mock.patch.object(metagen.subprocess, 'run', return_value=SimpleNamespace(stdout=command(src))), \
            mock.patch.object(metagen, 'run_json', side_effect=late), \
            mock.patch.object(metagen, 'provision', return_value='pinned'):
        with pytest.raises(ValueError, match='DLL changed'):
            metagen.install(src, out, Path('ninja'), fresh.VERSION, download=False)


def test_metagen_configuration_rejects_foreign_binding_without_partial_edit(tmp_path):
    src = tmp_path.resolve()
    sources(src)
    relative = 'third_party/llvm-build/Release+Asserts/bin/libclang.dll'
    put(src / relative, pe_dll())
    out = src / 'out/Default'
    path = put(out / 'args.gn', 'target_cpu="arm64"\nchrome_pgo_phase=2\n'
               f'v8_metagen_libclang_so="C:/ungoogled-chromium-windows/build/src/{relative}"\n'
               'v8_metagen_libclang_bindings_dir="C:/unknown/bindings"\n')
    before = path.read_bytes()
    with pytest.raises(ValueError, match='unverified'):
        metagen.configure(src, out, fresh.VERSION)
    assert path.read_bytes() == before


def test_fresh_requires_unavailable_manifest_not_just_version_strings(repo, tmp_path):
    path = repo / 'build/upstream-cache.json'
    original = path.read_text()
    manifest = json.loads(original)
    manifest['sources']['windows']['head_sha'] = 'f' * 40
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='exact x64 cache unavailability'):
        fresh.initialize(repo, tmp_path.resolve() / 'work', 'x64', resume=False)
    path.unlink()
    with pytest.raises(OSError):
        fresh.initialize(repo, tmp_path.resolve() / 'work', 'x64', resume=False)


def test_157_rust_merge_preserves_downloaded_crubit_and_adds_host_dev(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location('rust157', ROOT / 'build/windows/prep_rust_toolchain.py')
    rust = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rust)
    root = tmp_path.resolve()
    for name in ('cargo.exe', 'rustc.exe'):
        put(root / 'rust-toolchain-x64/rustc/bin' / name, b'host-x64')
    cargo = put(root / 'rust-toolchain/crubit/lib/third_party/crubit/Cargo.toml', FILES['crubit/Cargo.toml']['text'])
    lib = put(root / 'rust-toolchain-x64-dev/rustc-dev/lib/rustlib/x86_64-pc-windows-msvc/lib/librustc_driver.rlib', b'dev')
    argv = ['prep_rust_toolchain.py', '--third-party-root', str(root), '--arch', 'x64', '--chromium-version', fresh.VERSION]
    with mock.patch.object(sys, 'argv', argv), mock.patch.object(rust, 'write_installed_version'):
        rust.main()
    assert cargo.read_text() == FILES['crubit/Cargo.toml']['text']
    assert (root / 'rust-toolchain/lib/rustlib/x86_64-pc-windows-msvc/lib/librustc_driver.rlib').read_bytes() == lib.read_bytes()
    assert (root / 'rust-toolchain/bin/cargo.exe').read_bytes() == b'host-x64'
    with mock.patch.object(sys, 'argv', argv), pytest.raises(SystemExit):
        rust.main()
    assert cargo.exists()


def test_prepared_crubit_is_not_rebuilt_and_tampered_inputs_fail_first(tmp_path):
    work = tmp_path.resolve()
    src = host_tree(work / 'src')
    put(src / 'out/Chromix/args.gn', 'target_cpu="x64"\nhost_cpu="x64"\nchrome_pgo_phase=2\n')
    for name in tools.TOOLING:
        put(work / 'tooling/ungoogled-chromium-windows' / name, FILES['windows/' + name]['text'])
    runtime = dict(platform='win32', bits=64, process_machine=0x8664, native_machine=0x8664)
    with mock.patch.object(metagen, 'runtime_info', return_value=runtime), mock.patch.object(tools.subprocess, 'run') as run:
        tools.prepare(src, work)
        assert len(run.call_args_list) == 1 and run.call_args.args[0][-1] == '--help'
    file = work / 'tooling/ungoogled-chromium-windows/build.py'
    file.write_bytes(file.read_bytes() + b'# changed')
    with mock.patch.object(metagen, 'runtime_info', return_value=runtime), mock.patch.object(tools.subprocess, 'run') as run:
        with pytest.raises(ValueError, match='SHA256'):
            tools.prepare(src, work)
        run.assert_not_called()


def test_restored_generator_fingerprint_covers_native_ts_dom_esbuild_crubit(tmp_path):
    src = host_tree(tmp_path.resolve())
    first = tools.restored_fingerprint(src)
    assert set(first) == {"typescript", "typescript_dom", "esbuild", "crubit"}
    path = src / first["typescript_dom"]["path"]
    path.write_bytes(path.read_bytes() + b"// modified declaration\n")
    second = tools.restored_fingerprint(src)
    assert first["typescript_dom"] != second["typescript_dom"]
    path.unlink()
    with pytest.raises(ValueError):
        tools.restored_fingerprint(src)


def test_157_midl_cannot_be_silently_skipped(tmp_path):
    with pytest.raises(ValueError, match="MIDL script is missing"):
        midl.apply(tmp_path.resolve(), "windows", "arm64", dict(midl.WINDOWS157_RECORDS[0]["identity"]))
