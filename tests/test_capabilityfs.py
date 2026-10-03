"""Tests for the capability based filesystem wrapper."""

from __future__ import absolute_import, print_function, unicode_literals

import io
import shutil
import tempfile
import unittest

import fs.errors
import fs.move
from fs.capabilityfs import (
    ALL_CAPABILITIES,
    CREATE_ONLY_CAPABILITIES,
    READ_ONLY_CAPABILITIES,
    Capability,
    CapabilityFS,
)
from fs.memoryfs import MemoryFS
from fs.osfs import OSFS
from fs.wrapfs import WrapFS


# --------------------------------------------------------------------------- #
# Recording filesystem
# --------------------------------------------------------------------------- #


class RecordingFS(WrapFS):
    """Wrap a filesystem and record every call made to the wrapped object.

    The log lets tests assert that a rejected operation never reached
    the underlying layer, and in particular never triggered a write.
    """

    wrap_name = "recording"

    WRITE_METHODS = (
        "appendbytes",
        "appendtext",
        "makedir",
        "makedirs",
        "move",
        "movedir",
        "remove",
        "removedir",
        "removetree",
        "setinfo",
        "settimes",
        "touch",
        "upload",
        "writebytes",
        "writefile",
        "writetext",
    )

    def __init__(self, wrap_fs):
        super(RecordingFS, self).__init__(wrap_fs)
        self.calls = []

    def __getattribute__(self, name):
        attr = WrapFS.__getattribute__(self, name)
        if name.startswith("_") or not callable(attr) or not hasattr(
            self._wrap_fs, name
        ):
            return attr

        def recorder(*args, **kwargs):
            log = WrapFS.__getattribute__(self, "calls")
            log.append((name, args, kwargs))
            return attr(*args, **kwargs)

        return recorder

    def writes(self):
        return [entry for entry in self.calls if entry[0] in self.WRITE_METHODS]

    def openbin_writes(self):
        writes = []
        for name, args, kwargs in self.calls:
            if name != "openbin":
                continue
            mode = kwargs.get("mode", args[1] if len(args) > 1 else "r")
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                writes.append((args, kwargs))
        return writes

    def any_write_occurred(self):
        return bool(self.writes() or self.openbin_writes())

    def reset(self):
        self.calls = []


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_tree(fs_object):
    fs_object.makedirs("/src/sub")
    fs_object.writetext("/src/a.txt", "AAA")
    fs_object.writetext("/src/sub/b.txt", "BBB")
    fs_object.makedir("/dst")


def read_only(fs_object):
    return CapabilityFS(fs_object, READ_ONLY_CAPABILITIES)


def create_only(fs_object):
    return CapabilityFS(fs_object, CREATE_ONLY_CAPABILITIES)


class CapabilityTestCase(unittest.TestCase):
    def setUp(self):
        self.raw = MemoryFS()

    def tearDown(self):
        self.raw.close()


# --------------------------------------------------------------------------- #
# Capability set basics
# --------------------------------------------------------------------------- #


class TestCapabilitySet(CapabilityTestCase):
    def test_all_capabilities_granted(self):
        guarded = CapabilityFS(self.raw, ALL_CAPABILITIES)
        self.assertEqual(guarded.capabilities, ALL_CAPABILITIES)
        guarded.touch("/x")
        self.assertTrue(self.raw.exists("/x"))

    def test_unknown_capability_rejected(self):
        with self.assertRaises(ValueError):
            CapabilityFS(self.raw, ("read", "execute"))

    def test_empty_capabilities_deny_everything(self):
        guarded = CapabilityFS(self.raw, frozenset())
        self.raw.touch("/secret")
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.readbytes("/secret")
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.listdir("/")
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.writebytes("/x", b"x")
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.remove("/secret")

    def test_has_capability(self):
        guarded = create_only(self.raw)
        self.assertTrue(guarded.has_capability(Capability.CREATE))
        self.assertFalse(guarded.has_capability(Capability.DELETE))

    def test_restrict_only_narrows(self):
        guarded = CapabilityFS(self.raw, ALL_CAPABILITIES)
        narrowed = guarded.restrict({Capability.READ, Capability.DELETE})
        self.assertEqual(
            narrowed.capabilities, {Capability.READ, Capability.DELETE}
        )
        # Asking for a capability the parent does not have cannot grant it.
        twice = narrowed.restrict(ALL_CAPABILITIES)
        self.assertEqual(
            twice.capabilities, {Capability.READ, Capability.DELETE}
        )

    def test_meta_reflects_read_only(self):
        guarded = read_only(self.raw)
        self.assertTrue(guarded.getmeta()["read_only"])
        self.assertFalse(guarded.getmeta()["supports_rename"])
        full = CapabilityFS(self.raw, ALL_CAPABILITIES)
        self.assertFalse(full.getmeta()["read_only"])


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


