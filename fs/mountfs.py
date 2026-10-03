"""Manage other filesystems as a folder hierarchy.
"""

from __future__ import absolute_import, print_function, unicode_literals

import io
import typing
from contextlib import contextmanager
from threading import RLock

from six import text_type

from . import errors
from .base import FS
from .enums import ResourceType
from .iotools import RawWrapper
from .memoryfs import MemoryFS
from .mode import Mode, validate_openbin_mode
from .path import abspath, forcedir, normpath

if typing.TYPE_CHECKING:
    from typing import (
        Any,
        BinaryIO,
        Collection,
        Dict,
        Iterator,
        List,
        MutableSequence,
        Optional,
        Text,
        Tuple,
        Union,
    )

    from .info import Info, RawInfo
    from .permissions import Permissions
    from .subfs import SubFS

    M = typing.TypeVar("M", bound="MountFS")


class MountError(Exception):
    """Thrown when mounts conflict."""


class _BackendOwnership(object):
    def __init__(self, fs):
        # type: (FS) -> None
        self.fs = fs
        self.lock = RLock()
        self.managed_count = 0
        self.external_count = 0
        self.references = 0
        self.closed = False
        self.close_requested = False

    def add_mount(self, managed):
        # type: (bool) -> None
        if managed:
            self.managed_count += 1
        else:
            self.external_count += 1

    def remove_mount(self, managed):
        # type: (bool) -> None
        if managed:
            self.managed_count -= 1
        else:
            self.external_count -= 1

    def acquire(self):
        # type: () -> None
        with self.lock:
            if self.closed or self.close_requested:
                raise errors.FilesystemClosed()
            self.references += 1

    def release(self):
        # type: () -> bool
        with self.lock:
            self.references -= 1
            if (
                self.references
                or self.managed_count
                or self.external_count
                or not self.close_requested
                or self.closed
            ):
                return False
            self.closed = True
            return True

    def close_fs(self):
        # type: () -> None
        self.fs.close()


class _MountGeneration(object):
    def __init__(self, path, fs, owner, auto_close):
        # type: (Text, FS, _BackendOwnership, bool) -> None
        self.path = path
        self.fs = fs
        self.owner = owner
        self.auto_close = auto_close
        self.retired = False
        owner.add_mount(auto_close)
        owner.acquire()

    def retire(self):
        # type: () -> bool
        should_close = False
        with self.owner.lock:
            if not self.retired:
                self.retired = True
                self.owner.remove_mount(self.auto_close)
                self.owner.references -= 1
                should_close = (
                    self.auto_close
                    and self.owner.external_count == 0
                    and self.owner.references == 0
                    and not self.owner.closed
                )
                if should_close:
                    self.owner.closed = True
                elif self.auto_close and self.owner.external_count == 0:
                    self.owner.close_requested = True
        return should_close


class _ManagedBinaryFile(RawWrapper):
    def __init__(self, file, owner, mode):
        # type: (BinaryIO, _BackendOwnership, Text) -> None
        bin_mode = Mode(mode).to_platform_bin()
        super(_ManagedBinaryFile, self).__init__(file, mode=bin_mode)
        self._owner = owner
        self._released = False

    def close(self):
        # type: () -> None
        if self.closed:
            return
        try:
            io.RawIOBase.close(self)
            if not self._f.closed:
                self._f.close()
        finally:
            if not self._released:
                self._released = True
                if self._owner.release():
                    self._owner.close_fs()


