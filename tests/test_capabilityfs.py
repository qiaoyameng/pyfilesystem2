"""Tests for `fs.capabilityfs`."""

from __future__ import unicode_literals

import io
import unittest

from fs import errors
from fs.capabilityfs import (
    ALL_CAPABILITIES,
    READ_CAPABILITIES,
    Capability,
    CapabilityFS,
    CapabilitySubFS,
)
from fs.memoryfs import MemoryFS
from fs.subfs import ClosingSubFS, SubFS
from fs.test import FSTestCases


# Operations that create or modify state on the terminal filesystem.
_WRITE_CALLS = (
    "appendbytes",
    "appendtext",
    "makedir",
    "makedirs",
    "move",
    "movedir",
    "openbin_write",
    "remove",
    "removedir",
    "removetree",
    "setinfo",
    "settimes",
    "touch",
    "upload",
    "writebytes",
    "writefile",
)


class RecordingFS(MemoryFS):
    """A MemoryFS recording every state-changing call made on it."""

    def __init__(self):
        super(RecordingFS, self).__init__()
        self.calls = []

    def _record(self, name, *args, **kwargs):
        self.calls.append(name)

    def appendbytes(self, path, data):
        self._record("appendbytes", path)
        return MemoryFS.appendbytes(self, path, data)

    def appendtext(self, path, text, **kwargs):
        self._record("appendtext", path)
        return MemoryFS.appendtext(self, path, text, **kwargs)

    def makedir(self, path, permissions=None, recreate=False):
        self._record("makedir", path)
        return MemoryFS.makedir(self, path, permissions=permissions, recreate=recreate)

    def makedirs(self, path, permissions=None, recreate=False):
        self._record("makedirs", path)
        return MemoryFS.makedirs(
            self, path, permissions=permissions, recreate=recreate
        )

    def move(self, src_path, dst_path, overwrite=False, preserve_time=False):
        self._record("move", src_path, dst_path)
        return MemoryFS.move(
            self,
            src_path,
            dst_path,
            overwrite=overwrite,
            preserve_time=preserve_time,
        )

    def movedir(self, src_path, dst_path, create=False, preserve_time=False):
        self._record("movedir", src_path, dst_path)
        return MemoryFS.movedir(
            self,
            src_path,
            dst_path,
            create=create,
            preserve_time=preserve_time,
        )

    def openbin(self, path, mode="r", buffering=-1, **options):
        if any(char in mode for char in "wxa+"):
            self._record("openbin_write", path, mode)
        return MemoryFS.openbin(
            self, path, mode=mode, buffering=buffering, **options
        )

    def remove(self, path):
        self._record("remove", path)
        return MemoryFS.remove(self, path)

    def removedir(self, path):
        self._record("removedir", path)
        return MemoryFS.removedir(self, path)

    def removetree(self, path):
        self._record("removetree", path)
        return MemoryFS.removetree(self, path)

    def setinfo(self, path, info):
        self._record("setinfo", path)
        return MemoryFS.setinfo(self, path, info)

    def settimes(self, path, accessed=None, modified=None):
        self._record("settimes", path)
        return MemoryFS.settimes(
            self, path, accessed=accessed, modified=modified
        )

    def touch(self, path):
        self._record("touch", path)
        return MemoryFS.touch(self, path)

    def upload(self, path, file, chunk_size=None, **options):
        self._record("upload", path)
        return MemoryFS.upload(self, path, file, chunk_size=chunk_size, **options)

    def writebytes(self, path, contents):
        self._record("writebytes", path)
        return MemoryFS.writebytes(self, path, contents)

    def writefile(self, path, file, **kwargs):
        self._record("writefile", path)
        return MemoryFS.writefile(self, path, file, **kwargs)


class CapabilityTestMixin(object):
    def assertNoWrites(self, recording, fn, *args, **kwargs):
        """Assert ``fn`` is denied and never reached the terminal FS."""
        before = len(recording.calls)
        with self.assertRaises(errors.PermissionDenied):
            fn(*args, **kwargs)
        self.assertEqual(
            recording.calls[before:],
            [],
            "a denied operation reached the terminal filesystem",
        )


