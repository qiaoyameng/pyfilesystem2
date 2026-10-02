from __future__ import unicode_literals

import calendar
import datetime
import errno
import hashlib
import os
import shutil
import tempfile
import unittest
from parameterized import parameterized

import fs.copy
from fs import open_fs
from fs.wrapfs import WrapFS


class _ShortReadFile(object):
    """Proxy for a readable file object that returns short reads."""

    def __init__(self, file, read_size):
        self._file = file
        self._read_size = read_size

    def read(self, size=-1):
        if size is None or size < 0:
            size = self._read_size
        return self._file.read(min(size, self._read_size))

    def __getattr__(self, name):
        return getattr(self._file, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._file.close()


class _ShortWriteFile(object):
    """Proxy for a writable file object that performs short writes."""

    def __init__(self, file, write_size):
        self._file = file
        self._write_size = write_size

    def write(self, data):
        return self._file.write(bytes(data[: self._write_size]))

    def __getattr__(self, name):
        return getattr(self._file, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._file.close()


class ShortIOFS(WrapFS):
    """Filesystem wrapper returning short-read / short-write streams."""

    def __init__(self, wrap_fs, read_size=None, write_size=None):
        self._read_size = read_size
        self._write_size = write_size
        super(ShortIOFS, self).__init__(wrap_fs)

    def openbin(self, path, mode="r", buffering=-1, **options):
        file = super(ShortIOFS, self).openbin(
            path, mode=mode, buffering=buffering, **options
        )
        if "r" in mode and self._read_size is not None:
            return _ShortReadFile(file, self._read_size)
        if any(m in mode for m in "wax+") and self._write_size is not None:
            return _ShortWriteFile(file, self._write_size)
        return file


def _create_sandbox_dir(prefix="pyfilesystem2_sandbox_", home=None):
    if home is None:
        return tempfile.mkdtemp(prefix=prefix)
    else:
        sandbox_path = os.path.join(home, prefix)
        mkdirp(sandbox_path)
        return sandbox_path


def _touch(root, filepath):
    # create abs filename
    abs_filepath = os.path.join(root, filepath)
    # create dir
    dirname = os.path.dirname(abs_filepath)
    mkdirp(dirname)
    # touch file
    with open(abs_filepath, "a"):
        os.utime(
            abs_filepath, None
        )  # update the mtime in case the file exists, same as touch

    return abs_filepath


def _write_file(filepath, write_chars=1024):
    with open(filepath, "w") as f:
        f.write("1" * write_chars)
    return filepath


def _delay_file_utime(filepath, delta_sec):
    utcnow = datetime.datetime.utcnow()
    unix_timestamp = calendar.timegm(utcnow.timetuple())
    times = unix_timestamp + delta_sec, unix_timestamp + delta_sec
    os.utime(filepath, times)


def mkdirp(path):
    # os.makedirs(path, exist_ok=True) only for python3.?
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise


class TestCopySimple(unittest.TestCase):
    @parameterized.expand([(0,), (1,), (2,), (4,)])
    def test_copy_fs(self, workers):
        namespaces = ("details", "modified")

        src_fs = open_fs("mem://")
        src_fs.makedirs("foo/bar")
        src_fs.makedirs("foo/empty")
        src_fs.touch("test.txt")
        src_fs.touch("foo/bar/baz.txt")
        src_file1_info = src_fs.getinfo("test.txt", namespaces)
        src_file2_info = src_fs.getinfo("foo/bar/baz.txt", namespaces)

        dst_fs = open_fs("mem://")
        fs.copy.copy_fs(src_fs, dst_fs, workers=workers, preserve_time=True)

        self.assertTrue(dst_fs.isdir("foo/empty"))
        self.assertTrue(dst_fs.isdir("foo/bar"))
        self.assertTrue(dst_fs.isfile("test.txt"))

        dst_file1_info = dst_fs.getinfo("test.txt", namespaces)
        dst_file2_info = dst_fs.getinfo("foo/bar/baz.txt", namespaces)
        self.assertEqual(dst_file1_info.modified, src_file1_info.modified)
        self.assertEqual(dst_file2_info.modified, src_file2_info.modified)

    def test_copy_value_error(self):
        src_fs = open_fs("mem://")
        dst_fs = open_fs("mem://")
        with self.assertRaises(ValueError):
            fs.copy.copy_fs(src_fs, dst_fs, workers=-1)

    def test_copy_dir0(self):
        namespaces = ("details", "modified")

        src_fs = open_fs("mem://")
        src_fs.makedirs("foo/bar")
        src_fs.makedirs("foo/empty")
        src_fs.touch("test.txt")
        src_fs.touch("foo/bar/baz.txt")
        src_file2_info = src_fs.getinfo("foo/bar/baz.txt", namespaces)

        with open_fs("mem://") as dst_fs:
            fs.copy.copy_dir(src_fs, "/foo", dst_fs, "/", workers=0, preserve_time=True)
            self.assertTrue(dst_fs.isdir("bar"))
            self.assertTrue(dst_fs.isdir("empty"))
            self.assertTrue(dst_fs.isfile("bar/baz.txt"))

            dst_file2_info = dst_fs.getinfo("bar/baz.txt", namespaces)
            self.assertEqual(dst_file2_info.modified, src_file2_info.modified)

    @parameterized.expand([(0,), (1,), (2,), (4,)])
    def test_copy_dir(self, workers):
        namespaces = ("details", "modified")

        src_fs = open_fs("mem://")
        src_fs.makedirs("foo/bar")
        src_fs.makedirs("foo/empty")
        src_fs.touch("test.txt")
        src_fs.touch("foo/bar/baz.txt")
        src_file2_info = src_fs.getinfo("foo/bar/baz.txt", namespaces)

        with open_fs("mem://") as dst_fs:
            fs.copy.copy_dir(
                src_fs, "/foo", dst_fs, "/", workers=workers, preserve_time=True
            )
            self.assertTrue(dst_fs.isdir("bar"))
            self.assertTrue(dst_fs.isdir("empty"))
            self.assertTrue(dst_fs.isfile("bar/baz.txt"))

            dst_file2_info = dst_fs.getinfo("bar/baz.txt", namespaces)
            self.assertEqual(dst_file2_info.modified, src_file2_info.modified)

    def test_copy_large(self):
        data1 = b"foo" * 512 * 1024
        data2 = b"bar" * 2 * 512 * 1024
        data3 = b"baz" * 3 * 512 * 1024
        data4 = b"egg" * 7 * 512 * 1024
        with open_fs("temp://") as src_fs:
            src_fs.writebytes("foo", data1)
            src_fs.writebytes("bar", data2)
            src_fs.makedir("dir1").writebytes("baz", data3)
            src_fs.makedirs("dir2/dir3").writebytes("egg", data4)
            for workers in (0, 1, 2, 4):
                with open_fs("temp://") as dst_fs:
                    fs.copy.copy_fs(src_fs, dst_fs, workers=workers)
                    self.assertEqual(dst_fs.readbytes("foo"), data1)
                    self.assertEqual(dst_fs.readbytes("bar"), data2)
                    self.assertEqual(dst_fs.readbytes("dir1/baz"), data3)
                    self.assertEqual(dst_fs.readbytes("dir2/dir3/egg"), data4)

    def test_copy_dir_on_copy(self):
        src_fs = open_fs("mem://")
        src_fs.touch("baz.txt")

        on_copy_calls = []

        def on_copy(*args):
            on_copy_calls.append(args)

        dst_fs = open_fs("mem://")
        fs.copy.copy_dir(src_fs, "/", dst_fs, "/", on_copy=on_copy)
        self.assertEqual(on_copy_calls, [(src_fs, "/baz.txt", dst_fs, "/baz.txt")])


class TestCopyIfNewer(unittest.TestCase):
    copy_if_condition = "newer"

    def test_copy_file_if_same_fs(self):
        src_fs = open_fs("mem://")
        src_fs.makedir("foo2").touch("exists")
        src_fs.makedir("foo1").touch("test1.txt")
        src_fs.settimes(
            "foo2/exists", datetime.datetime.utcnow() + datetime.timedelta(hours=1)
        )
        self.assertTrue(
            fs.copy.copy_file_if(
                src_fs,
                "foo1/test1.txt",
                src_fs,
                "foo2/test1.txt.copy",
                self.copy_if_condition,
            )
        )
        self.assertFalse(
            fs.copy.copy_file_if(
                src_fs, "foo1/test1.txt", src_fs, "foo2/exists", self.copy_if_condition
            )
        )
        self.assertTrue(src_fs.exists("foo2/test1.txt.copy"))

    def test_copy_file_if_dst_is_older(self):
        try:
            # create first dst ==> dst is older the src ==> file should be copied
            dst_dir = _create_sandbox_dir()
            dst_file1 = _touch(dst_dir, "file1.txt")
            _write_file(dst_file1)

            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "file1.txt")
            _write_file(src_file1)

            # ensure src file is newer than dst, changing its modification time
            _delay_file_utime(src_file1, delta_sec=60)

            src_fs = open_fs("osfs://" + src_dir)
            dst_fs = open_fs("osfs://" + dst_dir)

            self.assertTrue(dst_fs.exists("/file1.txt"))

            copied = fs.copy.copy_file_if(
                src_fs, "/file1.txt", dst_fs, "/file1.txt", self.copy_if_condition
            )

            self.assertTrue(copied)
            self.assertTrue(dst_fs.exists("/file1.txt"))
        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)

    def test_copy_file_if_dst_doesnt_exists(self):
        try:
            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "file1.txt")
            _write_file(src_file1)

            dst_dir = _create_sandbox_dir()

            src_fs = open_fs("osfs://" + src_dir)
            dst_fs = open_fs("osfs://" + dst_dir)

            copied = fs.copy.copy_file_if(
                src_fs, "/file1.txt", dst_fs, "/file1.txt", self.copy_if_condition
            )

            self.assertTrue(copied)
            self.assertTrue(dst_fs.exists("/file1.txt"))
        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)

    def test_copy_file_if_dst_is_newer(self):
        try:
            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "file1.txt")
            _write_file(src_file1)

            dst_dir = _create_sandbox_dir()
            dst_file1 = _touch(dst_dir, "file1.txt")
            _write_file(dst_file1)

            # ensure dst file is newer than src, changing its modification time
            _delay_file_utime(dst_file1, delta_sec=60)

            src_fs = open_fs("osfs://" + src_dir)
            dst_fs = open_fs("osfs://" + dst_dir)

            self.assertTrue(dst_fs.exists("/file1.txt"))

            copied = fs.copy.copy_file_if(
                src_fs, "/file1.txt", dst_fs, "/file1.txt", self.copy_if_condition
            )

            self.assertFalse(copied)
        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)

    def test_copy_fs_if(self):
        try:
            dst_dir = _create_sandbox_dir()
            dst_file1 = _touch(dst_dir, "file1.txt")
            dst_file2 = _touch(dst_dir, "file2.txt")
            _write_file(dst_file1)
            _write_file(dst_file2)

            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "file1.txt")
            src_file2 = _touch(src_dir, "file2.txt")
            src_file3 = _touch(src_dir, "file3.txt")
            _write_file(src_file1)
            _write_file(src_file2)
            _write_file(src_file3)

            # ensure src_file1 is newer than dst_file1, changing its modification time
            # ensure dst_file2 is newer than src_file2, changing its modification time
            _delay_file_utime(src_file1, delta_sec=60)
            _delay_file_utime(dst_file2, delta_sec=60)

            src_fs = open_fs("osfs://" + src_dir)
            dst_fs = open_fs("osfs://" + dst_dir)

            self.assertTrue(dst_fs.exists("/file1.txt"))
            self.assertTrue(dst_fs.exists("/file2.txt"))

            copied = []

            def on_copy(src_fs, src_path, dst_fs, dst_path):
                copied.append(dst_path)

            fs.copy.copy_fs_if(
                src_fs, dst_fs, on_copy=on_copy, condition=self.copy_if_condition
            )

            self.assertTrue("/file1.txt" in copied)
            self.assertTrue("/file2.txt" not in copied)
            self.assertTrue("/file3.txt" in copied)
            self.assertTrue(dst_fs.exists("/file1.txt"))
            self.assertTrue(dst_fs.exists("/file2.txt"))
            self.assertTrue(dst_fs.exists("/file3.txt"))

            src_fs.close()
            dst_fs.close()

        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)

    def test_copy_dir_if(self):
        try:
            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "file1.txt")
            _write_file(src_file1)

            src_file2 = _touch(src_dir, os.path.join("one_level_down", "file2.txt"))
            _write_file(src_file2)

            dst_dir = _create_sandbox_dir()
            mkdirp(os.path.join(dst_dir, "target_dir"))
            dst_file1 = _touch(dst_dir, os.path.join("target_dir", "file1.txt"))
            _write_file(dst_file1)

            # ensure dst file is newer than src, changing its modification time
            _delay_file_utime(dst_file1, delta_sec=60)

            src_fs = open_fs("osfs://" + src_dir)
            dst_fs = open_fs("osfs://" + dst_dir)

            copied = []

            def on_copy(src_fs, src_path, dst_fs, dst_path):
                copied.append(dst_path)

            fs.copy.copy_dir_if(
                src_fs,
                "/",
                dst_fs,
                "/target_dir/",
                on_copy=on_copy,
                condition=self.copy_if_condition,
            )

            self.assertEqual(copied, ["/target_dir/one_level_down/file2.txt"])
            self.assertTrue(dst_fs.exists("/target_dir/one_level_down/file2.txt"))

            src_fs.close()
            dst_fs.close()
        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)

    def test_copy_dir_if_same_fs(self):
        try:
            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "src" + os.sep + "file1.txt")
            _write_file(src_file1)

            _create_sandbox_dir(home=src_dir)

            src_fs = open_fs("osfs://" + src_dir)

            copied = []

            def on_copy(src_fs, src_path, dst_fs, dst_path):
                copied.append(dst_path)

            fs.copy.copy_dir_if(
                src_fs, "/src", src_fs, "/dst", on_copy=on_copy, condition="newer"
            )

            self.assertEqual(copied, ["/dst/file1.txt"])
            self.assertTrue(src_fs.exists("/dst/file1.txt"))

            src_fs.close()

        finally:
            shutil.rmtree(src_dir)

    def test_copy_dir_if_multiple_files(self):
        try:
            src_dir = _create_sandbox_dir()
            src_fs = open_fs("osfs://" + src_dir)
            src_fs.makedirs("foo/bar")
            src_fs.makedirs("foo/empty")
            src_fs.touch("test.txt")
            src_fs.touch("foo/bar/baz.txt")

            dst_dir = _create_sandbox_dir()
            dst_fs = open_fs("osfs://" + dst_dir)

            fs.copy.copy_dir_if(src_fs, "/foo", dst_fs, "/", condition="newer")

            self.assertTrue(dst_fs.isdir("bar"))
            self.assertTrue(dst_fs.isdir("empty"))
            self.assertTrue(dst_fs.isfile("bar/baz.txt"))
        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)


