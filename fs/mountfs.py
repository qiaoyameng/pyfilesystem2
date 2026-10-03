"""Manage other filesystems as a folder hierarchy.
"""

from __future__ import absolute_import, print_function, unicode_literals

import typing

from collections import OrderedDict

from six import text_type

from . import errors
from .base import FS
from .iotools import RawWrapper
from .memoryfs import MemoryFS
from .mode import validate_openbin_mode
from .path import abspath, basename, forcedir, join, normpath

if typing.TYPE_CHECKING:
    from typing import (
        Any,
        BinaryIO,
        Callable,
        Collection,
        Iterator,
        List,
        Optional,
        Sequence,
        Text,
        Tuple,
        Union,
    )

    from .enums import ResourceType
    from .info import Info, RawInfo
    from .permissions import Permissions
    from .subfs import SubFS


class MountError(Exception):
    """Thrown when mounts conflict."""


class _MountEntry(object):
    def __init__(self, path, fs, owned):
        # type: (Text, FS, bool) -> None
        self.path = path
        self.fs = fs
        self.owned = owned
        self.references = 0
        self.retired = False
        self.closed = False

    def acquire(self):
        # type: () -> None
        self.references += 1

    def _close_if_unused(self):
        # type: () -> Optional[FS]
        if self.owned and self.retired and self.references == 0 and not self.closed:
            self.closed = True
            return self.fs
        return None

    def retire(self):
        # type: () -> Optional[FS]
        self.retired = True
        return self._close_if_unused()

    def release(self):
        # type: () -> Optional[FS]
        self.references -= 1
        return self._close_if_unused()


