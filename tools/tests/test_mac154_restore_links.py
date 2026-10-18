import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import fetch_upstream_cache as fetcher
from tools import restore_upstream_cache as restore
from tools.tests import test_restore_upstream_cache as fixtures


MODULE = "third_party/devtools-frontend/src/node_modules/esbuild"
ALIAS = "third_party/devtools-frontend/src/node_modules/.bin/esbuild"
EXTERNAL = ("/Users/runner/work/ungoogled-chromium-macos/ungoogled-chromium-macos/"
            "build/esbuild-node/node_modules/esbuild")
IDENTITY = {
    "chromium_version": "154.0.8037.97",
    "ungoogled_commit": "3e46b13825f808f0886e484d44532372655e5fe4",
    "head_sha": "f7ba75f94442abda7ac3ea81790c217f8636d3ba",
    "platform": "macos", "repository": "ungoogled-software/ungoogled-chromium-macos",
    "repository_id": 177203026, "head_branch": "154.0.8037.97", "event": "push",
    "workflow_path": ".github/workflows/build.yml", "run_id": 37654894671,
}
ARTIFACTS = {
    "x64": (11527088964, "github_build_artifact_x86_64", 12505793478,
            "sha256:f79f7e5769b25d588ab68aa8c2963caeb40e04504b375a97e7288ee77467f186"),
    "arm64": (11520487436, "github_build_artifact_arm64", 10872477177,
              "sha256:4fc2c995fc01213d3e819ff05db169fb14f0f93487b387f23cbf9a7b2e493e6a"),
}


def identity_for(arch):
    artifact_id, name, size, digest = ARTIFACTS[arch]
    return dict(IDENTITY, arch=arch, artifact_id=artifact_id, artifact_name=name,
                artifact_size_in_bytes=size, artifact_digest=digest)


def links(source="src", target=EXTERNAL, alias="../esbuild/bin/esbuild"):
    return [(source + "/" + MODULE, "sym", target), (source + "/" + ALIAS, "sym", alias)]