class TestCapabilityFS(FSTestCases, unittest.TestCase):
    """The full-capability wrapper must behave like a normal FS."""

    def make_fs(self):
        self.mem = MemoryFS()
        return CapabilityFS(self.mem, ALL_CAPABILITIES, close_wrapped=True)


class TestCapabilities(unittest.TestCase, CapabilityTestMixin):
    def setUp(self):
        self.rec = RecordingFS()
        self.rec.makedirs("dir/sub")
        self.rec.writetext("file.txt", "hello")
        self.rec.writetext("dir/inside.txt", "data")
        self.rec.writetext("dir/sub/deep.txt", "deep")

    def _fs(self, *caps, **kwargs):
        return CapabilityFS(self.rec, caps, close_wrapped=False, **kwargs)

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #

    def test_read_permitted(self):
        fs_ = self._fs(Capability.READ)
        self.assertEqual(fs_.readbytes("file.txt"), b"hello")
        with fs_.openbin("file.txt", "r") as f:
            self.assertEqual(f.read(), b"hello")

    def test_read_denied(self):
        fs_ = self._fs(Capability.LIST, Capability.CREATE)
        self.assertNoWrites(self.rec, fs_.readbytes, "file.txt")
        self.assertNoWrites(self.rec, fs_.openbin, "file.txt", "r")
        self.assertNoWrites(self.rec, fs_.download, "file.txt", io.BytesIO())

    # ------------------------------------------------------------------ #
    # Listing
    # ------------------------------------------------------------------ #

    def test_list_permitted(self):
        fs_ = self._fs(Capability.LIST)
        self.assertEqual(sorted(fs_.listdir("/")), ["dir", "file.txt"])
        self.assertEqual(
            sorted(info.name for info in fs_.scandir("dir")),
            ["inside.txt", "sub"],
        )

    def test_list_denied(self):
        fs_ = self._fs(Capability.READ)
        self.assertNoWrites(self.rec, fs_.listdir, "/")
        self.assertNoWrites(self.rec, fs_.scandir, "/")
        self.assertNoWrites(self.rec, fs_.filterdir, "/")

    # ------------------------------------------------------------------ #
    # Creation / open modes
    # ------------------------------------------------------------------ #

    def test_create_new_file(self):
        fs_ = self._fs(Capability.CREATE)
        fs_.writebytes("new.txt", b"x")
        self.assertEqual(self.rec.readbytes("new.txt"), b"x")

    def test_create_denied(self):
        fs_ = self._fs(Capability.READ)
        self.assertNoWrites(self.rec, fs_.writebytes, "new.txt", b"x")
        self.assertNoWrites(self.rec, fs_.makedir, "newdir")
        self.assertNoWrites(self.rec, fs_.create, "new.txt")

    def test_open_mode_create_then_reopen(self):
        # A wrapper that can create but not overwrite.
        fs_ = self._fs(Capability.CREATE, Capability.READ)
        fs_.writetext("created.txt", "first")

        # "w" re-opening the existing file truncates -> overwrite needed.
        self.assertNoWrites(
            self.rec, fs_.openbin, "created.txt", "w"
        )
        # "a" on the existing file appends without truncating.
        with fs_.openbin("created.txt", "a") as f:
            f.write(b"!")
        self.assertEqual(self.rec.readbytes("created.txt"), b"first!")

        # "x" fails on the terminal, but needs only the create gate.
        with self.assertRaises(errors.FileExists):
            fs_.openbin("created.txt", "x")

        # "w" on a non-existing file only needs create.
        with fs_.openbin("brand-new.txt", "w") as f:
            f.write(b"new")
        self.assertEqual(self.rec.readbytes("brand-new.txt"), b"new")

    def test_open_mode_rplus_requires_overwrite(self):
        fs_ = self._fs(Capability.CREATE, Capability.READ)
        self.assertNoWrites(self.rec, fs_.openbin, "file.txt", "r+")

        fs2 = self._fs(Capability.READ, Capability.OVERWRITE)
        with fs2.openbin("file.txt", "r+") as f:
            f.seek(0)
            f.write(b"HELL")
        self.assertEqual(self.rec.readbytes("file.txt"), b"HELLo")

    def test_open_mode_x_creates(self):
        fs_ = self._fs(Capability.CREATE)
        with fs_.openbin("exclusive.txt", "x") as f:
            f.write(b"x")
        self.assertTrue(self.rec.exists("exclusive.txt"))

    def test_open_mode_read_missing_needs_no_create(self):
        fs_ = self._fs(Capability.READ)
        with self.assertRaises(errors.ResourceNotFound):
            fs_.openbin("missing.txt", "r")

    # ------------------------------------------------------------------ #
    # Overwriting
    # ------------------------------------------------------------------ #

    def test_overwrite_denied_does_not_truncate(self):
        fs_ = self._fs(Capability.CREATE, Capability.READ)
        original = self.rec.readbytes("file.txt")
        self.assertNoWrites(self.rec, fs_.writebytes, "file.txt", b"REPLACED")
        # Contents must be untouched.
        self.assertEqual(self.rec.readbytes("file.txt"), original)

    def test_overwrite_permitted(self):
        fs_ = self._fs(Capability.OVERWRITE, Capability.CREATE, Capability.READ)
        fs_.writebytes("file.txt", b"replaced")
        self.assertEqual(self.rec.readbytes("file.txt"), b"replaced")

    def test_create_wipe_existing(self):
        fs_ = self._fs(Capability.OVERWRITE, Capability.READ)
        self.assertTrue(fs_.create("file.txt", wipe=True))
        self.assertEqual(self.rec.getsize("file.txt"), 0)

    # ------------------------------------------------------------------ #
    # Deletion
    # ------------------------------------------------------------------ #

    def test_delete_file_denied(self):
        fs_ = self._fs(Capability.READ)
        self.assertNoWrites(self.rec, fs_.remove, "file.txt")
        self.assertTrue(self.rec.exists("file.txt"))

    def test_delete_dir_denied(self):
        fs_ = self._fs(Capability.READ, Capability.CREATE)
        self.assertNoWrites(self.rec, fs_.removedir, "dir/sub")
        self.assertNoWrites(self.rec, fs_.removetree, "dir")
        self.assertTrue(self.rec.exists("dir/inside.txt"))

    def test_delete_permitted(self):
        fs_ = self._fs(Capability.DELETE, Capability.LIST)
        fs_.remove("file.txt")
        self.assertFalse(self.rec.exists("file.txt"))
        fs_.removetree("dir")
        self.assertFalse(self.rec.exists("dir"))

    # ------------------------------------------------------------------ #
    # Metadata
    # ------------------------------------------------------------------ #

    def test_meta_denied(self):
        fs_ = self._fs(Capability.READ)
        self.assertNoWrites(self.rec, fs_.setinfo, "file.txt", {})
        self.assertNoWrites(self.rec, fs_.settimes, "file.txt")
        self.assertNoWrites(self.rec, fs_.touch, "file.txt")

    def test_meta_permitted_on_existing(self):
        fs_ = self._fs(Capability.META)
        fs_.setinfo("file.txt", {"details": {"modified": 0}})
        fs_.settimes("file.txt")

    def test_touch_missing_needs_create(self):
        fs_ = self._fs(Capability.META)
        self.assertNoWrites(self.rec, fs_.touch, "missing.txt")

    # ------------------------------------------------------------------ #
    # Copy / move / upload
    # ------------------------------------------------------------------ #

    def test_copy_requires_read_and_create(self):
        fs_ = self._fs(Capability.READ)
        self.assertNoWrites(self.rec, fs_.copy, "file.txt", "copy.txt")
        self.assertFalse(self.rec.exists("copy.txt"))

    def test_copy_new(self):
        fs_ = self._fs(Capability.READ, Capability.CREATE)
        fs_.copy("file.txt", "copy.txt")
        self.assertEqual(self.rec.readbytes("copy.txt"), b"hello")

    def test_copy_existing_requires_overwrite(self):
        self.rec.writebytes("dst.txt", b"old")
        fs_ = self._fs(Capability.READ, Capability.CREATE)
        self.assertNoWrites(self.rec, fs_.copy, "file.txt", "dst.txt")
        self.assertEqual(self.rec.readbytes("dst.txt"), b"old")

        fs2 = self._fs(Capability.READ, Capability.OVERWRITE)
        fs2.copy("file.txt", "dst.txt", overwrite=True)
        self.assertEqual(self.rec.readbytes("dst.txt"), b"hello")

    def test_copy_preserve_time_requires_meta(self):
        fs_ = self._fs(Capability.READ, Capability.CREATE)
        self.assertNoWrites(
            self.rec,
            fs_.copy,
            "file.txt",
            "copy.txt",
            False,
            True,
        )
        self.assertFalse(self.rec.exists("copy.txt"))

    def test_copydir_permissions(self):
        fs_ = self._fs(Capability.READ)
        self.assertNoWrites(
            self.rec, fs_.copydir, "dir", "newdir", True
        )
        fs2 = self._fs(Capability.READ, Capability.CREATE, Capability.LIST)
        fs2.copydir("dir", "newdir", create=True)
        self.assertTrue(self.rec.exists("newdir/inside.txt"))
        self.assertTrue(self.rec.exists("newdir/sub/deep.txt"))

    def test_copydir_collision_requires_overwrite(self):
        self.rec.makedirs("dst/sub")
        self.rec.writebytes("dst/inside.txt", b"old")
        fs_ = self._fs(Capability.READ, Capability.LIST, Capability.CREATE)
        self.assertNoWrites(
            self.rec, fs_.copydir, "dir", "dst"
        )
        self.assertEqual(self.rec.readbytes("dst/inside.txt"), b"old")

    def test_move_cross_directory(self):
        fs_ = self._fs(
            Capability.READ,
            Capability.CREATE,
            Capability.DELETE,
        )
        fs_.move("file.txt", "dir/moved.txt")
        self.assertFalse(self.rec.exists("file.txt"))
        self.assertEqual(self.rec.readbytes("dir/moved.txt"), b"hello")

    def test_move_requires_delete(self):
        fs_ = self._fs(Capability.READ, Capability.CREATE)
        self.assertNoWrites(
            self.rec, fs_.move, "file.txt", "moved.txt"
        )
        # The failed move must leave both source and destination alone.
        self.assertTrue(self.rec.exists("file.txt"))
        self.assertFalse(self.rec.exists("moved.txt"))

    def test_move_existing_requires_overwrite(self):
        self.rec.writebytes("dst.txt", b"old")
        fs_ = self._fs(
            Capability.READ, Capability.CREATE, Capability.DELETE
        )
        self.assertNoWrites(
            self.rec, fs_.move, "file.txt", "dst.txt", False, False
        )
        self.assertEqual(self.rec.readbytes("dst.txt"), b"old")
        self.assertTrue(self.rec.exists("file.txt"))

    def test_movedir_cross_directory(self):
        fs_ = self._fs(
            Capability.READ,
            Capability.LIST,
            Capability.CREATE,
            Capability.DELETE,
        )
        fs_.movedir("dir", "moved", create=True)
        self.assertFalse(self.rec.exists("dir/inside.txt"))
        self.assertTrue(self.rec.exists("moved/inside.txt"))

    def test_movedir_requires_delete(self):
        fs_ = self._fs(
            Capability.READ, Capability.LIST, Capability.CREATE
        )
        self.assertNoWrites(
            self.rec, fs_.movedir, "dir", "moved", True
        )
        self.assertTrue(self.rec.exists("dir/inside.txt"))
        self.assertFalse(self.rec.exists("moved"))

    def test_upload_new_requires_create(self):
        fs_ = self._fs(Capability.READ)
        stream = io.BytesIO(b"payload")
        self.assertNoWrites(self.rec, fs_.upload, "uploaded.bin", stream)
        self.assertFalse(self.rec.exists("uploaded.bin"))

    def test_upload_existing_requires_overwrite(self):
        fs_ = self._fs(Capability.CREATE, Capability.READ)
        self.assertNoWrites(
            self.rec, fs_.upload, "file.txt", io.BytesIO(b"payload")
        )
        self.assertEqual(self.rec.readbytes("file.txt"), b"hello")

    def test_upload_new_with_create(self):
        fs_ = self._fs(Capability.CREATE)
        fs_.upload("uploaded.bin", io.BytesIO(b"payload"))
        self.assertEqual(self.rec.readbytes("uploaded.bin"), b"payload")


