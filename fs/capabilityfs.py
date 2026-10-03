"""Capability based filesystem wrapper.

The `CapabilityFS` wraps another filesystem (or other wrapper) and
grants access only through an explicit *capability set*.  A background
plugin that may only read files can be given ``READ`` and ``LIST``,
while an upload plugin may be given ``CREATE`` without being able to
delete or overwrite existing data::

    >>> import fs.capabilityfs as capfs
    >>> from fs.memoryfs import MemoryFS
    >>> raw_fs = MemoryFS()
    >>> reader = capfs.CapabilityFS(raw_fs, capfs.READ_ONLY_CAPABILITIES)
    >>> reader.listdir("/")
    []

Capabilities only ever narrow: a sub-filesystem created with
`~fs.capabilityfs.CapabilityFS.opendir` inherits the intersection of
the parent capabilities and any additional restriction, it can never
recover a capability that was previously denied.

The wrapped filesystem is *owned by the caller*: closing the wrapper
does not close the underlying filesystem unless ``close_wrap=True`` is
passed to the constructor.
"""

from __future__ import absolute_import, print_function, unicode_literals

import typing

from .copy import copy_modified_time
from .error_tools import unwrap_errors
from .errors import (
    DestinationExists,
    IllegalBackReference,
    PermissionDenied,
    RemoveRootError,
)
from .mode import Mode
from .path import abspath, isbase, join, normpath, relpath
from .subfs import SubFS
from .wrapfs import WrapFS

if typing.TYPE_CHECKING:
    from typing import (
        IO,
        Any,
        AnyStr,
        BinaryIO,
        Callable,
        Collection,
        FrozenSet,
        Iterable,
        Iterator,
        Mapping,
        Optional,
        Text,
        Tuple,
    )

    from .base import FS
    from .info import Info, RawInfo
    from .permissions import Permissions

    _C = typing.TypeVar("_C", bound="CapabilityFS")


class Capability(object):
    """Named filesystem capabilities.

    A capability is a string token.  Pass a `frozenset` (or any
    iterable) of these tokens to `CapabilityFS` to describe what a
    caller may do with the wrapped filesystem.
    """

    #: Read the *contents* of an existing file (``open`` / ``openbin``
    #: in a readable mode, ``readbytes``, ``readtext``, ``download``,
    #: ``copy`` / ``move`` source side, hashing).
    READ = "read"

    #: List or inspect the contents of a directory (``listdir``,
    #: ``scandir``, ``filterdir``).
    LIST = "list"

    #: Create new files or directories.  Does not, on its own, allow
    #: modifying resources that already exist.
    CREATE = "create"

    #: Overwrite or truncate an existing file, or append to one.
    OVERWRITE = "overwrite"

    #: Remove files or directories (``remove``, ``removedir``,
    #: ``removetree``, ``move`` / ``movedir`` source side).
    DELETE = "delete"

    #: Alter metadata of an existing resource (``setinfo``,
    #: ``settimes``, ``touch`` on an existing resource).
    METADATA = "metadata"


ALL_CAPABILITIES = frozenset(
    {
        Capability.READ,
        Capability.LIST,
        Capability.CREATE,
        Capability.OVERWRITE,
        Capability.DELETE,
        Capability.METADATA,
    }
)

#: Capabilities that grant every operation.
FULL_CAPABILITIES = ALL_CAPABILITIES

#: Capabilities that only permit reading existing data.
READ_ONLY_CAPABILITIES = frozenset({Capability.READ, Capability.LIST})

#: Capabilities that permit creating files but never destroying or
#: replacing existing data.
CREATE_ONLY_CAPABILITIES = frozenset(
    {Capability.READ, Capability.LIST, Capability.CREATE}
)


