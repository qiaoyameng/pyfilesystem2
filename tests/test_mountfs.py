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
        self.assertEqual(mount_fs.listdir("/m3"), ["foo"])
        m3.makedir("foo")
        self.assertEqual(sorted(mount_fs.listdir("/m3")), ["foo"])

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

    def test_replace_mount_binds_handles_to_generation(self):
        mount_fs = MountFS(auto_close=False)
        old_fs = MemoryFS()
        new_fs = MemoryFS()
        old_fs.writetext("message.txt", "old")
        new_fs.writetext("message.txt", "new")
        mount_fs.mount("/data", old_fs)

        with mount_fs.open("/data/message.txt", "rb") as old_file:
            returned = mount_fs.replace_mount("/data", new_fs)
            with mount_fs.open("/data/message.txt", "rb") as new_file:
                self.assertEqual(new_file.read(), b"new")
            self.assertIs(returned, old_fs)
            self.assertFalse(old_fs.isclosed())
            self.assertFalse(new_fs.isclosed())

        self.assertFalse(old_fs.isclosed())
        self.assertFalse(new_fs.isclosed())

    def test_replace_mount_reverse_close_order_waits_for_last_handle(self):
        mount_fs = MountFS()
        old_fs = MemoryFS()
        new_fs = MemoryFS()
        mount_fs.mount("/data", old_fs)

        old_file = mount_fs.openbin("/data/file.bin", "wb")
        self.assertIs(mount_fs.replace_mount("/data", new_fs), old_fs)
        new_file = mount_fs.openbin("/data/file.bin", "wb")
        self.assertFalse(old_fs.isclosed())

        new_file.close()
        self.assertFalse(old_fs.isclosed())
        old_file.close()
        self.assertTrue(old_fs.isclosed())

    def test_explicit_backend_is_not_owned_without_auto_close(self):
        mount_fs = MountFS(auto_close=False)
        backend = MemoryFS()
        mount_fs.mount("/data", backend)
        file_obj = mount_fs.openbin("/data/file.bin", "wb")

        mount_fs.unmount("/data")
        file_obj.close()
        mount_fs.close()

        self.assertFalse(backend.isclosed())

    def test_explicit_auto_close_waits_for_open_handle(self):
        mount_fs = MountFS(auto_close=False)
        backend = MemoryFS()
        mount_fs.mount("/data", backend, auto_close=True)
        file_obj = mount_fs.openbin("/data/file.bin", "wb")

        self.assertIs(mount_fs.unmount("/data"), backend)
        self.assertFalse(backend.isclosed())
        file_obj.close()
        self.assertTrue(backend.isclosed())

    def test_replace_mount_requires_existing_mount(self):
        mount_fs = MountFS()
        with self.assertRaises(MountError):
            mount_fs.replace_mount("/missing", MemoryFS())
        self.assertEqual(mount_fs.mounts, [])

    def test_replace_mount_failure_keeps_old_generation(self):
        mount_fs = MountFS()
        backend = MemoryFS()
        backend.writetext("file.txt", "data")
        mount_fs.mount("/data", backend)

        with self.assertRaises(TypeError):
            mount_fs.replace_mount("/data", object())

        self.assertEqual(mount_fs.readtext("/data/file.txt"), "data")
        self.assertIs(mount_fs.mounts[0][1], backend)

    def test_unmount_nested_mount_strategy(self):
        mount_fs = MountFS()
        parent = MemoryFS()
        child = MemoryFS()
        mount_fs.mount("/parent", parent)
        mount_fs.mount("/parent/child", child)

        with self.assertRaises(MountError):
            mount_fs.unmount("/parent")

        self.assertIs(mount_fs.unmount("/parent/child"), child)
        self.assertIs(mount_fs.unmount("/parent"), parent)

    def test_close_is_idempotent_with_open_handles(self):
        mount_fs = MountFS()
        backend = MemoryFS()
        mount_fs.mount("/data", backend)
        file_obj = mount_fs.openbin("/data/file.bin", "wb")

        mount_fs.close()
        self.assertFalse(backend.isclosed())
        mount_fs.close()
        self.assertFalse(backend.isclosed())

        file_obj.close()
        self.assertTrue(backend.isclosed())
