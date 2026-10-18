"""Linux155 donor link authorization remains artifact-exact and version-isolated."""
import unittest

from tools import restore_upstream_cache as restore
from tools.tests import test_linux154_restore_links as previous


IDENTITY = {
    "chromium_version": "155.0.8059.39",
    "ungoogled_commit": "ffeb45a2bb88bc56a1dfed31410aeb10e9efcd5a",
    "head_sha": "025d99db1673a21992534551598b73ca79360762",
    "platform": "linux", "arch": "x64",
    "repository": "ungoogled-software/ungoogled-chromium-portablelinux",
    "repository_id": 177191557, "head_branch": "155.0.8059.39-1", "event": "push",
    "workflow_path": ".github/workflows/build.yml", "run_id": 37906884923,
    "artifact_id": 11652570743, "artifact_name": "build-cache-x86_64",
    "artifact_size_in_bytes": 4733942111,
    "artifact_digest": "sha256:34182b5fdbec949fbe29d320a7d7200eb9cd4b284c18b8daa0c516ab4816fa1a",
}
ARM_IDENTITY = dict(
    IDENTITY, arch="arm64", artifact_id=11656090008, artifact_name="build-cache-arm64",
    artifact_size_in_bytes=5677163828,
    artifact_digest="sha256:a4c0bfd8020cd34f27a652ae449f9e8547a468b8b813a11a64acf8a0891db447")


NEW_IDENTITIES = tuple(dict(
    restore.LINUX15579_ESBUILD_IDENTITY, arch=arch, artifact_id=artifact,
    artifact_name=name, artifact_size_in_bytes=size, artifact_digest=digest)
    for arch, (artifact, name, size, digest) in restore.LINUX15579_ESBUILD_ARTIFACTS.items())


class Linux155RestoreLinksTest(unittest.TestCase):
    def test_every_field_type_and_platform_is_exact(self):
        for identity in (IDENTITY, ARM_IDENTITY, *NEW_IDENTITIES):
            for name in (restore.LINUX154_ESBUILD_LINK, restore.LINUX154_ESBUILD_ALIAS):
                self.assertTrue(restore.is_known_external_link(name, "linux", identity))
                for platform in ("windows", "macos", "off"):
                    self.assertFalse(restore.is_known_external_link(name, platform, identity))
                for key, value in identity.items():
                    bad_values = ("unknown", float(value)) if type(value) is int else ("unknown",)
                    for bad in bad_values:
                        with self.subTest(key=key, bad=bad):
                            self.assertFalse(restore.is_known_external_link(
                                name, "linux", dict(identity, **{key: bad})))
                    missing = dict(identity)
                    del missing[key]
                    self.assertFalse(restore.is_known_external_link(name, "linux", missing))
                self.assertFalse(restore.is_known_external_link(name, "linux", dict(identity, extra=True)))
                for suffix in ("/bin/esbuild", "-other", "/../other"):
                    self.assertFalse(restore.is_known_external_link(name + suffix, "linux", identity))

    def test_154_and155_do_not_authorize_mixed_artifacts(self):
        for base, artifacts, platform in (
                (restore.LINUX154_ESBUILD_IDENTITY, restore.LINUX154_ESBUILD_ARTIFACTS, "linux"),
                (restore.MAC154_ESBUILD_IDENTITY, restore.MAC154_ESBUILD_ARTIFACTS, "macos")):
            for arch, (artifact, name, size, digest) in artifacts.items():
                identity = dict(base, arch=arch, artifact_id=artifact, artifact_name=name,
                                artifact_size_in_bytes=size, artifact_digest=digest)
                self.assertTrue(restore.is_known_external_link(restore.LINUX154_ESBUILD_LINK,
                                                              platform, identity))
                self.assertFalse(restore.is_known_external_link(restore.LINUX154_ESBUILD_LINK,
                    platform, dict(identity, chromium_version="155.0.8059.39")))

    def test_current_metadata_matches_only_the_exact_architecture(self):
        from tools.import_upstream_cache import repository_identity
        from pathlib import Path
        repo = Path(__file__).resolve().parents[2]
        for identity in NEW_IDENTITIES:
            self.assertEqual(repository_identity(repo, "linux", identity["arch"])[0], identity)
            for old in (IDENTITY, ARM_IDENTITY):
                mixed = dict(identity, artifact_id=old["artifact_id"],
                             artifact_digest=old["artifact_digest"])
                self.assertFalse(restore.is_known_external_link(
                    restore.LINUX154_ESBUILD_LINK, "linux", mixed))

    def test_omitted_chain_is_recorded_but_never_recreated(self):
        fixture = previous.Linux154RestoreLinksTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        cache = fixture.fixture
        for identity in (IDENTITY, ARM_IDENTITY, *NEW_IDENTITIES):
            cache.make_cache("linux", identity["arch"])
            relative = cache.donor.relative_to(cache.cache / "tree").as_posix()
            paths = [restore.LINUX154_ESBUILD_LINK, restore.LINUX154_ESBUILD_ALIAS]
            result = dict(skipped_external_symlinks=2,
                          external_symlink_paths=[relative + "/" + name for name in paths])
            self.assertEqual(restore.missing_host_links(cache.cache, cache.donor, result,
                                                       "linux", identity), sorted(paths))
            self.assertTrue(all(not (cache.donor / name).exists() for name in paths))
            result.update(skipped_external_symlinks=1,
                          external_symlink_paths=[relative + "/" + paths[1]])
            with self.assertRaisesRegex(restore.Miss, "incomplete esbuild"):
                restore.missing_host_links(cache.cache, cache.donor, result, "linux", identity)


if __name__ == "__main__":
    unittest.main()
