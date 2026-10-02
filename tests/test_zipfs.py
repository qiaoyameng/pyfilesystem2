# -*- encoding: UTF-8
from __future__ import unicode_literals

import sys

import os
import six
import tempfile
import unittest
import zipfile

from fs import zipfs
from fs.compress import write_zip
from fs.enums import Seek
from fs.errors import FileExpected, InvalidPath, NoURL, ResourceNotFound
from fs.opener import open_fs
from fs.opener.errors import NotWriteable
from fs.test import FSTestCases

from .test_archives import ArchiveTestCases


class TestWriteReadZipFS(unittest.TestCase):
    def setUp(self):
        fh, self._temp_path = tempfile.mkstemp()
        os.close(fh)

    def tearDown(self):
        os.remove(self._temp_path)

    def test_unicode_paths(self):
        # https://github.com/PyFilesystem/pyfilesystem2/issues/135
        with zipfs.ZipFS(self._temp_path, write=True) as zip_fs:
            zip_fs.writetext("Файл", "some content")

        with zipfs.ZipFS(self._temp_path) as zip_fs:
            paths = list(zip_fs.walk.files())
            for path in paths:
                self.assertIsInstance(path, six.text_type)
                with zip_fs.openbin(path) as f:
                    f.read()


class TestWriteZipFS(FSTestCases, unittest.TestCase):
    """
    Test ZIPFS implementation.

    When writing, a ZipFS is essentially a TempFS.

    """

    def make_fs(self):
        _zip_file = tempfile.TemporaryFile()
        fs = zipfs.ZipFS(_zip_file, write=True)
        fs._zip_file = _zip_file
        return fs

    def destroy_fs(self, fs):
        fs.close()
        del fs._zip_file