class TestRootContainment(unittest.TestCase, CapabilityTestMixin):
    def setUp(self):
        self.rec = RecordingFS()
        self.rec.makedirs("sandbox/sub")
        self.rec.writetext("sandbox/file.txt", "inside")
        self.rec.writetext("outside.txt", "secret")

    def test_root_mapping(self):
        fs_ = CapabilityFS(
            self.rec, ALL_CAPABILITIES, root="sandbox", close_wrapped=False
        )
        self.assertEqual(fs_.readtext("file.txt"), "inside")
        self.assertEqual(sorted(fs_.listdir("/")), ["file.txt", "sub"])
        fs_.writebytes("new.txt", b"x")
        self.assertTrue(self.rec.exists("sandbox/new.txt"))

    def test_escape_denied(self):
        fs_ = CapabilityFS(
            self.rec, READ_CAPABILITIES, root="sandbox", close_wrapped=False
        )
        for candidate in ("../outside.txt", "/../outside.txt", "sub/../../x"):
            self.assertNoWrites(self.rec, fs_.readtext, candidate)

    def test_root_absolute_escape(self):
        fs_ = CapabilityFS(
            self.rec, ALL_CAPABILITIES, root="sandbox", close_wrapped=False
        )
        # An absolute-looking path is still relative to the wrapper root.
        fs_.writebytes("/file2.txt", b"y")
        self.assertTrue(self.rec.exists("sandbox/file2.txt"))
        self.assertFalse(self.rec.exists("file2.txt"))


