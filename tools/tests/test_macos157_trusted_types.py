"""Probe real Chromium DOM and Trusted Types without weakening declaration checks."""
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
from unittest import mock

import pytest

from tools import macos_restored_generators as gen

FIXTURES = Path(__file__).parent / "fixtures"
DECLARATIONS = json.loads((FIXTURES / "macos157_trusted_types.json").read_text())


def write_declarations(src):
    for name, expected in gen.TRUSTED_TYPES157.items():
        entry = DECLARATIONS["files"][name]
        assert gen.sha256(entry["text"].encode()) == expected
        path = src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(entry["text"].encode())


def test_exact_declarations_survive_both_donors_and_are_production_dependencies():
    for name, expected in gen.TRUSTED_TYPES157.items():
        for arch in ("arm64", "x64"):
            assert DECLARATIONS["donors"][arch]["files"][name]["sha256"] == expected
    for name in ("tools/typescript/tsconfig_base.json", "tools/typescript/tsconfig_definitions_base.json"):
        entry = DECLARATIONS["files"][name]
        assert gen.sha256(entry["text"].encode()) == entry["sha256"]
        options = json.loads(entry["text"])["compilerOptions"]
        assert options["types"] == ["trusted-types"]
        assert options["typeRoots"][0].endswith("third_party/node/node_modules/@types")


@pytest.mark.parametrize("name", tuple(gen.TRUSTED_TYPES157))
@pytest.mark.parametrize("failure", ["missing", "wrong", "symlink"])
def test_missing_wrong_or_linked_declarations_fail_before_executing_tools(tmp_path, name, failure):
    write_declarations(tmp_path)
    path = tmp_path / name
    if failure == "missing":
        path.unlink()
    elif failure == "wrong":
        path.write_bytes(path.read_bytes() + b"\n// changed")
    else:
        outside = tmp_path / "outside"
        path.rename(outside)
        path.symlink_to(outside)
    with mock.patch("tools.prepare_restored_build.host_identity", return_value=("macos", "arm64")), \
            mock.patch.object(gen.subprocess, "run") as run, pytest.raises((ValueError, OSError)):
        gen.smoke(tmp_path, "arm64", pins=gen.PINS157)
    run.assert_not_called()


@pytest.fixture
def real_compiler(tmp_path):
    node = os.environ.get("CHROMIX_TEST_NODE") or shutil.which("node")
    if not node:
        pytest.skip("native Node required for actual TypeScript compilation")
    package = json.loads(gzip.decompress((FIXTURES / "macos157_official_typescript.json.gz").read_bytes()))
    root = tmp_path / "src"
    for name, mode, text in package["donor_files"]:
        path = root / gen.package_path("node-typescript157") / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
        path.chmod(mode)
    write_declarations(root)
    assert gen.package_identity(root, "node-typescript157")["variant"] == "chromium157-official-patched-pruned-domains"
    return root, node, str(root / gen.package_path("node-typescript157") / "bin/tsc")


def test_actual_official_compiler_positive_and_negative_dom_types(real_compiler, tmp_path):
    src, node, tsc = real_compiler
    project = tmp_path / "project"
    project.mkdir()
    options = {"target": "es2023", "module": "commonjs", "strict": True,
               "noEmitOnError": True, "skipLibCheck": False, "libReplacement": False,
               "outDir": "out", "types": ["trusted-types"],
               "typeRoots": [str(src / "third_party/node/node_modules/@types")]}
    config = project / "tsconfig.json"
    config.write_text(json.dumps({"compilerOptions": options, "files": ["sample.ts"]}))
    sample = project / "sample.ts"
    sample.write_text("export function apply(html: TrustedHTML, url: TrustedScriptURL): void {\n"
                      "  document.body.innerHTML = html;\n"
                      "  document.createElement('script').src = url;\n"
                      "}\nconsole.log(42);\n")
    env = {k: v for k, v in os.environ.items() if k not in ("NODE_OPTIONS", "NODE_PATH")}
    env["PATH"] = ""

    def compile_sample():
        return subprocess.run([node, tsc, "--project", str(config)], cwd=project, env=env,
                              text=True, capture_output=True, timeout=60)

    result = compile_sample()
    assert result.returncode == 0, result.stdout + result.stderr
    emitted = project / "out/sample.js"
    assert subprocess.check_output([node, str(emitted)], env=env, text=True).strip() == "42"
    emitted.unlink()
    sample.write_text("const invalid: TrustedHTML = 'plain text';\n"
                      "const wrongKind: TrustedScriptURL = {} as TrustedHTML;\n")
    result = compile_sample()
    assert result.returncode != 0 and "TS2322" in result.stdout
    assert not emitted.exists()
    sample.write_text("console.log(42);\n")
    options["types"] = []
    config.write_text(json.dumps({"compilerOptions": options, "files": ["sample.ts"]}))
    result = compile_sample()
    assert result.returncode != 0 and "TS2304" in result.stdout
    assert "TrustedHTML" in result.stdout and "TrustedScriptURL" in result.stdout
    assert not emitted.exists()


def test_real_native_smoke_and_156_probe_unchanged():
    location = os.environ.get("CHROMIX_TEST_MACOS_GENERATOR_SOURCE")
    if not location:
        pytest.skip("verified native donor/tool tree required for full native smoke")
    src = Path(location).resolve()
    before = gen.package_identity(src, "node-typescript157")
    current = gen.smoke(src, "arm64", pins=gen.PINS157)
    assert current["node_typescript"] == "Version 6.0.2"
    for name, expected in gen.TRUSTED_TYPES157.items():
        assert current["tools"][name]["sha256"] == expected
    historical = gen.smoke(src, "arm64", pins=gen.PINS156)
    assert "node_typescript" not in historical
    assert not set(gen.TRUSTED_TYPES157).intersection(historical["tools"])
    assert gen.package_identity(src, "node-typescript157") == before


def test_trusted_declaration_identity_only_changes_157_manifest():
    old = {pins: gen.manifest_digest(pins) for pins in (gen.PINS, gen.PINS155, gen.PINS156)}
    current = gen.manifest_digest(gen.PINS157)
    with mock.patch.dict(gen.TRUSTED_TYPES157, {next(iter(gen.TRUSTED_TYPES157)): "0" * 64}):
        assert gen.manifest_digest(gen.PINS157) != current
        assert {pins: gen.manifest_digest(pins) for pins in old} == old