class TestCopyIfOlder(unittest.TestCase):
    copy_if_condition = "older"

    def test_copy_file_if_same_fs(self):
        src_fs = open_fs("mem://")
        src_fs.makedir("foo2").touch("exists")
        src_fs.makedir("foo1").touch("test1.txt")
        src_fs.settimes(
            "foo2/exists", datetime.datetime.utcnow() - datetime.timedelta(hours=1)
        )
        self.assertTrue(
            fs.copy.copy_file_if(
                src_fs,
                "foo1/test1.txt",
                src_fs,
                "foo2/test1.txt.copy",
                self.copy_if_condition,
            )
        )
        self.assertFalse(
            fs.copy.copy_file_if(
                src_fs, "foo1/test1.txt", src_fs, "foo2/exists", self.copy_if_condition
            )
        )
        self.assertTrue(src_fs.exists("foo2/test1.txt.copy"))

    def test_copy_file_if_dst_is_older(self):
        try:
            # create first dst ==> dst is older the src ==> file should be copied
            dst_dir = _create_sandbox_dir()
            dst_file1 = _touch(dst_dir, "file1.txt")
            _write_file(dst_file1)

            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "file1.txt")
            _write_file(src_file1)

            # ensure src file is newer than dst, changing its modification time
            _delay_file_utime(src_file1, delta_sec=60)

            src_fs = open_fs("osfs://" + src_dir)
            dst_fs = open_fs("osfs://" + dst_dir)

            self.assertTrue(dst_fs.exists("/file1.txt"))

            copied = fs.copy.copy_file_if(
                src_fs, "/file1.txt", dst_fs, "/file1.txt", self.copy_if_condition
            )

            self.assertFalse(copied)
            self.assertTrue(dst_fs.exists("/file1.txt"))
        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)

    def test_copy_file_if_dst_doesnt_exists(self):
        try:
            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "file1.txt")
            _write_file(src_file1)

            dst_dir = _create_sandbox_dir()

            src_fs = open_fs("osfs://" + src_dir)
            dst_fs = open_fs("osfs://" + dst_dir)

            copied = fs.copy.copy_file_if(
                src_fs, "/file1.txt", dst_fs, "/file1.txt", self.copy_if_condition
            )

            self.assertTrue(copied)
            self.assertTrue(dst_fs.exists("/file1.txt"))
        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)

    def test_copy_file_if_dst_is_newer(self):
        try:
            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "file1.txt")
            _write_file(src_file1)

            dst_dir = _create_sandbox_dir()
            dst_file1 = _touch(dst_dir, "file1.txt")
            _write_file(dst_file1)

            # ensure dst file is newer than src, changing its modification time
            _delay_file_utime(dst_file1, delta_sec=60)

            src_fs = open_fs("osfs://" + src_dir)
            dst_fs = open_fs("osfs://" + dst_dir)

            self.assertTrue(dst_fs.exists("/file1.txt"))

            copied = fs.copy.copy_file_if(
                src_fs, "/file1.txt", dst_fs, "/file1.txt", self.copy_if_condition
            )

            self.assertTrue(copied)
        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)

    def test_copy_fs_if(self):
        try:
            dst_dir = _create_sandbox_dir()
            dst_file1 = _touch(dst_dir, "file1.txt")
            dst_file2 = _touch(dst_dir, "file2.txt")
            _write_file(dst_file1)
            _write_file(dst_file2)

            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "file1.txt")
            src_file2 = _touch(src_dir, "file2.txt")
            src_file3 = _touch(src_dir, "file3.txt")
            _write_file(src_file1)
            _write_file(src_file2)
            _write_file(src_file3)

            # ensure src_file1 is newer than dst_file1, changing its modification time
            # ensure dst_file2 is newer than src_file2, changing its modification time
            _delay_file_utime(src_file1, delta_sec=60)
            _delay_file_utime(dst_file2, delta_sec=60)

            src_fs = open_fs("osfs://" + src_dir)
            dst_fs = open_fs("osfs://" + dst_dir)

            self.assertTrue(dst_fs.exists("/file1.txt"))
            self.assertTrue(dst_fs.exists("/file2.txt"))

            copied = []

            def on_copy(src_fs, src_path, dst_fs, dst_path):
                copied.append(dst_path)

            fs.copy.copy_fs_if(
                src_fs, dst_fs, on_copy=on_copy, condition=self.copy_if_condition
            )

            self.assertTrue("/file1.txt" not in copied)
            self.assertTrue("/file2.txt" in copied)
            self.assertTrue("/file3.txt" in copied)
            self.assertTrue(dst_fs.exists("/file1.txt"))
            self.assertTrue(dst_fs.exists("/file2.txt"))
            self.assertTrue(dst_fs.exists("/file3.txt"))

            src_fs.close()
            dst_fs.close()

        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)

    def test_copy_dir_if(self):
        try:
            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "file1.txt")
            _write_file(src_file1)

            src_file2 = _touch(src_dir, os.path.join("one_level_down", "file2.txt"))
            _write_file(src_file2)

            dst_dir = _create_sandbox_dir()
            mkdirp(os.path.join(dst_dir, "target_dir"))
            dst_file1 = _touch(dst_dir, os.path.join("target_dir", "file1.txt"))
            _write_file(dst_file1)

            # ensure src file is newer than dst, changing its modification time
            _delay_file_utime(src_file1, delta_sec=60)

            src_fs = open_fs("osfs://" + src_dir)
            dst_fs = open_fs("osfs://" + dst_dir)

            copied = []

            def on_copy(src_fs, src_path, dst_fs, dst_path):
                copied.append(dst_path)

            fs.copy.copy_dir_if(
                src_fs,
                "/",
                dst_fs,
                "/target_dir/",
                on_copy=on_copy,
                condition=self.copy_if_condition,
            )

            self.assertEqual(copied, ["/target_dir/one_level_down/file2.txt"])
            self.assertTrue(dst_fs.exists("/target_dir/one_level_down/file2.txt"))

            src_fs.close()
            dst_fs.close()
        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)

    def test_copy_dir_if_same_fs(self):
        try:
            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "src" + os.sep + "file1.txt")
            _write_file(src_file1)

            _create_sandbox_dir(home=src_dir)

            src_fs = open_fs("osfs://" + src_dir)

            copied = []

            def on_copy(src_fs, src_path, dst_fs, dst_path):
                copied.append(dst_path)

            fs.copy.copy_dir_if(
                src_fs, "/src", src_fs, "/dst", on_copy=on_copy, condition="newer"
            )

            self.assertEqual(copied, ["/dst/file1.txt"])
            self.assertTrue(src_fs.exists("/dst/file1.txt"))

            src_fs.close()

        finally:
            shutil.rmtree(src_dir)

    def test_copy_dir_if_multiple_files(self):
        try:
            src_dir = _create_sandbox_dir()
            src_fs = open_fs("osfs://" + src_dir)
            src_fs.makedirs("foo/bar")
            src_fs.makedirs("foo/empty")
            src_fs.touch("test.txt")
            src_fs.touch("foo/bar/baz.txt")

            dst_dir = _create_sandbox_dir()
            dst_fs = open_fs("osfs://" + dst_dir)

            fs.copy.copy_dir_if(src_fs, "/foo", dst_fs, "/", condition="newer")

            self.assertTrue(dst_fs.isdir("bar"))
            self.assertTrue(dst_fs.isdir("empty"))
            self.assertTrue(dst_fs.isfile("bar/baz.txt"))
        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)


