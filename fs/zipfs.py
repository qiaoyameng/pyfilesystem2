"""Manage the filesystem in a Zip archive.
"""

from __future__ import print_function, unicode_literals

import sys
import typing

import six
import zipfile
from collections import OrderedDict
from datetime import datetime

from . import errors
from ._url_tools import url_quote
from .base import FS
from .compress import write_zip
from .enums import ResourceType, Seek
from .info import Info
from .iotools import RawWrapper
from .opener import open_fs
from .path import basename, relpath
from .permissions import Permissions
from .time import datetime_to_epoch
from .wrapfs import WrapFS

if typing.TYPE_CHECKING:
    from typing import (
        Any,
        BinaryIO,
        Collection,
        Dict,
        List,
        Optional,
        SupportsInt,
        Text,
        Tuple,
        Union,
    )

    from .info import RawInfo
    from .subfs import SubFS

    R = typing.TypeVar("R", bound="ReadZipFS")


class _ZipExtFile(RawWrapper):
    def __init__(self, fs, member):  # noqa: D107
        # type: (ReadZipFS, ZipMember) -> None
        self._zip = _zip = fs._zip
        self._zip_info = member.zip_info
        self._end = member.file_size
        self._pos = 0
        super(_ZipExtFile, self).__init__(
            _zip.open(self._zip_info), "r", member.raw_name
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


class ZipMember(object):
    """A member of the central directory of a zip archive.

    The ``index`` of a member is its position in the central directory.
    Unlike the member name, it remains unambiguous when an archive contains
    duplicates or paths that normalize to the same location.
    """

    __slots__ = ("index", "raw_name", "path", "is_dir", "issue", "zip_info")

    def __init__(
        self,
        index,  # type: int
        raw_name,  # type: Text
        path,  # type: Optional[Text]
        is_dir,  # type: bool
        zip_info,  # type: zipfile.ZipInfo
        issue=None,  # type: Optional[Text]
    ):
        # type: (...) -> None
        self.index = index
        self.raw_name = raw_name
        self.path = path
        self.is_dir = is_dir
        self.issue = issue
        self.zip_info = zip_info

    def __repr__(self):
        # type: () -> Text
        return "ZipMember(index={!r}, name={!r})".format(self.index, self.raw_name)

    def __eq__(self, other):
        # type: (object) -> bool
        return isinstance(other, ZipMember) and self.index == other.index

    def __ne__(self, other):
        # type: (object) -> bool
        return not self == other

    def __hash__(self):
        # type: () -> int
        return hash(("ZipMember", self.index))

    @property
    def name(self):
        # type: () -> Text
        """The member name stored in the central directory."""
        return self.raw_name

    @property
    def file_name(self):
        # type: () -> Text
        """The member name stored in the central directory."""
        return self.raw_name

    @property
    def filename(self):
        # type: () -> Text
        """The member name stored in the central directory."""
        return self.raw_name

    @property
    def size(self):
        # type: () -> int
        """The uncompressed size of the member."""
        return self.zip_info.file_size

    @property
    def file_size(self):
        # type: () -> int
        """The uncompressed size of the member."""
        return self.zip_info.file_size

    @property
    def id(self):
        # type: () -> int
        """The central-directory index of this member."""
        return self.index

    @property
    def entry_id(self):
        # type: () -> int
        """The central-directory index of this member."""
        return self.index

    @property
    def crc(self):
        # type: () -> int
        """The CRC-32 checksum of the uncompressed data."""
        return self.zip_info.CRC

    @property
    def CRC(self):
        # type: () -> int
        """The CRC-32 checksum of the uncompressed data."""
        return self.zip_info.CRC

    @property
    def compress_type(self):
        # type: () -> int
        """The compression method used by this member."""
        return self.zip_info.compress_type

    @property
    def compress_size(self):
        # type: () -> int
        """The compressed size of the member."""
        return self.zip_info.compress_size

    @property
    def date_time(self):
        # type: () -> Tuple[int, int, int, int, int, int]
        """The modification timestamp stored by the member."""
        return self.zip_info.date_time


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
    ):
        # type: (...) -> FS
        # This magic returns a different class instance based on the
        # value of the ``write`` parameter.
        if write:
            return WriteZipFS(
                file, compression=compression, encoding=encoding, temp_fs=temp_fs
            )
        else:
            return ReadZipFS(file, encoding=encoding)

    if typing.TYPE_CHECKING:

        def __init__(
            self,
            file,  # type: Union[Text, BinaryIO]
            write=False,  # type: bool
            compression=zipfile.ZIP_DEFLATED,  # type: int
            encoding="utf-8",  # type: Text
            temp_fs="temp://__ziptemp__",  # type: Text
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
    """A readable zip file."""

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
    def __init__(self, file, encoding="utf-8"):  # noqa: D107
        # type: (Union[BinaryIO, Text], Text) -> None
        super(ReadZipFS, self).__init__()
        self._file = file
        self.encoding = encoding
        self._zip = zipfile.ZipFile(file, "r")
        self._members = []  # type: List[ZipMember]
        self._path_members = OrderedDict()  # type: Dict[Text, List[ZipMember]]
        self._directory_paths = set()  # type: set
        self._ambiguous_paths = set()  # type: set
        self._directory_entries = OrderedDict()  # type: Dict[Text, Optional[ZipMember]]
        self._load_members()

    def __repr__(self):
        # type: () -> Text
        return "ReadZipFS({!r})".format(self._file)

    def __str__(self):
        # type: () -> Text
        return "<zipfs '{}'>".format(self._file)

    def _decode_name(self, zip_info):
        # type: (zipfile.ZipInfo) -> Text
        if six.PY2:
            return zip_info.filename.decode(self.encoding, "replace")
        return zip_info.filename

    @staticmethod
    def _normalize_member_name(raw_name):
        # type: (Text) -> Tuple[Optional[Text], Optional[Text]]
        is_dir = raw_name.endswith("/")
        name = raw_name[:-1] if is_dir else raw_name
        issues = []  # type: List[Text]
        if name.startswith("/"):
            issues.append("absolute path treated as archive-relative")
            name = name.lstrip("/")

        components = []  # type: List[Text]
        for component in name.split("/"):
            if component == "":
                continue
            if component == ".":
                continue
            if component == "..":
                if components:
                    components.pop()
                else:
                    issues.append("parent directory reference removed from root")
            else:
                components.append(component)

        if not components:
            if is_dir:
                return "", "; ".join(issues) if issues else None
            return None, "invalid empty file name"
        return "/".join(components), "; ".join(issues) if issues else None

    def _load_members(self):
        # type: () -> None
        for index, zip_info in enumerate(self._zip.infolist()):
            raw_name = self._decode_name(zip_info)
            path, issue = self._normalize_member_name(raw_name)
            member = ZipMember(
                index,
                raw_name,
                path,
                raw_name.endswith("/"),
                zip_info,
                issue,
            )
            self._members.append(member)
            if path is not None:
                self._path_members.setdefault(path, []).append(member)

        for members in self._path_members.values():
            for member in members:
                parent = member.path
                while parent:
                    parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
                    self._directory_paths.add(parent)

        all_paths = set(self._path_members)
        all_paths.update(self._directory_paths)
        for path in all_paths:
            members = self._path_members.get(path, [])
            file_members = [member for member in members if not member.is_dir]
            directory_members = [member for member in members if member.is_dir]
            is_explicit_directory = bool(directory_members)
            is_implicit_directory = path in self._directory_paths
            is_ambiguous = (
                len(file_members) > 1
                or len(directory_members) > 1
                or bool(
                    file_members and (is_explicit_directory or is_implicit_directory)
                )
                or bool(is_implicit_directory and len(directory_members) > 1)
            )

            if path and is_ambiguous:
                self._ambiguous_paths.add(path)
                collision = "normalized path collision with entries: {}".format(
                    ", ".join(str(member.index) for member in members)
                )
                for member in members:
                    member.issue = (
                        "; ".join((member.issue, collision))
                        if member.issue
                        else collision
                    )
            elif directory_members:
                self._directory_entries[path] = directory_members[0]
            else:
                self._directory_entries[path] = None

    def members(self):
        # type: () -> List[ZipMember]
        """Return all central-directory members in archive order."""
        self.check()
        return list(self._members)

    getmembers = members
    list_members = members

    def getmember(self, member):
        # type: (Union[int, ZipMember, zipfile.ZipInfo]) -> ZipMember
        """Get a member by central-directory index or concrete entry identity."""
        self.check()
        if isinstance(member, ZipMember):
            index = member.index
            if index < 0 or index >= len(self._members):
                raise errors.ResourceNotFound("zip member {}".format(index))
            if self._members[index].zip_info is not member.zip_info:
                raise errors.ResourceNotFound("zip member {}".format(index))
            return self._members[index]
        elif isinstance(member, zipfile.ZipInfo):
            for zip_member in self._members:
                if zip_member.zip_info is member:
                    return zip_member
            raise errors.ResourceNotFound("zip member {!r}".format(member.filename))
        elif isinstance(member, int) and not isinstance(member, bool):
            index = member
            if index < 0:
                raise errors.ResourceNotFound("zip member {}".format(index))
        else:
            raise TypeError("member must be a ZipMember or central-directory index")
        try:
            return self._members[index]
        except IndexError:
            raise errors.ResourceNotFound("zip member {}".format(index))

    def openmember(self, member, mode="r", buffering=-1, **options):
        # type: (Union[int, ZipMember, zipfile.ZipInfo], Text, int, **Any) -> BinaryIO
        """Open a file member by its central-directory identity."""
        self.check()
        if "w" in mode or "+" in mode or "a" in mode:
            resource_name = (
                member.raw_name if isinstance(member, ZipMember) else repr(member)
            )
            raise errors.ResourceReadOnly(resource_name)
        zip_member = self.getmember(member)
        if zip_member.is_dir:
            raise errors.FileExpected(zip_member.raw_name)
        return _ZipExtFile(self, zip_member)

    openbin_member = openmember

    def readmember(self, member):
        # type: (Union[int, ZipMember, zipfile.ZipInfo]) -> bytes
        """Read a file member by its central-directory identity."""
        self.check()
        zip_member = self.getmember(member)
        if zip_member.is_dir:
            raise errors.FileExpected(zip_member.raw_name)
        return self._zip.read(zip_member.zip_info)

    readbytes_member = readmember

    def _get_path_member(self, path):
        # type: (Text) -> Union[ZipMember, bool, None]
        if path in self._ambiguous_paths:
            indexes = [str(member.index) for member in self._path_members[path]]
            message = (
                "path '{path}' matches multiple central-directory entries: "
                + ", ".join(indexes)
            )
            raise errors.ResourceInvalid(path, msg=message)
        members = self._path_members.get(path)
        if members:
            return members[0]
        return self._directory_entries.get(path, False)  # type: ignore

    def getinfo(self, path, namespaces=None):
        # type: (Text, Optional[Collection[Text]]) -> Info
        _path = self.validatepath(path)
        namespaces = namespaces or ()
        raw_info = {}  # type: Dict[Text, Dict[Text, object]]

        if _path == "/":
            raw_info["basic"] = {"name": "", "is_dir": True}
            if "details" in namespaces:
                raw_info["details"] = {"type": int(ResourceType.directory)}

        else:
            resource_path = relpath(_path)
            zip_member = self._get_path_member(resource_path)
            if zip_member is False:
                raise errors.ResourceNotFound(path)
            is_dir = zip_member is None or zip_member.is_dir
            raw_info["basic"] = {
                "name": basename(resource_path),
                "is_dir": is_dir,
            }

            if zip_member is not None:
                zip_info = zip_member.zip_info
                if "details" in namespaces:
                    raw_info["details"] = {
                        "size": zip_info.file_size,
                        "type": int(
                            ResourceType.directory
                            if is_dir
                            else ResourceType.file
                        ),
                        "modified": datetime_to_epoch(datetime(*zip_info.date_time)),
                    }
                if "zip" in namespaces:
                    raw_info["zip"] = {
                        k: getattr(zip_info, k)
                        for k in zip_info.__slots__  # type: ignore
                        if not k.startswith("_")
                    }
                if "access" in namespaces:
                    # check the zip was created on UNIX to get permissions
                    if zip_info.external_attr and zip_info.create_system == 3:
                        raw_info["access"] = {
                            "permissions": Permissions(
                                mode=zip_info.external_attr >> 16 & 0xFFF
                            ).dump()
                        }
            elif "details" in namespaces:
                raw_info["details"] = {"type": int(ResourceType.directory)}

        return Info(raw_info)

    def setinfo(self, path, info):
        # type: (Text, RawInfo) -> None
        self.check()
        raise errors.ResourceReadOnly(path)

    def listdir(self, path):
        # type: (Text) -> List[Text]
        _path = relpath(self.validatepath(path))
        zip_member = self._get_path_member(_path)
        if zip_member is not False and zip_member is not None and not zip_member.is_dir:
            raise errors.DirectoryExpected(path)
        if zip_member is False:
            raise errors.ResourceNotFound(path)

        prefix = "" if not _path else _path + "/"
        children = OrderedDict()  # type: Dict[Text, None]
        for member in self._members:
            child = member.path
            if child is not None and child.startswith(prefix):
                name = child[len(prefix):].split("/", 1)[0]
                if name:
                    children[name] = None
        return list(children.keys())

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

        _path = relpath(self.validatepath(path))
        zip_member = self._get_path_member(_path)
        if zip_member is False:
            raise errors.ResourceNotFound(path)
        elif zip_member is None or zip_member.is_dir:
            raise errors.FileExpected(path)

        return _ZipExtFile(self, zip_member)

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
        _path = relpath(self.validatepath(path))
        zip_member = self._get_path_member(_path)
        if zip_member is False or zip_member is None:
            raise errors.ResourceNotFound(path)
        if zip_member.is_dir:
            raise errors.FileExpected(path)
        return self._zip.read(zip_member.zip_info)

    def geturl(self, path, purpose="download"):
        # type: (Text, Text) -> Text
        if purpose == "fs" and isinstance(self._file, six.string_types):
            quoted_file = url_quote(self._file)
            quoted_path = url_quote(path)
            return "zip://{}!/{}".format(quoted_file, quoted_path)
        else:
            raise errors.NoURL(path, purpose)
