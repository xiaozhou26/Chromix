"""Cross-platform contracts for runner-local Windows V8 metagen dependencies."""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools import install_windows_metagen_bindings as bindings

ROOT = Path(__file__).resolve().parents[2]
FILES = {"__init__.py": b"# pinned LLVM package\n", "cindex.py": b"# pinned cindex\n"}
HASHES = {name: hashlib.sha256(data).hexdigest() for name, data in FILES.items()}
V8_PINS = {
    "155.0.8059.39": "56832c166a78bd1eb61068ac69876d6880eaabdf",
    "155.0.8059.79": "d89479ac5446a374c55a35be19b5a23437558d99",
}


def pe_dll(machine=bindings.AMD64):
    # PE32+ headers and one aligned .text section; not a libclang implementation.
    data = bytearray(1024)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 64)
    data[64:68] = b"PE\0\0"
    struct.pack_into("<HH", data, 68, machine, 1)
    struct.pack_into("<HHH", data, 84, 240, 0x2022, 0x20B)
    optional = 88
    struct.pack_into("<I", data, optional + 4, 512)
    struct.pack_into("<I", data, optional + 20, 0x1000)
    struct.pack_into("<Q", data, optional + 24, 0x180000000)
    struct.pack_into("<II", data, optional + 32, 0x1000, 512)
    struct.pack_into("<HH", data, optional + 40, 6, 0)
    struct.pack_into("<HH", data, optional + 48, 6, 0)
    struct.pack_into("<II", data, optional + 56, 0x2000, 512)
    struct.pack_into("<H", data, optional + 68, 3)
    struct.pack_into("<QQQQ", data, optional + 72, 0x100000, 0x1000, 0x100000, 0x1000)
    struct.pack_into("<I", data, optional + 108, 16)
    section = optional + 240
    data[section:section + 8] = b".text\0\0\0"
    struct.pack_into("<IIII", data, section + 8, 1, 0x1000, 512, 512)
    struct.pack_into("<I", data, section + 36, 0x60000020)
    data[512] = 0xC3
    return bytes(data)