class CapabilityFS(WrapFS["FS"]):
    """A filesystem wrapper enforcing a capability set.

    Arguments:
        wrap_fs (FS): The wrapped filesystem.  The wrapper never takes
            ownership of it unless ``close_wrap`` is `True`.
        capabilities (collections.abc.Iterable[str]): Allowed capability
            tokens (see `Capability`).
        close_wrap (bool): If `True`, closing the wrapper (including via
            the context manager) also closes the wrapped filesystem.
            Defaults to `False`, since the underlying object is owned by
            the caller.

    Any public operation requiring a capability that is not part of the
    granted set raises `~fs.errors.PermissionDenied` *before* the
    wrapped filesystem is touched, so a denied call can never perform a
    partial write.
    """

    wrap_name = "capabilities"

    # Paths, as seen by this wrapper layer, are always rooted here; a
    # :class:`CapabilitySubFS` overrides this with its own directory.
    _sub_dir = "/"  # type: Text
    # Class level defaults keep ``__del__`` safe even if ``__init__``
    # raises before the instance attributes are assigned.
    _capabilities = frozenset()  # type: FrozenSet[Text]
    _close_wrap = False

    def __init__(self, wrap_fs, capabilities=ALL_CAPABILITIES, close_wrap=False):
        # type: (FS, Iterable[Text], bool) -> None
        super(CapabilityFS, self).__init__(wrap_fs)
        self._set_capabilities(capabilities)
        self._close_wrap = bool(close_wrap)

    def _set_capabilities(self, capabilities):
        # type: (Iterable[Text]) -> None
        caps = frozenset(capabilities)
        unknown = caps - ALL_CAPABILITIES
        if unknown:
            raise ValueError(
                "unknown capabilities: {}".format(", ".join(sorted(unknown)))
            )
        self._capabilities = caps

    # ------------------------------------------------------------------ #
    # Capability introspection
    # ------------------------------------------------------------------ #

    @property
    def capabilities(self):
        # type: () -> FrozenSet[Text]
        """`frozenset`: the capabilities granted to this wrapper."""
        return self._capabilities

    def has_capability(self, capability):
        # type: (Text) -> bool
        """Check if a single capability is granted."""
        return capability in self._capabilities

    def restrict(self, capabilities):
        # type: (_C, Iterable[Text]) -> _C
        """Return a view of this wrapper with a *narrower* capability set.

        The returned wrapper shares the same root, so ``READ`` on the
        narrowed view still covers the whole tree; it can never gain a
        capability this wrapper does not already hold.  Use
        `opendir` (optionally with ``capabilities=``) to additionally
        confine paths to a sub-directory.
        """
        narrowed = self._capabilities.intersection(capabilities)
        return type(self)(self, narrowed, close_wrap=False)

    # ------------------------------------------------------------------ #
    # Path confinement
    # ------------------------------------------------------------------ #

    def delegate_path(self, path):
        # type: (Text) -> Tuple[FS, Text]
        _path = self._confine(path)
        return self._wrap_fs, _path

    def _confine(self, path):
        # type: (Text) -> Text
        """Normalise ``path`` and ensure it stays inside this root.

        Raises:
            fs.errors.IllegalBackReference: if the path resolves outside
                of the wrapper root.
        """
        self.check()
        absolute = abspath(normpath(path))
        if self._sub_dir != "/":
            relative = relpath(absolute)
            if relative == ".." or relative.startswith("../"):
                raise IllegalBackReference(path)
        return join(self._sub_dir, relpath(absolute))

    def validatepath(self, path):
        # type: (Text) -> Text
        self.check()
        raw_path = self._confine(path)
        # Let the wrapped layer validate too (invalid characters,
        # platform path length limits, ...).
        self._wrap_fs.validatepath(raw_path)
        return abspath(normpath(path))

    # ------------------------------------------------------------------ #
    # Enforcement helpers
    # ------------------------------------------------------------------ #

    def _deny(self, path, required):
        # type: (Text, Collection[Text]) -> PermissionDenied
        missing = sorted(set(required) - self._capabilities)
        msg = (
            "permission denied: {path} requires {required} "
            "(missing {missing})"
        ).format(
            path=path,
            required=",".join(sorted(required)),
            missing=",".join(missing),
        )
        return PermissionDenied(path=path, msg=msg)

    def _require(self, path, *required):
        # type: (Text, Text) -> Text
        """Check capabilities and path confinement before delegating.

        Returns the raw path on the wrapped filesystem.
        """
        missing = set(required) - self._capabilities
        if missing:
            raise self._deny(path, required)
        return self._confine(path)

    def _probe_exists(self, raw_path):
        # type: (Text) -> bool
        """Existence probe on the wrapped layer *without* capability checks.

        Used internally to decide between the ``CREATE`` and
        ``OVERWRITE`` requirements.  This never reads file contents and
        never mutates anything.
        """
        target_fs, target_path = self._unwrap(raw_path)
        with unwrap_errors(target_path):
            return target_fs.exists(target_path)

    def _unwrap(self, raw_path):
        # type: (Text) -> Tuple[FS, Text]
        """Resolve ``raw_path`` (a path on the wrapped layer) all the
        way down to the first non-capability filesystem.

        Each intermediate wrapper translates the path through its own
        ``delegate_path``, so nested sub-filesystems probe the correct
        physical resource.
        """
        layer = self
        probe_path = raw_path
        while isinstance(layer._wrap_fs, CapabilityFS):
            parent = layer._wrap_fs
            probe_path = parent.delegate_path(probe_path)[1]
            layer = parent
        return layer._wrap_fs, probe_path

    def _target_caps(self, raw_path, require_overwrite):
        # type: (Text, bool) -> Tuple[Text, ...]
        """Capabilities needed to create or replace ``raw_path``."""
        if require_overwrite or self._probe_exists(raw_path):
            return (Capability.CREATE, Capability.OVERWRITE)
        return (Capability.CREATE,)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def close(self):
        # type: () -> None
        if not self.isclosed():
            try:
                if self._close_wrap:
                    self._wrap_fs.close()
            finally:
                super(CapabilityFS, self).close()

    # ------------------------------------------------------------------ #
    # Primitive mutating operations
    # ------------------------------------------------------------------ #

    def openbin(self, path, mode="r", buffering=-1, **options):
        # type: (Text, Text, int, **Any) -> BinaryIO
        parsed = Mode(mode)
        required = set()  # type: set
        if parsed.reading:
            required.add(Capability.READ)
        if parsed.writing:
            required.add(Capability.CREATE)
            raw_path = self._confine(path)
            exists = self._probe_exists(raw_path)
            if exists and (parsed.appending or not parsed.exclusive):
                # Writing to a resource that already exists modifies it:
                # 'a' extends it, 'r'/'w' truncate or mutate it.  'x'
                # never touches an existing resource (the wrapped layer
                # raises FileExists), so OVERWRITE is not needed there.
                # Appending to a *missing* file is plain creation.
                required.add(Capability.OVERWRITE)
            raw_path = self._require(path, *required)
        else:
            raw_path = self._require(path, *required)
        with unwrap_errors(path):
            return self._wrap_fs.openbin(
                raw_path, mode=mode, buffering=buffering, **options
            )

    def makedir(self, path, permissions=None, recreate=False):
        # type: (Text, Optional[Permissions], bool) -> SubFS
        raw_path = self._require(path, Capability.CREATE)
        with unwrap_errors(path):
            self._wrap_fs.makedir(
                raw_path, permissions=permissions, recreate=recreate
            )
        absolute = abspath(normpath(path))
        return CapabilitySubFS(self, absolute, self._capabilities, close_wrap=False)

    def remove(self, path):
        # type: (Text) -> None
        raw_path = self._require(path, Capability.DELETE)
        with unwrap_errors(path):
            self._wrap_fs.remove(raw_path)

    def removedir(self, path):
        # type: (Text) -> None
        raw_path = self._require(path, Capability.DELETE)
        absolute = abspath(normpath(path))
        if absolute == "/" and self._sub_dir == "/":
            raise RemoveRootError()
        with unwrap_errors(path):
            self._wrap_fs.removedir(raw_path)

    def setinfo(self, path, info):
        # type: (Text, RawInfo) -> None
        raw_path = self._require(path, Capability.METADATA)
        with unwrap_errors(path):
            self._wrap_fs.setinfo(raw_path, info)

    # ------------------------------------------------------------------ #
    # Primitive reading operations
    # ------------------------------------------------------------------ #

    def getinfo(self, path, namespaces=None):
        # type: (Text, Optional[Collection[Text]]) -> Info
        raw_path = self._require(path, Capability.READ)
        with unwrap_errors(path):
            raw_info = self._wrap_fs.getinfo(
                raw_path, namespaces=namespaces
            ).raw
        if abspath(normpath(path)) == "/":
            raw_info = dict(raw_info)
            raw_info["basic"]["name"] = ""
        from .info import Info

        return Info(raw_info)

    def listdir(self, path):
        # type: (Text) -> list
        raw_path = self._require(path, Capability.LIST)
        with unwrap_errors(path):
            return self._wrap_fs.listdir(raw_path)

    def scandir(self, path, namespaces=None, page=None):
        # type: (Text, Optional[Collection[Text]], Optional[Tuple[int, int]])
        # type: (...) -> Iterator[Info]
        raw_path = self._require(path, Capability.LIST)
        with unwrap_errors(path):
            for info in self._wrap_fs.scandir(
                raw_path, namespaces=namespaces, page=page
            ):
                yield info

    def filterdir(
        self,
        path,
        files=None,
        dirs=None,
        exclude_dirs=None,
        exclude_files=None,
        namespaces=None,
        page=None,
    ):
        raw_path = self._require(path, Capability.LIST)
        with unwrap_errors(path):
            for info in self._wrap_fs.filterdir(
                raw_path,
                files=files,
                dirs=dirs,
                exclude_dirs=exclude_dirs,
                exclude_files=exclude_files,
                namespaces=namespaces,
                page=page,
            ):
                yield info

    # ------------------------------------------------------------------ #
    # High level data operations (gated on the primitive ones, but
    # implemented explicitly so the exact capability requirements are
    # visible before any delegation happens).
    # ------------------------------------------------------------------ #

    def appendbytes(self, path, data):
        # type: (Text, bytes) -> None
        if not isinstance(data, bytes):
            raise TypeError("must be bytes")
        raw_path = self._confine(path)
        self._require(
            path, *self._target_caps(raw_path, self._probe_exists(raw_path))
        )
        with unwrap_errors(path):
            self._wrap_fs.appendbytes(raw_path, data)

    def appendtext(self, path, text, encoding="utf-8", errors=None, newline=""):
        # type: (Text, Text, Text, Optional[Text], Text) -> None
        raw_path = self._confine(path)
        self._require(
            path, *self._target_caps(raw_path, self._probe_exists(raw_path))
        )
        with unwrap_errors(path):
            self._wrap_fs.appendtext(
                raw_path,
                text,
                encoding=encoding,
                errors=errors,
                newline=newline,
            )

    def create(self, path, wipe=False):
        # type: (Text, bool) -> bool
        raw_path = self._confine(path)
        exists = self._probe_exists(raw_path)
        if not wipe and exists:
            return False
        self._require(path, *self._target_caps(raw_path, wipe and exists))
        with unwrap_errors(path):
            return self._wrap_fs.create(raw_path, wipe=wipe)

    def touch(self, path):
        # type: (Text) -> None
        raw_path = self._confine(path)
        exists = self._probe_exists(raw_path)
        if exists:
            self._require(path, Capability.METADATA)
        else:
            self._require(path, Capability.CREATE)
        with unwrap_errors(path):
            self._wrap_fs.touch(raw_path)

    def settimes(self, path, accessed=None, modified=None):
        # type: (Text, Optional[Any], Optional[Any]) -> None
        raw_path = self._require(path, Capability.METADATA)
        with unwrap_errors(path):
            self._wrap_fs.settimes(raw_path, accessed=accessed, modified=modified)

    def readbytes(self, path):
        # type: (Text) -> bytes
        raw_path = self._require(path, Capability.READ)
        with unwrap_errors(path):
            return self._wrap_fs.readbytes(raw_path)

    def readtext(self, path, encoding=None, errors=None, newline=""):
        # type: (Text, Optional[Text], Optional[Text], Text) -> Text
        raw_path = self._require(path, Capability.READ)
        with unwrap_errors(path):
            return self._wrap_fs.readtext(
                raw_path, encoding=encoding, errors=errors, newline=newline
            )

    def download(self, path, file, chunk_size=None, **options):
        # type: (Text, BinaryIO, Optional[int], **Any) -> None
        raw_path = self._require(path, Capability.READ)
        with unwrap_errors(path):
            self._wrap_fs.download(raw_path, file, chunk_size=chunk_size, **options)

    def writebytes(self, path, contents):
        # type: (Text, bytes) -> None
        if not isinstance(contents, bytes):
            raise TypeError("contents must be bytes")
        raw_path = self._confine(path)
        self._require(path, *self._target_caps(raw_path, self._probe_exists(raw_path)))
        with unwrap_errors(path):
            self._wrap_fs.writebytes(raw_path, contents)

    def writetext(self, path, contents, encoding="utf-8", errors=None, newline=""):
        # type: (Text, Text, Text, Optional[Text], Text) -> None
        raw_path = self._confine(path)
        self._require(path, *self._target_caps(raw_path, self._probe_exists(raw_path)))
        with unwrap_errors(path):
            self._wrap_fs.writetext(
                raw_path,
                contents,
                encoding=encoding,
                errors=errors,
                newline=newline,
            )

    def upload(self, path, file, chunk_size=None, **options):
        # type: (Text, BinaryIO, Optional[int], **Any) -> None
        raw_path = self._confine(path)
        self._require(path, *self._target_caps(raw_path, self._probe_exists(raw_path)))
        with unwrap_errors(path):
            self._wrap_fs.upload(raw_path, file, chunk_size=chunk_size, **options)

    def writefile(self, path, file, encoding=None, errors=None, newline=""):
        # type: (Text, IO[AnyStr], Optional[Text], Optional[Text], Text) -> None
        raw_path = self._confine(path)
        self._require(path, *self._target_caps(raw_path, self._probe_exists(raw_path)))
        with unwrap_errors(path):
            self._wrap_fs.writefile(
                raw_path,
                file,
                encoding=encoding,
                errors=errors,
                newline=newline,
            )

    # ------------------------------------------------------------------ #
    # Copy / move
    # ------------------------------------------------------------------ #

    def copy(self, src_path, dst_path, overwrite=False, preserve_time=False):
        # type: (Text, Text, bool, bool) -> None
        raw_src = self._require(src_path, Capability.READ)
        raw_dst = self._confine(dst_path)
        dst_exists = self._probe_exists(raw_dst)
        if dst_exists and not overwrite:
            raise DestinationExists(dst_path)
        self._require(dst_path, *self._target_caps(raw_dst, dst_exists))
        if raw_src == raw_dst:
            from .errors import IllegalDestination

            raise IllegalDestination(dst_path)
        if preserve_time:
            self._require(dst_path, Capability.METADATA)
        with self._lock:
            with unwrap_errors({raw_src: src_path, raw_dst: dst_path}):
                with self._wrap_fs.openbin(raw_src, "rb") as read_file:
                    self._wrap_fs.upload(raw_dst, read_file)
                if preserve_time:
                    copy_modified_time(
                        self._wrap_fs,
                        raw_src,
                        self._wrap_fs,
                        raw_dst,
                    )

    def copydir(self, src_path, dst_path, create=False, preserve_time=False):
        # type: (Text, Text, bool, bool) -> None
        raw_src = self._require(src_path, Capability.READ, Capability.LIST)
        raw_dst = self._confine(dst_path)
        if isbase(raw_src, raw_dst):
            from .errors import IllegalDestination

            raise IllegalDestination(dst_path)
        dst_exists = self._probe_exists(raw_dst)
        if not create and not dst_exists:
            from .errors import ResourceNotFound

            raise ResourceNotFound(dst_path)
        self._require(
            dst_path,
            Capability.LIST,
            *self._target_caps(raw_dst, dst_exists)
        )
        if preserve_time:
            self._require(dst_path, Capability.METADATA)
        from .copy import copy_dir

        with unwrap_errors({raw_src: src_path, raw_dst: dst_path}):
            copy_dir(
                self._wrap_fs,
                raw_src,
                self._wrap_fs,
                raw_dst,
                preserve_time=preserve_time,
            )

    def move(self, src_path, dst_path, overwrite=False, preserve_time=False):
        # type: (Text, Text, bool, bool) -> None
        raw_src = self._require(src_path, Capability.READ, Capability.DELETE)
        raw_dst = self._confine(dst_path)
        dst_exists = self._probe_exists(raw_dst)
        if dst_exists and not overwrite:
            raise DestinationExists(dst_path)
        self._require(dst_path, *self._target_caps(raw_dst, dst_exists))
        if raw_src == raw_dst:
            return
        if preserve_time:
            self._require(dst_path, Capability.METADATA)
        with self._lock:
            with unwrap_errors({raw_src: src_path, raw_dst: dst_path}):
                # Bypass any rename fast path (which would skip
                # enforcement) and use the checked copy + remove path.
                with self._wrap_fs.openbin(raw_src, "rb") as read_file:
                    self._wrap_fs.upload(raw_dst, read_file)
                if preserve_time:
                    copy_modified_time(
                        self._wrap_fs,
                        raw_src,
                        self._wrap_fs,
                        raw_dst,
                    )
                self._wrap_fs.remove(raw_src)

    def movedir(self, src_path, dst_path, create=False, preserve_time=False):
        # type: (Text, Text, bool, bool) -> None
        raw_src = self._require(
            src_path, Capability.READ, Capability.LIST, Capability.DELETE
        )
        raw_dst = self._confine(dst_path)
        if raw_src == raw_dst:
            return
        if isbase(raw_src, raw_dst):
            from .errors import IllegalDestination

            raise IllegalDestination(dst_path)
        dst_exists = self._probe_exists(raw_dst)
        if not create and not dst_exists:
            from .errors import ResourceNotFound

            raise ResourceNotFound(dst_path)
        self._require(
            dst_path,
            Capability.LIST,
            *self._target_caps(raw_dst, dst_exists)
        )
        if preserve_time:
            self._require(dst_path, Capability.METADATA)
        from .move import move_dir

        with unwrap_errors({raw_src: src_path, raw_dst: dst_path}):
            move_dir(
                self._wrap_fs,
                raw_src,
                self._wrap_fs,
                raw_dst,
                preserve_time=preserve_time,
            )

    # ------------------------------------------------------------------ #
    # Trees and directories
    # ------------------------------------------------------------------ #

    def makedirs(self, path, permissions=None, recreate=False):
        # type: (Text, Optional[Permissions], bool) -> SubFS
        raw_path = self._require(path, Capability.CREATE)
        with unwrap_errors(path):
            self._wrap_fs.makedirs(
                raw_path, permissions=permissions, recreate=recreate
            )
        absolute = abspath(normpath(path))
        return CapabilitySubFS(self, absolute, self._capabilities, close_wrap=False)

    def removetree(self, dir_path):
        # type: (Text) -> None
        raw_path = self._require(dir_path, Capability.DELETE, Capability.LIST)
        absolute = abspath(normpath(dir_path))
        if absolute == "/":
            from .path import join as _join

            with unwrap_errors(dir_path):
                for info in self._wrap_fs.scandir(raw_path):
                    child = _join(raw_path, info.name)
                    if info.is_dir:
                        self._wrap_fs.removetree(child)
                    else:
                        self._wrap_fs.remove(child)
        else:
            with unwrap_errors(dir_path):
                self._wrap_fs.removetree(raw_path)

    def opendir(self, path, factory=None, capabilities=None):
        # type: (Text, object, Optional[Iterable[Text]]) -> CapabilitySubFS
        """Open a sub-directory, with *narrowed* capabilities.

        Arguments:
            path (str): Path to a directory on the filesystem.
            factory (callable, optional): Factory for the returned
                object.  When omitted, a `CapabilitySubFS` is returned.
            capabilities (iterable[str], optional): Additional
                capability restriction.  The resulting sub-filesystem
                holds the *intersection* of these capabilities with the
                parent capabilities, so a denied capability can never be
                restored.  Defaults to the parent capabilities.
        """
        raw_path = self._require(path, Capability.READ)
        absolute = abspath(normpath(path))
        if not self._probe_exists(self._confine(path)):
            from .errors import ResourceNotFound

            raise ResourceNotFound(path)
        # A directory check is a metadata read; reuse the READ grant.
        with unwrap_errors(path):
            if not self._wrap_fs.getinfo(raw_path).is_dir:
                from .errors import DirectoryExpected

                raise DirectoryExpected(path=path)
        granted = (
            self._capabilities
            if capabilities is None
            else self._capabilities.intersection(capabilities)
        )
        if factory is None:
            return CapabilitySubFS(self, absolute, granted, close_wrap=False)
        opened = factory(self, absolute)
        from .subfs import ClosingSubFS

        if isinstance(opened, CapabilitySubFS) or isinstance(opened, ClosingSubFS):
            # Respect a factory that explicitly takes over closing
            # (ClosingSubFS) or already carries capability state.
            return opened
        # A custom factory may return an arbitrary SubFS; wrap it so
        # capabilities can never be wider than this layer's.
        return CapabilityFS(opened, granted, close_wrap=False)

    # ------------------------------------------------------------------ #
    # Metadata-only reads (allowed for anyone; never mutate)
    # ------------------------------------------------------------------ #

    def exists(self, path):
        # type: (Text) -> bool
        raw_path = self._require(path, Capability.READ)
        with unwrap_errors(path):
            return self._wrap_fs.exists(raw_path)

    def getsize(self, path):
        # type: (Text) -> int
        raw_path = self._require(path, Capability.READ)
        with unwrap_errors(path):
            return self._wrap_fs.getsize(raw_path)

    def gettype(self, path):
        # type: (Text) -> object
        raw_path = self._require(path, Capability.READ)
        with unwrap_errors(path):
            return self._wrap_fs.gettype(raw_path)

    def isdir(self, path):
        # type: (Text) -> bool
        raw_path = self._require(path, Capability.READ)
        with unwrap_errors(path):
            return self._wrap_fs.isdir(raw_path)

    def isfile(self, path):
        # type: (Text) -> bool
        raw_path = self._require(path, Capability.READ)
        with unwrap_errors(path):
            return self._wrap_fs.isfile(raw_path)

    def islink(self, path):
        # type: (Text) -> bool
        raw_path = self._require(path, Capability.READ)
        with unwrap_errors(path):
            return self._wrap_fs.islink(raw_path)

    def isempty(self, path):
        # type: (Text) -> bool
        raw_path = self._require(path, Capability.READ, Capability.LIST)
        with unwrap_errors(path):
            return self._wrap_fs.isempty(raw_path)

    def getsyspath(self, path):
        # type: (Text) -> Text
        raw_path = self._require(path, Capability.READ)
        with unwrap_errors(path):
            return self._wrap_fs.getsyspath(raw_path)

    def hassyspath(self, path):
        # type: (Text) -> bool
        raw_path = self._require(path, Capability.READ)
        with unwrap_errors(path):
            return self._wrap_fs.hassyspath(raw_path)

    def geturl(self, path, purpose="download"):
        # type: (Text, Text) -> Text
        raw_path = self._require(path, Capability.READ)
        with unwrap_errors(path):
            return self._wrap_fs.geturl(raw_path, purpose=purpose)

    def hasurl(self, path, purpose="download"):
        # type: (Text, Text) -> bool
        raw_path = self._require(path, Capability.READ)
        with unwrap_errors(path):
            return self._wrap_fs.hasurl(raw_path, purpose=purpose)

    def desc(self, path):
        # type: (Text) -> Text
        raw_path = self._require(path, Capability.READ)
        with unwrap_errors(path):
            return self._wrap_fs.desc(raw_path)

    def hash(self, path, name):
        # type: (Text, Text) -> Text
        raw_path = self._require(path, Capability.READ)
        with unwrap_errors(path):
            return self._wrap_fs.hash(raw_path, name)

    def getmeta(self, namespace="standard"):
        # type: (Text) -> Mapping[Text, object]
        self.check()
        meta = dict(self._wrap_fs.getmeta(namespace=namespace))
        if namespace == "standard":
            # A capability wrapper without any of the mutating
            # capabilities is read-only, and we never delegate a native
            # rename (moves go through the checked copy+remove path).
            write_caps = (
                Capability.CREATE,
                Capability.OVERWRITE,
                Capability.DELETE,
                Capability.METADATA,
            )
            meta["read_only"] = not any(
                cap in self._capabilities for cap in write_caps
            )
            meta["supports_rename"] = False
        return meta