class TestReadZipFS(ArchiveTestCases, unittest.TestCase):
    """
    Test Reading zip files.

    """

    def compress(self, fs):
        fh, self._temp_path = tempfile.mkstemp()
        os.close(fh)
        write_zip(fs, self._temp_path)

    def load_archive(self):
        return zipfs.ZipFS(self._temp_path)

    def remove_archive(self):
        os.remove(self._temp_path)

    def test_large(self):
        test_fs = open_fs("mem://")
        test_fs.writebytes("test.bin", b"a" * 50000)
        write_zip(test_fs, self._temp_path)

        self.fs = self.load_archive()

        with self.fs.openbin("test.bin") as f:
            self.assertEqual(f.read(), b"a" * 50000)
        with self.fs.openbin("test.bin") as f:
            self.assertEqual(f.read(50000), b"a" * 50000)
        with self.fs.openbin("test.bin") as f:
            self.assertEqual(f.read1(), b"a" * 50000)
        with self.fs.openbin("test.bin") as f:
            self.assertEqual(f.read1(50000), b"a" * 50000)

    def test_getinfo(self):
        super(TestReadZipFS, self).test_getinfo()
        top = self.fs.getinfo("top.txt", ["zip"])
        if sys.platform in ("linux", "darwin"):
            self.assertEqual(top.get("zip", "create_system"), 3)

    def test_openbin(self):
        with self.fs.openbin("top.txt") as f:
            self.assertEqual(f.name, "top.txt")
        with self.fs.openbin("top.txt") as f:
            self.assertRaises(ValueError, f.seek, -2, Seek.set)
        with self.fs.openbin("top.txt") as f:
            self.assertRaises(ValueError, f.seek, 2, Seek.end)
        with self.fs.openbin("top.txt") as f:
            self.assertRaises(ValueError, f.seek, 0, 5)

    def test_read(self):
        with self.fs.openbin("top.txt") as f:
            self.assertEqual(f.read(), b"Hello, World")
        with self.fs.openbin("top.txt") as f:
            self.assertEqual(f.read(5), b"Hello")
            self.assertEqual(f.read(7), b", World")
        with self.fs.openbin("top.txt") as f:
            self.assertEqual(f.read(12), b"Hello, World")

    def test_read1(self):
        with self.fs.openbin("top.txt") as f:
            self.assertEqual(f.read1(), b"Hello, World")
        with self.fs.openbin("top.txt") as f:
            self.assertEqual(f.read1(5), b"Hello")
            self.assertEqual(f.read1(7), b", World")
        with self.fs.openbin("top.txt") as f:
            self.assertEqual(f.read1(12), b"Hello, World")

    def test_seek_set(self):
        with self.fs.openbin("top.txt") as f:
            self.assertEqual(f.tell(), 0)
            self.assertEqual(f.read(), b"Hello, World")
            self.assertEqual(f.tell(), 12)
            self.assertEqual(f.read(), b"")
            self.assertEqual(f.tell(), 12)
            self.assertEqual(f.seek(0), 0)
            self.assertEqual(f.tell(), 0)
            self.assertEqual(f.read1(), b"Hello, World")
            self.assertEqual(f.tell(), 12)
            self.assertEqual(f.seek(1), 1)
            self.assertEqual(f.tell(), 1)
            self.assertEqual(f.read(), b"ello, World")
            self.assertEqual(f.tell(), 12)
            self.assertEqual(f.seek(7), 7)
            self.assertEqual(f.tell(), 7)
            self.assertEqual(f.read(), b"World")
            self.assertEqual(f.tell(), 12)

    def test_seek_current(self):
        with self.fs.openbin("top.txt") as f:
            self.assertEqual(f.tell(), 0)
            self.assertEqual(f.read(5), b"Hello")
            self.assertEqual(f.tell(), 5)
            self.assertEqual(f.seek(2, Seek.current), 7)
            self.assertEqual(f.read1(), b"World")
            self.assertEqual(f.tell(), 12)
            self.assertEqual(f.seek(-1, Seek.current), 11)
            self.assertEqual(f.read(), b"d")
        with self.fs.openbin("top.txt") as f:
            self.assertRaises(ValueError, f.seek, -1, Seek.current)

    def test_seek_end(self):
        with self.fs.openbin("top.txt") as f:
            self.assertEqual(f.tell(), 0)
            self.assertEqual(f.seek(-12, Seek.end), 0)
            self.assertEqual(f.read1(5), b"Hello")
            self.assertEqual(f.seek(-7, Seek.end), 5)
            self.assertEqual(f.seek(-5, Seek.end), 7)
            self.assertEqual(f.read(), b"World")

    def test_geturl_for_fs(self):
        test_file = "foo/bar/egg/foofoo"
        expected = "zip://{zip_file_path}!/{file_inside_zip}".format(
            zip_file_path=self._temp_path.replace("\\", "/"), file_inside_zip=test_file
        )
        self.assertEqual(self.fs.geturl(test_file, purpose="fs"), expected)

    def test_geturl_for_fs_but_file_is_binaryio(self):
        self.fs._file = six.BytesIO()
        self.assertRaises(NoURL, self.fs.geturl, "test", "fs")

    def test_geturl_for_download(self):
        test_file = "foo/bar/egg/foofoo"
        with self.assertRaises(NoURL):
            self.fs.geturl(test_file)

    def test_read_non_existent_file(self):
        fs = zipfs.ZipFS(open(self._temp_path, "rb"))
        # it has been very difficult to catch exception in __del__()
        del fs._zip
        try:
            fs.close()
        except AttributeError:
            self.fail("Could not close tar fs properly")
        except Exception:
            self.fail("Strange exception in closing fs")


class TestReadZipFSMem(TestReadZipFS):
    def make_source_fs(self):
        return open_fs("mem://")