class TestCopyIfExists(unittest.TestCase):
    copy_if_condition = "exists"

    def test_copy_file_if_same_fs(self):
        src_fs = open_fs("mem://")
        src_fs.makedir("foo2").touch("exists")
        src_fs.makedir("foo1").touch("test1.txt")
        self.assertFalse(
            fs.copy.copy_file_if(
                src_fs,
                "foo1/test1.txt",
                src_fs,
                "foo2/test1.txt.copy",
                self.copy_if_condition,
            )
        )
        self.assertTrue(
            fs.copy.copy_file_if(
                src_fs, "foo1/test1.txt", src_fs, "foo2/exists", self.copy_if_condition
            )
        )
        self.assertFalse(src_fs.exists("foo2/test1.txt.copy"))

    def test_copy_file_if_dst_doesnt_exists(self):
        try:
            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "file1.txt")
            _write_file(src_file1)

            dst_dir = _create_sandbox_dir()

            src_fs = open_fs("osfs://" + src_dir)
            dst_fs = open_fs("osfs://" + dst_dir)

            copied = fs.copy.copy_file_if(
                src_fs, "/file1.txt", dst_fs, "/file1.txt", self.copy_if_condition
            )

            self.assertFalse(copied)
            self.assertFalse(dst_fs.exists("/file1.txt"))
        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)

    def test_copy_file_if_dst_exists(self):
        try:
            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "file1.txt")
            _write_file(src_file1)

            dst_dir = _create_sandbox_dir()
            dst_file1 = _touch(dst_dir, "file1.txt")
            _write_file(dst_file1)

            src_fs = open_fs("osfs://" + src_dir)
            dst_fs = open_fs("osfs://" + dst_dir)

            self.assertTrue(dst_fs.exists("/file1.txt"))

            copied = fs.copy.copy_file_if(
                src_fs, "/file1.txt", dst_fs, "/file1.txt", self.copy_if_condition
            )

            self.assertTrue(copied)
        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)

    def test_copy_fs_if(self):
        try:
            dst_dir = _create_sandbox_dir()
            dst_file1 = _touch(dst_dir, "file1.txt")
            _write_file(dst_file1)

            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "file1.txt")
            src_file2 = _touch(src_dir, "file2.txt")
            _write_file(src_file1)
            _write_file(src_file2)

            src_fs = open_fs("osfs://" + src_dir)
            dst_fs = open_fs("osfs://" + dst_dir)

            self.assertTrue(dst_fs.exists("/file1.txt"))

            copied = []

            def on_copy(src_fs, src_path, dst_fs, dst_path):
                copied.append(dst_path)

            fs.copy.copy_fs_if(
                src_fs, dst_fs, on_copy=on_copy, condition=self.copy_if_condition
            )

            self.assertEqual(copied, ["/file1.txt"])
            self.assertTrue(dst_fs.exists("/file1.txt"))
            self.assertFalse(dst_fs.exists("/file2.txt"))

            src_fs.close()
            dst_fs.close()

        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)

    def test_copy_dir_if(self):
        try:
            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "file1.txt")
            _write_file(src_file1)

            src_file2 = _touch(src_dir, os.path.join("one_level_down", "file2.txt"))
            _write_file(src_file2)

            dst_dir = _create_sandbox_dir()
            mkdirp(os.path.join(dst_dir, "target_dir"))
            dst_file1 = _touch(dst_dir, os.path.join("target_dir", "file1.txt"))
            _write_file(dst_file1)

            src_fs = open_fs("osfs://" + src_dir)
            dst_fs = open_fs("osfs://" + dst_dir)

            copied = []

            def on_copy(src_fs, src_path, dst_fs, dst_path):
                copied.append(dst_path)

            fs.copy.copy_dir_if(
                src_fs,
                "/",
                dst_fs,
                "/target_dir/",
                on_copy=on_copy,
                condition=self.copy_if_condition,
            )

            self.assertEqual(copied, ["/target_dir/file1.txt"])
            self.assertFalse(dst_fs.exists("/target_dir/one_level_down/file2.txt"))

            src_fs.close()
            dst_fs.close()
        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)

    def test_copy_dir_if_same_fs(self):
        try:
            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "src" + os.sep + "file1.txt")
            _write_file(src_file1)

            _create_sandbox_dir(home=src_dir)

            src_fs = open_fs("osfs://" + src_dir)

            copied = []

            def on_copy(src_fs, src_path, dst_fs, dst_path):
                copied.append(dst_path)

            fs.copy.copy_dir_if(
                src_fs, "/src", src_fs, "/dst", on_copy=on_copy, condition="newer"
            )

            self.assertEqual(copied, ["/dst/file1.txt"])
            self.assertTrue(src_fs.exists("/dst/file1.txt"))

            src_fs.close()

        finally:
            shutil.rmtree(src_dir)

    def test_copy_dir_if_multiple_files(self):
        try:
            src_dir = _create_sandbox_dir()
            src_fs = open_fs("osfs://" + src_dir)
            src_fs.makedirs("foo/bar")
            src_fs.makedirs("foo/empty")
            src_fs.touch("test.txt")
            src_fs.touch("foo/bar/baz.txt")

            dst_dir = _create_sandbox_dir()
            dst_fs = open_fs("osfs://" + dst_dir)

            fs.copy.copy_dir_if(src_fs, "/foo", dst_fs, "/", condition="newer")

            self.assertTrue(dst_fs.isdir("bar"))
            self.assertTrue(dst_fs.isdir("empty"))
            self.assertTrue(dst_fs.isfile("bar/baz.txt"))
        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)


