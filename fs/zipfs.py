"""Manage the filesystem in a Zip archive.
"""

from __future__ import print_function, unicode_literals

import sys
import typing

from collections import OrderedDict

import six
import zipfile
from datetime import datetime

from . import errors
from ._url_tools import url_quote
from .base import FS
from .compress import write_zip
from .enums import ResourceType, Seek
from .info import Info
from .iotools import RawWrapper
from .opener import open_fs
from .path import basename, dirname, normpath, relpath
from .permissions import Permissions
from .time import datetime_to_epoch
from .wrapfs import WrapFS

if typing.TYPE_CHECKING:
    from typing import (
        Any,
        BinaryIO,
        Collection,
        Dict,
        Iterable,
        List,
        Optional,
        Set,
        SupportsInt,
        Text,
        Tuple,
        Union,
    )

    from .info import RawInfo
    from .subfs import SubFS

    R = typing.TypeVar("R", bound="ReadZipFS")


class ZipEntry(object):
    """A specific entry in the central directory of a ZIP archive.

    Unlike a filesystem path, an entry is identified by its position in
    the central directory. It therefore remains unambiguous when an
    archive contains multiple members with the same name.
    """

    __slots__ = [
        "_index",
        "_name",
        "_path",
        "_is_dir",
        "_size",
        "_compressed_size",
        "_crc",
        "_compress_type",
        "_date_time",
        "_raw_info",
        "_issues",
    ]

    def __init__(
        self,
        index,  # type: int
        name,  # type: Text
        path,  # type: Optional[Text]
        is_dir,  # type: bool
        zip_info,  # type: zipfile.ZipInfo
        issues=(),  # type: Iterable[Text]
    ):
        # type: (...) -> None
        self._index = index
        self._name = name
        self._path = path
        self._is_dir = is_dir
        self._size = zip_info.file_size
        self._compressed_size = zip_info.compress_size
        self._crc = zip_info.CRC
        self._compress_type = zip_info.compress_type
        self._date_time = tuple(zip_info.date_time)
        self._raw_info = {
            key: self._copy_value(getattr(zip_info, key))
            for key in zip_info.__slots__
            if not key.startswith("_")
        }
        self._issues = tuple(issues)

    @staticmethod
    def _copy_value(value):
        # type: (object) -> object
        if isinstance(value, tuple):
            return tuple(value)
        return value

    def __repr__(self):
        # type: () -> Text
        kind = "directory" if self._is_dir else "file"
        return "ZipEntry({}, {!r}, {})".format(self._index, self._name, kind)

    def __eq__(self, other):
        # type: (object) -> bool
        if not isinstance(other, ZipEntry):
            return NotImplemented
        return self._index == other._index

    def __ne__(self, other):
        # type: (object) -> bool
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        # type: () -> int
        return hash(self._index)

    @property
    def index(self):
        # type: () -> int
        """`int`: the entry's central-directory position."""
        return self._index

    @property
    def name(self):
        # type: () -> Text
        """`str`: the member name as stored in the archive."""
        return self._name

    @property
    def filename(self):
        # type: () -> Text
        """`str`: alias of `name`, matching `zipfile.ZipInfo`."""
        return self._name

    @property
    def path(self):
        # type: () -> Optional[Text]
        """The normalized filesystem path, or `None` for unsafe entries."""
        return self._path

    @property
    def is_dir(self):
        # type: () -> bool
        """`bool`: whether the entry is a directory member."""
        return self._is_dir

    def isdir(self):
        # type: () -> bool
        """Return whether the entry is a directory."""
        return self._is_dir

    @property
    def size(self):
        # type: () -> int
        """`int`: uncompressed size in bytes."""
        return self._size

    @property
    def compressed_size(self):
        # type: () -> int
        """`int`: compressed size in bytes."""
        return self._compressed_size

    @property
    def header_offset(self):
        # type: () -> int
        """`int`: the local file header offset in the archive."""
        return typing.cast(int, self._raw_info["header_offset"])

    @property
    def crc(self):
        # type: () -> int
        """`int`: the CRC-32 checksum recorded in the archive."""
        return self._crc

    CRC = crc

    file_size = size
    compress_size = compressed_size

    @property
    def compress_type(self):
        # type: () -> int
        """`int`: the ZIP compression method identifier."""
        return self._compress_type

    @property
    def date_time(self):
        # type: () -> Tuple[int, int, int, int, int, int]
        """tuple: the modification timestamp recorded in the archive."""
        return typing.cast(
            "Tuple[int, int, int, int, int, int]", self._date_time
        )

    @property
    def raw_info(self):
        # type: () -> Dict[Text, object]
        """`dict`: a snapshot of the standard library's raw ZIP metadata."""
        return dict(self._raw_info)

    @property
    def issues(self):
        # type: () -> Tuple[Text, ...]
        """`tuple`: diagnostic issues for this archive entry."""
        return self._issues