class TestSubFSNarrowing(unittest.TestCase, CapabilityTestMixin):
    def setUp(self):
        self.rec = RecordingFS()
        self.rec.makedirs("a/b")
        self.rec.writetext("a/file.txt", "hello")
        self.rec.writetext("a/b/deep.txt", "deep")
        self.parent = CapabilityFS(
            self.rec,
            [
                Capability.READ,
                Capability.LIST,
                Capability.CREATE,
                Capability.OVERWRITE,
            ],
            close_wrapped=False,
        )

    def test_opendir_returns_capability_subfs(self):
        sub = self.parent.opendir("a")
        self.assertIsInstance(sub, CapabilitySubFS)
        self.assertEqual(sorted(sub.capabilities), sorted(self.parent.capabilities))

    def test_subfs_inherits_capabilities(self):
        sub = self.parent.opendir("a")
        self.assertTrue(sub.isfile("file.txt"))
        sub.writebytes("new.txt", b"x")
        self.assertTrue(self.rec.exists("a/new.txt"))

    def test_subfs_delete_denied_by_parent(self):
        sub = self.parent.opendir("a")
        self.assertNoWrites(self.rec, sub.remove, "file.txt")
        self.assertTrue(self.rec.exists("a/file.txt"))

    def test_subfs_narrows(self):
        sub_a = self.parent.opendir("a")
        sub_b = sub_a.opendir("b")
        sub_b = CapabilityFS(sub_b, [Capability.READ], close_wrapped=False)
        self.assertEqual(sorted(sub_b.capabilities), [Capability.READ])
        self.assertEqual(sub_b.readtext("deep.txt"), "deep")
        self.assertNoWrites(self.rec, sub_b.writebytes, "x.txt", b"x")
        self.assertNoWrites(self.rec, sub_b.listdir, "/")

    def test_rewrapping_cannot_restore_denied(self):
        sub = self.parent.opendir("a")
        wide = CapabilityFS(sub, ALL_CAPABILITIES, close_wrapped=False)
        # Parent denied DELETE and META, so the child cannot restore them.
        self.assertNotIn(Capability.DELETE, wide.capabilities)
        self.assertNotIn(Capability.META, wide.capabilities)
        self.assertNoWrites(self.rec, wide.remove, "file.txt")
        self.assertNoWrites(self.rec, wide.setinfo, "file.txt", {})

    def test_subfs_root_containment(self):
        sub = self.parent.opendir("a")
        for candidate in ("../file.txt", "/../outside.txt"):
            self.assertNoWrites(self.rec, sub.readtext, candidate)
        self.assertEqual(sub.readtext("b/deep.txt"), "deep")

    def test_makedir_returns_narrowed_subfs(self):
        sub = self.parent.makedir("new")
        self.assertIsInstance(sub, CapabilitySubFS)
        sub.writebytes("inside.txt", b"x")
        self.assertTrue(self.rec.exists("new/inside.txt"))

    def test_subfs_factory_default(self):
        sub = self.parent.opendir("a", factory=SubFS)
        self.assertIsInstance(sub, CapabilitySubFS)

    def test_closing_factory_closes_parent_chain(self):
        # ClosingSubFS semantics (see fs.subfs.ClosingSubFS) is to close
        # the parent chain when the sub filesystem is closed.
        sub = self.parent.opendir("a", factory=ClosingSubFS)
        sub.close()
        self.assertTrue(self.parent.isclosed())
        self.assertTrue(self.rec.isclosed())

    def test_regular_subfs_does_not_close_parent(self):
        sub = self.parent.opendir("a")
        sub.close()
        self.assertFalse(self.parent.isclosed())
        self.assertFalse(self.rec.isclosed())


