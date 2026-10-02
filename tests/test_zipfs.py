# -*- encoding: UTF-8
from __future__ import unicode_literals

import sys

import os
import six
import tempfile
import unittest
import warnings
import zipfile

from fs import zipfs
from fs.compress import write_zip
from fs.enums import Seek
from fs.errors import (
    FileExpected,
    IllegalBackReference,
    NoURL,
    ResourceInvalid,
    ResourceNotFound,
)
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


class TestZipMembers(unittest.TestCase):
    def setUp(self):
        fh, self._temp_path = tempfile.mkstemp("testzip-members.zip")
        os.close(fh)

        duplicate_entries = [
            ("dup", b"first", zipfile.ZIP_STORED),
            ("dup", b"second", zipfile.ZIP_DEFLATED),
            ("clash", b"file", zipfile.ZIP_STORED),
            ("clash/", b"", zipfile.ZIP_STORED),
            ("implicit", b"parent-file", zipfile.ZIP_STORED),
            ("implicit/child", b"child", zipfile.ZIP_DEFLATED),
            ("./normalized", b"dot", zipfile.ZIP_STORED),
            ("normalized", b"plain", zipfile.ZIP_DEFLATED),
            ("/etc/passwd", b"absolute", zipfile.ZIP_STORED),
            ("etc/passwd", b"safe-absolute", zipfile.ZIP_DEFLATED),
            ("../escape", b"escape", zipfile.ZIP_STORED),
            ("escape", b"safe-escape", zipfile.ZIP_DEFLATED),
        ]

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with zipfile.ZipFile(self._temp_path, mode="w") as archive:
                for name, data, compression in duplicate_entries:
                    info = zipfile.ZipInfo(name)
                    info.compress_type = compression
                    archive.writestr(info, data)

    def tearDown(self):
        os.remove(self._temp_path)

    def test_members_identify_central_directory_entries(self):
        with zipfs.ReadZipFS(self._temp_path) as archive_fs:
            members = archive_fs.getmembers()
            self.assertEqual(len(members), 12)
            self.assertEqual(
                [(member.index, member.raw_name) for member in members],
                [
                    (0, "dup"),
                    (1, "dup"),
                    (2, "clash"),
                    (3, "clash/"),
                    (4, "implicit"),
                    (5, "implicit/child"),
                    (6, "./normalized"),
                    (7, "normalized"),
                    (8, "/etc/passwd"),
                    (9, "etc/passwd"),
                    (10, "../escape"),
                    (11, "escape"),
                ],
            )

            first = archive_fs.getmember(0)
            second = archive_fs.getmember(1)
            self.assertEqual(first.file_size, 5)
            self.assertEqual(second.file_size, 6)
            self.assertEqual(first.crc, first.zip_info.CRC)
            self.assertEqual(second.crc, second.zip_info.CRC)
            self.assertEqual(first.compress_type, zipfile.ZIP_STORED)
            self.assertEqual(second.compress_type, zipfile.ZIP_DEFLATED)
            self.assertIn("entries: 0, 1", first.issue)
            self.assertIn("entries: 0, 1", second.issue)
            self.assertIn("entries: 6, 7", archive_fs.getmember(6).issue)

            self.assertEqual(archive_fs.readmember(first), b"first")
            self.assertEqual(archive_fs.readmember(1), b"second")
            self.assertEqual(
                archive_fs.readmember(second.zip_info), b"second"
            )
            with archive_fs.openmember(second) as member_file:
                self.assertEqual(member_file.read(), b"second")

            member_data = [
                b"first",
                b"second",
                b"file",
                None,
                b"parent-file",
                b"child",
                b"dot",
                b"plain",
                b"absolute",
                b"safe-absolute",
                b"escape",
                b"safe-escape",
            ]
            for index, data in enumerate(member_data):
                if data is not None:
                    self.assertEqual(archive_fs.readmember(index), data)

            with self.assertRaises(ResourceNotFound):
                archive_fs.getmember(-1)
            with self.assertRaises(ResourceNotFound):
                archive_fs.getmember(12)

        with zipfs.ReadZipFS(self._temp_path) as archive_fs:
            other = zipfs.ReadZipFS(self._temp_path)
            try:
                member = other.getmember(0)
                with self.assertRaises(ResourceNotFound):
                    archive_fs.getmember(member)
            finally:
                other.close()

    def test_path_access_reports_collisions(self):
        with zipfs.ReadZipFS(self._temp_path) as archive_fs:
            for path in (
                "dup",
                "clash",
                "implicit",
                "normalized",
                "etc/passwd",
                "escape",
            ):
                with self.assertRaises(ResourceInvalid):
                    archive_fs.getinfo(path)
                with self.assertRaises(ResourceInvalid):
                    archive_fs.openbin(path)

            with self.assertRaises(IllegalBackReference):
                archive_fs.openbin("../etc/passwd")

    def test_unsafe_members_remain_available_by_identity(self):
        with zipfs.ReadZipFS(self._temp_path) as archive_fs:
            absolute = archive_fs.getmember(8)
            parent = archive_fs.getmember(10)
            self.assertEqual(absolute.path, "etc/passwd")
            self.assertIn(
                "absolute path treated as archive-relative", absolute.issue
            )
            self.assertIn("entries: 8, 9", absolute.issue)
            self.assertEqual(parent.path, "escape")
            self.assertIn(
                "parent directory reference removed from root", parent.issue
            )
            self.assertIn("entries: 10, 11", parent.issue)
            self.assertEqual(archive_fs.readmember(absolute), b"absolute")
            self.assertEqual(archive_fs.readmember(parent), b"escape")

            directory = archive_fs.getmember(3)
            with self.assertRaises(FileExpected):
                archive_fs.openmember(directory)

    def test_member_identity_is_stable_after_reopen(self):
        with zipfs.ReadZipFS(self._temp_path) as archive_fs:
            first = archive_fs.getmember(0)
            second = archive_fs.getmember(1)
            first_bytes = archive_fs.readmember(first)
            second_bytes = archive_fs.readmember(second)

        with zipfs.ReadZipFS(self._temp_path) as archive_fs:
            reopened_first = archive_fs.getmember(0)
            reopened_second = archive_fs.getmember(1)
            self.assertEqual(first, reopened_first)
            self.assertEqual(second, reopened_second)
            self.assertEqual(archive_fs.readmember(reopened_first), first_bytes)
            self.assertEqual(archive_fs.readmember(reopened_second), second_bytes)


class TestOpener(unittest.TestCase):
    def test_not_writeable(self):
        with self.assertRaises(NotWriteable):
            open_fs("zip://foo.zip", writeable=True)