class WindowsMetagenBindingsTest(unittest.TestCase):
    version = "155.0.8059.39"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.src = self.root / "source cache/src"
        self.out = self.src / "out/Default"
        self.out.mkdir(parents=True)
        self.put(self.src / "chrome/VERSION", "".join(
            f"{key}={value}\n" for key, value in zip(
                ("MAJOR", "MINOR", "BUILD", "PATCH"), self.version.split("."))).encode())
        self.put(self.out / "args.gn", b'target_cpu = "x64"\n')
        self.site = self.root / "runtime Python/Lib/site-packages"
        self.library = self.src / "third_party/llvm-build/Release+Asserts/bin/libclang.dll"
        self.put(self.library, pe_dll())
        self.resource = self.src / "third_party/llvm-build/Release+Asserts/lib/clang/23"
        self.put(self.resource / "include/stddef.h", b"headers")
        self.script = self.src / "v8/tools/metagen/metagen.py"
        self.put(self.script, b"# metagen")
        self.put(self.src / "DEPS", f"vars = {{'v8_revision': '{V8_PINS[self.version]}'}}".encode())
        patch = mock.patch.object(bindings, "HASHES", HASHES)
        patch.start()
        self.addCleanup(patch.stop)

    def put(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def package(self, directory):
        for name, data in FILES.items():
            self.put(directory / name, data)

    def command(self, *, bundled=False):
        base = f'"{sys.executable}" ../../v8/tools/metagen/metagen.py --clang-resource-dir "{self.resource}" '
        if bundled:
            return base + f'--libclang-dir "{self.src / "third_party/llvm-libclang"}"'
        return base + f'--libclang-so "{self.library}" --libclang-bindings-dir "{self.site}" --expect-libclang-major 23'

    def contract(self):
        return bindings.action_contract(self.command(), self.out)

    def runtime(self):
        return {"platform": "win32", "bits": 64, "process_machine": bindings.AMD64,
                "native_machine": bindings.AMD64, "site_packages": [str(self.site)]}

    def test_command_reads_actual_python_paths_and_flags(self):
        contract = bindings.action_contract('echo unrelated\n' + self.command(), self.out)
        self.assertEqual(contract["python"], sys.executable)
        self.assertEqual(contract["script"], self.script)
        self.assertEqual(contract["bindings"], self.site)
        bindings.validate_paths(contract, self.src, self.runtime())

    def test_windows_backslashes_and_spaces_survive_tokenization(self):
        words = bindings.split_command('"C:\\runtime Python\\python3.exe" ../../v8/tools/metagen/metagen.py '
                                       '--libclang-bindings-dir "C:\\runtime Python\\Lib\\site-packages" '
                                       '"--extra-flag=-imsvcC:\\Program Files\\include"')
        self.assertEqual(words[0], r"C:\runtime Python\python3.exe")
        self.assertEqual(words[3], r"C:\runtime Python\Lib\site-packages")
        self.assertEqual(words[4], r"--extra-flag=-imsvcC:\Program Files\include")

    def test_bundled_source_layout(self):
        contract = bindings.action_contract(self.command(bundled=True), self.out)
        self.put(contract["library"], pe_dll())
        bindings.validate_paths(contract, self.src, self.runtime())
        self.assertEqual(contract["bindings"], self.src / "third_party/llvm-libclang/bindings/python")

    def test_zero_multiple_wrapped_and_mismatched_commands_rejected(self):
        for command in ("", self.command() + "\n" + self.command(), "cmd /c " + self.command(),
                        self.command().replace("--expect-libclang-major 23", "--expect-libclang-major 17"),
                        self.command().replace("--expect-libclang-major 23", ""),
                        self.command() + " --libclang-so duplicate",
                        self.command() + ' --libclang-dir "elsewhere"',
                        self.command().replace(str(self.resource), str(self.resource.parent / "22"))):
            with self.subTest(command=command), self.assertRaises(ValueError):
                bindings.action_contract(command, self.out)

    def test_foreign_site_packages_refused_instead_of_blind_install(self):
        contract = self.contract()
        contract["bindings"] = self.root / "donor Python/Lib/site-packages"
        with self.assertRaisesRegex(ValueError, "neither runtime"):
            bindings.validate_paths(contract, self.src, self.runtime())
        self.assertFalse(contract["bindings"].exists())

    def test_foreign_dll_resource_and_python_refused(self):
        for key in ("library", "resource", "script"):
            contract = self.contract()
            contract[key] = self.root / "foreign"
            with self.subTest(key=key), self.assertRaises(ValueError):
                bindings.validate_paths(contract, self.src, self.runtime())
        for runtime in (dict(self.runtime(), bits=32), dict(self.runtime(), platform="linux")):
            with self.assertRaisesRegex(ValueError, "64-bit Windows"):
                bindings.validate_paths(self.contract(), self.src, runtime)
        (self.resource / "include/stddef.h").unlink()
        with self.assertRaisesRegex(ValueError, "headers"):
            bindings.validate_paths(self.contract(), self.src, self.runtime())

    def test_source_package_preferred_offline_and_idempotent(self):
        source = self.src / bindings.SOURCE_PACKAGES[0]
        self.package(source)
        with mock.patch.object(bindings, "fetch_package", side_effect=AssertionError("unexpected network")):
            self.assertEqual(bindings.provision(self.src, self.site, download=True), str(source))
            before = {p: p.stat().st_mtime_ns for p in (self.site / "clang").iterdir()}
            self.assertEqual(bindings.provision(self.src, self.site, download=False), "already_installed")
            self.assertEqual(before, {p: p.stat().st_mtime_ns for p in before})
        self.assertEqual(self.library.read_bytes(), pe_dll())

    def test_each_fresh_runner_gets_bindings_even_with_source_snapshot(self):
        self.package(self.src / bindings.SOURCE_PACKAGES[1])
        for site in (self.site, self.root / "next runner/Lib/site-packages"):
            bindings.provision(self.src, site, download=False)
            self.assertEqual(bindings.read_package(site / "clang"), FILES)

    def test_download_requires_opt_in(self):
        with mock.patch.object(bindings, "fetch_package") as fetch:
            with self.assertRaisesRegex(ValueError, "explicitly allow"):
                bindings.provision(self.src, self.site, download=False)
            fetch.assert_not_called()
        self.assertFalse(self.site.exists())

    def test_missing_package_uses_only_pinned_python_files(self):
        with mock.patch.object(bindings, "fetch_package", return_value=FILES):
            self.assertIn(bindings.LLVM_COMMIT, bindings.provision(self.src, self.site, download=True))
        self.assertEqual(set(p.name for p in (self.site / "clang").iterdir()), set(FILES))
        self.assertEqual(self.library.read_bytes(), pe_dll())

    def test_download_uses_immutable_urls_and_verifies_each_file(self):
        opener = mock.Mock()
        urls = []
        def response(url, timeout):
            urls.append(url)
            self.assertIn('/' + bindings.LLVM_COMMIT + '/', url)
            self.assertEqual(timeout, 60)
            reply = mock.MagicMock()
            reply.__enter__.return_value = reply
            reply.status = 200
            reply.geturl.return_value = url
            reply.read.return_value = FILES[url.rsplit('/', 1)[1]]
            return reply
        opener.open.side_effect = response
        with mock.patch.object(bindings.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(bindings.fetch_package(), FILES)
        self.assertEqual(len(urls), 2)
        with self.assertRaisesRegex(ValueError, 'redirects'):
            bindings.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere')

    def test_hash_mismatch_fails_before_writes(self):
        with mock.patch.object(bindings, "fetch_package", return_value=dict(FILES, **{"cindex.py": b"bad"})):
            with self.assertRaisesRegex(ValueError, "SHA256"):
                bindings.provision(self.src, self.site, download=True)
        self.assertFalse(self.site.exists())

    def test_unverified_source_cannot_silently_fall_back_to_network(self):
        source = self.src / bindings.SOURCE_PACKAGES[0]
        self.package(source)
        (source / "cindex.py").write_bytes(b"wrong revision")
        with mock.patch.object(bindings, "fetch_package") as fetch:
            with self.assertRaisesRegex(ValueError, "SHA256"):
                bindings.provision(self.src, self.site, download=True)
            fetch.assert_not_called()

    def test_old_host_bindings_replaced_and_stale_bytecode_removed(self):
        self.put(self.site / "clang/cindex.py", b"old clang 21")
        self.put(self.site / "clang/__pycache__/cindex.pyc", b"old bytecode")
        self.package(self.src / bindings.SOURCE_PACKAGES[0])
        bindings.provision(self.src, self.site, download=False)
        self.assertEqual(bindings.read_package(self.site / "clang"), FILES)
        self.assertFalse((self.site / "clang/__pycache__").exists())

    def test_pypi_libclang_native_directory_never_overwritten(self):
        self.put(self.site / "clang/native/libclang.dll", b"PyPI libclang 17")
        with self.assertRaisesRegex(ValueError, "bundled runtime"):
            bindings.provision(self.src, self.site, download=True)
        self.assertEqual((self.site / "clang/native/libclang.dll").read_bytes(), b"PyPI libclang 17")

    def test_symlink_and_hardlink_destinations_refused(self):
        self.package(self.src / bindings.SOURCE_PACKAGES[0])
        external = self.root / "external"
        external.mkdir()
        self.site.mkdir(parents=True)
        try:
            (self.site / "clang").symlink_to(external, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"host does not permit directory symlinks: {error}")
        with self.assertRaisesRegex(ValueError, "linked"):
            bindings.provision(self.src, self.site, download=False)
        (self.site / "clang").unlink()
        (self.site / "clang").mkdir()
        self.put(external / "cindex.py", b"foreign")
        (self.site / "clang/cindex.py").hardlink_to(external / "cindex.py")
        with self.assertRaisesRegex(ValueError, "destination"):
            bindings.provision(self.src, self.site, download=False)
        self.assertEqual((external / "cindex.py").read_bytes(), b"foreign")

    def test_rebase_only_exact_donor_dll_and_preserve_site_argument(self):
        args = self.out / "args.gn"
        original = ('target_cpu = "x64"\n'
                    'v8_metagen_libclang_so = "C:/ungoogled-chromium-windows/build/src/'
                    'third_party/llvm-build/Release+Asserts/bin/libclang.dll"\n'
                    'v8_metagen_libclang_bindings_dir = "C:/hostedtoolcache/windows/Python/3.12.10/x64/Lib/site-packages"\n')
        args.write_text(original)
        self.assertEqual(bindings.configure(self.src, self.out, self.version)["status"], "rebased")
        self.assertIn(self.library.as_posix(), args.read_text())
        self.assertIn(original.splitlines()[2], args.read_text())
        before = args.read_bytes(), args.stat().st_mtime_ns
        bindings.configure(self.src, self.out, self.version)
        self.assertEqual(before, (args.read_bytes(), args.stat().st_mtime_ns))
        args.write_text(original.replace("C:/ungoogled-chromium-windows", "C:/unknown"))
        before = args.read_bytes()
        self.assertEqual(bindings.configure(self.src, self.out, self.version)["status"], "unchanged")
        self.assertEqual(before, args.read_bytes())

    def test_exact_version_table_preserves_historical_pins(self):
        self.assertEqual(bindings.PINS, {
            "154.0.8037.97": None,
            **{version: {"v8_revision": revision, "target_cpus": ("x64", "arm64")}
               for version, revision in V8_PINS.items()},
        })
        self.assertEqual(bindings.CHROMIUM_VERSION, "155.0.8059.39")
        self.assertEqual(bindings.V8_REVISION, V8_PINS["155.0.8059.39"])

    def test_other_supported_source_version_is_rejected_before_side_effects(self):
        other = next(version for version in V8_PINS if version != self.version)
        before = (self.out / "args.gn").read_bytes()
        with mock.patch.object(bindings.subprocess, "run") as ninja:
            with mock.patch.object(bindings, "provision") as provision:
                with self.assertRaisesRegex(ValueError, "chrome/VERSION"):
                    bindings.configure(self.src, self.out, other)
                with self.assertRaisesRegex(ValueError, "chrome/VERSION"):
                    bindings.install(self.src, self.out, Path("ninja"), other, download=True)
                ninja.assert_not_called()
                provision.assert_not_called()
        self.assertEqual((self.out / "args.gn").read_bytes(), before)

    def test_exact_deps_guard_before_installation(self):
        other = next(revision for version, revision in V8_PINS.items() if version != self.version)
        revisions = ("0" * 40, other)
        invalid_deps = [f"vars = {{'v8_revision': '{revision}'}}" for revision in revisions]
        invalid_deps += ["vars = {}", f"vars = {{'v8_revision': '{V8_PINS[self.version]}', "
                         f"'v8_revision': '{V8_PINS[self.version]}'}}"]
        for deps in invalid_deps:
            (self.src / "DEPS").write_text(deps)
            with self.subTest(deps=deps), mock.patch.object(bindings.subprocess, "run") as ninja:
                with mock.patch.object(bindings, "provision") as provision:
                    with self.assertRaisesRegex(ValueError, "exact DEPS"):
                        bindings.install(self.src, self.out, Path("ninja"), self.version, download=True)
                    ninja.assert_not_called()
                    provision.assert_not_called()

    def test_154_without_metagen_skips_but_155_missing_source_fails(self):
        self.script.unlink()
        self.assertEqual(bindings.install(self.src, self.out, Path("ninja"), "154.0.8037.97",
                                          download=False)["status"], "not_required")
        with self.assertRaisesRegex(ValueError, "missing"):
            bindings.install(self.src, self.out, Path("ninja"), self.version, download=True)

    def test_install_runs_actual_runtime_python_in_isolation(self):
        self.package(self.src / bindings.SOURCE_PACKAGES[0])
        replies = [self.runtime(), {"api_preflight": "passed", "libclang_version": "clang version 23.1.0"}]
        with mock.patch.object(bindings.subprocess, "run", return_value=SimpleNamespace(stdout=self.command())) as ninja:
            with mock.patch.object(bindings, "run_json", side_effect=replies) as run:
                result = bindings.install(self.src, self.out, Path("ninja"), self.version, download=False)
        self.assertEqual(ninja.call_args.args[0][-2:], ["commands", bindings.TARGET])
        probe_argv = run.call_args_list[1].args[0]
        self.assertEqual(probe_argv[:4], [sys.executable, "-I", "-S", "-B"])
        self.assertIn(str(self.site), probe_argv)
        self.assertIn(str(self.library), probe_argv)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["library_sha256"], bindings.digest(pe_dll()))

    @mock.patch.object(bindings, "runtime_info")
    def test_probe_import_api_version_and_parse(self, runtime):
        runtime.return_value = self.runtime()
        self.package(self.site / "clang")
        cursor = SimpleNamespace(kind="ENUM", get_children=lambda: iter([SimpleNamespace(enum_value=23)]))
        unit = SimpleNamespace(diagnostics=[], cursor=SimpleNamespace(get_children=lambda: iter([cursor])))
        config = mock.MagicMock()
        config.return_value.get_clang_version.return_value = "clang version 23.1.0"
        cindex = SimpleNamespace(__file__=str(self.site / "clang/cindex.py"), Config=config,
                                 Index=mock.Mock(), CursorKind=SimpleNamespace(ENUM_DECL="ENUM"),
                                 Diagnostic=SimpleNamespace(Error=3))
        cindex.Index.create.return_value.parse.return_value = unit
        with mock.patch.object(bindings.importlib, "import_module", side_effect=[cindex, object()]) as imports:
            with mock.patch.object(sys, "path", list(sys.path)):
                result = bindings.probe(self.site, self.library, self.src)
        self.assertEqual(result["api_preflight"], "passed")
        config.set_library_file.assert_called_once_with(str(self.library))
        config.set_compatibility_check.assert_called_once_with(True)
        self.assertEqual(imports.call_args_list[-1].args, ("metagen.cpp_hier",))
        for version in ("clang version 17.0.6", "clang version 23.0.0", "unknown"):
            config.return_value.get_clang_version.return_value = version
            with mock.patch.object(bindings.importlib, "import_module", return_value=cindex):
                with mock.patch.object(sys, "path", list(sys.path)):
                    with self.assertRaisesRegex(ValueError, "expected libclang"):
                        bindings.probe(self.site, self.library, self.src)
        config.return_value.get_clang_version.side_effect = RuntimeError("missing C API symbol")
        with mock.patch.object(bindings.importlib, "import_module", return_value=cindex):
            with mock.patch.object(sys, "path", list(sys.path)):
                with self.assertRaisesRegex(RuntimeError, "C API"):
                    bindings.probe(self.site, self.library, self.src)

    def test_ci_runs_configuration_and_preflight_on_every_stage(self):
        text = (ROOT / "build/windows/ci-stage.ps1").read_text()
        configure = text.index("$metagenBindings --configure")
        generate = text.index("& $gn gen $OutDir", configure)
        install = text.index("$metagenBindings --src", generate)
        plan = text.index("if ($RestoredUpstream)", install)
        validate = text.index("if ($ValidateOnly -or", plan)
        compile_start = text.index('Write-Host "==> Ninja compile jobs:', validate)
        self.assertLess(configure, generate)
        self.assertLess(generate, install)
        self.assertLess(install, plan)
        self.assertLess(validate, compile_start)
        self.assertNotIn("StageIndex", text[generate:plan])
        self.assertIn('if ($LASTEXITCODE -ne 0) { throw "Windows metagen bindings/API preflight failed" }', text)

    def test_arm64_target_installs_x64_host_bindings_without_profile_mutation(self):
        args = ('target_cpu = "arm64"\nv8_target_cpu = "arm64"\nhost_cpu = "x64"\ntarget_os = "win"\n'
                'chrome_pgo_phase = 2\npgo_data_path = "arm64.profdata"\nis_official_build = true\n')
        (self.out / "args.gn").write_text(args)
        self.package(self.src / bindings.SOURCE_PACKAGES[0])
        before = (self.out / "args.gn").read_bytes(), self.library.read_bytes()
        with mock.patch.object(bindings.subprocess, "run", return_value=SimpleNamespace(stdout=self.command())):
            with mock.patch.object(bindings, "run_json", side_effect=[self.runtime(), {"api_preflight": "passed"}]):
                result = bindings.install(self.src, self.out, Path("ninja"), self.version, download=False)
        self.assertEqual(result["target_cpu"], "arm64")
        self.assertEqual(before, ((self.out / "args.gn").read_bytes(), self.library.read_bytes()))

    def test_arm64_configuration_changes_only_donor_library_path(self):
        donor = "C:/ungoogled-chromium-windows/build/src/third_party/llvm-build/Release+Asserts/bin/libclang.dll"
        args = ('target_cpu = "arm64"\nhost_cpu = "x64"\nchrome_pgo_phase = 2\n'
                'pgo_data_path = "arm64.profdata"\nis_official_build = true\n'
                f'v8_metagen_libclang_so = "{donor}"\n')
        (self.out / "args.gn").write_text(args)
        self.assertEqual(bindings.configure(self.src, self.out, self.version)["status"], "rebased")
        self.assertEqual((self.out / "args.gn").read_text(), args.replace(donor, self.library.as_posix()))

    def test_strict_source_version_and_target_table(self):
        for version in ("155", "155.0.8059.40", "154.0.8037.98", "156.0.0.0"):
            for method in (bindings.configure, bindings.install):
                with self.subTest(version=version, method=method.__name__), self.assertRaisesRegex(ValueError, "pin"):
                    if method is bindings.install:
                        method(self.src, self.out, Path("ninja"), version, download=False)
                    else:
                        method(self.src, self.out, version)
        for args in ('', 'target_cpu = "x86"', 'target_cpu = "arm64"\ntarget_cpu = "arm64"',
                     'target_cpu = getenv("ARCH")', 'target_cpu = "arm64"\nv8_target_cpu = "x64"',
                     'target_cpu = "arm64"\nhost_cpu = "arm64"', 'target_cpu = "arm64"\ntarget_os = "linux"'):
            (self.out / "args.gn").write_text(args)
            with self.subTest(args=args), mock.patch.object(bindings, "provision") as provision:
                with self.assertRaises(ValueError):
                    bindings.install(self.src, self.out, Path("ninja"), self.version, download=False)
                provision.assert_not_called()
        (self.out / "args.gn").write_text('target_cpu = "arm64"\n')
        for text in ("MAJOR=155\nMINOR=0\nBUILD=8059\nPATCH=40\n",
                     "MAJOR=155\nMINOR=0\nBUILD=8059\nPATCH=39\nPATCH=39\n"):
            (self.src / "chrome/VERSION").write_text(text)
            with self.assertRaisesRegex(ValueError, "chrome/VERSION"):
                bindings.configure(self.src, self.out, self.version)

    def test_python_313_amd64_on_arm_host_is_explicitly_rejected(self):
        for runtime in (dict(self.runtime(), native_machine=0xAA64),
                        dict(self.runtime(), process_machine=0xAA64, native_machine=0xAA64),
                        dict(self.runtime(), process_machine=0xA641),
                        {k: v for k, v in self.runtime().items() if k != "native_machine"}):
            with self.subTest(runtime=runtime), self.assertRaisesRegex(ValueError, "native AMD64"):
                bindings.validate_paths(self.contract(), self.src, runtime)

    @mock.patch.object(bindings.sysconfig, "get_path", return_value="site-packages")
    def test_runtime_info_uses_native_api_not_python_313_machine_alias(self, get_path):
        import ctypes
        for process, native in ((0, bindings.AMD64), (bindings.AMD64, 0xAA64), (0, 0xAA64)):
            kernel = mock.Mock()
            def architecture(handle, process_pointer, native_pointer):
                process_pointer._obj.value = process
                native_pointer._obj.value = native
                return True
            kernel.IsWow64Process2.side_effect = architecture
            with mock.patch.object(bindings.sys, "platform", "win32"):
                with mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True):
                    runtime = bindings.runtime_info()
            self.assertEqual(runtime["native_machine"], native)
            self.assertEqual(runtime["process_machine"], process or native)
            if native != bindings.AMD64:
                with self.assertRaisesRegex(ValueError, "native AMD64"):
                    bindings.validate_runtime(runtime)
            else:
                bindings.validate_runtime(runtime)

    @mock.patch.object(bindings.sysconfig, "get_path", return_value="site-packages")
    def test_linux_runtime_inspection_does_not_import_or_load_windows_tools(self, get_path):
        with mock.patch.object(bindings.sys, "platform", "linux"):
            runtime = bindings.runtime_info()
        self.assertNotIn("native_machine", runtime)
        with mock.patch.object(bindings, "runtime_info", return_value=runtime):
            with mock.patch.object(bindings.importlib, "import_module") as imports:
                with self.assertRaisesRegex(ValueError, "Windows"):
                    bindings.probe(self.site, self.library, self.src)
                imports.assert_not_called()

    def test_non_amd64_and_malformed_dlls_rejected_before_import_or_provision(self):
        malformed = [pe_dll(0xAA64), pe_dll(0xA641), pe_dll(0x14C), b"not PE",
                     pe_dll()[:90], pe_dll()[:367]]
        for offset, format_, value in ((60, "<I", 0xFFFFFFFF), (60, "<I", 32),
                                       (64, "<I", 0), (70, "<H", 0), (70, "<H", 97),
                                       (84, "<H", 111), (84, "<H", 0xFFFF),
                                       (86, "<H", 0x0002), (86, "<H", 0x2000),
                                       (88, "<H", 0x10B)):
            data = bytearray(pe_dll())
            struct.pack_into(format_, data, offset, value)
            malformed.append(bytes(data))
        for data in malformed:
            self.library.write_bytes(data)
            with self.subTest(data=data[:8]), mock.patch.object(bindings, "runtime_info", return_value=self.runtime()):
                with mock.patch.object(bindings.importlib, "import_module") as imports:
                    with self.assertRaisesRegex(ValueError, "PE"):
                        bindings.probe(self.site, self.library, self.src)
                    imports.assert_not_called()
                with mock.patch.object(bindings.subprocess, "run", return_value=SimpleNamespace(stdout=self.command())):
                    with mock.patch.object(bindings, "run_json", return_value=self.runtime()):
                        with mock.patch.object(bindings, "provision") as provision:
                            with self.assertRaisesRegex(ValueError, "PE"):
                                bindings.install(self.src, self.out, Path("ninja"), self.version, download=False)
                            provision.assert_not_called()

    def test_runtime_subprocess_code_is_valid_and_cross_platform(self):
        with mock.patch.object(bindings.subprocess, "run", return_value=SimpleNamespace(stdout=self.command())):
            with mock.patch.object(bindings, "run_json", return_value=self.runtime()) as run:
                with mock.patch.object(bindings, "provision", side_effect=ValueError("stop after runtime")):
                    with self.assertRaisesRegex(ValueError, "stop after runtime"):
                        bindings.install(self.src, self.out, Path("ninja"), self.version, download=False)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], [sys.executable, "-I", "-c"])
        result = subprocess.run(argv, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["platform"], sys.platform)

    def test_cli_accepts_arm64_configuration_and_rejects_unknown_version(self):
        argv = [sys.executable, str(ROOT / "tools/install_windows_metagen_bindings.py"),
                "--configure", "--src", str(self.src), "--out", str(self.out), "--chromium-version"]
        (self.out / "args.gn").write_text('target_cpu = "arm64"\n')
        result = subprocess.run([*argv, self.version], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["status"], "bundled")
        result = subprocess.run([*argv, "155.0.8059.40"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("unsupported Chromium", result.stderr)

    def test_arm64_build_retains_x64_runner_and_native_profile(self):
        text = (ROOT / ".github/workflows/build-win-arm64-github.yml").read_text()
        build_jobs = text.split("  build-1:", 1)[1].split("  complete:", 1)[0]
        self.assertEqual(build_jobs.count("runs-on: windows-2022"), 12)
        self.assertIn("CHROMIX_PREFER_UPSTREAM_CACHE: '0'", text)
        self.assertIn("inputs.build_profile || 'native'", text)
        stage = (ROOT / "build/windows/ci-stage.ps1").read_text()
        self.assertIn('$mergeArgs += @("--preserve-pgo-from", $gnArgs)', stage)

    def test_cli_help_is_cross_platform(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools/install_windows_metagen_bindings.py"), "--help"],
                                capture_output=True, text=True, check=True)
        self.assertIn("--configure", result.stdout)


class Windows15579MetagenBindingsTest(WindowsMetagenBindingsTest):
    version = "155.0.8059.79"


if __name__ == "__main__":
    unittest.main()