@unittest.skipUnless(os.name == "posix", "POSIX source archive links")
class Mac154ExtractorLinksTest(unittest.TestCase):
    def extract(self, entries, *, platform="macos", roots=("src",), zipped=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        tree = Path(temporary.name)
        archive = io.BytesIO(fixtures.archive_bytes(entries, zipped=zipped))
        selection = fetcher.SourceSelection(roots, platform=platform)
        with mock.patch.object(fetcher, "require_space"):
            result = (fetcher.extract_zip if zipped else fetcher.extract_tar)(archive, tree, selection)
        return tree, result

    def test_exact_pair_is_omitted_in_either_order_and_archive_format(self):
        for zipped in (False, True):
            for reverse in (False, True):
                with self.subTest(zipped=zipped, reverse=reverse):
                    pair = links()[::-1] if reverse else links()
                    tree, result = self.extract(pair + [("src/kept", "file", b"source")], zipped=zipped)
                    self.assertEqual(result["skipped_external_symlinks"], 2)
                    self.assertEqual(set(result["external_symlink_paths"]), {"src/" + MODULE, "src/" + ALIAS})
                    self.assertEqual(result["remapped_internal_symlinks"], 0)
                    self.assertEqual((tree / "src/kept").read_bytes(), b"source")
                    for name in (MODULE, ALIAS):
                        self.assertFalse(os.path.lexists(tree / "src" / name))

    def test_module_without_npm_alias_is_recorded(self):
        _, result = self.extract(links()[:1])
        self.assertEqual(result["external_symlink_paths"], ["src/" + MODULE])
        self.assertEqual(result["skipped_external_symlinks"], 1)

    def test_wrong_absolute_module_targets_reject_even_without_alias(self):
        targets = ["/usr/lib/node_modules/esbuild/", EXTERNAL + "/", EXTERNAL + "-other",
                   EXTERNAL + "/../esbuild", EXTERNAL.replace("/Users/runner/", "/Users/other/"),
                   fetcher.ORIGINAL_SOURCE_ROOTS["macos"] + "/esbuild", "/tmp/esbuild"]
        for target in targets:
            for with_alias in (False, True):
                with self.subTest(target=target, with_alias=with_alias):
                    entries = links(target=target)
                    with self.assertRaisesRegex(fetcher.CacheMiss, "unsafe_esbuild_alias_module"):
                        self.extract(entries if with_alias else entries[:1])

    def test_alias_traversal_and_nonexact_alias_targets_reject(self):
        targets = ["../esbuild/../esbuild/bin/esbuild", "../../esbuild/bin/esbuild",
                   "./../esbuild/bin/esbuild", "../esbuild/bin/./esbuild", "../esbuild/bin/other",
                   EXTERNAL + "/bin/esbuild", "../../../../../../../../outside", "",
                   "../esbuild/bin/esbuild\n"]
        for target in targets:
            with self.subTest(target=target), self.assertRaisesRegex(fetcher.CacheMiss, "unsafe_esbuild_alias"):
                self.extract(links(alias=target))

    def test_exception_does_not_apply_to_other_platforms_or_source_roots(self):
        cases = [("linux", ("build/src",), "build/src"), ("windows", ("src",), "src"),
                 (None, ("src",), "src"), ("macos", ("build/src",), "build/src"),
                 ("macos", ("src", "extra"), "src")]
        for platform, roots, source in cases:
            with self.subTest(platform=platform, roots=roots), self.assertRaises(fetcher.CacheMiss):
                self.extract(links(source=source), platform=platform, roots=roots)

    def test_unknown_aliases_and_indirect_external_chains_still_reject(self):
        cases = [links()[:1] + [("src/other-alias", "sym", MODULE + "/bin/esbuild")],
                 links() + [("src/other-alias", "sym", MODULE + "/bin/esbuild")],
                 links() + [("src/other-alias", "sym", ALIAS)],
                 [("src/unknown", "sym", EXTERNAL), ("src/alias", "sym", "unknown/bin/esbuild")],
                 [("src/" + ALIAS, "sym", "../bridge/bin/esbuild"),
                  ("src/third_party/devtools-frontend/src/node_modules/bridge", "sym", EXTERNAL)]]
        for entries in cases:
            with self.subTest(entries=entries), self.assertRaises(fetcher.CacheMiss):
                self.extract(entries)

    def test_regular_module_and_internal_alias_are_preserved(self):
        tree, result = self.extract([("src/" + MODULE + "/bin/esbuild", "file", b"real module"), links()[1]])
        self.assertEqual(result["skipped_external_symlinks"], 0)
        self.assertEqual(result["external_symlink_paths"], [])
        self.assertEqual((tree / "src" / ALIAS).read_bytes(), b"real module")


class Mac154RestoreLinksTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RestoreUpstreamCacheTest()
        with mock.patch.object(tempfile, "tempdir", str(Path(tempfile.gettempdir()).resolve())):
            self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.make_cache("macos", "x64")

    def record_links(self, paths=(MODULE, ALIAS)):
        fixture = self.fixture
        fixture.result.update(skipped_external_symlinks=len(paths),
                              external_symlink_paths=["src/" + name for name in paths])
        fixture.write(fixture.cache / "result.json", json.dumps(fixture.result))

    def test_exact_identity_is_required_for_both_paths_and_architectures(self):
        for arch in ARTIFACTS:
            identity = identity_for(arch)
            for name in (MODULE, ALIAS):
                with self.subTest(arch=arch, name=name):
                    self.assertTrue(restore.is_known_external_link(name, "macos", identity))
                    self.assertFalse(restore.is_known_external_link(name, "macos"))
                    for platform in ("linux", "windows", "other"):
                        self.assertFalse(restore.is_known_external_link(name, platform, identity))
                    for key in identity:
                        changed = dict(identity, **{key: "unverified"})
                        self.assertFalse(restore.is_known_external_link(name, "macos", changed), key)
                        missing = dict(identity)
                        del missing[key]
                        self.assertFalse(restore.is_known_external_link(name, "macos", missing), key)
                    for key in ("repository_id", "run_id", "artifact_id", "artifact_size_in_bytes"):
                        changed = dict(identity, **{key: float(identity[key])})
                        self.assertFalse(restore.is_known_external_link(name, "macos", changed), key)
                    self.assertFalse(restore.is_known_external_link(name, "macos", dict(identity, extra=True)))
                    other_arch = "arm64" if arch == "x64" else "x64"
                    self.assertFalse(restore.is_known_external_link(name, "macos", dict(identity, arch=other_arch)))
                    for suffix in ("-other", "/bin/esbuild", "/../other"):
                        self.assertFalse(restore.is_known_external_link(name + suffix, "macos", identity))

    def test_extracted_pair_survives_result_restore_and_verify_receipts(self):
        fixture = self.fixture
        for arch in ARTIFACTS:
            with self.subTest(arch=arch):
                fixture.make_cache("macos", arch)
                fixture.work = Path(fixture.tmp.name) / ("work-" + arch)
                identity, _, manifest = restore.identities(fixture.repo, "macos", arch)
                expected = dict(restore.MAC155_ESBUILD_IDENTITY, arch=arch)
                artifact = restore.MAC155_ESBUILD_ARTIFACTS[arch]
                expected.update(zip(("artifact_id", "artifact_name", "artifact_size_in_bytes",
                                     "artifact_digest"), artifact))
                self.assertEqual(identity, expected)
                archive = io.BytesIO(fixtures.archive_bytes(links()))
                extracted = fetcher.extract_tar(archive, fixture.cache / "tree",
                                               fetcher.SourceSelection(["src"], platform="macos"))
                fixture.result.update(extracted)
                fixture.write(fixture.cache / "result.json", json.dumps(fixture.result))
                entry = fixture.invoke()
                self.assertEqual(entry["status"], "hit", entry)
                receipt = entry["receipt"]
                self.assertEqual(receipt["identity"], identity)
                self.assertEqual(receipt["manifest"], manifest)
                self.assertEqual(receipt["external_symlink_paths"], sorted((MODULE, ALIAS)))
                self.assertEqual(receipt["archive_external_symlink_paths"], extracted["external_symlink_paths"])
                self.assertEqual(restore.verify_restored(fixture.work, "macos", arch, fixture.repo), receipt)
                result = json.loads((fixture.cache / "result.json").read_text())
                self.assertEqual(result["status"], "consumed")
                self.assertEqual(result["skipped_external_symlinks"], 2)
                self.assertEqual(result["external_symlink_paths"], extracted["external_symlink_paths"])
                for name in (MODULE, ALIAS):
                    self.assertFalse(os.path.lexists(fixture.work / "src" / name))
                self.assertEqual((fixture.work / "src/chrome/source.cc").read_text(), "upstream\n")
                self.assertEqual((fixture.work / "src/out/Default/obj/output.o").read_bytes(), b"object")

    def test_restore_rejects_wrong_source_version(self):
        fixture = self.fixture
        self.record_links()
        fixture.write(fixture.donor / "chrome/VERSION", "MAJOR=153\nMINOR=0\nBUILD=8010\nPATCH=36\n")
        entry = fixture.invoke()
        self.assertEqual(entry["status"], "miss", entry)
        self.assertIn("chrome/VERSION", entry["reasons"][0])
        self.assertFalse((fixture.work / "src").exists())

    def test_restore_rejects_wrong_core_arch_and_artifact_digest(self):
        fixture = self.fixture
        for changes in ({"ungoogled_commit": "0" * 40}, {"arch": "arm64"},
                        {"artifact_digest": "sha256:" + "0" * 64}, {"chromium_version": "153.0.8010.36"}):
            with self.subTest(changes=changes):
                self.record_links()
                with self.assertRaisesRegex(restore.Miss, "unknown external symlink"):
                    restore.missing_host_links(fixture.cache, fixture.donor, fixture.result, "macos",
                                               dict(identity_for("x64"), **changes))

    def test_unknown_duplicate_existing_and_incomplete_omissions_reject(self):
        fixture = self.fixture
        for paths in ((ALIAS,), (MODULE, ALIAS, "unknown/tool"), (MODULE, MODULE), (MODULE + "-other",)):
            with self.subTest(paths=paths):
                self.record_links(paths)
                with self.assertRaises(restore.Miss):
                    restore.missing_host_links(fixture.cache, fixture.donor, fixture.result, "macos",
                                               identity_for("x64"))
        self.record_links((MODULE,))
        self.assertEqual(restore.missing_host_links(fixture.cache, fixture.donor, fixture.result,
                                                    "macos", identity_for("x64")), [MODULE])
        (fixture.donor / MODULE).mkdir(parents=True)
        with self.assertRaisesRegex(restore.Miss, "unexpectedly exists"):
            restore.missing_host_links(fixture.cache, fixture.donor, fixture.result, "macos", identity_for("x64"))

    def test_verify_rejects_incomplete_chain_unknown_paths_and_wrong_identity(self):
        fixture = self.fixture
        self.record_links()
        entry = fixture.invoke()
        self.assertEqual(entry["status"], "hit", entry)
        receipt = entry["receipt"]
        cases = [dict(receipt, external_symlink_paths=[ALIAS]),
                 dict(receipt, external_symlink_paths=[MODULE, ALIAS, "unknown/tool"]),
                 dict(receipt, identity=dict(receipt["identity"], artifact_digest="sha256:" + "0" * 64))]
        for changed in cases:
            with self.subTest(changed=changed):
                fixture.write(fixture.work / "src" / restore.MARKER, json.dumps(changed))
                with self.assertRaises(restore.Miss):
                    restore.verify_restored(fixture.work, "macos", "x64", fixture.repo)

    def test_legacy_import_does_not_create_a_module_placeholder(self):
        fixture = self.fixture
        result = {"skipped_external_symlinks": 2,
                  "external_symlink_paths": ["tree/src/" + name for name in (MODULE, ALIAS)]}
        with self.assertRaisesRegex(restore.Miss, "unknown external symlink"):
            restore.importer.preserve_external_tool_lookups(fixture.cache, fixture.donor, result)
        self.assertFalse(os.path.lexists(fixture.donor / MODULE))


if __name__ == "__main__":
    unittest.main()
