"""Capability based filesystem wrapper.

This module provides a `~fs.capabilityfs.CapabilityFS` wrapper which
restricts the operations permitted on a wrapped filesystem based on a
set of *capabilities*.

The following capabilities are distinguished:

``Capability.READ``
    Reading the contents of files.
``Capability.LIST``
    Listing and scanning the contents of directories.
``Capability.CREATE``
    Creating new files and directories.
``Capability.OVERWRITE``
    Replacing, truncating or otherwise modifying the contents of an
    already existing resource.
``Capability.DELETE``
    Removing files and directories.
``Capability.META``
    Reading and writing resource metadata, such as timestamps.

A derived filesystem (e.g. the one returned by `~fs.base.FS.opendir`)
can only *narrow* the capabilities of its parent; a capability that was
denied on the parent can never be re-granted by a child wrapper.

The wrapped filesystem is owned by the caller. Whether closing the
wrapper also closes the wrapped filesystem is configurable through the
``close_wrapped`` argument.
"""

from __future__ import absolute_import, print_function, unicode_literals

import typing

import six

from . import errors
from .base import FS
from .error_tools import unwrap_errors
from .info import Info
from .mode import Mode
from .path import abspath, isbase, join, normpath, relpath
from .subfs import SubFS
from .wrapfs import WrapFS

if typing.TYPE_CHECKING:
    from typing import (
        Any,
        AnyStr,
        BinaryIO,
        Collection,
        FrozenSet,
        IO,
        Iterable,
        Iterator,
        List,
        Mapping,
        Optional,
        Set,
        Text,
        Tuple,
        Type,
    )

    from datetime import datetime

    from .info import RawInfo
    from .permissions import Permissions


__all__ = [
    "Capability",
    "ALL_CAPABILITIES",
    "READ_CAPABILITIES",
    "CapabilityFS",
    "CapabilitySubFS",
    "ClosingCapabilitySubFS",
]


class Capability(six.text_type):
    """A capability that may be granted to a `CapabilityFS`.

    Capabilities are strings, so they may be compared directly against
    their ``str`` value::

        >>> Capability.READ == "read"
        True

    """

    READ = "read"
    LIST = "list"
    CREATE = "create"
    OVERWRITE = "overwrite"
    DELETE = "delete"
    META = "meta"


_ALL = frozenset(
    (
        Capability.READ,
        Capability.LIST,
        Capability.CREATE,
        Capability.OVERWRITE,
        Capability.DELETE,
        Capability.META,
    )
)  # type: FrozenSet[Text]

#: The set of every available capability.
ALL_CAPABILITIES = _ALL

#: The capabilities that only permit reading and listing access.
READ_CAPABILITIES = frozenset((Capability.READ, Capability.LIST))


def _as_capabilities(capabilities):
    # type: (Optional[Iterable[Text]]) -> FrozenSet[Text]
    """Normalize an iterable of capabilities into a frozen set."""
    if capabilities is None:
        return frozenset()
    unknown = set(capabilities) - _ALL
    if unknown:
        raise ValueError("unknown capabilities: {}".format(sorted(unknown)))
    return frozenset(capabilities)


_C = typing.TypeVar("_C", bound="CapabilityFS")
_F = typing.TypeVar("_F", bound="FS", covariant=True)


def _unwrap_chain(wrap_fs, capabilities):
    # type: (FS, Optional[Iterable[Text]]) -> Tuple[FS, Text, FrozenSet[Text]]
    """Flatten a chain of `CapabilityFS` wrappers.

    Returns a tuple of ``(<terminal fs>, <root path on terminal>,
    <effective capabilities>)``. The terminal filesystem is the
    ultimately wrapped, non-capability filesystem object, which remains
    owned by the caller.
    """
    root_parts = []  # type: List[Text]
    if isinstance(wrap_fs, CapabilityFS):
        # When nesting, start from every capability and intersect with
        # each ancestor: the result can only be narrower than the
        # chain. Explicitly supplied capabilities are included in the
        # intersection as well.
        effective = _ALL
        if capabilities is not None:
            effective = effective & _as_capabilities(capabilities)
    else:
        # The wrapped object is the caller-owned terminal filesystem;
        # default to no capabilities and honour the explicit argument.
        effective = _as_capabilities(capabilities)
    terminal = wrap_fs
    while isinstance(terminal, CapabilityFS):
        effective = effective & terminal.capabilities
        root_parts.append(terminal._root)
        terminal = terminal._wrap_fs
    root = "/"
    for part in reversed(root_parts):
        root = join(root, part.lstrip("/")) if part != "/" else root
    if root == "":
        root = "/"
    return terminal, root, effective