class TestDirsZipFS(unittest.TestCase):
    def test_implied(self):
        """Test zipfs creates intermediate directories."""
        fh, path = tempfile.mkstemp("testzip.zip")
        try:
            os.close(fh)
            with zipfile.ZipFile(path, mode="w") as z:
                z.writestr("foo/bar/baz/egg", b"hello")
            with zipfs.ReadZipFS(path) as zip_fs:
                foo = zip_fs.getinfo("foo", ["details"])
                self.assertEqual(zip_fs.getinfo("foo/bar").name, "bar")
                self.assertEqual(zip_fs.getinfo("foo/bar/baz").name, "baz")
                self.assertTrue(foo.is_dir)
                self.assertTrue(zip_fs.isfile("foo/bar/baz/egg"))
        finally:
            os.remove(path)


class TestReadZipEntries(unittest.TestCase):
    def make_archive(self):
        fd, path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("duplicate.txt", b"first")
            archive.writestr("duplicate.txt", b"second")
            archive.writestr("collision", b"file")
            archive.writestr("collision/", b"")
            archive.writestr("./normalized", b"dot")
            archive.writestr("normalized", b"plain")
            archive.writestr("./safe.txt", b"safe")
            archive.writestr("implicit/child.txt", b"child")
            archive.writestr("block", b"blocker")
            archive.writestr("block/grand/child.txt", b"blocked child")
            archive.writestr("../outside.txt", b"outside")
            archive.writestr("/absolute.txt", b"absolute")
        return path

    def test_duplicate_entries_have_stable_identities(self):
        path = self.make_archive()
        try:
            with zipfs.ReadZipFS(path) as archive_fs:
                entries = archive_fs.get_entries("duplicate.txt")
                self.assertEqual(len(entries), 2)
                self.assertEqual(
                    [archive_fs.read_entry(entry) for entry in entries],
                    [b"first", b"second"],
                )
                self.assertEqual(
                    [archive_fs.read_entry(entry.index) for entry in entries],
                    [b"first", b"second"],
                )
                self.assertEqual(archive_fs.readbytes("duplicate.txt"), b"second")
                self.assertEqual(entries[0].size, 5)
                self.assertEqual(entries[1].size, 6)
                self.assertEqual(entries[0].compress_type, zipfile.ZIP_DEFLATED)
                self.assertNotEqual(entries[0].crc, entries[1].crc)

                with archive_fs.open_entry(entries[0]) as bin_file:
                    self.assertEqual(bin_file.read(), b"first")
                with archive_fs.open_entry(1) as bin_file:
                    self.assertEqual(bin_file.read(), b"second")

                identities = [(entry.index, entry.crc) for entry in entries]

            with zipfs.ReadZipFS(path) as archive_fs:
                reopened_entries = archive_fs.get_entries("duplicate.txt")
                self.assertEqual(
                    [(entry.index, entry.crc) for entry in reopened_entries],
                    identities,
                )
                self.assertEqual(
                    [archive_fs.read_entry(index) for index, _ in identities],
                    [b"first", b"second"],
                )
        finally:
            os.remove(path)

    def test_duplicate_path_policy(self):
        path = self.make_archive()
        try:
            with zipfs.ReadZipFS(path, duplicate="first") as archive_fs:
                self.assertEqual(archive_fs.readbytes("duplicate.txt"), b"first")
            with zipfs.ReadZipFS(path, duplicate="error") as archive_fs:
                with self.assertRaises(InvalidPath):
                    archive_fs.readbytes("duplicate.txt")
                self.assertEqual(archive_fs.read_entry(0), b"first")
                self.assertEqual(archive_fs.read_entry(1), b"second")
        finally:
            os.remove(path)

    def test_duplicate_directory_policy_uses_central_order(self):
        fd, path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        try:
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("dir/", b"")
                archive.writestr("dir/child.txt", b"first child")
                archive.writestr("dir/", b"")
                archive.writestr("dir/child.txt", b"second child")

            with zipfs.ReadZipFS(path, duplicate="first") as archive_fs:
                self.assertEqual(
                    archive_fs.readbytes("dir/child.txt"), b"first child"
                )
                self.assertEqual(archive_fs.listdir("dir"), ["child.txt"])
            with zipfs.ReadZipFS(path, duplicate="last") as archive_fs:
                self.assertEqual(
                    archive_fs.readbytes("dir/child.txt"), b"second child"
                )
            with zipfs.ReadZipFS(path, duplicate="error") as archive_fs:
                with self.assertRaises(InvalidPath):
                    archive_fs.readbytes("dir/child.txt")
                self.assertEqual(
                    [
                        archive_fs.read_entry(entry)
                        for entry in archive_fs.get_entries("dir/child.txt")
                    ],
                    [b"first child", b"second child"],
                )
        finally:
            os.remove(path)

    def test_path_collisions_and_implicit_directories_are_diagnosable(self):
        path = self.make_archive()
        try:
            with zipfs.ReadZipFS(path) as archive_fs:
                collision_entries = archive_fs.get_entries("collision")
                self.assertEqual(
                    {entry.issues[0] for entry in collision_entries},
                    {"type_collision"},
                )
                with self.assertRaises(InvalidPath):
                    archive_fs.getinfo("collision")

                normalized_entries = archive_fs.get_entries("normalized")
                self.assertTrue(
                    all(
                        "normalized_collision" in entry.issues
                        for entry in normalized_entries
                    )
                )
                with self.assertRaises(InvalidPath):
                    archive_fs.openbin("normalized")
                self.assertEqual(archive_fs.readbytes("safe.txt"), b"safe")

                implicit = archive_fs.getinfo("implicit", ["details", "zip"])
                self.assertTrue(implicit.is_dir)
                self.assertTrue(implicit.get("zip", "implicit"))
                self.assertEqual(
                    archive_fs.readbytes("implicit/child.txt"), b"child"
                )

                blocker = next(
                    entry
                    for entry in archive_fs.get_entries()
                    if entry.name == "block"
                )
                blocked_child = next(
                    entry
                    for entry in archive_fs.get_entries()
                    if entry.name == "block/grand/child.txt"
                )
                self.assertEqual(blocker.issues, ("type_collision",))
                self.assertIn(
                    "ancestor_not_directory", blocked_child.issues
                )
                with self.assertRaises(InvalidPath):
                    archive_fs.getinfo("block")
                with self.assertRaises(InvalidPath):
                    archive_fs.getinfo("block/grand")
                with self.assertRaises(InvalidPath):
                    archive_fs.readbytes("block/grand/child.txt")
                self.assertEqual(archive_fs.read_entry(blocker), b"blocker")
                self.assertEqual(
                    archive_fs.read_entry(blocked_child), b"blocked child"
                )
        finally:
            os.remove(path)

    def test_unsafe_member_names_remain_entry_only(self):
        path = self.make_archive()
        try:
            with zipfs.ReadZipFS(path) as archive_fs:
                unsafe = {
                    entry.name: entry
                    for entry in archive_fs.get_entries()
                    if entry.path is None
                }
                self.assertEqual(unsafe["../outside.txt"].issues, ("unsafe_path",))
                self.assertEqual(
                    unsafe["/absolute.txt"].issues, ("absolute_path",)
                )
                self.assertEqual(
                    archive_fs.read_entry(unsafe["../outside.txt"]), b"outside"
                )
                self.assertEqual(
                    archive_fs.read_entry(unsafe["/absolute.txt"]), b"absolute"
                )

                with self.assertRaises(ResourceNotFound):
                    archive_fs.openbin("/absolute.txt")
                with self.assertRaises(ResourceNotFound):
                    archive_fs.openbin("outside.txt")
                with self.assertRaises(FileExpected):
                    directory_entry = next(
                        entry
                        for entry in archive_fs.get_entries()
                        if entry.name == "collision/"
                    )
                    archive_fs.open_entry(directory_entry)
        finally:
            os.remove(path)


class TestOpener(unittest.TestCase):
    def test_not_writeable(self):
        with self.assertRaises(NotWriteable):
            open_fs("zip://foo.zip", writeable=True)