class TestReadAccess(CapabilityTestCase):
    def test_read_permitted(self):
        self.raw.writebytes("/f", b"data")
        guarded = read_only(self.raw)
        self.assertEqual(guarded.readbytes("/f"), b"data")
        self.assertEqual(guarded.readtext("/f"), "data")

    def test_list_permitted(self):
        self.raw.touch("/a")
        guarded = read_only(self.raw)
        self.assertEqual(guarded.listdir("/"), ["a"])
        self.assertEqual([i.name for i in guarded.scandir("/")], ["a"])

    def test_read_without_capability_denied(self):
        self.raw.touch("/f")
        guarded = CapabilityFS(
            self.raw,
            {Capability.LIST, Capability.CREATE},
        )
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.readbytes("/f")
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.download("/f", io.BytesIO())

    def test_list_without_capability_denied(self):
        guarded = CapabilityFS(self.raw, {Capability.READ})
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.listdir("/")
        with self.assertRaises(fs.errors.PermissionDenied):
            list(guarded.scandir("/"))


# --------------------------------------------------------------------------- #
# Primitive writes
# --------------------------------------------------------------------------- #


class TestWriteAccess(CapabilityTestCase):
    def test_create_new_file(self):
        guarded = create_only(self.raw)
        guarded.writebytes("/new", b"x")
        self.assertEqual(self.raw.readbytes("/new"), b"x")

    def test_overwrite_requires_capability(self):
        self.raw.writebytes("/f", b"old")
        guarded = create_only(self.raw)
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.writebytes("/f", b"new")
        # Original content is untouched.
        self.assertEqual(self.raw.readbytes("/f"), b"old")

    def test_makedir_requires_create(self):
        guarded = read_only(self.raw)
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.makedir("/d")
        self.assertFalse(self.raw.exists("/d"))

    def test_remove_requires_delete(self):
        self.raw.touch("/f")
        guarded = create_only(self.raw)
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.remove("/f")
        self.assertTrue(self.raw.exists("/f"))

    def test_removedir_requires_delete(self):
        self.raw.makedir("/d")
        guarded = read_only(self.raw)
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.removedir("/d")
        self.assertTrue(self.raw.exists("/d"))

    def test_removetree_requires_delete_and_list(self):
        self.raw.makedirs("/d/sub")
        guarded = CapabilityFS(
            self.raw, {Capability.READ, Capability.CREATE, Capability.DELETE}
        )
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.removetree("/d")
        self.assertTrue(self.raw.exists("/d/sub"))

    def test_cannot_remove_root(self):
        guarded = CapabilityFS(self.raw, ALL_CAPABILITIES)
        with self.assertRaises(fs.errors.RemoveRootError):
            guarded.removedir("/")

    def test_metadata_requires_capability(self):
        self.raw.touch("/f")
        guarded = create_only(self.raw)
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.setinfo("/f", {"details": {"modified": 0}})
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.settimes("/f")
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.touch("/f")

    def test_touch_creates_without_metadata_capability(self):
        guarded = create_only(self.raw)
        guarded.touch("/new")
        self.assertTrue(self.raw.exists("/new"))


# --------------------------------------------------------------------------- #
# open / openbin mode combinations
# --------------------------------------------------------------------------- #


