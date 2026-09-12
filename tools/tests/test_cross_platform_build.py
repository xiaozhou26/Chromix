import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
PACKAGE_MACOS = REPO / "build" / "macos" / "package-macos.sh"
PREPARE = REPO / "build" / "prepare-ungoogled.sh"
WORKFLOWS = REPO / ".github" / "workflows"
REVISIONS = REPO / "build" / "ungoogled-revisions.psd1"


class CrossPlatformBuildRegressionTest(unittest.TestCase):
    def test_prepare_uses_pinned_core_archive_and_platform_layers(self):
        source = PREPARE.read_text(encoding="utf-8")
        self.assertIn("PLATFORM_KEY=UngoogledLinux", source)
        self.assertIn("PLATFORM_KEY=UngoogledMacOS", source)
        self.assertIn('utils/downloads.py" retrieve', source)
        self.assertIn('utils/downloads.py" unpack', source)
        self.assertIn('utils/prune_binaries.py', source)
        self.assertIn('utils/patches.py" apply "$SRC" "$CORE_REPO/patches"', source)
        self.assertIn('utils/patches.py" apply "$SRC" "$PLATFORM_PATCHES"', source)
        self.assertLess(source.index('utils/patches.py" apply "$SRC" "$CORE_REPO/patches"'), source.index('utils/patches.py" apply "$SRC" "$PLATFORM_PATCHES"'))
        self.assertLess(source.index('utils/patches.py" apply "$SRC" "$PLATFORM_PATCHES"'), source.index('utils/prune_binaries.py'))
        self.assertLess(source.index('utils/prune_binaries.py'), source.index('"$REPO/build/apply-patches.sh" "$SRC"'))
        self.assertIn('"$REPO/build/apply-patches.sh" "$SRC"', source)
        self.assertNotIn("fetch --nohooks", source)
        self.assertNotIn("gclient sync", source)

    def test_domain_substitution_runs_after_platform_tool_downloads(self):
        linux = (REPO / "build" / "build.sh").read_text(encoding="utf-8")
        macos = (REPO / "build" / "macos" / "build.sh").read_text(encoding="utf-8")
        for source in (linux, macos):
            self.assertIn("CHROMIX_APPLY_DOMAIN_SUBSTITUTION:-1", source)
            self.assertIn("utils/domain_substitution.py", source)

    def test_platform_commits_are_explicitly_pinned(self):
        source = REVISIONS.read_text(encoding="utf-8")
        self.assertIn(
            'UngoogledLinuxCommit = "02c59ed68d1963a647bb478064823d114e466ffb"',
            source,
        )
        self.assertIn(
            'UngoogledMacOSCommit = "038db2b41f7aeb00bbceb2f5a56912b26eb5b284"',
            source,
        )

    def test_workflow_matches_sdk_asset_names(self):
        source = "\n".join((WORKFLOWS / f"build-{platform}-{arch}.yml").read_text()
                           for platform in ("linux", "macos") for arch in ("x64", "arm64"))
        posix_workflow = REPO / ".github" / "workflows" / "build-posix-github.yml"
        posix_source = posix_workflow.read_text(encoding="utf-8")
        for asset in (
            "chromix-linux-x64",
            "chromix-linux-arm64",
            "chromix-mac-x64",
            "chromix-mac-arm64",
        ):
            self.assertIn(asset, source)
        # The POSIX reusable workflow receives the artifact name as an input.
        self.assertIn("${{ inputs.artifact }}", posix_source)
        self.assertIn("artifact:", source)
        # All five targets are reusable staged workflows now; runner names,
        # caches, and stage chains live in the called workflow files.
        self.assertNotIn("macos-14", source)
        self.assertIn("actions/cache@v4", posix_source)
        self.assertIn("download_cache", posix_source)
        self.assertIn("build-12:", (WORKFLOWS / "build-win-x64-github.yml").read_text())
        self.assertIn("8-stage snapshot/resume", source)
        self.assertIn(".github/workflows/build-posix-github.yml", source)

    def test_posix_generator_is_deterministic(self):
        import subprocess
        import sys
        import tempfile

        with tempfile.TemporaryDirectory(prefix="chromix workflow ") as temp:
            output = Path(temp) / ".github/workflows/build-posix-github.yml"
            output.parent.mkdir(parents=True)
            for _ in range(2):
                subprocess.run([sys.executable, str(REPO / "tools/gen_posix_workflow.py")],
                               cwd=temp, check=True, capture_output=True)
                self.assertEqual(output.read_bytes(), (WORKFLOWS / output.name).read_bytes())

    def test_posix_stages_pin_host_node_and_go(self):
        import yaml

        source = (WORKFLOWS / "build-posix-github.yml").read_text()
        workflow = yaml.safe_load(source)
        self.assertNotIn("go.dev/VERSION", source)
        self.assertNotIn("/usr/local/go/bin", source)
        for index in range(1, 9):
            with self.subTest(stage=index):
                steps = workflow["jobs"][f"posix-{index}"]["steps"]
                node = next(step for step in steps if step.get("uses") == "actions/setup-node@v4")
                go = next(step for step in steps if step.get("uses") == "actions/setup-go@v5")
                verify = next(step for step in steps if step.get("name") == "Verify Go version")
                stage = next(step for step in steps if step.get("id") == "stage")
                self.assertEqual(node["with"]["node-version"], "24.20.0")
                self.assertEqual(go["with"], {"go-version": "1.27.1", "cache": False})
                self.assertNotIn("if", go)
                self.assertLess(steps.index(go), steps.index(verify))
                self.assertLess(steps.index(verify), steps.index(stage))
                self.assertIn("go version", verify["run"])
                self.assertIn('test "$(go env GOVERSION)" = go1.27.1', verify["run"])
                mac_tools = next(step for step in steps if step.get("name") == "Install macOS build tools")
                self.assertEqual(mac_tools["run"], "brew install ninja coreutils gpatch zstd")

    def test_linux_arm64_cross_build_requires_same_run_native_verification(self):
        import yaml

        workflow = yaml.safe_load((WORKFLOWS / 'build-linux-arm64.yml').read_text())
        build = workflow["jobs"]["build"]
        self.assertIn("!inputs.use_upstream_cache && 'ubuntu-24.04-arm' || 'ubuntu-24.04'", build["with"]["runner"])
        self.assertEqual(build["with"]["arch"], "arm64")
        posix = yaml.safe_load((REPO / ".github/workflows/build-posix-github.yml").read_text())
        verify = posix["jobs"]["verify-linux-arm64"]
        self.assertEqual(verify["runs-on"], "ubuntu-24.04-arm")
        self.assertEqual(verify["needs"], [f"posix-{n}" for n in range(1, 9)])
        self.assertIn("always()", verify["if"])
        self.assertIn("!contains(needs.*.result, 'failure')", verify["if"])
        self.assertNotIn("continue-on-error", verify)
        download = next(step for step in verify["steps"] if step.get("uses", "").startswith("actions/download-artifact@"))
        self.assertEqual(download["with"]["name"], "${{ inputs.artifact }}")
        self.assertNotIn("run-id", download["with"])
        check = next(step["run"] for step in verify["steps"] if step.get("name") == "Verify checksum and native launcher")
        self.assertLess(check.index("sha256sum --check --strict"), check.index("module._extract_zip"))
        self.assertIn('"sdk/python/chromix/_binary.py"', check)
        self.assertNotIn("unzip -q", check)
        self.assertIn("verify_linux_bundle.py", check)
        self.assertIn("--arch arm64 --runtime", check)
        self.assertLess(check.index("module._extract_zip"), check.index("prepare-ci-sandbox.sh"))
        self.assertLess(check.index("prepare-ci-sandbox.sh"), check.index("verify_linux_bundle.py"))
        self.assertNotIn("--no-sandbox", check)
        stage = (REPO / "build/posix/ci-stage.sh").read_text()
        cross = stage[stage.index('HOST_ARCH="$(uname -m)"'):stage.index('VERSION_OUTPUT=')]
        self.assertIn("verify_linux_bundle.py", cross)
        self.assertIn("emit runtime_verified false", cross)
        self.assertIn("emit status compiled", cross)
        self.assertNotIn("--version", cross)

    def test_linux_restore_ninja_is_pinned_for_both_native_architectures(self):
        import ast

        module = ast.parse((REPO / "tools/gen_posix_workflow.py").read_text())
        assignment = next(node for node in module.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "LINUX_CLEAN"
                                  for target in node.targets))
        source = ast.literal_eval(assignment.value)
        step = source.split("- name: Install restored-build Ninja v6", 1)[1]
        self.assertIn("runner.os == 'Linux' && inputs.use_upstream_cache", step)
        self.assertIn("releases/download/v1.12.1/", step)
        self.assertIn("ninja-linux.zip", step)
        self.assertIn("ninja-linux-aarch64.zip", step)
        self.assertIn("6f98805688d19672bd699fbbfa2c2cf0fc054ac3df1f0e6a47664d963d530255", step)
        self.assertIn("5c25c6570b0155e95fce5918cb95f1ad9870df5768653afe128db822301a05a1", step)
        self.assertIn("--max-filesize 2097152", step)
        self.assertIn('"${NINJA_DIR}/ninja" --version', step)
        self.assertLess(step.index("sha256sum --check --strict"), step.index("unzip -q"))
        self.assertLess(step.index("unzip -q"), step.index("--version"))
        workflow = (REPO / ".github/workflows/build-posix-github.yml").read_text()
        self.assertEqual(workflow.count("- name: Install restored-build Ninja v6"), 8)
        self.assertEqual(workflow.count("chromix-build/upstream-cache-ninja.json"), 8)

    def test_all_initial_stages_upload_preparation_and_hidden_receipts(self):
        import yaml

        for filename, jobs in (("build-posix-github.yml", [f"posix-{n}" for n in range(1, 9)]),
                               ("build-win-x64-github.yml", ["build-1"])):
            workflow = yaml.safe_load((REPO / ".github/workflows" / filename).read_text())
            for job in jobs:
                with self.subTest(workflow=filename, job=job):
                    diagnostics = [step for step in workflow["jobs"][job]["steps"]
                                   if step.get("name") in ('Upload build diagnostics', 'Upload upstream cache diagnostics')]
                    self.assertEqual(len(diagnostics), 1)
                    options = diagnostics[0]["with"]
                    self.assertTrue(options["include-hidden-files"])
                    for name in ("upstream-cache-preparation.json", ".chromix-upstream-restored.json",
                                 ".chromix-restored-patches.json"):
                        self.assertIn(name, options["path"])
                    self.assertNotIn("**", options["path"])
                    if filename == "build-posix-github.yml":
                        self.assertIn("chromix-build/upstream-reuse/", options["path"])

    def test_build_arguments_cover_host_toolchain_compatibility(self):
        linux = (REPO / "build" / "build.sh").read_text(encoding="utf-8")
        macos = (REPO / "build" / "args.macos.gn").read_text(encoding="utf-8")
        self.assertIn("-Wno-deprecated-declarations", linux)
        self.assertIn("use_unified_system_module = false", macos)

    def test_macos_packager_normalizes_intel_name_and_uses_portable_tools(self):
        source = PACKAGE_MACOS.read_text(encoding="utf-8")
        self.assertRegex(source, r'ARCH="\$\{3:-\$\(uname -m\)\}"')
        self.assertIn('x86_64|amd64) ARCH=x64', source)
        self.assertNotIn("GNU tar is required", source)
        self.assertIn('zip -X -q -r', source)
        self.assertIn('shasum -a 256', source)
        self.assertIn("CHROMIX_CHROMIUM_LICENSE", source)
        self.assertIn("LICENSE.chromix", source)
        self.assertIn("LICENSE.chromium", source)


if __name__ == "__main__":
    unittest.main()
