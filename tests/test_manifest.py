from __future__ import unicode_literals

import hashlib
import os
import shutil
import tempfile
import unittest

from fs import errors
from fs.info import Info
from fs.manifest import (
    TYPE_DIRECTORY,
    TYPE_FILE,
    TYPE_SYMLINK,
    Manifest,
    ManifestEntry,
    compare_manifests,
    generate_manifest,
)
from fs.memoryfs import MemoryFS
from fs.osfs import OSFS
from fs.wrapfs import WrapFS


def _populate(fs):
    """Create an equivalent tree on any filesystem."""
    fs.makedirs("alpha/beta")
    fs.makedirs("empty")
    fs.writetext("alpha/one.txt", "one")
    fs.writetext("alpha/beta/two.txt", "two two")
    fs.writetext("gamma.bin", "0123" * 1000)


class _CaseInsensitiveMemoryFS(MemoryFS):
    """A MemoryFS that reports itself as case-insensitive."""

    def getmeta(self, namespace="standard"):
        meta = super(_CaseInsensitiveMemoryFS, self).getmeta(namespace)
        if namespace == "standard":
            meta["case_insensitive"] = True
        return meta


class _UnstableFS(WrapFS):
    """A proxy filesystem whose file metadata changes on every lookup."""

    def __init__(self, wrap_fs):
        super(_UnstableFS, self).__init__(wrap_fs)
        self._tick = 0
        self.read_count = 0

    def getinfo(self, path, namespaces=None):
        info = super(_UnstableFS, self).getinfo(path, namespaces)
        if namespaces and "details" in namespaces and info.is_file:
            self._tick += 1
            raw = dict(info.raw)
            details = dict(raw.get("details", {}))
            details["modified"] = float(self._tick)
            raw["details"] = details
            info = Info(raw)
        return info

    def openbin(self, path, mode="r", buffering=-1, **options):
        self.read_count += 1
        return super(_UnstableFS, self).openbin(path, mode, buffering, **options)


class TestGenerateManifest(unittest.TestCase):
    def test_memory_and_osfs_equal(self):
        mem_fs = MemoryFS()
        _populate(mem_fs)
        tmp_dir = tempfile.mkdtemp()
        try:
            os_fs = OSFS(tmp_dir)
            _populate(os_fs)
            mem_manifest = generate_manifest(mem_fs)
            os_manifest = generate_manifest(os_fs)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.assertTrue(mem_manifest.stable)
        self.assertTrue(os_manifest.stable)
        self.assertEqual(mem_manifest, os_manifest)
        self.assertTrue(compare_manifests(mem_manifest, os_manifest).equal)

    def test_entry_records(self):
        fs = MemoryFS()
        _populate(fs)
        manifest = generate_manifest(fs)

        by_path = {entry.path: entry for entry in manifest}
        self.assertEqual(
            sorted(by_path),
            [
                "alpha",
                "alpha/beta",
                "alpha/beta/two.txt",
                "alpha/one.txt",
                "empty",
                "gamma.bin",
            ],
        )
        self.assertEqual(by_path["alpha"].type, TYPE_DIRECTORY)
        self.assertIsNone(by_path["alpha"].digest)
        entry = by_path["alpha/one.txt"]
        self.assertEqual(entry.type, TYPE_FILE)
        self.assertEqual(entry.size, 3)
        self.assertEqual(
            entry.digest, hashlib.sha256(b"one").hexdigest()
        )
        self.assertTrue(entry.stable)

    def test_entries_sorted(self):
        fs = MemoryFS()
        fs.makedir("m")
        for name in ["z.txt", "a.txt", "m/k.txt", "m/a.txt"]:
            fs.writetext(name, "data")
        manifest = generate_manifest(fs)
        paths = [entry.path for entry in manifest]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(manifest.paths, tuple(paths))

    def test_subdirectory_root(self):
        fs = MemoryFS()
        _populate(fs)
        manifest = generate_manifest(fs, "alpha")
        self.assertEqual(
            manifest.paths,
            ("alpha/beta", "alpha/beta/two.txt", "alpha/one.txt"),
        )

    def test_unsupported_hash(self):
        fs = MemoryFS()
        fs.writetext("foo.txt", "foo")
        with self.assertRaises(errors.UnsupportedHash):
            generate_manifest(fs, hash_name="nosuchhash")

    def test_unstable_detected(self):
        fs = _UnstableFS(MemoryFS())
        fs.writetext("data.txt", "some data")
        manifest = generate_manifest(fs, retries=2)

        self.assertFalse(manifest.stable)
        self.assertEqual(manifest.unstable, ("data.txt",))
        entry = manifest.get("data.txt")
        self.assertFalse(entry.stable)
        # Best-effort record is still present.
        self.assertEqual(entry.size, len("some data"))
        self.assertEqual(entry.digest, hashlib.sha256(b"some data").hexdigest())
        # Limited retries: 1 initial attempt + 2 retries.
        self.assertEqual(fs.read_count, 3)

    def test_stable_after_retry(self):
        fs = _UnstableFS(MemoryFS())
        fs.writetext("data.txt", "some data")
        original_getinfo = fs.getinfo
        calls = []

        def flaky_getinfo(path, namespaces=None):
            info = original_getinfo(path, namespaces)
            if len(calls) >= 2:
                # Stop mutating metadata after the first attempt.
                if not hasattr(flaky_getinfo, "_frozen"):
                    flaky_getinfo._frozen = info
                return flaky_getinfo._frozen
            calls.append(path)
            return info

        fs.getinfo = flaky_getinfo
        manifest = generate_manifest(fs, retries=2)
        self.assertTrue(manifest.stable)

    def test_vanishing_file_unstable(self):
        fs = MemoryFS()
        fs.writetext("gone.txt", "here today")
        original_openbin = fs.openbin

        def removing_openbin(path, mode="r", buffering=-1, **options):
            fs.remove("gone.txt")
            return original_openbin(path, mode, buffering, **options)

        fs.openbin = removing_openbin
        manifest = generate_manifest(fs, retries=1)
        entry = manifest.get("gone.txt")
        self.assertFalse(entry.stable)
        self.assertIsNone(entry.digest)
        self.assertFalse(manifest.stable)

    def test_case_conflict_diagnosed(self):
        fs = _CaseInsensitiveMemoryFS()
        fs.writetext("Foo.txt", "a")
        fs.writetext("foo.txt", "b")
        manifest = generate_manifest(fs)
        self.assertTrue(manifest.case_insensitive)
        self.assertEqual(len(manifest.warnings), 1)
        self.assertIn("Foo.txt", manifest.warnings[0])
        self.assertIn("foo.txt", manifest.warnings[0])

    def test_no_case_conflict_on_case_sensitive(self):
        fs = MemoryFS()
        fs.writetext("Foo.txt", "a")
        fs.writetext("foo.txt", "b")
        manifest = generate_manifest(fs)
        self.assertFalse(manifest.case_insensitive)
        self.assertEqual(manifest.warnings, ())

    @unittest.skipUnless(hasattr(os, "symlink"), "No symlink support")
    def test_symlink_recorded(self):
        tmp_dir = tempfile.mkdtemp()
        try:
            fs = OSFS(tmp_dir)
            fs.writetext("target.txt", "data")
            os.symlink("target.txt", os.path.join(tmp_dir, "link.txt"))
            manifest = generate_manifest(fs)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        entry = manifest.get("link.txt")
        self.assertEqual(entry.type, TYPE_SYMLINK)
        self.assertEqual(entry.link_target, "target.txt")
        self.assertIsNone(entry.digest)
        self.assertTrue(manifest.stable)