class TestOpenModes(CapabilityTestCase):
    def setUp(self):
        super(TestOpenModes, self).setUp()
        self.raw.writebytes("/f", b"data")

    def test_read_modes(self):
        guarded = read_only(self.raw)
        with guarded.openbin("/f", "rb") as f:
            self.assertEqual(f.read(), b"data")
        with guarded.open("/f", "rt") as f:
            self.assertEqual(f.read(), "data")

    def test_write_new_allowed_with_create_only(self):
        guarded = create_only(self.raw)
        with guarded.openbin("/new", "wb") as f:
            f.write(b"x")
        self.assertEqual(self.raw.readbytes("/new"), b"x")

    def test_write_existing_denied_without_overwrite(self):
        guarded = create_only(self.raw)
        for mode in ("wb", "wt", "w+b", "r+b"):
            with self.assertRaises(fs.errors.PermissionDenied):
                guarded.openbin("/f", mode)

    def test_append_existing_denied_without_overwrite(self):
        guarded = create_only(self.raw)
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.openbin("/f", "ab")
        self.assertEqual(self.raw.readbytes("/f"), b"data")

    def test_append_new_allowed_with_create_only(self):
        guarded = create_only(self.raw)
        with guarded.openbin("/new", "ab") as f:
            f.write(b"x")
        self.assertEqual(self.raw.readbytes("/new"), b"x")

    def test_exclusive_create_does_not_require_overwrite(self):
        guarded = create_only(self.raw)
        with guarded.openbin("/x", "xb") as f:
            f.write(b"x")
        # 'x' on an existing file is rejected by the wrapped layer,
        # never silently overwritten.
        with self.assertRaises(fs.errors.FileExists):
            guarded.openbin("/x", "xb")
        self.assertEqual(self.raw.readbytes("/x"), b"x")

    def test_read_denied_for_write_only_view(self):
        guarded = CapabilityFS(
            self.raw, {Capability.CREATE, Capability.OVERWRITE}
        )
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.openbin("/f", "rb")
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.openbin("/f", "r+b")
        # Plain write is fine even without READ.
        with guarded.openbin("/f", "wb"):
            pass

    def test_create_then_reopen_mode_difference(self):
        """Creating a file only needs CREATE; reopening it to write needs
        OVERWRITE, even though both use mode ``w``.
        """
        guarded = create_only(self.raw)
        guarded.writebytes("/made", b"first")
        # Second open is an overwrite, not a create.
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.openbin("/made", "wb")
        # Reopening for reading is fine.
        with guarded.openbin("/made", "rb") as f:
            self.assertEqual(f.read(), b"first")

    def test_overwrite_capability_replaces_content(self):
        guarded = CapabilityFS(
            self.raw, {Capability.CREATE, Capability.OVERWRITE}
        )
        with guarded.openbin("/f", "wb") as f:
            f.write(b"replaced")
        self.assertEqual(self.raw.readbytes("/f"), b"replaced")


# --------------------------------------------------------------------------- #
# upload / download / high level helpers
# --------------------------------------------------------------------------- #


class TestUploadDownload(CapabilityTestCase):
    def test_upload_new(self):
        guarded = create_only(self.raw)
        guarded.upload("/new", io.BytesIO(b"xyz"))
        self.assertEqual(self.raw.readbytes("/new"), b"xyz")

    def test_upload_overwrite_denied(self):
        self.raw.touch("/existing")
        guarded = create_only(self.raw)
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.upload("/existing", io.BytesIO(b"xyz"))
        self.assertEqual(self.raw.readbytes("/existing"), b"")

    def test_writefile_new_and_existing(self):
        guarded = create_only(self.raw)
        guarded.writefile("/new", io.BytesIO(b"abc"))
        self.assertEqual(self.raw.readbytes("/new"), b"abc")
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.writefile("/new", io.BytesIO(b"def"))

    def test_download_requires_read(self):
        self.raw.touch("/f")
        guarded = CapabilityFS(self.raw, {Capability.CREATE})
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.download("/f", io.BytesIO())

    def test_appendbytes_needs_overwrite_on_existing(self):
        self.raw.writebytes("/f", b"a")
        guarded = create_only(self.raw)
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.appendbytes("/f", b"b")
        self.assertEqual(self.raw.readbytes("/f"), b"a")
        guarded.appendbytes("/new", b"b")
        self.assertEqual(self.raw.readbytes("/new"), b"b")

    def test_create_wipe_requires_overwrite(self):
        self.raw.touch("/f")
        guarded = create_only(self.raw)
        self.assertFalse(guarded.create("/f"))  # exists, no wipe: no-op
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.create("/f", wipe=True)
        self.assertTrue(guarded.create("/new"))