class TestCopyIfNotExists(unittest.TestCase):
    copy_if_condition = "not_exists"

    def test_copy_file_if_same_fs(self):
        src_fs = open_fs("mem://")
        src_fs.makedir("foo2").touch("exists")
        src_fs.makedir("foo1").touch("test1.txt")
        self.assertTrue(
            fs.copy.copy_file_if(
                src_fs,
                "foo1/test1.txt",
                src_fs,
                "foo2/test1.txt.copy",
                self.copy_if_condition,
            )
        )
        self.assertFalse(
            fs.copy.copy_file_if(
                src_fs, "foo1/test1.txt", src_fs, "foo2/exists", self.copy_if_condition
            )
        )
        self.assertTrue(src_fs.exists("foo2/test1.txt.copy"))

    def test_copy_file_if_dst_doesnt_exists(self):
        try:
            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "file1.txt")
            _write_file(src_file1)

            dst_dir = _create_sandbox_dir()

            src_fs = open_fs("osfs://" + src_dir)
            dst_fs = open_fs("osfs://" + dst_dir)

            copied = fs.copy.copy_file_if(
                src_fs, "/file1.txt", dst_fs, "/file1.txt", self.copy_if_condition
            )

            self.assertTrue(copied)
            self.assertTrue(dst_fs.exists("/file1.txt"))
        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)

    def test_copy_file_if_dst_exists(self):
        try:
            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "file1.txt")
            _write_file(src_file1)

            dst_dir = _create_sandbox_dir()
            dst_file1 = _touch(dst_dir, "file1.txt")
            _write_file(dst_file1)

            src_fs = open_fs("osfs://" + src_dir)
            dst_fs = open_fs("osfs://" + dst_dir)

            self.assertTrue(dst_fs.exists("/file1.txt"))

            copied = fs.copy.copy_file_if(
                src_fs, "/file1.txt", dst_fs, "/file1.txt", self.copy_if_condition
            )

            self.assertFalse(copied)
            self.assertTrue(dst_fs.exists("/file1.txt"))
        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)

    def test_copy_fs_if(self):
        try:
            dst_dir = _create_sandbox_dir()
            dst_file1 = _touch(dst_dir, "file1.txt")
            _write_file(dst_file1)

            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "file1.txt")
            src_file2 = _touch(src_dir, "file2.txt")
            _write_file(src_file1)
            _write_file(src_file2)

            src_fs = open_fs("osfs://" + src_dir)
            dst_fs = open_fs("osfs://" + dst_dir)

            self.assertTrue(dst_fs.exists("/file1.txt"))

            copied = []

            def on_copy(src_fs, src_path, dst_fs, dst_path):
                copied.append(dst_path)

            fs.copy.copy_fs_if(
                src_fs, dst_fs, on_copy=on_copy, condition=self.copy_if_condition
            )

            self.assertEqual(copied, ["/file2.txt"])
            self.assertTrue(dst_fs.exists("/file1.txt"))
            self.assertTrue(dst_fs.exists("/file2.txt"))

            src_fs.close()
            dst_fs.close()

        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)

    def test_copy_dir_if(self):
        try:
            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "file1.txt")
            _write_file(src_file1)

            src_file2 = _touch(src_dir, os.path.join("one_level_down", "file2.txt"))
            _write_file(src_file2)

            dst_dir = _create_sandbox_dir()
            mkdirp(os.path.join(dst_dir, "target_dir"))
            dst_file1 = _touch(dst_dir, os.path.join("target_dir", "file1.txt"))
            _write_file(dst_file1)

            src_fs = open_fs("osfs://" + src_dir)
            dst_fs = open_fs("osfs://" + dst_dir)

            copied = []

            def on_copy(src_fs, src_path, dst_fs, dst_path):
                copied.append(dst_path)

            fs.copy.copy_dir_if(
                src_fs,
                "/",
                dst_fs,
                "/target_dir/",
                on_copy=on_copy,
                condition=self.copy_if_condition,
            )

            self.assertEqual(copied, ["/target_dir/one_level_down/file2.txt"])
            self.assertTrue(dst_fs.exists("/target_dir/file1.txt"))
            self.assertTrue(dst_fs.exists("/target_dir/one_level_down/file2.txt"))

            src_fs.close()
            dst_fs.close()
        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)

    def test_copy_dir_if_same_fs(self):
        try:
            src_dir = _create_sandbox_dir()
            src_file1 = _touch(src_dir, "src" + os.sep + "file1.txt")
            _write_file(src_file1)

            _create_sandbox_dir(home=src_dir)

            src_fs = open_fs("osfs://" + src_dir)

            copied = []

            def on_copy(src_fs, src_path, dst_fs, dst_path):
                copied.append(dst_path)

            fs.copy.copy_dir_if(
                src_fs, "/src", src_fs, "/dst", on_copy=on_copy, condition="newer"
            )

            self.assertEqual(copied, ["/dst/file1.txt"])
            self.assertTrue(src_fs.exists("/dst/file1.txt"))

            src_fs.close()

        finally:
            shutil.rmtree(src_dir)

    def test_copy_dir_if_multiple_files(self):
        try:
            src_dir = _create_sandbox_dir()
            src_fs = open_fs("osfs://" + src_dir)
            src_fs.makedirs("foo/bar")
            src_fs.makedirs("foo/empty")
            src_fs.touch("test.txt")
            src_fs.touch("foo/bar/baz.txt")

            dst_dir = _create_sandbox_dir()
            dst_fs = open_fs("osfs://" + dst_dir)

            fs.copy.copy_dir_if(src_fs, "/foo", dst_fs, "/", condition="newer")

            self.assertTrue(dst_fs.isdir("bar"))
            self.assertTrue(dst_fs.isdir("empty"))
            self.assertTrue(dst_fs.isfile("bar/baz.txt"))
        finally:
            shutil.rmtree(src_dir)
            shutil.rmtree(dst_dir)


