"""Exact Linux156 metadata and external-link authorization, including the rerun."""
import json
from datetime import datetime, timezone
from pathlib import Path
import unittest

from tools import fetch_upstream_cache as fetch
from tools import restore_upstream_cache as restore
from tools.import_upstream_cache import repository_identity
from tools.tests import test_linux155_restore_links as old
from tools.tests import test_linux154_restore_links as old154

METADATA = json.loads((Path(__file__).parent / "fixtures/linux156_cache_metadata.json").read_text())
NOW = datetime(2026, 11, 8, tzinfo=timezone.utc)
IDENTITIES = tuple(dict(restore.LINUX156_ESBUILD_IDENTITY, arch=arch, artifact_id=artifact,
                       artifact_name=name, artifact_size_in_bytes=size, artifact_digest=digest)
                   for arch, (artifact, name, size, digest) in restore.LINUX156_ESBUILD_ARTIFACTS.items())
LINKS = (restore.LINUX154_ESBUILD_LINK, restore.LINUX154_ESBUILD_ALIAS, restore.LINUX156_TSC_LINK)


class Linux156CacheTest(unittest.TestCase):
    def test_real_attempt2_metadata_matches_current_manifest(self):
        self.assertEqual(METADATA["run"]["run_attempt"], 2)
        self.assertEqual(METADATA["run"]["conclusion"], "success")
        for identity in IDENTITIES:
            arch = identity["arch"]
            self.assertEqual(repository_identity(fetch.ROOT, "linux", arch)[0], identity)
            pin, _ = fetch.load_manifest("linux", arch, 41095740465)
            self.assertNotIn("run_attempt", pin)
            artifact = next(a for a in METADATA["artifacts"] if a["id"] == pin["artifact"]["id"])
            self.assertEqual(pin["artifact"]["expires_at"], artifact["expires_at"])
            fetch.validate_metadata(pin, METADATA["run"], artifact, NOW)
            for key, bad in (("conclusion", "failure"), ("head_sha", "a" * 40),
                             ("head_branch", "156.0.8078.73-1"), ("event", "workflow_dispatch")):
                with self.subTest(key=key), self.assertRaises(fetch.CacheMiss):
                    fetch.validate_metadata(pin, dict(METADATA["run"], **{key: bad}), artifact, NOW)
            for key, bad in (("expired", True), ("digest", "sha256:" + "0" * 64),
                             ("size_in_bytes", artifact["size_in_bytes"] + 1)):
                with self.subTest(key=key), self.assertRaises(fetch.CacheMiss):
                    fetch.validate_metadata(pin, METADATA["run"], dict(artifact, **{key: bad}), NOW)
            other = next(a for a in METADATA["artifacts"] if a["id"] != artifact["id"])
            with self.assertRaisesRegex(fetch.CacheMiss, "artifact_mismatch"):
                fetch.validate_metadata(pin, METADATA["run"], other, NOW)
            with self.assertRaisesRegex(fetch.CacheMiss, "artifact_expired"):
                fetch.validate_metadata(pin, METADATA["run"], artifact,
                                        datetime(2027, 1, 29, tzinfo=timezone.utc))

    def test_all_identity_fields_and_links_are_exact(self):
        for identity in IDENTITIES:
            for name in LINKS:
                self.assertTrue(restore.is_known_external_link(name, "linux", identity))
                for platform in ("macos", "windows", "other"):
                    self.assertFalse(restore.is_known_external_link(name, platform, identity))
                for key, value in identity.items():
                    for bad in ("wrong", None, float(value)) if type(value) is int else ("wrong", None):
                        with self.subTest(key=key, name=name, bad=bad):
                            self.assertFalse(restore.is_known_external_link(name, "linux", dict(identity, **{key: bad})))
                    missing = dict(identity)
                    del missing[key]
                    self.assertFalse(restore.is_known_external_link(name, "linux", missing))
                self.assertFalse(restore.is_known_external_link(name, "linux", dict(identity, extra=True)))
                for suffix in ("/../other", "/tsc", "-other"):
                    self.assertFalse(restore.is_known_external_link(name + suffix, "linux", identity))

    def test_historical_profiles_do_not_authorize_new_tsc_link(self):
        identities = [old.IDENTITY, old.ARM_IDENTITY]
        for arch, (artifact, name, size, digest) in restore.LINUX15579_ESBUILD_ARTIFACTS.items():
            identities.append(dict(restore.LINUX15579_ESBUILD_IDENTITY, arch=arch, artifact_id=artifact,
                                   artifact_name=name, artifact_size_in_bytes=size, artifact_digest=digest))
        for identity in identities:
            self.assertTrue(restore.is_known_external_link(LINKS[0], "linux", identity))
            self.assertFalse(restore.is_known_external_link(LINKS[2], "linux", identity))
        self.assertNotIn(LINKS[2], restore.HOST_LINKS)

    def test_omitted_tsc_is_recorded_and_never_recreated(self):
        fixture = old154.Linux154RestoreLinksTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        cache = fixture.fixture
        for identity in IDENTITIES:
            cache.make_cache("linux", identity["arch"])
            relative = cache.donor.relative_to(cache.cache / "tree").as_posix()
            result = dict(skipped_external_symlinks=3,
                          external_symlink_paths=[relative + "/" + name for name in LINKS])
            self.assertEqual(restore.missing_host_links(cache.cache, cache.donor, result,
                                                       "linux", identity), sorted(LINKS))
            self.assertTrue(all(not (cache.donor / name).exists() for name in LINKS))
            result["external_symlink_paths"][-1] += "-other"
            with self.assertRaisesRegex(restore.Miss, "unknown external symlink"):
                restore.missing_host_links(cache.cache, cache.donor, result, "linux", identity)