# --------------------------------------------------------------------------- #
# copy / move / copydir / movedir
# --------------------------------------------------------------------------- #


class TestCopyMove(CapabilityTestCase):
    def setUp(self):
        super(TestCopyMove, self).setUp()
        make_tree(self.raw)

    def test_copy_file(self):
        guarded = CapabilityFS(
            self.raw, {Capability.READ, Capability.LIST, Capability.CREATE}
        )
        guarded.copy("/src/a.txt", "/dst/a.txt")
        self.assertEqual(self.raw.readtext("/dst/a.txt"), "AAA")

    def test_copy_to_existing_requires_overwrite(self):
        self.raw.writetext("/dst/a.txt", "OLD")
        guarded = CapabilityFS(
            self.raw, {Capability.READ, Capability.LIST, Capability.CREATE}
        )
        with self.assertRaises(fs.errors.DestinationExists):
            guarded.copy("/src/a.txt", "/dst/a.txt")
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.copy("/src/a.txt", "/dst/a.txt", overwrite=True)
        self.assertEqual(self.raw.readtext("/dst/a.txt"), "OLD")

    def test_copy_without_read_denied(self):
        guarded = CapabilityFS(self.raw, {Capability.CREATE})
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.copy("/src/a.txt", "/dst/a.txt")
        self.assertFalse(self.raw.exists("/dst/a.txt"))

    def test_copy_dir(self):
        guarded = CapabilityFS(
            self.raw, {Capability.READ, Capability.LIST, Capability.CREATE}
        )
        guarded.copydir("/src", "/dst/copy", create=True)
        self.assertTrue(self.raw.isfile("/dst/copy/a.txt"))
        self.assertTrue(self.raw.isfile("/dst/copy/sub/b.txt"))

    def test_move_file_across_directories(self):
        guarded = CapabilityFS(
            self.raw,
            {
                Capability.READ,
                Capability.LIST,
                Capability.CREATE,
                Capability.DELETE,
            },
        )
        guarded.move("/src/a.txt", "/dst/a.txt")
        self.assertFalse(self.raw.exists("/src/a.txt"))
        self.assertEqual(self.raw.readtext("/dst/a.txt"), "AAA")

    def test_move_without_delete_is_no_write_at_destination(self):
        guarded = CapabilityFS(
            self.raw, {Capability.READ, Capability.LIST, Capability.CREATE}
        )
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.move("/src/a.txt", "/dst/a.txt")
        self.assertTrue(self.raw.exists("/src/a.txt"))
        self.assertFalse(self.raw.exists("/dst/a.txt"))

    def test_move_overwrite_requires_capability(self):
        self.raw.writetext("/dst/a.txt", "OLD")
        guarded = CapabilityFS(
            self.raw,
            {
                Capability.READ,
                Capability.LIST,
                Capability.CREATE,
                Capability.DELETE,
            },
        )
        with self.assertRaises(fs.errors.DestinationExists):
            guarded.move("/src/a.txt", "/dst/a.txt")
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.move("/src/a.txt", "/dst/a.txt", overwrite=True)
        self.assertEqual(self.raw.readtext("/dst/a.txt"), "OLD")
        self.assertTrue(self.raw.exists("/src/a.txt"))

    def test_move_dir_across_directories(self):
        guarded = CapabilityFS(
            self.raw,
            {
                Capability.READ,
                Capability.LIST,
                Capability.CREATE,
                Capability.DELETE,
            },
        )
        guarded.movedir("/src", "/dst/src", create=True)
        self.assertFalse(self.raw.exists("/src"))
        self.assertTrue(self.raw.isfile("/dst/src/sub/b.txt"))

    def test_move_dir_without_delete_denied(self):
        guarded = CapabilityFS(
            self.raw, {Capability.READ, Capability.LIST, Capability.CREATE}
        )
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.movedir("/src", "/dst/src", create=True)
        self.assertTrue(self.raw.exists("/src/a.txt"))
        self.assertFalse(self.raw.exists("/dst/src"))