ZipMember = ZipEntry


class _ZipExtFile(RawWrapper):
    def __init__(self, fs, zip_info):  # noqa: D107
        # type: (ReadZipFS, zipfile.ZipInfo) -> None
        self._zip_info = zip_info
        self._zip = _zip = fs._zip
        self._end = zip_info.file_size
        self._pos = 0
        super(_ZipExtFile, self).__init__(
            _zip.open(zip_info), "r", zip_info.filename
        )

    # NOTE(@althonos): Starting from Python 3.7, files inside a Zip archive are
    #                  seekable provided they were opened from a seekable file
    #                  handle. Before that, we can emulate a seek using the
    #                  read method, although it adds a ton of overhead and is
    #                  way less efficient than extracting once to a BytesIO.
    if sys.version_info < (3, 7):

        def read(self, size=-1):
            # type: (int) -> bytes
            buf = self._f.read(-1 if size is None else size)
            self._pos += len(buf)
            return buf

        def read1(self, size=-1):
            # type: (int) -> bytes
            buf = self._f.read1(-1 if size is None else size)  # type: ignore
            self._pos += len(buf)
            return buf

        def tell(self):
            # type: () -> int
            return self._pos

        def seekable(self):
            return True

        def seek(self, offset, whence=Seek.set):
            # type: (int, SupportsInt) -> int
            """Change stream position.

            Change the stream position to the given byte offset. The
            offset is interpreted relative to the position indicated by
            ``whence``.

            Arguments:
                offset (int): the offset to the new position, in bytes.
                whence (int): the position reference. Possible values are:
                    * `Seek.set`: start of stream (the default).
                    * `Seek.current`: current position; offset may be negative.
                    * `Seek.end`: end of stream; offset must be negative.

            Returns:
                int: the new absolute position.

            Raises:
                ValueError: when ``whence`` is not known, or ``offset``
                    is invalid.

            Note:
                Zip compression does not support seeking, so the seeking
                is emulated. Seeking somewhere else than the current position
                will need to either:
                    * reopen the file and restart decompression
                    * read and discard data to advance in the file

            """
            _whence = int(whence)
            if _whence == Seek.current:
                offset += self._pos
            if _whence == Seek.current or _whence == Seek.set:
                if offset < 0:
                    raise ValueError("Negative seek position {}".format(offset))
            elif _whence == Seek.end:
                if offset > 0:
                    raise ValueError("Positive seek position {}".format(offset))
                offset += self._end
            else:
                raise ValueError(
                    "Invalid whence ({}, should be {}, {} or {})".format(
                        _whence, Seek.set, Seek.current, Seek.end
                    )
                )

            if offset < self._pos:
                self._f = self._zip.open(self._zip_info)  # type: ignore
                self._pos = 0
            self.read(offset - self._pos)
            return self._pos

    else:

        def seek(self, offset, whence=Seek.set):
            # type: (int, SupportsInt) -> int
            """Change stream position.

            Change the stream position to the given byte offset. The
            offset is interpreted relative to the position indicated by
            ``whence``.

            Arguments:
                offset (int): the offset to the new position, in bytes.
                whence (int): the position reference. Possible values are:
                    * `Seek.set`: start of stream (the default).
                    * `Seek.current`: current position; offset may be negative.
                    * `Seek.end`: end of stream; offset must be negative.

            Returns:
                int: the new absolute position.

            Raises:
                ValueError: when ``whence`` is not known, or ``offset``
                    is invalid.

            """
            _whence = int(whence)
            _pos = self.tell()
            if _whence == Seek.set:
                if offset < 0:
                    raise ValueError("Negative seek position {}".format(offset))
            elif _whence == Seek.current:
                if _pos + offset < 0:
                    raise ValueError("Negative seek position {}".format(offset))
            elif _whence == Seek.end:
                if offset > 0:
                    raise ValueError("Positive seek position {}".format(offset))
            else:
                raise ValueError(
                    "Invalid whence ({}, should be {}, {} or {})".format(
                        _whence, Seek.set, Seek.current, Seek.end
                    )
                )

            return self._f.seek(offset, _whence)