class TestCompareManifests(unittest.TestCase):
    def _make_fs(self):
        fs = MemoryFS()
        _populate(fs)
        return fs

    def test_equal_manifests(self):
        manifest_a = generate_manifest(self._make_fs())
        manifest_b = generate_manifest(self._make_fs())
        diff = compare_manifests(manifest_a, manifest_b)
        self.assertTrue(diff.equal)
        self.assertEqual(diff.added, ())
        self.assertEqual(diff.removed, ())
        self.assertEqual(diff.changed, ())
        self.assertEqual(diff.type_conflicts, ())

    def test_added_removed_changed(self):
        manifest_a = generate_manifest(self._make_fs())
        fs = self._make_fs()
        fs.writetext("new.txt", "new")
        fs.remove("alpha/one.txt")
        fs.writetext("gamma.bin", "different content")
        manifest_b = generate_manifest(fs)

        diff = compare_manifests(manifest_a, manifest_b)
        self.assertFalse(diff.equal)
        self.assertEqual(diff.added, ("new.txt",))
        self.assertEqual(diff.removed, ("alpha/one.txt",))
        self.assertEqual(diff.changed, ("gamma.bin",))
        self.assertEqual(diff.type_conflicts, ())

    def test_type_conflict(self):
        manifest_a = generate_manifest(self._make_fs())
        fs = self._make_fs()
        fs.remove("alpha/one.txt")
        fs.makedir("alpha/one.txt")
        manifest_b = generate_manifest(fs)

        diff = compare_manifests(manifest_a, manifest_b)
        self.assertEqual(diff.type_conflicts, ("alpha/one.txt",))
        self.assertNotIn("alpha/one.txt", diff.changed)

    def test_unstable_propagated(self):
        fs = _UnstableFS(self._make_fs())
        unstable_manifest = generate_manifest(fs)
        stable_manifest = generate_manifest(self._make_fs())
        diff = compare_manifests(stable_manifest, unstable_manifest)
        self.assertIn("alpha/one.txt", diff.unstable)

    def test_hash_mismatch_raises(self):
        manifest_md5 = generate_manifest(self._make_fs(), hash_name="md5")
        manifest_sha = generate_manifest(self._make_fs(), hash_name="sha256")
        with self.assertRaises(ValueError):
            compare_manifests(manifest_md5, manifest_sha)


class TestManifestObjects(unittest.TestCase):
    def test_entry_equality_ignores_stability(self):
        entry_a = ManifestEntry("a.txt", TYPE_FILE, size=1, digest="x", stable=True)
        entry_b = ManifestEntry("a.txt", TYPE_FILE, size=1, digest="x", stable=False)
        self.assertEqual(entry_a, entry_b)
        self.assertNotEqual(entry_a, ManifestEntry("a.txt", TYPE_DIRECTORY))

    def test_manifest_get_normalizes(self):
        manifest = Manifest(
            [ManifestEntry("foo/bar.txt", TYPE_FILE, size=1, digest="x")], "sha256"
        )
        self.assertIsNotNone(manifest.get("/foo//bar.txt"))
        self.assertIsNone(manifest.get("foo/baz.txt"))