class TestCopyMoveOSFS(unittest.TestCase):
    """Cross-directory move on an FS advertising ``supports_rename``."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.raw = OSFS(self.tmp)
        make_tree(self.raw)

    def tearDown(self):
        self.raw.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rename_fast_path_is_guarded(self):
        self.assertTrue(self.raw.getmeta().get("supports_rename"))
        guarded = CapabilityFS(
            self.raw,
            {Capability.READ, Capability.LIST, Capability.CREATE},
        )
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.move("/src/a.txt", "/dst/a.txt")
        self.assertTrue(self.raw.exists("/src/a.txt"))
        self.assertFalse(self.raw.exists("/dst/a.txt"))

    def test_allowed_move_uses_copy_and_remove(self):
        guarded = CapabilityFS(self.raw, ALL_CAPABILITIES)
        guarded.move("/src/a.txt", "/dst/a.txt")
        self.assertFalse(self.raw.exists("/src/a.txt"))
        self.assertTrue(self.raw.exists("/dst/a.txt"))
        guarded.movedir("/src", "/dst/src", create=True)
        self.assertTrue(self.raw.exists("/dst/src/sub/b.txt"))
        self.assertFalse(self.raw.exists("/src"))


# --------------------------------------------------------------------------- #
# Recording based "no writes on denial" checks
# --------------------------------------------------------------------------- #


class TestNoWriteOnDenial(unittest.TestCase):
    def setUp(self):
        self.mem = MemoryFS()
        make_tree(self.mem)
        self.raw = RecordingFS(self.mem)
        self.guarded = CapabilityFS(
            self.raw, {Capability.READ, Capability.LIST}
        )

    def tearDown(self):
        self.mem.close()

    def assert_no_write(self, func, *args, **kwargs):
        self.raw.reset()
        with self.assertRaises(fs.errors.PermissionDenied):
            func(*args, **kwargs)
        self.assertFalse(
            self.raw.any_write_occurred(),
            msg="denied operation triggered a write: {!r}".format(
                self.raw.calls
            ),
        )

    def test_denied_writebytes(self):
        self.assert_no_write(self.guarded.writebytes, "/x", b"x")

    def test_denied_open_for_writing(self):
        for mode in ("wb", "ab", "w+b", "wt"):
            self.assert_no_write(self.guarded.openbin, "/x", mode)

    def test_denied_remove(self):
        self.assert_no_write(self.guarded.remove, "/src/a.txt")

    def test_denied_removedir(self):
        self.assert_no_write(self.guarded.removedir, "/dst")

    def test_denied_makedir(self):
        self.assert_no_write(self.guarded.makedir, "/newdir")

    def test_denied_setinfo(self):
        self.assert_no_write(
            self.guarded.setinfo, "/src/a.txt", {"details": {"modified": 0}}
        )

    def test_denied_copy(self):
        self.assert_no_write(
            self.guarded.copy, "/src/a.txt", "/dst/a.txt"
        )

    def test_denied_move(self):
        self.assert_no_write(
            self.guarded.move, "/src/a.txt", "/dst/a.txt"
        )

    def test_denied_movedir(self):
        self.assert_no_write(
            self.guarded.movedir, "/src", "/dst/src",
        )

    def test_denied_upload(self):
        self.assert_no_write(
            self.guarded.upload, "/dst/new", io.BytesIO(b"x")
        )

    def test_allowed_reads_do_not_write(self):
        self.raw.reset()
        self.guarded.readbytes("/src/a.txt")
        list(self.guarded.scandir("/src"))
        self.assertFalse(self.raw.any_write_occurred())


class TestCreateOnlyNoOverwriteRecording(unittest.TestCase):
    """An upload-only plugin: creates files, never deletes or replaces."""

    def setUp(self):
        self.mem = MemoryFS()
        self.mem.writetext("/existing", "keep")
        self.raw = RecordingFS(self.mem)
        self.plugin = CapabilityFS(
            self.raw,
            {Capability.READ, Capability.LIST, Capability.CREATE},
        )

    def tearDown(self):
        self.mem.close()

    def test_existing_file_immune(self):
        self.raw.reset()
        with self.assertRaises(fs.errors.PermissionDenied):
            self.plugin.writebytes("/existing", b"hack")
        with self.assertRaises(fs.errors.PermissionDenied):
            self.plugin.remove("/existing")
        with self.assertRaises(fs.errors.PermissionDenied):
            self.plugin.upload("/existing", io.BytesIO(b"hack"))
        self.assertFalse(self.raw.any_write_occurred())
        self.assertEqual(self.mem.readtext("/existing"), "keep")

    def test_can_drop_new_files(self):
        self.plugin.makedir("/incoming")
        self.plugin.upload("/incoming/a", io.BytesIO(b"new"))
        self.assertEqual(self.mem.readbytes("/incoming/a"), b"new")


# --------------------------------------------------------------------------- #
# Sub-filesystems: narrowing and confinement
# --------------------------------------------------------------------------- #


class TestSubFS(CapabilityTestCase):
    def setUp(self):
        super(TestSubFS, self).setUp()
        make_tree(self.raw)

    def test_opendir_narrows_capabilities(self):
        full = CapabilityFS(self.raw, ALL_CAPABILITIES)
        sub = full.opendir("/src", capabilities=READ_ONLY_CAPABILITIES)
        self.assertEqual(sub.capabilities, READ_ONLY_CAPABILITIES)
        self.assertEqual(sub.readtext("/a.txt"), "AAA")
        with self.assertRaises(fs.errors.PermissionDenied):
            sub.writebytes("/x", b"x")
        with self.assertRaises(fs.errors.PermissionDenied):
            sub.remove("/a.txt")

    def test_narrowing_can_never_restore(self):
        readonly = read_only(self.raw)
        sub = readonly.opendir(
            "/src", capabilities=ALL_CAPABILITIES
        )
        self.assertEqual(sub.capabilities, READ_ONLY_CAPABILITIES)
        with self.assertRaises(fs.errors.PermissionDenied):
            sub.writebytes("/x", b"x")

    def test_path_stays_inside_root(self):
        readonly = read_only(self.raw)
        sub = readonly.opendir("/src")
        with self.assertRaises(fs.errors.IllegalBackReference):
            sub.readtext("../a.txt")
        with self.assertRaises(fs.errors.IllegalBackReference):
            sub.readtext("../../etc/passwd")
        with self.assertRaises(fs.errors.IllegalBackReference):
            sub.listdir("..")
        self.assertEqual(sub.readtext("a.txt"), "AAA")

    def test_nested_subfs_resolves_correctly(self):
        full = CapabilityFS(self.raw, ALL_CAPABILITIES)
        deep = full.opendir("/src").opendir("/sub")
        self.assertEqual(deep.readtext("/b.txt"), "BBB")
        deep.writebytes("/c.txt", b"C")
        self.assertEqual(self.raw.readbytes("/src/sub/c.txt"), b"C")

    def test_nested_subfs_inherits_narrowing(self):
        full = CapabilityFS(self.raw, ALL_CAPABILITIES)
        sub = full.opendir(
            "/src",
            capabilities={
                Capability.READ,
                Capability.LIST,
                Capability.CREATE,
            },
        )
        deep = sub.opendir("/sub", capabilities=ALL_CAPABILITIES)
        self.assertEqual(
            deep.capabilities,
            {Capability.READ, Capability.LIST, Capability.CREATE},
        )
        with self.assertRaises(fs.errors.PermissionDenied):
            deep.remove("/b.txt")
        with self.assertRaises(fs.errors.PermissionDenied):
            deep.writebytes("/b.txt", b"x")  # overwrite
        deep.writebytes("/new", b"x")
        self.assertTrue(self.raw.exists("/src/sub/new"))

    def test_makedir_returns_capability_subfs(self):
        guarded = CapabilityFS(
            self.raw,
            {Capability.READ, Capability.LIST, Capability.CREATE},
        )
        sub = guarded.makedir("/made")
        self.assertIsInstance(sub, CapabilityFS)
        self.assertEqual(
            sub.capabilities,
            {Capability.READ, Capability.LIST, Capability.CREATE},
        )
        with self.assertRaises(fs.errors.PermissionDenied):
            sub.remove("/anything")

    def test_move_across_subfs_directories(self):
        full = CapabilityFS(self.raw, ALL_CAPABILITIES)
        src = full.opendir("/src")
        dst = full.opendir("/dst")
        # Cross-directory move performed through the parent view.
        full.move("/src/a.txt", "/dst/a.txt")
        self.assertFalse(src.exists("/a.txt"))
        self.assertEqual(dst.readtext("/a.txt"), "AAA")


# --------------------------------------------------------------------------- #
# Close semantics
# --------------------------------------------------------------------------- #


class TestClose(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_wrapper_does_not_own_fs_by_default(self):
        raw = OSFS(self.tmp)
        guarded = CapabilityFS(raw, ALL_CAPABILITIES)
        guarded.close()
        self.assertTrue(guarded.isclosed())
        self.assertFalse(raw.isclosed())
        raw.close()

    def test_close_wrap_closes_underlying(self):
        raw = OSFS(self.tmp)
        guarded = CapabilityFS(raw, ALL_CAPABILITIES, close_wrap=True)
        guarded.close()
        self.assertTrue(raw.isclosed())

    def test_context_manager_without_close_wrap(self):
        raw = OSFS(self.tmp)
        with CapabilityFS(raw, READ_ONLY_CAPABILITIES) as guarded:
            self.assertEqual(guarded.listdir("/"), [])
        self.assertTrue(guarded.isclosed())
        self.assertFalse(raw.isclosed())
        raw.close()

    def test_closed_wrapper_rejects_operations(self):
        raw = OSFS(self.tmp)
        guarded = CapabilityFS(raw, ALL_CAPABILITIES)
        guarded.close()
        with self.assertRaises(fs.errors.FilesystemClosed):
            guarded.listdir("/")
        raw.close()


# --------------------------------------------------------------------------- #
# FSTestCases conformance with every capability granted
# --------------------------------------------------------------------------- #


from fs.test import FSTestCases  # noqa: E402


class TestFullConformance(FSTestCases, unittest.TestCase):
    def make_fs(self):
        self.backing = MemoryFS()
        return CapabilityFS(self.backing, ALL_CAPABILITIES)

    def destroy_fs(self, fs):
        fs.close()
        self.backing.close()


class TestWrapFactory(unittest.TestCase):
    def test_limit_capabilities_factory(self):
        import fs.wrap
        from fs.capabilityfs import CapabilityFS as CFS

        raw = MemoryFS()
        guarded = fs.wrap.limit_capabilities(raw, READ_ONLY_CAPABILITIES)
        self.assertIsInstance(guarded, CFS)
        self.assertEqual(guarded.capabilities, READ_ONLY_CAPABILITIES)
        guarded.close()
        self.assertFalse(raw.isclosed())  # caller still owns it
        raw.close()


class TestPreserveTime(CapabilityTestCase):
    def setUp(self):
        super(TestPreserveTime, self).setUp()
        make_tree(self.raw)

    def _guarded(self, extra=()):
        caps = {
            Capability.READ,
            Capability.LIST,
            Capability.CREATE,
            Capability.DELETE,
        }
        caps.update(extra)
        return CapabilityFS(self.raw, caps)

    def test_copy_preserve_time_requires_metadata(self):
        guarded = self._guarded()
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.copy(
                "/src/a.txt", "/dst/a.txt", preserve_time=True
            )
        self.assertFalse(self.raw.exists("/dst/a.txt"))

    def test_copy_preserve_time_allowed_with_metadata(self):
        guarded = self._guarded({Capability.OVERWRITE, Capability.METADATA})
        guarded.copy(
            "/src/a.txt", "/dst/a.txt", preserve_time=True
        )
        self.assertTrue(self.raw.exists("/dst/a.txt"))

    def test_move_preserve_time_requires_metadata(self):
        guarded = self._guarded()
        with self.assertRaises(fs.errors.PermissionDenied):
            guarded.move(
                "/src/a.txt", "/dst/a.txt", preserve_time=True
            )
        self.assertTrue(self.raw.exists("/src/a.txt"))
        self.assertFalse(self.raw.exists("/dst/a.txt"))
