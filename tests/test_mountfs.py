from __future__ import unicode_literals

import unittest

from fs.memoryfs import MemoryFS
from fs.mountfs import MountError, MountFS
from fs.tempfs import TempFS
from fs.test import FSTestCases


class TestMountFS(FSTestCases, unittest.TestCase):
    """Test OSFS implementation."""

    def make_fs(self):
        fs = MountFS()
        mem_fs = MemoryFS()
        fs.mount("/", mem_fs)
        return fs


class TestMountFS2(FSTestCases, unittest.TestCase):
    """Test OSFS implementation."""

    def make_fs(self):
        fs = MountFS()
        mem_fs = MemoryFS()
        fs.mount("/foo", mem_fs)
        return fs.opendir("foo")


class TestMountFSBehaviours(unittest.TestCase):
    def test_bad_mount(self):
        mount_fs = MountFS()
        with self.assertRaises(TypeError):
            mount_fs.mount("foo", 5)
        with self.assertRaises(TypeError):
            mount_fs.mount("foo", b"bar")

    def test_listdir(self):
        mount_fs = MountFS()
        self.assertEqual(mount_fs.listdir("/"), [])
        m1 = MemoryFS()
        m3 = MemoryFS()
        m4 = TempFS()
        mount_fs.mount("/m1", m1)
        mount_fs.mount("/m2", "temp://")
        mount_fs.mount("/m3", m3)
        mount_fs.mount("/m3/foo", m4)
        self.assertEqual(sorted(mount_fs.listdir("/")), ["m1", "m2", "m3"])
        self.assertEqual(sorted(mount_fs.listdir("/m3")), ["foo"])
        self.assertEqual(mount_fs.listdir("/m3/foo"), [])

    def test_mount_path_prefix_is_not_nested(self):
        mount_fs = MountFS()
        mounted = MemoryFS()
        mounted.writebytes("/file.txt", b"mounted")
        mount_fs.mount("/data", mounted)
        mount_fs.makedirs("/data2")
        mount_fs.writebytes("/data2/file.txt", b"default")

        self.assertEqual(mount_fs.readbytes("/data/file.txt"), b"mounted")
        self.assertEqual(mount_fs.readbytes("/data2/file.txt"), b"default")

    def test_auto_close(self):
        """Test MountFS auto close is working"""
        mount_fs = MountFS()
        m1 = MemoryFS()
        m2 = MemoryFS()
        mount_fs.mount("/m1", m1)
        mount_fs.mount("/m2", m2)
        self.assertFalse(m1.isclosed())
        self.assertFalse(m2.isclosed())
        mount_fs.close()
        self.assertTrue(m1.isclosed())
        self.assertTrue(m2.isclosed())

    def test_no_auto_close(self):
        """Test MountFS auto close can be disabled"""
        mount_fs = MountFS(auto_close=False)
        m1 = MemoryFS()
        m2 = MemoryFS()
        mount_fs.mount("/m1", m1)
        mount_fs.mount("/m2", m2)
        self.assertFalse(m1.isclosed())
        self.assertFalse(m2.isclosed())
        mount_fs.close()
        self.assertFalse(m1.isclosed())
        self.assertFalse(m2.isclosed())

    def test_external_backend_is_not_owned(self):
        mount_fs = MountFS()
        backend = MemoryFS()
        mount_fs.mount("/external", backend, own=False)
        mount_fs.close()
        self.assertFalse(backend.isclosed())

    def test_replace_mount_binds_handles_to_generations(self):
        mount_fs = MountFS()
        old_backend = MemoryFS()
        new_backend = MemoryFS()
        old_backend.writebytes("/file.txt", b"old")
        new_backend.writebytes("/file.txt", b"new")
        mount_fs.mount("/data", old_backend)

        old_file = mount_fs.open("/data/file.txt", "rb")
        mount_fs.replace_mount("/data", new_backend)
        new_file = mount_fs.open("/data/file.txt", "rb")

        self.assertEqual(old_file.read(), b"old")
        self.assertEqual(new_file.read(), b"new")
        self.assertFalse(old_backend.isclosed())
        self.assertFalse(new_backend.isclosed())

        new_file.close()
        self.assertFalse(old_backend.isclosed())
        self.assertFalse(new_backend.isclosed())

        old_file.close()
        self.assertTrue(old_backend.isclosed())
        self.assertFalse(new_backend.isclosed())

    def test_replace_mount_text_handles_keep_backend_alive(self):
        mount_fs = MountFS()
        old_backend = MemoryFS()
        new_backend = MemoryFS()
        old_backend.writetext("/file.txt", "old")
        new_backend.writetext("/file.txt", "new")
        mount_fs.mount("/data", old_backend)

        old_file = mount_fs.open("/data/file.txt")
        mount_fs.replace_mount("/data", new_backend)
        new_file = mount_fs.open("/data/file.txt")

        self.assertEqual(old_file.read(), "old")
        self.assertEqual(new_file.read(), "new")
        new_file.close()
        self.assertFalse(new_backend.isclosed())
        self.assertFalse(old_backend.isclosed())
        old_file.close()
        self.assertTrue(old_backend.isclosed())
        self.assertFalse(new_backend.isclosed())

    def test_replace_failure_keeps_old_mount(self):
        mount_fs = MountFS()
        backend = MemoryFS()
        replacement = MemoryFS()
        backend.writebytes("/file.txt", b"unchanged")
        mount_fs.mount("/data", backend)

        with self.assertRaises(MountError):
            mount_fs.replace_mount("/missing", replacement)
        self.assertEqual(mount_fs.readbytes("/data/file.txt"), b"unchanged")
        self.assertFalse(replacement.isclosed())

    def test_unmount_waits_for_existing_handles(self):
        mount_fs = MountFS()
        backend = MemoryFS()
        backend.writebytes("/file.txt", b"data")
        mount_fs.mount("/data", backend)
        handle = mount_fs.open("/data/file.txt", "rb")

        mount_fs.unmount("/data")
        self.assertFalse(backend.isclosed())
        self.assertEqual(handle.read(), b"data")
        handle.close()
        self.assertTrue(backend.isclosed())

    def test_unmount_rejects_parent_with_nested_mount(self):
        mount_fs = MountFS()
        parent = MemoryFS()
        child = MemoryFS()
        mount_fs.mount("/parent", parent)
        mount_fs.mount("/parent/child", child)

        with self.assertRaises(MountError):
            mount_fs.unmount("/parent")
        self.assertTrue(mount_fs.isdir("/parent/child"))

        mount_fs.unmount("/parent", recursive=True)
        self.assertFalse(mount_fs.exists("/parent"))

    def test_close_is_idempotent(self):
        mount_fs = MountFS()
        backend = MemoryFS()
        mount_fs.mount("/data", backend)
        mount_fs.close()
        mount_fs.close()
        self.assertTrue(backend.isclosed())

    def test_empty(self):
        """Test MountFS with nothing mounted."""
        mount_fs = MountFS()
        self.assertEqual(mount_fs.listdir("/"), [])

    def test_mount_self(self):
        mount_fs = MountFS()
        with self.assertRaises(ValueError):
            mount_fs.mount("/", mount_fs)

    def test_desc(self):
        mount_fs = MountFS()
        mount_fs.desc("/")