class CapabilitySubFS(CapabilityFS, SubFS):
    """A sub-directory view with capabilities no wider than its parent."""

    def __init__(
        self, parent_fs, path, capabilities=ALL_CAPABILITIES, close_wrap=False
    ):
        # type: (CapabilityFS, Text, Iterable[Text], bool) -> None
        # SubFS.__init__ calls WrapFS.__init__ directly, then set up the
        # capability state ourselves.
        SubFS.__init__(self, parent_fs, path)
        # ``SubFS.__init__`` records ``path`` relative to this layer, but
        # delegate_path joins it onto the parent's raw root; store the
        # already-resolved absolute path so nested sub-filesystems do
        # not stack prefixes.
        self._sub_dir = path
        narrowed = parent_fs.capabilities.intersection(capabilities)
        self._set_capabilities(narrowed)
        self._close_wrap = bool(close_wrap)

    def opendir(self, path, factory=None, capabilities=None):
        # type: (Text, object, Optional[Iterable[Text]]) -> CapabilitySubFS
        return CapabilityFS.opendir(
            self, path, factory=factory, capabilities=capabilities
        )


__all__ = [
    "ALL_CAPABILITIES",
    "Capability",
    "CapabilityFS",
    "CapabilitySubFS",
    "CREATE_ONLY_CAPABILITIES",
    "FULL_CAPABILITIES",
    "READ_ONLY_CAPABILITIES",
]