class ZipFS(WrapFS):
    """Read and write zip files.

    There are two ways to open a `ZipFS` for the use cases of reading
    a zip file, and creating a new one.

    If you open the `ZipFS` with  ``write`` set to `False` (the default)
    then the filesystem will be a read-only filesystem which maps to
    the files and directories within the zip file. Files are
    decompressed on the fly when you open them.

    Here's how you might extract and print a readme from a zip file::

        with ZipFS('foo.zip') as zip_fs:
            readme = zip_fs.readtext('readme.txt')

    If you open the `ZipFS` with ``write`` set to `True`, then the `ZipFS`
    will be an empty temporary filesystem. Any files / directories you
    create in the `ZipFS` will be written in to a zip file when the `ZipFS`
    is closed.

    Here's how you might write a new zip file containing a ``readme.txt``
    file::

        with ZipFS('foo.zip', write=True) as new_zip:
            new_zip.writetext(
                'readme.txt',
                'This zip file was written by PyFilesystem'
            )


    Arguments:
        file (str or io.IOBase): An OS filename, or an open file object.
        write (bool): Set to `True` to write a new zip file, or `False`
            (default) to read an existing zip file.
        compression (int): Compression to use (one of the constants
            defined in the `zipfile` module in the stdlib).
        duplicate (str): Strategy used when a filesystem path refers to
            more than one ZIP member. Use ``"last"`` (default) or
            ``"first"`` to select a member, or ``"error"`` to reject
            ambiguous paths. Members can always be opened individually by
            their central-directory entry identity.
        temp_fs (str or FS): An FS URL or an FS instance to use to
            store data prior to zipping. Defaults to creating a new
            `~fs.tempfs.TempFS`.

    """

    # TODO: __new__ returning different types may be too 'magical'
    def __new__(  # type: ignore
        cls,
        file,  # type: Union[Text, BinaryIO]
        write=False,  # type: bool
        compression=zipfile.ZIP_DEFLATED,  # type: int
        encoding="utf-8",  # type: Text
        temp_fs="temp://__ziptemp__",  # type: Union[Text, FS]
        duplicate="last",  # type: Text
        duplicate_policy=None,  # type: Optional[Text]
    ):
        # type: (...) -> FS
        duplicate = duplicate_policy or duplicate
        # This magic returns a different class instance based on the
        # value of the ``write`` parameter.
        if write:
            return WriteZipFS(
                file, compression=compression, encoding=encoding, temp_fs=temp_fs
            )
        else:
            return ReadZipFS(file, encoding=encoding, duplicate=duplicate)

    if typing.TYPE_CHECKING:

        def __init__(
            self,
            file,  # type: Union[Text, BinaryIO]
            write=False,  # type: bool
            compression=zipfile.ZIP_DEFLATED,  # type: int
            encoding="utf-8",  # type: Text
            temp_fs="temp://__ziptemp__",  # type: Text
            duplicate="last",  # type: Text
            duplicate_policy=None,  # type: Optional[Text]
        ):  # noqa: D107
            # type: (...) -> None
            pass