class MountFS(FS):
    """A virtual filesystem that maps directories on to other file-systems."""

    _meta = {
        "virtual": True,
        "read_only": False,
        "unicode_paths": True,
        "case_insensitive": False,
        "invalid_path_chars": "\0",
    }

    def __init__(self, auto_close=True):
        # type: (bool) -> None
        """Create a new `MountFS` instance.

        Arguments:
            auto_close (bool): If `True` (the default), filesystems
                opened or explicitly handed over to `MountFS` are
                closed when their mount is removed and all managed
                file handles have been released.

        """
        super(MountFS, self).__init__()
        self.auto_close = auto_close
        self.default_fs = MemoryFS()
        self._mount_lock = RLock()
        self._owners = {}  # type: Dict[FS, _BackendOwnership]
        self._default_owner = self._owner_for_fs(self.default_fs)
        self._default_generation = _MountGeneration(
            "/", self.default_fs, self._default_owner, True
        )
        self._mounts = {}  # type: Dict[Text, _MountGeneration]
        self.mounts = []  # type: MutableSequence[Tuple[Text, FS]]

    def __repr__(self):
        # type: () -> str
        return "MountFS(auto_close={!r})".format(self.auto_close)

    def __str__(self):
        # type: () -> str
        return "<mountfs>"

    def _owner_for_fs(self, fs):
        # type: (FS) -> _BackendOwnership
        owner = self._owners.get(fs)
        if owner is None:
            owner = _BackendOwnership(fs)
            self._owners[fs] = owner
        return owner

    def _make_generation(self, path, fs, managed):
        # type: (Text, FS, bool) -> _MountGeneration
        owner = self._owner_for_fs(fs)
        return _MountGeneration(path, fs, owner, managed)

    def _retire_generation(self, generation):
        # type: (_MountGeneration) -> None
        if generation.retire():
            generation.owner.close_fs()

    def _prepare_fs(self, fs, auto_close):
        # type: (Union[FS, Text], Optional[bool]) -> Tuple[FS, bool, bool]
        if isinstance(fs, text_type):
            from .opener import open_fs

            fs = open_fs(fs)
            managed = True
            created_here = True
        else:
            managed = bool(auto_close)
            created_here = False

        if not isinstance(fs, FS):
            raise TypeError("fs argument must be an FS object or a FS URL")

        if fs is self:
            raise ValueError("Unable to mount self")
        if fs.isclosed():
            raise MountError("unable to mount a closed filesystem")

        return fs, managed, created_here

    def _sync_mounts(self):
        # type: () -> None
        self.mounts[:] = [
            (path, generation.fs)
            for path, generation in sorted(self._mounts.items())
        ]

    def _mount_path(self, path):
        # type: (Text) -> Text
        return forcedir(abspath(normpath(path)))

    def _resolve_locked(self, path):
        # type: (Text) -> Tuple[_MountGeneration, Text]
        _path = forcedir(abspath(normpath(path)))
        for mount_path, generation in sorted(
            self._mounts.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if _path.startswith(mount_path):
                delegate_path = _path[len(mount_path) :].rstrip("/")
                return generation, delegate_path
        return self._default_generation, path

    @contextmanager
    def _delegate(self, path):
        # type: (Text) -> Iterator[Tuple[FS, Text, Optional[_BackendOwnership]]]
        self.check()
        with self._mount_lock:
            generation, delegate_path = self._resolve_locked(path)
            owner = generation.owner
            owner.acquire()
        try:
            yield generation.fs, delegate_path, owner
        finally:
            if owner.release():
                owner.close_fs()

    def _direct_child_mounts_locked(self, path):
        # type: (Text) -> List[Tuple[Text, Text]]
        directory = forcedir(abspath(normpath(path)))
        children = []
        for mount_path in self._mounts:
            if not mount_path.startswith(directory) or mount_path == directory:
                continue
            relative = mount_path[len(directory) :].rstrip("/")
            if "/" not in relative:
                children.append((relative, mount_path))
        return children

    def _mount_info(self, name):
        # type: (Text) -> Info
        raw = {
            "basic": {"name": name, "is_dir": True},
            "details": {"type": int(ResourceType.directory)},
        }
        return Info(raw)

    def mount(self, path, fs, auto_close=None):
        # type: (Text, Union[FS, Text], Optional[bool]) -> None
        """Mount a filesystem on a path.

        Backends supplied directly are not closed automatically unless
        ``auto_close=True``. Filesystems opened from a URL are owned by
        the mount.

        """
        if auto_close is None:
            auto_close = self.auto_close
        fs, managed, created_here = self._prepare_fs(fs, auto_close)
        _path = self._mount_path(path)
        self.check()

        old_generation = None
        old_owner = None
        generation = None
        try:
            with self._mount_lock:
                if _path in self._mounts:
                    raise MountError("mount point already exists")
                if _path == "/" and not self._default_generation.retired:
                    old_generation = self._default_generation
                generation = self._make_generation(_path, fs, managed)
                self._mounts[_path] = generation
                self._sync_mounts()
                old_owner = (
                    old_generation.owner
                    if old_generation is not None
                    else None
                )
        except Exception:
            if generation is not None:
                generation.retire()
            if created_here:
                fs.close()
            raise

        should_close_old = bool(
            old_generation is not None and old_generation.retire()
        )
        if should_close_old and old_owner is not None:
            old_owner.close_fs()

    def replace_mount(self, path, fs, auto_close=None):
        # type: (Text, Union[FS, Text], Optional[bool]) -> FS
        """Replace a mounted backend without invalidating open files.

        New path lookups use the new filesystem. Handles opened before
        replacement remain bound to the previous generation and keep it
        open until they are closed.

        """
        if auto_close is None:
            auto_close = self.auto_close
        fs, managed, created_here = self._prepare_fs(fs, auto_close)
        _path = self._mount_path(path)
        self.check()

        generation = None
        try:
            with self._mount_lock:
                old_generation = self._mounts.get(_path)
                if old_generation is None and not (
                    _path == "/" and not self._default_generation.retired
                ):
                    raise MountError("mount point does not exist")
                if old_generation is None:
                    old_generation = self._default_generation
                generation = self._make_generation(_path, fs, managed)
                self._mounts[_path] = generation
                self._sync_mounts()
                old_owner = old_generation.owner
        except Exception:
            if generation is not None:
                generation.retire()
            if created_here:
                fs.close()
            raise

        should_close_old = old_generation.retire()
        if should_close_old:
            old_owner.close_fs()
        return old_generation.fs

    def unmount(self, path, recursive=False):
        # type: (Text, bool) -> FS
        """Remove a mount and return its backend.

        An owned backend remains open until all file handles from that
        mount generation have been closed.

        """
        _path = self._mount_path(path)
        self.check()

        with self._mount_lock:
            generation = self._mounts.get(_path)
            if generation is None:
                raise MountError("mount point does not exist")

            descendants = [
                mount_path
                for mount_path in self._mounts
                if mount_path != _path and mount_path.startswith(_path)
            ]
            if descendants and not recursive:
                raise MountError("mount point contains nested mounts")

            generations = [generation]
            if recursive:
                generations = [
                    self._mounts[mount_path]
                    for mount_path in sorted(descendants, key=len, reverse=True)
                ] + generations
            close_root_default = _path == "/"

            for item in generations:
                del self._mounts[item.path]
            self._sync_mounts()
            if close_root_default:
                self.default_fs = MemoryFS()
                self._default_owner = self._owner_for_fs(self.default_fs)
                self._default_generation = _MountGeneration(
                    "/", self.default_fs, self._default_owner, True
                )

        should_close = [item.retire() for item in generations]
        for item, close_item in zip(generations, should_close):
            if close_item:
                item.owner.close_fs()
        return generations[-1].fs

    def close(self):
        # type: () -> None
        if self.isclosed():
            return

        with self._mount_lock:
            generations = [
                self._mounts[path]
                for path in sorted(self._mounts, key=len, reverse=True)
            ]
            self._mounts.clear()
            self.mounts[:] = []
            self._closed = True

        for item in generations:
            self._retire_generation(item)
        if not self._default_generation.retired:
            self._retire_generation(self._default_generation)
        super(MountFS, self).close()

    def desc(self, path):
        # type: (Text) -> Text
        if not self.exists(path):
            raise errors.ResourceNotFound(path)
        with self._delegate(path) as delegate:
            fs, delegate_path, _ = delegate
            if fs is self.default_fs:
                fs = self
            return "{path} on {fs}".format(fs=fs, path=delegate_path)

    def getinfo(self, path, namespaces=None):
        # type: (Text, Optional[Collection[Text]]) -> Info
        with self._delegate(path) as delegate:
            fs, delegate_path, _ = delegate
            return fs.getinfo(delegate_path, namespaces=namespaces)

    def listdir(self, path):
        # type: (Text) -> List[Text]
        with self._delegate(path) as delegate:
            fs, delegate_path, _ = delegate
            names = set(fs.listdir(delegate_path))
            with self._mount_lock:
                names.update(name for name, _ in self._direct_child_mounts_locked(path))
            return sorted(names)

    def makedir(self, path, permissions=None, recreate=False):
        # type: (Text, Optional[Permissions], bool) -> SubFS[FS]
        _path = forcedir(abspath(normpath(path)))
        with self._mount_lock:
            mounted = _path in self._mounts
        if mounted:
            if recreate:
                return self.opendir(path)
            raise errors.DirectoryExists(path)
        with self._delegate(path) as delegate:
            fs, delegate_path, _ = delegate
            return fs.makedir(
                delegate_path, permissions=permissions, recreate=recreate
            )

    def openbin(self, path, mode="r", buffering=-1, **kwargs):
        # type: (Text, Text, int, **Any) -> BinaryIO
        validate_openbin_mode(mode)
        self.check()
        with self._mount_lock:
            generation, delegate_path = self._resolve_locked(path)
            owner = generation.owner
            owner.acquire()
            fs = generation.fs
        try:
            bin_file = fs.openbin(
                delegate_path, mode=mode, buffering=buffering, **kwargs
            )
        except Exception:
            if owner.release():
                owner.close_fs()
            raise
        return _ManagedBinaryFile(bin_file, owner, mode)

    def remove(self, path):
        # type: (Text) -> None
        with self._delegate(path) as delegate:
            fs, delegate_path, _ = delegate
            return fs.remove(delegate_path)

    def removedir(self, path):
        # type: (Text) -> None
        _path = forcedir(abspath(normpath(path)))
        if _path == "/":
            raise errors.RemoveRootError(path)
        with self._mount_lock:
            if _path in self._mounts:
                raise errors.DirectoryNotEmpty(path)
        with self._delegate(path) as delegate:
            fs, delegate_path, _ = delegate
            return fs.removedir(delegate_path)

    def scandir(
        self,
        path,  # type: Text
        namespaces=None,  # type: Optional[Collection[Text]]
        page=None,  # type: Optional[Tuple[int, int]]
    ):
        # type: (...) -> Iterator[Info]
        with self._delegate(path) as delegate:
            fs, delegate_path, _ = delegate
            entries = list(fs.scandir(delegate_path, namespaces=namespaces))
        names = {info.name for info in entries}
        with self._mount_lock:
            children = self._direct_child_mounts_locked(path)
        entries.extend(
            self._mount_info(name) for name, _ in children if name not in names
        )
        entries.sort(key=lambda info: info.name)
        if page is not None:
            start, end = page
            entries = entries[start:end]
        return iter(entries)

    def setinfo(self, path, info):
        # type: (Text, RawInfo) -> None
        with self._delegate(path) as delegate:
            fs, delegate_path, _ = delegate
            return fs.setinfo(delegate_path, info)

    def readbytes(self, path):
        # type: (Text) -> bytes
        with self._delegate(path) as delegate:
            fs, delegate_path, _ = delegate
            return fs.readbytes(delegate_path)

    def download(self, path, file, chunk_size=None, **options):
        # type: (Text, BinaryIO, Optional[int], **Any) -> None
        with self._delegate(path) as delegate:
            fs, delegate_path, _ = delegate
            return fs.download(
                delegate_path, file, chunk_size=chunk_size, **options
            )

    def readtext(
        self,
        path,  # type: Text
        encoding=None,  # type: Optional[Text]
        errors=None,  # type: Optional[Text]
        newline="",  # type: Text
    ):
        # type: (...) -> Text
        with self._delegate(path) as delegate:
            fs, delegate_path, _ = delegate
            return fs.readtext(
                delegate_path,
                encoding=encoding,
                errors=errors,
                newline=newline,
            )

    def getsize(self, path):
        # type: (Text) -> int
        with self._delegate(path) as delegate:
            fs, delegate_path, _ = delegate
            return fs.getsize(delegate_path)

    def getsyspath(self, path):
        # type: (Text) -> Text
        with self._delegate(path) as delegate:
            fs, delegate_path, _ = delegate
            return fs.getsyspath(delegate_path)

    def gettype(self, path):
        # type: (Text) -> ResourceType
        with self._delegate(path) as delegate:
            fs, delegate_path, _ = delegate
            return fs.gettype(delegate_path)

    def geturl(self, path, purpose="download"):
        # type: (Text, Text) -> Text
        with self._delegate(path) as delegate:
            fs, delegate_path, _ = delegate
            return fs.geturl(delegate_path, purpose=purpose)

    def hasurl(self, path, purpose="download"):
        # type: (Text, Text) -> bool
        with self._delegate(path) as delegate:
            fs, delegate_path, _ = delegate
            return fs.hasurl(delegate_path, purpose=purpose)

    def isdir(self, path):
        # type: (Text) -> bool
        with self._delegate(path) as delegate:
            fs, delegate_path, _ = delegate
            return fs.isdir(delegate_path)

    def isfile(self, path):
        # type: (Text) -> bool
        with self._delegate(path) as delegate:
            fs, delegate_path, _ = delegate
            return fs.isfile(delegate_path)

    def upload(self, path, file, chunk_size=None, **options):
        # type: (Text, BinaryIO, Optional[int], **Any) -> None
        with self._delegate(path) as delegate:
            fs, delegate_path, _ = delegate
            return fs.upload(
                delegate_path, file, chunk_size=chunk_size, **options
            )

    def writebytes(self, path, contents):
        # type: (Text, bytes) -> None
        with self._delegate(path) as delegate:
            fs, delegate_path, _ = delegate
            return fs.writebytes(delegate_path, contents)

    def writetext(
        self,
        path,  # type: Text
        contents,  # type: Text
        encoding="utf-8",  # type: Text
        errors=None,  # type: Optional[Text]
        newline="",  # type: Text
    ):
        # type: (...) -> None
        with self._delegate(path) as delegate:
            fs, delegate_path, _ = delegate
            return fs.writetext(
                delegate_path,
                contents,
                encoding=encoding,
                errors=errors,
                newline=newline,
            )
