from __future__ import unicode_literals

import hashlib
import os
import unittest

from fs.manifest import (
    EntryType,
    ManifestEntry,
    build_manifest,
    diff_manifests,
)
from fs.memoryfs import MemoryFS
from fs.tempfs import TempFS


def populate(fs):
    """Create an equivalent tree on any filesystem."""
    fs.makedirs("foo/bar")
    fs.setbytes("foo/bar/one.txt", b"one")
    fs.setbytes("foo/two.txt", b"two two")
    fs.setbytes("empty.bin", b"")
    fs.makedirs("dir with spaces")
    fs.setbytes("dir with spaces/three.txt", b"three")


class MutatingMemoryFS(MemoryFS):
    """A MemoryFS that rewrites a file every time it is opened for reading."""

    def __init__(self):
        super(MutatingMemoryFS, self).__init__()
        self.mutations = 0

    def openbin(self, path, mode="r", buffering=-1, **options):
        if mode == "r" and path == "/mutating.bin":
            self.mutations += 1
            self.setbytes(path, b"mutation %d" % self.mutations)
        return super(MutatingMemoryFS, self).openbin(path, mode, buffering, **options)


class TestBuildManifest(unittest.TestCase):
    def test_equivalent_trees_match_across_backends(self):
        memory_fs = MemoryFS()
        temp_fs = TempFS()
        try:
            populate(memory_fs)
            populate(temp_fs)
            memory_manifest = build_manifest(memory_fs)
            temp_manifest = build_manifest(temp_fs)
        finally:
            memory_fs.close()
            temp_fs.close()
        self.assertEqual(memory_manifest, temp_manifest)
        self.assertEqual(memory_manifest.content_keys(), temp_manifest.content_keys())
        self.assertEqual(memory_manifest.unstable, ())
        self.assertEqual(temp_manifest.unstable, ())

    def test_entries_sorted_by_path(self):
        fs = MemoryFS()
        populate(fs)
        manifest = build_manifest(fs)
        paths = [entry.path for entry in manifest]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(paths[0], "dir with spaces")

    def test_file_records(self):
        fs = MemoryFS()
        populate(fs)
        manifest = build_manifest(fs)
        entry = manifest["foo/two.txt"]
        self.assertEqual(entry.type, EntryType.FILE)
        self.assertEqual(entry.size, 7)
        self.assertEqual(entry.digest, hashlib.sha256(b"two two").hexdigest())
        self.assertTrue(entry.stable)
        empty = manifest["empty.bin"]
        self.assertEqual(empty.size, 0)
        self.assertEqual(empty.digest, hashlib.sha256(b"").hexdigest())

    def test_directory_records(self):
        fs = MemoryFS()
        populate(fs)
        manifest = build_manifest(fs)
        entry = manifest["foo/bar"]
        self.assertEqual(entry.type, EntryType.DIRECTORY)
        self.assertIsNone(entry.size)
        self.assertIsNone(entry.digest)

    def test_symlink_records(self):
        fs = TempFS()
        try:
            fs.setbytes("target.txt", b"data")
            os.symlink("target.txt", fs.getsyspath("link.txt"))
            manifest = build_manifest(fs)
        finally:
            fs.close()
        entry = manifest["link.txt"]
        self.assertEqual(entry.type, EntryType.SYMLINK)
        self.assertEqual(entry.target, "target.txt")
        self.assertIsNone(entry.digest)

    def test_subdirectory_root(self):
        fs = MemoryFS()
        populate(fs)
        manifest = build_manifest(fs, "foo")
        self.assertIn("bar/one.txt", manifest)
        self.assertIn("two.txt", manifest)
        self.assertNotIn("empty.bin", manifest)

    def test_unstable_file_is_flagged(self):
        fs = MutatingMemoryFS()
        fs.setbytes("mutating.bin", b"original")
        fs.setbytes("quiet.bin", b"quiet")
        manifest = build_manifest(fs, retries=2)
        entry = manifest["mutating.bin"]
        self.assertFalse(entry.stable)
        self.assertEqual(entry.attempts, 3)
        self.assertEqual(manifest.unstable, ("mutating.bin",))
        self.assertTrue(manifest["quiet.bin"].stable)

    def test_case_conflicts(self):
        fs = MemoryFS()
        fs.setbytes("Case.txt", b"upper")
        fs.setbytes("case.txt", b"lower")
        fs.setbytes("other.txt", b"other")
        manifest = build_manifest(fs)
        self.assertFalse(manifest.case_insensitive)
        self.assertEqual(manifest.case_conflicts, (("Case.txt", "case.txt"),))

    def test_no_case_conflicts(self):
        fs = MemoryFS()
        populate(fs)
        manifest = build_manifest(fs)
        self.assertEqual(manifest.case_conflicts, ())


class TestDiffManifests(unittest.TestCase):
    def make_manifest(self):
        fs = MemoryFS()
        populate(fs)
        return fs, build_manifest(fs)

    def test_identical_trees(self):
        fs, old = self.make_manifest()
        new = build_manifest(fs)
        diff = diff_manifests(old, new)
        self.assertTrue(diff.is_empty)
        self.assertFalse(diff)
        self.assertEqual(old, new)

    def test_added_removed_changed(self):
        fs, old = self.make_manifest()
        fs.setbytes("new.txt", b"new")
        fs.remove("empty.bin")
        fs.setbytes("foo/two.txt", b"changed")
        new = build_manifest(fs)
        diff = old.diff(new)
        self.assertEqual(diff.added, ("new.txt",))
        self.assertEqual(diff.removed, ("empty.bin",))
        self.assertEqual(diff.changed, ("foo/two.txt",))
        self.assertEqual(diff.type_conflicts, ())
        self.assertTrue(diff)
        self.assertNotEqual(old, new)

    def test_type_conflicts(self):
        fs, old = self.make_manifest()
        fs.remove("foo/two.txt")
        fs.makedir("foo/two.txt")
        new = build_manifest(fs)
        diff = diff_manifests(old, new)
        self.assertEqual(diff.type_conflicts, ("foo/two.txt",))
        self.assertEqual(diff.changed, ())
        self.assertEqual(diff.added, ())
        self.assertEqual(diff.removed, ())

    def test_entry_equality_ignores_stability(self):
        entry_a = ManifestEntry("a.txt", EntryType.FILE, size=1, digest="x", stable=True)
        entry_b = ManifestEntry("a.txt", EntryType.FILE, size=1, digest="x", stable=False)
        self.assertEqual(entry_a, entry_b)
        entry_c = ManifestEntry("a.txt", EntryType.FILE, size=2, digest="x")
        self.assertNotEqual(entry_a, entry_c)