@six.python_2_unicode_compatible
class WriteZipFS(WrapFS):
    """A writable zip file."""

    def __init__(
        self,
        file,  # type: Union[Text, BinaryIO]
        compression=zipfile.ZIP_DEFLATED,  # type: int
        encoding="utf-8",  # type: Text
        temp_fs="temp://__ziptemp__",  # type: Union[Text, FS]
    ):  # noqa: D107
        # type: (...) -> None
        self._file = file
        self.compression = compression
        self.encoding = encoding
        self._temp_fs_url = temp_fs
        self._temp_fs = open_fs(temp_fs)
        self._meta = dict(self._temp_fs.getmeta())  # type: ignore
        super(WriteZipFS, self).__init__(self._temp_fs)

    def __repr__(self):
        # type: () -> Text
        t = "WriteZipFS({!r}, compression={!r}, encoding={!r}, temp_fs={!r})"
        return t.format(self._file, self.compression, self.encoding, self._temp_fs_url)

    def __str__(self):
        # type: () -> Text
        return "<zipfs-write '{}'>".format(self._file)

    def delegate_path(self, path):
        # type: (Text) -> Tuple[FS, Text]
        return self._temp_fs, path

    def delegate_fs(self):
        # type: () -> FS
        return self._temp_fs

    def close(self):
        # type: () -> None
        if not self.isclosed():
            try:
                self.write_zip()
            finally:
                self._temp_fs.close()
        super(WriteZipFS, self).close()

    def write_zip(
        self,
        file=None,  # type: Union[Text, BinaryIO, None]
        compression=None,  # type: Optional[int]
        encoding=None,  # type: Optional[Text]
    ):
        # type: (...) -> None
        """Write zip to a file.

        Arguments:
            file (str or io.IOBase, optional): Destination file, may be
                a file name or an open file handle.
            compression (int, optional): Compression to use (one of the
                constants defined in the `zipfile` module in the stdlib).
            encoding (str, optional): The character encoding to use
                (default uses the encoding defined in
                `~WriteZipFS.__init__`).

        Note:
            This is called automatically when the ZipFS is closed.

        """
        if not self.isclosed():
            write_zip(
                self._temp_fs,
                file or self._file,
                compression=compression or self.compression,
                encoding=encoding or self.encoding,
            )