@six.python_2_unicode_compatible
class CapabilityFS(WrapFS, typing.Generic[_F]):
    """A filesystem wrapper enforcing a set of capabilities.

    Example:
        >>> from fs.capabilityfs import CapabilityFS, Capability
        >>> from fs.memoryfs import MemoryFS
        >>> mem = MemoryFS()
        >>> ro = CapabilityFS(mem, [Capability.READ, Capability.LIST])
        >>> ro.writetext("file.txt", "hello")
        Traceback (most recent call last):
        ...
        fs.errors.PermissionDenied: permission denied

    Arguments:
        wrap_fs (FS): The wrapped filesystem, which remains owned by
            the caller.
        capabilities (iterable of `Capability`): The capabilities
            granted on this wrapper. Anything not in this set is
            denied. Defaults to no capability at all.
        root (str): The path, on the wrapped filesystem, of the folder
            this wrapper maps to its own root. Defaults to ``"/"``.
        close_wrapped (bool): If `True`, closing this wrapper also
            closes the wrapped filesystem. Defaults to `False`, since
            the wrapped filesystem is owned by the caller and may be
            shared with other wrappers.

    """

    wrap_name = "capabilities"

    def __init__(
        self,
        wrap_fs,  # type: FS
        capabilities=None,  # type: Optional[Iterable[Text]]
        root="/",  # type: Text
        close_wrapped=False,  # type: bool
    ):
        # type: (...) -> None
        terminal, effective_root, effective = _unwrap_chain(
            wrap_fs, capabilities
        )
        if root != "/":
            effective_root = join(effective_root, relpath(normpath(root)))
            if effective_root == "":
                effective_root = "/"
        self._wrap_fs = terminal
        self._capabilities = effective
        self._root = abspath(normpath(effective_root))
        self._close_wrapped = bool(close_wrapped)
        self._sub_dir = "/"  # type: Text
        FS.__init__(self)

    def __repr__(self):
        # type: () -> Text
        return "{}(root={!r}, capabilities={!r}, wrap={!r})".format(
            self.__class__.__name__,
            self._root,
            sorted(self._capabilities),
            self._wrap_fs,
        )

    def __str__(self):
        # type: () -> Text
        return "{parent}{dir} [{caps}]".format(
            parent=self._wrap_fs,
            dir="" if self._sub_dir == "/" else self._sub_dir,
            caps=",".join(sorted(self._capabilities)) or "none",
        )

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #

    @property
    def capabilities(self):
        # type: () -> FrozenSet[Text]
        """`frozenset`: the capabilities granted to this filesystem."""
        return self._capabilities

    @property
    def root_path(self):
        # type: () -> Text
        """`str`: the mapped path on the terminal filesystem."""
        return self._root

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _has(self, capability):
        # type: (Text) -> bool
        return capability in self._capabilities

    def _require(self, capability, path=None):
        # type: (Text, Optional[Text]) -> None
        """Raise `~fs.errors.PermissionDenied` if a capability is absent."""
        if capability not in self._capabilities:
            raise errors.PermissionDenied(path)

    def _resolve(self, path):
        # type: (Text) -> Tuple[FS, Text]
        """Resolve a wrapper path to ``(<terminal fs>, <terminal path>)``.

        Back-references that would resolve outside of the wrapper root
        are rejected after resolution, so containment can not be
        bypassed with ``..`` segments.
        """
        try:
            absolute = abspath(normpath(path))
        except errors.IllegalBackReference:
            raise errors.PermissionDenied(path)
        terminal_path = (
            absolute
            if self._root == "/"
            else join(self._root, absolute.lstrip("/"))
        )
        terminal_path = abspath(terminal_path)
        if terminal_path != self._root and not isbase(self._root, terminal_path):
            raise errors.PermissionDenied(path)
        return self._wrap_fs, terminal_path

    def delegate_path(self, path):
        # type: (Text) -> Tuple[FS, Text]
        return self._resolve(path)

    def delegate_fs(self):
        # type: () -> FS
        return self._wrap_fs

    def _check_open_mode(self, mode, path):
        # type: (Text, Text) -> None
        """Check that an open mode is permitted for the given path."""
        open_mode = Mode(mode)
        if open_mode.reading and not open_mode.writing:
            self._require(Capability.READ, path)
            return
        absolute = abspath(normpath(path))
        if open_mode.appending:
            self._require(Capability.CREATE, path)
        elif open_mode.exclusive:
            self._require(Capability.CREATE, path)
        elif open_mode.truncate:
            if self.exists(absolute):
                self._require(Capability.OVERWRITE, path)
            else:
                self._require(Capability.CREATE, path)
        elif open_mode.reading:
            # Updating modes such as "r+": the resource must already
            # exist, and writing to it counts as overwriting.
            self._require(Capability.READ, path)
            self._require(Capability.OVERWRITE, path)

    # ------------------------------------------------------------------ #
    # Opening files
    # ------------------------------------------------------------------ #

    def openbin(self, path, mode="r", buffering=-1, **options):
        # type: (Text, Text, int, **Any) -> BinaryIO
        self.check()
        self._check_open_mode(mode, path)
        _fs, _path = self._resolve(path)
        with unwrap_errors(path):
            return _fs.openbin(_path, mode=mode, buffering=buffering, **options)

    def open(
        self,
        path,  # type: Text
        mode="r",  # type: Text
        buffering=-1,  # type: int
        encoding=None,  # type: Optional[Text]
        errors=None,  # type: Optional[Text]
        newline="",  # type: Text
        line_buffering=False,  # type: bool
        **options  # type: Any
    ):
        # type: (...) -> IO[AnyStr]
        # FS.open performs the mode to openbin mapping itself, so the
        # capability check is applied inside openbin.
        return FS.open(
            self,
            path,
            mode=mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
            line_buffering=line_buffering,
            **options
        )

    # ------------------------------------------------------------------ #
    # Directories
    # ------------------------------------------------------------------ #

    def listdir(self, path):
        # type: (Text) -> List[Text]
        self.check()
        self._require(Capability.LIST, path)
        _fs, _path = self._resolve(path)
        with unwrap_errors(path):
            return _fs.listdir(_path)

    def scandir(
        self,
        path,  # type: Text
        namespaces=None,  # type: Optional[Collection[Text]]
        page=None,  # type: Optional[Tuple[int, int]]
    ):
        # type: (...) -> Iterator[Info]
        self.check()
        self._require(Capability.LIST, path)
        _fs, _path = self._resolve(path)

        def _iterate():
            # type: () -> Iterator[Info]
            with unwrap_errors(path):
                for info in _fs.scandir(
                    _path, namespaces=namespaces, page=page
                ):
                    yield info

        return iter(list(_iterate()))

    def filterdir(
        self,
        path,  # type: Text
        files=None,  # type: Optional[Iterable[Text]]
        dirs=None,  # type: Optional[Iterable[Text]]
        exclude_dirs=None,  # type: Optional[Iterable[Text]]
        exclude_files=None,  # type: Optional[Iterable[Text]]
        namespaces=None,  # type: Optional[Collection[Text]]
        page=None,  # type: Optional[Tuple[int, int]]
    ):
        # type: (...) -> Iterator[Info]
        self.check()
        self._require(Capability.LIST, path)
        _fs, _path = self._resolve(path)

        def _iterate():
            # type: () -> Iterator[Info]
            with unwrap_errors(path):
                for info in _fs.filterdir(
                    _path,
                    files=files,
                    dirs=dirs,
                    exclude_dirs=exclude_dirs,
                    exclude_files=exclude_files,
                    namespaces=namespaces,
                    page=page,
                ):
                    yield info

        return iter(list(_iterate()))

    def makedir(
        self,
        path,  # type: Text
        permissions=None,  # type: Optional[Permissions]
        recreate=False,  # type: bool
    ):
        # type: (...) -> CapabilitySubFS
        self.check()
        absolute = abspath(normpath(path))
        if not (recreate and (absolute == "/" or self.exists(absolute))):
            self._require(Capability.CREATE, path)
        _fs, _path = self._resolve(path)
        with unwrap_errors(path):
            _fs.makedir(_path, permissions=permissions, recreate=recreate)
        return self.opendir(absolute)

    def makedirs(
        self,
        path,  # type: Text
        permissions=None,  # type: Optional[Permissions]
        recreate=False,  # type: bool
    ):
        # type: (...) -> CapabilitySubFS
        # FS.makedirs walks the path and calls makedir, which applies
        # the capability checks itself.
        return FS.makedirs(
            self, path, permissions=permissions, recreate=recreate
        )

    # ------------------------------------------------------------------ #
    # Removing
    # ------------------------------------------------------------------ #

    def remove(self, path):
        # type: (Text) -> None
        self.check()
        self._require(Capability.DELETE, path)
        _fs, _path = self._resolve(path)
        with unwrap_errors(path):
            _fs.remove(_path)

    def removedir(self, path):
        # type: (Text) -> None
        self.check()
        absolute = abspath(normpath(path))
        if absolute == "/":
            raise errors.RemoveRootError()
        self._require(Capability.DELETE, path)
        _fs, _path = self._resolve(path)
        with unwrap_errors(path):
            _fs.removedir(_path)

    def removetree(self, dir_path):
        # type: (Text) -> None
        self.check()
        self._require(Capability.DELETE, dir_path)
        self._require(Capability.LIST, dir_path)
        # FS.removetree walks the tree itself and calls remove/removedir,
        # so all further operations are already checked.
        FS.removetree(self, dir_path)

    # ------------------------------------------------------------------ #
    # Metadata
    # ------------------------------------------------------------------ #

    def getinfo(self, path, namespaces=None):
        # type: (Text, Optional[Collection[Text]]) -> Info
        self.check()
        _fs, _path = self._resolve(path)
        with unwrap_errors(path):
            raw_info = _fs.getinfo(_path, namespaces=namespaces).raw
        if abspath(normpath(path)) == "/":
            raw_info = dict(raw_info)
            raw_info["basic"]["name"] = ""  # type: ignore
        return Info(raw_info)

    def setinfo(self, path, info):
        # type: (Text, RawInfo) -> None
        self.check()
        self._require(Capability.META, path)
        _fs, _path = self._resolve(path)
        with unwrap_errors(path):
            _fs.setinfo(_path, info)

    def settimes(self, path, accessed=None, modified=None):
        # type: (Text, Optional[datetime], Optional[datetime]) -> None
        self.check()
        self._require(Capability.META, path)
        _fs, _path = self._resolve(path)
        with unwrap_errors(path):
            _fs.settimes(_path, accessed=accessed, modified=modified)

    # ------------------------------------------------------------------ #
    # Writing helpers
    # ------------------------------------------------------------------ #

    def create(self, path, wipe=False):
        # type: (Text, bool) -> bool
        self.check()
        absolute = abspath(normpath(path))
        exists = self.exists(absolute)
        if wipe and exists:
            self._require(Capability.OVERWRITE, path)
        elif not exists:
            self._require(Capability.CREATE, path)
        # FS.create performs the actual openbin("wb") call; the openbin
        # gate enforces the same capability decisions a second time.
        return FS.create(self, path, wipe=wipe)

    def touch(self, path):
        # type: (Text) -> None
        self.check()
        absolute = abspath(normpath(path))
        if self.exists(absolute):
            self._require(Capability.META, path)
        else:
            self._require(Capability.CREATE, path)
        FS.touch(self, path)

    def writebytes(self, path, contents):
        # type: (Text, bytes) -> None
        FS.writebytes(self, path, contents)

    def upload(self, path, file, chunk_size=None, **options):
        # type: (Text, BinaryIO, Optional[int], **Any) -> None
        FS.upload(self, path, file, chunk_size=chunk_size, **options)

    def writefile(
        self,
        path,  # type: Text
        file,  # type: IO[AnyStr]
        encoding=None,  # type: Optional[Text]
        errors=None,  # type: Optional[Text]
        newline="",  # type: Text
    ):
        # type: (...) -> None
        FS.writefile(
            self,
            path,
            file,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    def appendbytes(self, path, data):
        # type: (Text, bytes) -> None
        FS.appendbytes(self, path, data)

    def appendtext(
        self,
        path,  # type: Text
        text,  # type: Text
        encoding="utf-8",  # type: Text
        errors=None,  # type: Optional[Text]
        newline="",  # type: Text
    ):
        # type: (...) -> None
        FS.appendtext(
            self,
            path,
            text,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    def download(self, path, file, chunk_size=None, **options):
        # type: (Text, BinaryIO, Optional[int], **Any) -> None
        FS.download(self, path, file, chunk_size=chunk_size, **options)

    def readbytes(self, path):
        # type: (Text) -> bytes
        return FS.readbytes(self, path)

    def readtext(
        self,
        path,  # type: Text
        encoding=None,  # type: Optional[Text]
        errors=None,  # type: Optional[Text]
        newline="",  # type: Text
    ):
        # type: (...) -> Text
        return FS.readtext(
            self, path, encoding=encoding, errors=errors, newline=newline
        )

    def hash(self, path, name):
        # type: (Text, Text) -> Text
        return FS.hash(self, path, name)

    # ------------------------------------------------------------------ #
    # Copy and move
    # ------------------------------------------------------------------ #

    def _iter_files(self, dir_path):
        # type: (Text) -> Iterator[Text]
        """Iterate over absolute paths of all files below ``dir_path``."""
        for info in self.scandir(dir_path):
            child = join(dir_path, info.name)
            if info.is_dir:
                for nested in self._iter_files(child):
                    yield nested
            else:
                yield child

    def _collision_files(self, src_absolute, dst_absolute):
        # type: (Text, Text) -> Set[Text]
        """Return relative file paths in ``src`` that also exist in ``dst``."""
        collisions = set()  # type: Set[Text]
        for file_path in self._iter_files(src_absolute):
            relative = file_path[len(src_absolute):].lstrip("/")
            if self.exists(join(dst_absolute, relative)):
                collisions.add(relative)
        return collisions

    def copy(self, src_path, dst_path, overwrite=False, preserve_time=False):
        # type: (Text, Text, bool, bool) -> None
        self.check()
        src = abspath(normpath(src_path))
        dst = abspath(normpath(dst_path))
        self._require(Capability.READ, src_path)
        if self.exists(dst):
            self._require(Capability.OVERWRITE, dst_path)
        else:
            self._require(Capability.CREATE, dst_path)
        if preserve_time:
            self._require(Capability.META, dst_path)
        # FS.copy validates existence and opens the source and
        # destination through this wrapper's openbin, so no write can
        # reach the terminal filesystem without passing a capability gate.
        FS.copy(
            self,
            src,
            dst,
            overwrite=overwrite,
            preserve_time=preserve_time,
        )

    def copydir(self, src_path, dst_path, create=False, preserve_time=False):
        # type: (Text, Text, bool, bool) -> None
        self.check()
        src = abspath(normpath(src_path))
        dst = abspath(normpath(dst_path))
        self._require(Capability.READ, src_path)
        self._require(Capability.LIST, src_path)
        if self.exists(dst):
            collisions = self._collision_files(src, dst)
            if collisions:
                self._require(Capability.OVERWRITE, dst_path)
        else:
            if not create:
                raise errors.ResourceNotFound(dst_path)
            self._require(Capability.CREATE, dst_path)
        if preserve_time:
            self._require(Capability.META, dst_path)
        FS.copydir(
            self,
            src,
            dst,
            create=create,
            preserve_time=preserve_time,
        )

    def move(self, src_path, dst_path, overwrite=False, preserve_time=False):
        # type: (Text, Text, bool, bool) -> None
        self.check()
        src = abspath(normpath(src_path))
        dst = abspath(normpath(dst_path))
        self._require(Capability.READ, src_path)
        self._require(Capability.DELETE, src_path)
        if self.exists(dst):
            self._require(Capability.OVERWRITE, dst_path)
        else:
            self._require(Capability.CREATE, dst_path)
        if preserve_time:
            self._require(Capability.META, dst_path)
        # Disable any rename fast path in FS.move by reporting no
        # rename support; the fallback performs open/upload/remove,
        # all of which are gated through this wrapper.
        FS.move(
            self,
            src,
            dst,
            overwrite=overwrite,
            preserve_time=preserve_time,
        )

    def movedir(self, src_path, dst_path, create=False, preserve_time=False):
        # type: (Text, Text, bool, bool) -> None
        self.check()
        src = abspath(normpath(src_path))
        dst = abspath(normpath(dst_path))
        self._require(Capability.READ, src_path)
        self._require(Capability.LIST, src_path)
        self._require(Capability.DELETE, src_path)
        if self.exists(dst):
            collisions = self._collision_files(src, dst)
            if collisions:
                self._require(Capability.OVERWRITE, dst_path)
        else:
            if not create:
                raise errors.ResourceNotFound(dst_path)
            self._require(Capability.CREATE, dst_path)
        if preserve_time:
            self._require(Capability.META, dst_path)
        FS.movedir(
            self,
            src,
            dst,
            create=create,
            preserve_time=preserve_time,
        )

    # ------------------------------------------------------------------ #
    # Sub filesystems, meta and closing
    # ------------------------------------------------------------------ #

    def getmeta(self, namespace="standard"):
        # type: (Text) -> Mapping[Text, object]
        self.check()
        meta = dict(self.delegate_fs().getmeta(namespace=namespace))
        if namespace == "standard":
            structural_write_caps = {
                Capability.CREATE,
                Capability.OVERWRITE,
                Capability.DELETE,
            }
            meta["read_only"] = not bool(
                self._capabilities & structural_write_caps
            )
            # Never use a native rename: it would bypass the wrapper's
            # move checks.
            meta["supports_rename"] = False
        return meta

    def opendir(
        self,  # type: _C
        path,  # type: Text
        factory=None,  # type: Optional[Type[SubFS]]
    ):
        # type: (...) -> CapabilitySubFS[_C]
        if not self.getinfo(path).is_dir:
            raise errors.DirectoryExpected(path=path)
        if factory is None or factory is SubFS:
            # A regular sub-filesystem does not own its parent, so
            # closing it must never close the wrapped chain.
            return CapabilitySubFS(self, path, close_wrapped=False)
        from .subfs import ClosingSubFS

        if factory is ClosingSubFS:
            return ClosingCapabilitySubFS(self, path, close_wrapped=False)
        return factory(self, path)

    def validatepath(self, path):
        # type: (Text) -> Text
        self.check()
        # Resolving both validates containment and validates the path
        # against the terminal filesystem.
        _fs, _path = self._resolve(path)
        with unwrap_errors(path):
            _fs.validatepath(_path)
        return abspath(normpath(path))

    def __del__(self):
        # Garbage collection must never close the terminal filesystem:
        # a derived sub-filesystem is not the owner of its parent and
        # may be discarded while the parent (or another sub-filesystem)
        # is still in use. Only an explicit close() propagates, and only
        # when the wrapper was constructed with close_wrapped=True.
        self._closed = True

    def close(self):
        # type: () -> None
        if not self.isclosed():
            FS.close(self)
            if self._close_wrapped:
                self._wrap_fs.close()


@six.python_2_unicode_compatible
class CapabilitySubFS(CapabilityFS[_F], SubFS):
    """A sub-directory on a `CapabilityFS`.

    A derived sub-filesystem may only narrow the capabilities of its
    parent; capabilities denied by the parent can never be restored by
    the child.
    """

    def __init__(self, parent_fs, path, capabilities=None, close_wrapped=False):
        # type: (CapabilityFS, Text, Optional[Iterable[Text]], bool) -> None
        # Paths are resolved relative to the parent wrapper, and
        # capabilities intersected with the parent's, so a child can
        # only ever reduce its permissions.
        self._parent = parent_fs
        CapabilityFS.__init__(
            self,
            parent_fs,
            capabilities,
            root=abspath(normpath(path)),
            close_wrapped=close_wrapped,
        )
        self._sub_dir = abspath(normpath(path))

    def __repr__(self):
        # type: () -> Text
        return "{}(root={!r}, capabilities={!r})".format(
            self.__class__.__name__,
            self._root,
            sorted(self._capabilities),
        )

    def __str__(self):
        # type: () -> Text
        return "{parent}{dir} [{caps}]".format(
            parent=self._wrap_fs,
            dir=self._sub_dir if self._sub_dir != "/" else "",
            caps=",".join(sorted(self._capabilities)) or "none",
        )

    def opendir(
        self,  # type: _C
        path,  # type: Text
        factory=None,  # type: Optional[Type[SubFS]]
    ):
        # type: (...) -> CapabilitySubFS[_C]
        return CapabilityFS.opendir(self, path, factory=factory)


class ClosingCapabilitySubFS(CapabilitySubFS, typing.Generic[_F]):
    """A `CapabilitySubFS` that closes its parent chain when closed."""

    def close(self):
        # type: () -> None
        if not self.isclosed():
            # Mark this filesystem closed, then close the terminal
            # filesystem directly: a closing sub-filesystem owns the
            # whole chain for the duration of its context.
            FS.close(self)
            parent = getattr(self, "_parent", None)
            if parent is not None:
                FS.close(parent)
            self._wrap_fs.close()