class TestCopyFileAtomic(unittest.TestCase):
    def setUp(self):
        self.src_dir = _create_sandbox_dir()
        self.dst_dir = _create_sandbox_dir()
        self.src_fs = open_fs("osfs://" + self.src_dir)
        self.dst_fs = open_fs("osfs://" + self.dst_dir)
        self.data = bytes(
            bytearray((i * 31 + 7) % 256 for i in range(256 * 1024 + 13))
        )
        self.src_fs.writebytes("src.bin", self.data)
        self.data_hash = hashlib.sha256(self.data).hexdigest()

    def tearDown(self):
        self.src_fs.close()
        self.dst_fs.close()
        shutil.rmtree(self.src_dir, ignore_errors=True)
        shutil.rmtree(self.dst_dir, ignore_errors=True)

    def _wrap(self, read_size=None, write_size=None):
        src_fs = ShortIOFS(self.src_fs, read_size=read_size)
        dst_fs = ShortIOFS(self.dst_fs, write_size=write_size)
        return src_fs, dst_fs

    def test_complete_copy(self):
        src_fs, dst_fs = self._wrap(read_size=1000, write_size=777)
        progress_calls = []

        def progress(written, total):
            progress_calls.append((written, total))

        result = fs.copy.copy_file_atomic(
            src_fs,
            "src.bin",
            dst_fs,
            "dst.bin",
            progress=progress,
            chunk_size=4096,
        )

        self.assertEqual(result, len(self.data))
        self.assertEqual(self.dst_fs.getsize("dst.bin"), len(self.data))
        self.assertEqual(self.dst_fs.readbytes("dst.bin"), self.data)
        self.assertEqual(self.dst_fs.hash("dst.bin", "sha256"), self.data_hash)
        # progress reports bytes actually written, ending at the total
        self.assertTrue(progress_calls)
        self.assertTrue(all(total == len(self.data) for _, total in progress_calls))
        writtens = [written for written, _ in progress_calls]
        self.assertEqual(writtens, sorted(writtens))
        self.assertEqual(writtens[-1], len(self.data))
        # short writes mean progress is reported per write, not per read
        self.assertGreater(len(writtens), (len(self.data) + 4095) // 4096)
        # no temporary files left behind
        self.assertEqual(self.dst_fs.listdir("/"), ["dst.bin"])

    def test_cancel_mid_copy_keeps_destination(self):
        old_data = b"old contents that must survive"
        self.dst_fs.writebytes("dst.bin", old_data)
        src_fs, dst_fs = self._wrap(read_size=1000, write_size=777)

        def progress(written, total):
            return written < 50000

        with self.assertRaises(fs.copy.CopyCancelled):
            fs.copy.copy_file_atomic(
                src_fs,
                "src.bin",
                dst_fs,
                "dst.bin",
                progress=progress,
                chunk_size=4096,
            )

        # the pre-existing destination is untouched
        self.assertEqual(self.dst_fs.readbytes("dst.bin"), old_data)
        # the temporary file created by the copy has been cleaned up
        self.assertEqual(self.dst_fs.listdir("/"), ["dst.bin"])

    def test_callback_exception_cleans_up(self):
        self.dst_fs.writebytes("dst.bin", b"old")

        def progress(written, total):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            fs.copy.copy_file_atomic(
                self.src_fs, "src.bin", self.dst_fs, "dst.bin", progress=progress
            )
        self.assertEqual(self.dst_fs.readbytes("dst.bin"), b"old")
        self.assertEqual(self.dst_fs.listdir("/"), ["dst.bin"])

    def test_same_path_rejected(self):
        with self.assertRaises(fs.errors.IllegalDestination):
            fs.copy.copy_file_atomic(self.src_fs, "src.bin", self.src_fs, "src.bin")

    def test_no_overwrite(self):
        self.dst_fs.writebytes("dst.bin", b"old")
        with self.assertRaises(fs.errors.DestinationExists):
            fs.copy.copy_file_atomic(
                self.src_fs, "src.bin", self.dst_fs, "dst.bin", overwrite=False
            )
        self.assertEqual(self.dst_fs.readbytes("dst.bin"), b"old")

    def test_destination_directory_rejected(self):
        self.dst_fs.makedir("dst.bin")
        with self.assertRaises(fs.errors.FileExpected):
            fs.copy.copy_file_atomic(self.src_fs, "src.bin", self.dst_fs, "dst.bin")

    def test_non_atomic_backend_refused(self):
        mem_fs = open_fs("mem://")
        try:
            with self.assertRaises(fs.errors.Unsupported):
                fs.copy.copy_file_atomic(self.src_fs, "src.bin", mem_fs, "dst.bin")
            self.assertFalse(mem_fs.exists("dst.bin"))
        finally:
            mem_fs.close()

    def test_non_atomic_backend_opt_in(self):
        mem_fs = open_fs("mem://")
        try:
            result = fs.copy.copy_file_atomic(
                self.src_fs,
                "src.bin",
                mem_fs,
                "dst.bin",
                allow_non_atomic=True,
            )
            self.assertEqual(result, len(self.data))
            self.assertEqual(mem_fs.readbytes("dst.bin"), self.data)
            self.assertEqual(mem_fs.listdir("/"), ["dst.bin"])
        finally:
            mem_fs.close()

    def test_temp_name_does_not_clobber_user_files(self):
        # a user file that merely looks like a temporary name is left alone
        self.dst_fs.writebytes(".dst.bin.deadbeef.tmp", b"user data")
        fs.copy.copy_file_atomic(self.src_fs, "src.bin", self.dst_fs, "dst.bin")
        self.assertEqual(
            self.dst_fs.readbytes(".dst.bin.deadbeef.tmp"), b"user data"
        )
        self.assertEqual(self.dst_fs.readbytes("dst.bin"), self.data)
        self.assertEqual(
            sorted(self.dst_fs.listdir("/")),
            [".dst.bin.deadbeef.tmp", "dst.bin"],
        )


if __name__ == "__main__":
    unittest.main()