@six.python_2_unicode_compatible
class ReadZipFS(FS):
    """A readable zip file.

    Normal filesystem paths apply an explicit duplicate-name policy.
    The central-directory methods `get_entries`, `get_entry`, and
    `open_entry` provide unambiguous access when several members share a
    name or normalize to the same filesystem path.
    """

    _meta = {
        "case_insensitive": False,
        "network": False,
        "read_only": True,
        "supports_rename": False,
        "thread_safe": True,
        "unicode_paths": True,
        "virtual": False,
    }

    @errors.CreateFailed.catch_all
    def __init__(
        self,
        file,
        encoding="utf-8",
        duplicate="last",
        duplicate_policy=None,
    ):  # noqa: D107
        # type: (Union[BinaryIO, Text], Text, Text, Optional[Text]) -> None
        super(ReadZipFS, self).__init__()
        self._file = file
        self.encoding = encoding
        duplicate = duplicate_policy or duplicate
        if duplicate not in ("first", "last", "error"):
            raise ValueError("duplicate must be one of 'first', 'last' or 'error'")
        self.duplicate = duplicate
        self._zip = zipfile.ZipFile(file, "r")
        self._entries = []  # type: List[ZipEntry]
        self._path_entries = OrderedDict()  # type: OrderedDict[Text, ZipEntry]
        self._normalized_collisions = set()  # type: Set[Text]
        self._implicit_directories = set()  # type: Set[Text]
        self._accessible_entries = set()  # type: Set[int]
        self._entry_access = {}  # type: Dict[int, bool]
        self._build_entries()

    def __repr__(self):
        # type: () -> Text
        return "ReadZipFS({!r})".format(self._file)

    def __str__(self):
        # type: () -> Text
        return "<zipfs '{}'>".format(self._file)

    def _decode_name(self, zip_info):
        # type: (zipfile.ZipInfo) -> Text
        name = zip_info.filename
        if six.PY2:
            return name.decode(self.encoding, "replace")
        return name

    @staticmethod
    def _safe_zip_path(name, is_dir):
        # type: (Text, bool) -> Tuple[Optional[Text], Optional[Text]]
        candidate = name.rstrip("/") if is_dir else name
        if is_dir and not candidate:
            return "", None
        try:
            normalized = normpath(candidate)
        except errors.IllegalBackReference:
            return None, "unsafe_path"

        if normalized.startswith("/"):
            return None, "absolute_path"
        if "\\" in normalized or not normalized:
            return None, "unsafe_path"

        return normalized, None

    @staticmethod
    def _add_issue(entry, issue):
        # type: (ZipEntry, Text) -> ZipEntry
        if issue not in entry.issues:
            entry._issues = entry.issues + (issue,)
        return entry

    def _build_entries(self):
        # type: () -> None
        candidates = OrderedDict()  # type: OrderedDict[Text, List[ZipEntry]]

        for index, zip_info in enumerate(self._zip.infolist()):
            raw_name = self._decode_name(zip_info)
            is_dir = raw_name.endswith("/")
            safe_path, path_issue = self._safe_zip_path(raw_name, is_dir)
            entry = ZipEntry(
                index,
                raw_name,
                safe_path,
                is_dir,
                zip_info,
                (path_issue,) if path_issue else (),
            )
            self._entries.append(entry)
            if safe_path is not None:
                candidates.setdefault(safe_path, []).append(entry)

        for path, entries in candidates.items():
            first = entries[0]
            is_dir = first.is_dir
            if any(entry.is_dir != is_dir for entry in entries):
                for entry in entries:
                    self._add_issue(entry, "type_collision")
                continue

            selected = entries[-1] if self.duplicate == "last" else first
            if len(entries) > 1:
                issue = "duplicate_entry"
                if any(entry.name != selected.name for entry in entries):
                    issue = "normalized_collision"
                for entry in entries:
                    self._add_issue(entry, issue)
                if issue == "duplicate_entry" and self.duplicate == "error":
                    continue
            self._path_entries[path] = selected

        explicit_paths = set(self._path_entries)
        for path in list(explicit_paths):
            parent = dirname(path)
            while parent and parent not in self._implicit_directories:
                if parent in candidates:
                    parent_entry = self._path_entries.get(parent)
                    if parent_entry is None or not parent_entry.is_dir:
                        if parent_entry is not None:
                            self._add_issue(parent_entry, "type_collision")
                        child = self._path_entries.get(path)
                        if child is not None:
                            self._add_issue(child, "ancestor_not_directory")
                    break
                self._implicit_directories.add(parent)
                parent = dirname(parent)

        for entries in candidates.values():
            for entry in entries:
                if entry.path is not None and entry.path not in self._path_entries:
                    self._normalized_collisions.add(entry.path)

        for entry in self._entries:
            if entry.path is not None and self._entry_is_accessible(
                entry, candidates, self._entry_access
            ):
                self._accessible_entries.add(entry.index)

    def _entry_is_accessible(self, entry, candidates, checked):
        # type: (ZipEntry, OrderedDict[Text, List[ZipEntry]], Dict[int, bool]) -> bool
        if entry.index in checked:
            return checked[entry.index]

        selected = self._path_entries.get(entry.path or "")
        if selected is not entry or any(
            issue != "duplicate_entry" for issue in entry.issues
        ):
            checked[entry.index] = False
            return False

        parent_path = dirname(entry.path or "")
        if not parent_path:
            checked[entry.index] = True
            return True
        if parent_path in self._implicit_directories:
            checked[entry.index] = True
            return True

        parent_entries = candidates.get(parent_path, [])
        parent_directories = [
            candidate for candidate in parent_entries if candidate.is_dir
        ]
        if not parent_directories:
            self._add_issue(entry, "ancestor_not_directory")
            checked[entry.index] = False
            return False

        applicable = parent_directories[0]
        for candidate in parent_directories:
            if candidate.index <= entry.index:
                applicable = candidate
            else:
                break
        selected_parent = self._path_entries.get(parent_path)
        accessible = (
            selected_parent is applicable
            and self._entry_is_accessible(applicable, candidates, checked)
        )
        if not accessible:
            self._add_issue(entry, "ancestor_collision")
        checked[entry.index] = accessible
        return accessible

    def _is_implicit_dir(self, path):
        # type: (Text) -> bool
        return path in self._implicit_directories

    def _implicit_dir_is_accessible(self, path):
        # type: (Text) -> bool
        parent = dirname(path)
        if not parent:
            return True
        if self._is_implicit_dir(parent):
            return self._implicit_dir_is_accessible(parent)
        parent_entry = self._path_entries.get(parent)
        return (
            parent_entry is not None
            and parent_entry.is_dir
            and parent_entry.index in self._accessible_entries
        )

    def _is_under_base(self, path, base_path):
        # type: (Text, Text) -> bool
        if not base_path:
            return bool(path)
        return path.startswith(base_path + "/")

    def _child_name(self, path, base_path):
        # type: (Text, Text) -> Text
        remainder = path[len(base_path) + 1 :] if base_path else path
        return remainder.split("/", 1)[0]

    def _resolve_path(self, path):
        # type: (Text) -> ZipEntry
        _path = self.validatepath(path)

        if _path == "/":
            root = self._path_entries.get("")
            if root is None:
                if "" in self._normalized_collisions:
                    raise errors.InvalidPath(path)
                raise errors.DirectoryExpected(path)
            if not root.is_dir or root.index not in self._accessible_entries:
                raise errors.DirectoryExpected(path)
            return root

        relative_path = relpath(_path)
        entry = self._path_entries.get(relative_path)
        if entry is None:
            if self._is_implicit_dir(relative_path):
                raise KeyError(relative_path)
            if relative_path in self._normalized_collisions:
                raise errors.InvalidPath(path)
            raise errors.ResourceNotFound(path)
        if any(issue != "duplicate_entry" for issue in entry.issues):
            raise errors.InvalidPath(path)
        if entry.index not in self._accessible_entries:
            raise errors.InvalidPath(path)
        return entry

    def _zip_info_for_entry(self, entry):
        # type: (ZipEntry) -> zipfile.ZipInfo
        return self._zip.infolist()[entry.index]

    def get_entries(self, path=None):
        # type: (Optional[Text]) -> List[ZipEntry]
        """List central-directory entries, optionally filtered by path.

        All entries are returned, including entries with unsafe or
        ambiguous names. When ``path`` is given, only explicit entries
        whose normalized filesystem path matches it are returned.
        """
        self.check()
        if path is None:
            return list(self._entries)

        relative_path = relpath(self.validatepath(path))
        if not relative_path:
            return [
                entry
                for entry in self._entries
                if entry.path == "" and entry.is_dir
            ]
        return [
            entry
            for entry in self._entries
            if entry.path == relative_path
        ]

    def get_entry(self, identity):
        # type: (Union[int, ZipEntry]) -> ZipEntry
        """Get a central-directory entry by its stable identity."""
        self.check()
        if isinstance(identity, ZipEntry):
            index = identity.index
        else:
            index = identity
        if index < 0:
            raise IndexError("ZIP entry index out of range: {}".format(index))
        try:
            return self._entries[index]
        except IndexError:
            raise IndexError("ZIP entry index out of range: {}".format(index))

    def open_entry(self, identity, mode="r", buffering=-1, **options):
        # type: (Union[int, ZipEntry], Text, int, **Any) -> BinaryIO
        """Open a specific central-directory entry by identity.

        This method does not resolve the member through its filename, so
        duplicate names remain unambiguous. It only decompresses into the
        returned stream and never extracts to a local path.
        """
        self.check()
        if "w" in mode or "+" in mode or "a" in mode:
            raise errors.ResourceReadOnly(
                getattr(identity, "name", str(identity))
            )
        entry = self.get_entry(identity)
        if entry.is_dir:
            raise errors.FileExpected(entry.name)
        return _ZipExtFile(self, self._zip_info_for_entry(entry))  # type: ignore

    def read_entry(self, identity):
        # type: (Union[int, ZipEntry]) -> bytes
        """Read bytes from a specific central-directory entry."""
        self.check()
        entry = self.get_entry(identity)
        if entry.is_dir:
            raise errors.FileExpected(entry.name)
        return self._zip.read(self._zip_info_for_entry(entry))

    getmembers = get_entries
    get_members = get_entries
    list_entries = get_entries
    list_members = get_entries
    members = get_entries
    getmember = get_entry
    get_member = get_entry
    openmember = open_entry
    open_member = open_entry
    openmemberbin = open_entry
    readmember = read_entry
    read_member = read_entry

    def getinfo(self, path, namespaces=None):
        # type: (Text, Optional[Collection[Text]]) -> Info
        _path = self.validatepath(path)
        namespaces = namespaces or ()
        raw_info = {}  # type: Dict[Text, Dict[Text, object]]

        if _path == "/":
            raw_info["basic"] = {"name": "", "is_dir": True}
            root = self._path_entries.get("")
            if root is None and "" in self._normalized_collisions:
                raise errors.InvalidPath(path)
            if "details" in namespaces:
                raw_info["details"] = {"type": int(ResourceType.directory)}
            if "zip" in namespaces:
                if root is not None:
                    raw_info["zip"] = root.raw_info

        else:
            relative_path = relpath(_path)
            entry = self._path_entries.get(relative_path)
            implicit = False
            if entry is None:
                if not self._is_implicit_dir(relative_path):
                    if relative_path in self._normalized_collisions:
                        raise errors.InvalidPath(path)
                    raise errors.ResourceNotFound(path)
                if not self._implicit_dir_is_accessible(relative_path):
                    raise errors.InvalidPath(path)
                implicit = True
                is_dir = True
            else:
                if any(issue != "duplicate_entry" for issue in entry.issues):
                    raise errors.InvalidPath(path)
                if entry.index not in self._accessible_entries:
                    raise errors.InvalidPath(path)
                is_dir = entry.is_dir

            raw_info["basic"] = {
                "name": basename(relative_path),
                "is_dir": is_dir,
            }

            if entry is not None:
                zip_info = self._zip_info_for_entry(entry)
                if "details" in namespaces:
                    raw_info["details"] = {
                        "size": zip_info.file_size,
                        "type": int(
                            ResourceType.directory if is_dir else ResourceType.file
                        ),
                        "modified": datetime_to_epoch(datetime(*zip_info.date_time)),
                    }
                if "zip" in namespaces:
                    raw_info["zip"] = entry.raw_info
                    raw_info["zip"]["entry_index"] = entry.index
                if "access" in namespaces:
                    if zip_info.external_attr and zip_info.create_system == 3:
                        raw_info["access"] = {
                            "permissions": Permissions(
                                mode=zip_info.external_attr >> 16 & 0xFFF
                            ).dump()
                        }
            elif "details" in namespaces:
                raw_info["details"] = {"type": int(ResourceType.directory)}
            if implicit and "zip" in namespaces:
                raw_info["zip"] = {"implicit": True}

        return Info(raw_info)

    def setinfo(self, path, info):
        # type: (Text, RawInfo) -> None
        self.check()
        raise errors.ResourceReadOnly(path)

    def listdir(self, path):
        # type: (Text) -> List[Text]
        self.check()
        _path = self.validatepath(path)
        if _path != "/" and not self.getinfo(path).is_dir:
            raise errors.DirectoryExpected(path)

        base_path = relpath(_path)
        children = OrderedDict()  # type: OrderedDict[Text, None]
        for entry in self._entries:
            if entry.path is None:
                continue
            selected = self._path_entries.get(entry.path)
            if selected is not entry:
                continue
            if entry.index not in self._accessible_entries:
                continue
            if not self._is_under_base(entry.path, base_path):
                continue
            name = self._child_name(entry.path, base_path)
            if name:
                children[name] = None

        for directory in self._implicit_directories:
            if not self._is_under_base(directory, base_path):
                continue
            if not self._implicit_dir_is_accessible(directory):
                continue
            name = self._child_name(directory, base_path)
            if name:
                children[name] = None

        return list(children)

    def makedir(
        self,  # type: R
        path,  # type: Text
        permissions=None,  # type: Optional[Permissions]
        recreate=False,  # type: bool
    ):
        # type: (...) -> SubFS[R]
        self.check()
        raise errors.ResourceReadOnly(path)

    def openbin(self, path, mode="r", buffering=-1, **kwargs):
        # type: (Text, Text, int, **Any) -> BinaryIO
        self.check()
        if "w" in mode or "+" in mode or "a" in mode:
            raise errors.ResourceReadOnly(path)

        try:
            entry = self._resolve_path(path)
        except KeyError:
            raise errors.FileExpected(path)
        if entry.is_dir:
            raise errors.FileExpected(path)

        return _ZipExtFile(self, self._zip_info_for_entry(entry))  # type: ignore

    def remove(self, path):
        # type: (Text) -> None
        self.check()
        raise errors.ResourceReadOnly(path)

    def removedir(self, path):
        # type: (Text) -> None
        self.check()
        raise errors.ResourceReadOnly(path)

    def close(self):
        # type: () -> None
        super(ReadZipFS, self).close()
        if hasattr(self, "_zip"):
            self._zip.close()

    def readbytes(self, path):
        # type: (Text) -> bytes
        self.check()
        try:
            entry = self._resolve_path(path)
        except KeyError:
            raise errors.FileExpected(path)
        if entry.is_dir:
            raise errors.FileExpected(path)
        return self._zip.read(self._zip_info_for_entry(entry))

    def geturl(self, path, purpose="download"):
        # type: (Text, Text) -> Text
        if purpose == "fs" and isinstance(self._file, six.string_types):
            quoted_file = url_quote(self._file)
            quoted_path = url_quote(path)
            return "zip://{}!/{}".format(quoted_file, quoted_path)
        else:
            raise errors.NoURL(path, purpose)