class _ManagedBinaryFile(RawWrapper):
    def __init__(self, binary_file, release):
        # type: (BinaryIO, Callable[[], Optional[FS]]) -> None
        super(_ManagedBinaryFile, self).__init__(binary_file)
        self._release_callback = release

    def _release_and_close(self):
        # type: () -> None
        release = self._release_callback
        self._release_callback = lambda: None
        fs_to_close = release()
        if fs_to_close is not None:
            fs_to_close.close()

    def close(self):
        # type: () -> None
        if not self.closed:
            try:
                super(_ManagedBinaryFile, self).close()
            except Exception:
                try:
                    self._release_and_close()
                except Exception:
                    pass
                raise
            self._release_and_close()


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
            auto_close (bool): If `True` (the default), the child
                filesystems mounted from backend objects will be closed
                when their managed mount generation is retired. Backends
                mounted with ``own=False`` are never closed by this FS.

        """
        super(MountFS, self).__init__()
        self.auto_close = auto_close
        self.default_fs = MemoryFS()  # type: FS
        self._default_entry = _MountEntry("/", self.default_fs, True)
        self._mount_entries = []  # type: List[_MountEntry]

    @property
    def mounts(self):
        # type: () -> Sequence[Tuple[Text, FS]]
        return [(entry.path, entry.fs) for entry in self._mount_entries]

    def __repr__(self):
        # type: () -> str
        return "MountFS(auto_close={!r})".format(self.auto_close)

    def __str__(self):
        # type: () -> str
        return "<mountfs>"

    def _delegate(self, path):
        # type: (Text) -> Tuple[FS, Text]
        fs, delegate_path, _entry = self._delegate_entry(path)
        return fs, delegate_path

    def _delegate_entry(self, path, acquire=False):
        # type: (Text, bool) -> Tuple[FS, Text, Optional[_MountEntry]]
        """Get the delegate FS for a given path.

        Arguments:
            path (str): A path.

        Returns:
            (FS, str): a tuple of ``(<fs>, <path>)`` for a mounted filesystem,
            or ``(None, None)`` if no filesystem is mounted on the
            given ``path``.

        """
        self.check()
        _path = forcedir(abspath(normpath(path)))
        with self._lock:
            for entry in self._mount_entries:
                if entry.path == "/" or _path.startswith(entry.path):
                    delegate_path = _path[len(entry.path) :].rstrip("/") or "/"
                    if acquire:
                        entry.acquire()
                    return entry.fs, delegate_path, entry

        delegate_path = _path.rstrip("/") or "/"
        if acquire:
            self._default_entry.acquire()
        return self.default_fs, delegate_path, self._default_entry

    def _prepare_backend(self, fs, own):
        # type: (Union[FS, Text], Optional[bool]) -> Tuple[FS, bool, bool]
        opened_url = False
        if isinstance(fs, text_type):
            from .opener import open_fs

            fs = open_fs(fs)
            opened_url = True

        if not isinstance(fs, FS):
            raise TypeError("fs argument must be an FS object or a FS URL")

        if fs is self:
            raise ValueError("Unable to mount self")

        if own is None:
            owned = opened_url or self.auto_close
        else:
            owned = own

        return fs, owned, opened_url

    def _mount_marker(self, mount_path):
        # type: (Text) -> Tuple[FS, Text]
        if mount_path == "/":
            return self.default_fs, "/"
        with self._lock:
            parent_path = "/"
            normalized_path = mount_path.rstrip("/")
            name = basename(normalized_path)
            stripped_path = normalized_path[1:]
            if "/" in stripped_path:
                parent_path = mount_path.rstrip("/").rsplit("/", 1)[0] or "/"
            parent_fs, delegate_path, _entry = self._delegate_entry(parent_path)
        return parent_fs, join(delegate_path, name)

    def _release_entry(self, entry):
        # type: (Optional[_MountEntry]) -> None
        if entry is None:
            return
        with self._lock:
            fs_to_close = entry.release()
        if fs_to_close is not None:
            fs_to_close.close()

    def _release_handle(self, entry):
        # type: (_MountEntry) -> Optional[FS]
        with self._lock:
            return entry.release()

    def _child_mount_names(self, path):
        # type: (Text) -> List[Text]
        directory = forcedir(abspath(normpath(path)))
        names = []
        with self._lock:
            for entry in self._mount_entries:
                if not (
                    entry.path == directory.rstrip("/")
                    or entry.path.startswith(directory)
                ):
                    continue
                remainder = entry.path[len(directory) :].strip("/")
                if remainder and "/" not in remainder:
                    names.append(remainder)
        return names

    def mount(self, path, fs, own=None):
        # type: (Text, Union[FS, Text], Optional[bool]) -> None
        """Mounts a host FS object on a given path.

        Arguments:
            path (str): A path within the MountFS.
            fs (FS or str): A filesystem (instance or URL) to mount.
            own (bool, optional): Set to `False` to retain ownership of
                an externally supplied filesystem. URLs are always owned.

        """
        fs, owned, opened_url = self._prepare_backend(fs, own)
        _path = forcedir(abspath(normpath(path)))

        with self._lock:
            self.check()
            if any(entry.path == _path for entry in self._mount_entries):
                raise MountError("mount point already exists")

        marker_fs, marker_path = self._mount_marker(_path)
        try:
            marker_fs.makedirs(marker_path, recreate=True)
        except Exception:
            if opened_url:
                fs.close()
            raise

        entry = _MountEntry(_path, fs, owned)
        with self._lock:
            if any(existing.path == _path for existing in self._mount_entries):
                if opened_url:
                    fs.close()
                raise MountError("mount point already exists")
            self._mount_entries.append(entry)
            self._mount_entries.sort(key=lambda item: len(item.path), reverse=True)

    def replace_mount(self, path, fs, own=None):
        # type: (Text, Union[FS, Text], Optional[bool]) -> FS
        """Replace a mount without invalidating existing open handles.

        New opens resolve to the replacement, while handles opened before
        the replacement remain bound to the old backend. An owned old
        backend is closed after those managed handles are closed.
        """
        _path = forcedir(abspath(normpath(path)))
        with self._lock:
            self.check()
            if not any(entry.path == _path for entry in self._mount_entries):
                raise MountError("no mount at path")

        fs, owned, _opened_url = self._prepare_backend(fs, own)

        with self._lock:
            self.check()
            for index, entry in enumerate(self._mount_entries):
                if entry.path == _path:
                    break
            else:
                index = None

            if index is None:
                if _opened_url:
                    fs.close()
                raise MountError("no mount at path")

            old_entry = self._mount_entries[index]
            if old_entry.fs is fs:
                return fs
            new_entry = _MountEntry(_path, fs, owned)
            self._mount_entries[index] = new_entry
            self._mount_entries.sort(key=lambda item: len(item.path), reverse=True)
            fs_to_close = old_entry.retire()

        if fs_to_close is not None:
            fs_to_close.close()
        return old_entry.fs

    def unmount(self, path, recursive=False):
        # type: (Text, bool) -> List[FS]
        """Remove a mount point, or a mount subtree when recursive.

        Owned retired backends stay open until every handle opened through
        the removed generation is closed.
        """
        _path = forcedir(abspath(normpath(path)))
        with self._lock:
            self.check()
            if recursive:
                selected = [
                    entry
                    for entry in self._mount_entries
                    if entry.path == _path
                    or (
                        _path == "/"
                        or entry.path.startswith(_path.rstrip("/") + "/")
                    )
                ]
            else:
                selected = [
                    entry for entry in self._mount_entries if entry.path == _path
                ]
            if not selected:
                raise MountError("no mount at path")
            if not recursive:
                prefix = "/" if _path == "/" else _path.rstrip("/") + "/"
                if any(
                    entry.path != _path and entry.path.startswith(prefix)
                    for entry in self._mount_entries
                ):
                    raise MountError("mount point has nested mounts")

            selected.sort(key=lambda entry: len(entry.path), reverse=True)
            cleanups = []
            for entry in selected:
                if entry.path == "/":
                    continue
                parent_path = entry.path.rstrip("/").rsplit("/", 1)[0] or "/"
                parent_fs, delegate_parent, _parent_entry = self._delegate_entry(
                    parent_path
                )
                cleanups.append(
                    (
                        parent_fs,
                        join(
                            delegate_parent,
                            basename(entry.path.rstrip("/")),
                        ),
                    )
                )
            selected_ids = {id(entry) for entry in selected}
            self._mount_entries = [
                entry
                for entry in self._mount_entries
                if id(entry) not in selected_ids
            ]
            filesystems_to_close = []
            for entry in selected:
                fs_to_close = entry.retire()
                if fs_to_close is not None:
                    filesystems_to_close.append(fs_to_close)

        for cleanup_fs, marker_path in cleanups:
            try:
                cleanup_fs.removedir(marker_path)
            except (
                errors.ResourceNotFound,
                errors.DirectoryNotEmpty,
                errors.DirectoryExpected,
            ):
                pass

        for fs_to_close in filesystems_to_close:
            fs_to_close.close()
        return [entry.fs for entry in selected]

    def close(self):
        # type: () -> None
        if self.isclosed():
            return

        with self._lock:
            self._closed = True
            entries = list(self._mount_entries)
            del self._mount_entries[:]
            entries.append(self._default_entry)
            filesystems_to_close = []
            for entry in entries:
                fs_to_close = entry.retire()
                if fs_to_close is not None:
                    filesystems_to_close.append(fs_to_close)

        super(MountFS, self).close()
        for fs_to_close in filesystems_to_close:
            fs_to_close.close()

    def desc(self, path):
        # type: (Text) -> Text
        if not self.exists(path):
            raise errors.ResourceNotFound(path)
        fs, delegate_path = self._delegate(path)
        if fs is self.default_fs:
            fs = self
        return "{path} on {fs}".format(fs=fs, path=delegate_path)

    def getinfo(self, path, namespaces=None):
        # type: (Text, Optional[Collection[Text]]) -> Info
        self.check()
        fs, _path = self._delegate(path)
        return fs.getinfo(_path, namespaces=namespaces)

    def listdir(self, path):
        # type: (Text) -> List[Text]
        self.check()
        fs, _path = self._delegate(path)
        child_names = self._child_mount_names(path)
        try:
            names = fs.listdir(_path)
        except errors.ResourceNotFound:
            if not child_names:
                raise
            names = []
        return list(OrderedDict.fromkeys(names + child_names))

    def makedir(self, path, permissions=None, recreate=False):
        # type: (Text, Optional[Permissions], bool) -> SubFS[FS]
        self.check()
        fs, _path = self._delegate(path)
        return fs.makedir(_path, permissions=permissions, recreate=recreate)

    def openbin(self, path, mode="r", buffering=-1, **kwargs):
        # type: (Text, Text, int, **Any) -> BinaryIO
        validate_openbin_mode(mode)
        fs, _path, entry = self._delegate_entry(path, acquire=True)
        try:
            binary_file = fs.openbin(_path, mode=mode, buffering=buffering, **kwargs)
        except Exception:
            self._release_entry(entry)
            raise
        return _ManagedBinaryFile(
            binary_file, lambda: self._release_handle(entry)
        )

    def remove(self, path):
        # type: (Text) -> None
        self.check()
        fs, _path = self._delegate(path)
        return fs.remove(_path)

    def removedir(self, path):
        # type: (Text) -> None
        self.check()
        path = normpath(path)
        if path in ("", "/"):
            raise errors.RemoveRootError(path)
        fs, _path = self._delegate(path)
        return fs.removedir(_path)

    def readbytes(self, path):
        # type: (Text) -> bytes
        self.check()
        fs, _path = self._delegate(path)
        return fs.readbytes(_path)

    def download(self, path, file, chunk_size=None, **options):
        # type: (Text, BinaryIO, Optional[int], **Any) -> None
        fs, _path = self._delegate(path)
        return fs.download(_path, file, chunk_size=chunk_size, **options)

    def readtext(
        self,
        path,  # type: Text
        encoding=None,  # type: Optional[Text]
        errors=None,  # type: Optional[Text]
        newline="",  # type: Text
    ):
        # type: (...) -> Text
        self.check()
        fs, _path = self._delegate(path)
        return fs.readtext(_path, encoding=encoding, errors=errors, newline=newline)

    def getsize(self, path):
        # type: (Text) -> int
        self.check()
        fs, _path = self._delegate(path)
        return fs.getsize(_path)

    def getsyspath(self, path):
        # type: (Text) -> Text
        self.check()
        fs, _path = self._delegate(path)
        return fs.getsyspath(_path)

    def gettype(self, path):
        # type: (Text) -> ResourceType
        self.check()
        fs, _path = self._delegate(path)
        return fs.gettype(_path)

    def geturl(self, path, purpose="download"):
        # type: (Text, Text) -> Text
        self.check()
        fs, _path = self._delegate(path)
        return fs.geturl(_path, purpose=purpose)

    def hasurl(self, path, purpose="download"):
        # type: (Text, Text) -> bool
        self.check()
        fs, _path = self._delegate(path)
        return fs.hasurl(_path, purpose=purpose)

    def isdir(self, path):
        # type: (Text) -> bool
        self.check()
        fs, _path = self._delegate(path)
        return fs.isdir(_path)

    def isfile(self, path):
        # type: (Text) -> bool
        self.check()
        fs, _path = self._delegate(path)
        return fs.isfile(_path)

    def scandir(
        self,
        path,  # type: Text
        namespaces=None,  # type: Optional[Collection[Text]]
        page=None,  # type: Optional[Tuple[int, int]]
    ):
        # type: (...) -> Iterator[Info]
        self.check()
        fs, _path = self._delegate(path)
        return fs.scandir(_path, namespaces=namespaces, page=page)

    def setinfo(self, path, info):
        # type: (Text, RawInfo) -> None
        self.check()
        fs, _path = self._delegate(path)
        return fs.setinfo(_path, info)

    def validatepath(self, path):
        # type: (Text) -> Text
        self.check()
        fs, _path = self._delegate(path)
        fs.validatepath(_path)
        path = abspath(normpath(path))
        return path

    def upload(self, path, file, chunk_size=None, **options):
        # type: (Text, BinaryIO, Optional[int], **Any) -> None
        self.check()
        fs, _path = self._delegate(path)
        return fs.upload(_path, file, chunk_size=chunk_size, **options)

    def writebytes(self, path, contents):
        # type: (Text, bytes) -> None
        self.check()
        fs, _path = self._delegate(path)
        return fs.writebytes(_path, contents)

    def writetext(
        self,
        path,  # type: Text
        contents,  # type: Text
        encoding="utf-8",  # type: Text
        errors=None,  # type: Optional[Text]
        newline="",  # type: Text
    ):
        # type: (...) -> None
        fs, _path = self._delegate(path)
        return fs.writetext(
            _path, contents, encoding=encoding, errors=errors, newline=newline
        )