class TestClosingBehavior(unittest.TestCase):
    def test_default_does_not_close_wrapped(self):
        terminal = MemoryFS()
        wrapper = CapabilityFS(terminal, ALL_CAPABILITIES, close_wrapped=False)
        wrapper.close()
        self.assertTrue(wrapper.isclosed())
        self.assertFalse(terminal.isclosed())
        with self.assertRaises(errors.FilesystemClosed):
            wrapper.listdir("/")
        terminal.writetext("a.txt", "still usable")
        self.assertTrue(terminal.exists("a.txt"))

    def test_close_wrapped(self):
        terminal = MemoryFS()
        wrapper = CapabilityFS(terminal, ALL_CAPABILITIES, close_wrapped=True)
        wrapper.close()
        self.assertTrue(terminal.isclosed())

    def test_close_is_idempotent(self):
        terminal = MemoryFS()
        wrapper = CapabilityFS(terminal, ALL_CAPABILITIES, close_wrapped=True)
        wrapper.close()
        wrapper.close()
        self.assertTrue(wrapper.isclosed())

    def test_garbage_collected_subfs_does_not_close_parent(self):
        import gc

        terminal = MemoryFS()
        wrapper = CapabilityFS(terminal, ALL_CAPABILITIES, close_wrapped=True)
        wrapper.writetext("a.txt", "x")
        sub = wrapper.makedir("sub")
        del sub
        gc.collect()
        # The parent and terminal must remain usable.
        wrapper.writebytes("b.txt", b"y")
        self.assertFalse(terminal.isclosed())


class TestMeta(unittest.TestCase):
    def test_read_only_meta(self):
        terminal = MemoryFS()
        ro = CapabilityFS(terminal, READ_CAPABILITIES, close_wrapped=False)
        meta = ro.getmeta()
        self.assertTrue(meta["read_only"])
        self.assertFalse(meta["supports_rename"])

    def test_writable_meta(self):
        terminal = MemoryFS()
        rw = CapabilityFS(terminal, ALL_CAPABILITIES, close_wrapped=False)
        meta = rw.getmeta()
        self.assertFalse(meta["read_only"])
        # Rename is always disabled on the wrapper to keep move gated.
        self.assertFalse(meta["supports_rename"])


class TestCapabilityConstructor(unittest.TestCase):
    def test_default_has_no_capabilities(self):
        wrapper = CapabilityFS(MemoryFS(), close_wrapped=False)
        self.assertEqual(wrapper.capabilities, frozenset())

    def test_unknown_capability_rejected(self):
        with self.assertRaises(ValueError):
            CapabilityFS(MemoryFS(), ["bogus"], close_wrapped=False)

    def test_capability_is_string(self):
        self.assertEqual(Capability.READ, "read")
