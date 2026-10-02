"""Functions for copying resources *between* filesystem.
"""

from __future__ import print_function, unicode_literals

import typing

import binascii
import hashlib
import os
import warnings

from .errors import (
    CopyCancelled,
    FileExpected,
    IllegalDestination,
    ResourceInvalid,
    ResourceNotFound,
    Unsupported,
    UnsupportedHash,
)
from .opener import manage_fs
from .path import abspath, basename, combine, dirname, frombase, isbase, normpath
from .tools import is_thread_safe
from .walk import Walker

if typing.TYPE_CHECKING:
    from typing import Any, Callable, Dict, Optional, Set, Text, Union

    from .base import FS

    _OnCopy = Callable[[FS, Text, FS, Text], object]
    _OnProgress = Callable[[int, Optional[int], FS, Text, FS, Text], object]


_COPY_CHUNK_SIZE = 64 * 1024


def _hash_file(fs, path, algorithm):
    # type: (FS, Text, Text) -> Text
    hash_object = hashlib.new(algorithm)
    with fs.openbin(path) as read_file:
        while True:
            chunk = read_file.read(1024 * 1024)
            if not chunk:
                break
            hash_object.update(chunk)
    return hash_object.hexdigest()


def _report_progress(
    on_progress, bytes_written, total_size, src_fs, src_path, dst_fs, dst_path
):
    # type: (...) -> None
    if on_progress is not None and on_progress(
        bytes_written, total_size, src_fs, src_path, dst_fs, dst_path
    ) is False:
        raise CopyCancelled(dst_path)


def _write_all(
    write_file,
    chunk,
    on_progress,
    total_size,
    src_fs,
    src_path,
    dst_fs,
    dst_path,
    bytes_written,
    hash_object,
):
    # type: (...) -> int
    offset = 0
    while offset < len(chunk):
        write_count = write_file.write(chunk[offset:])
        if write_count is None:
            write_count = len(chunk) - offset
        if write_count <= 0:
            raise IOError("destination stream wrote no data")
        offset += write_count
        bytes_written += write_count
        hash_object.update(chunk[offset - write_count : offset])
        _report_progress(
            on_progress,
            bytes_written,
            total_size,
            src_fs,
            src_path,
            dst_fs,
            dst_path,
        )
    return bytes_written


def _unique_path(fs, directory, prefix, attempted=None):
    # type: (FS, Text, Text, Optional[Set[Text]]) -> Text
    attempted = attempted if attempted is not None else set()  # type: Set[Text]
    for _ in range(64):
        suffix = binascii.hexlify(os.urandom(8)).decode("ascii")
        base_directory = directory.rstrip("/")
        if base_directory:
            path = "{}/.{}.{}".format(base_directory, prefix, suffix)
        else:
            path = "/.{}.{}".format(prefix, suffix)
        if path in attempted:
            continue
        attempted.add(path)
        if not fs.exists(path):
            return path
    raise Unsupported(directory, msg="unable to allocate a temporary filename")


def copy_fs(
    src_fs,  # type: Union[FS, Text]
    dst_fs,  # type: Union[FS, Text]
    walker=None,  # type: Optional[Walker]
    on_copy=None,  # type: Optional[_OnCopy]
    workers=0,  # type: int
    preserve_time=False,  # type: bool
):
    # type: (...) -> None
    """Copy the contents of one filesystem to another.

    Arguments:
        src_fs (FS or str): Source filesystem (URL or instance).
        dst_fs (FS or str): Destination filesystem (URL or instance).
        walker (~fs.walk.Walker, optional): A walker object that will be
            used to scan for files in ``src_fs``. Set this if you only want
            to consider a sub-set of the resources in ``src_fs``.
        on_copy (callable): A function callback called after a single file copy
            is executed. Expected signature is ``(src_fs, src_path, dst_fs,
            dst_path)``.
        workers (int): Use `worker` threads to copy data, or ``0`` (default) for
            a single-threaded copy.
        preserve_time (bool): If `True`, try to preserve mtime of the
            resources (defaults to `False`).

    """
    return copy_fs_if(
        src_fs, dst_fs, "always", walker, on_copy, workers, preserve_time=preserve_time
    )


def copy_fs_if_newer(
    src_fs,  # type: Union[FS, Text]
    dst_fs,  # type: Union[FS, Text]
    walker=None,  # type: Optional[Walker]
    on_copy=None,  # type: Optional[_OnCopy]
    workers=0,  # type: int
    preserve_time=False,  # type: bool
):
    # type: (...) -> None
    """Copy the contents of one filesystem to another, checking times.

    .. deprecated:: 2.5.0
       Use `~fs.copy.copy_fs_if` with ``condition="newer"`` instead.

    """
    warnings.warn(
        "copy_fs_if_newer is deprecated. Use copy_fs_if instead.", DeprecationWarning
    )
    return copy_fs_if(
        src_fs, dst_fs, "newer", walker, on_copy, workers, preserve_time=preserve_time
    )


def copy_fs_if(
    src_fs,  # type: Union[FS, Text]
    dst_fs,  # type: Union[FS, Text]
    condition="always",  # type: Text
    walker=None,  # type: Optional[Walker]
    on_copy=None,  # type: Optional[_OnCopy]
    workers=0,  # type: int
    preserve_time=False,  # type: bool
):
    # type: (...) -> None
    """Copy the contents of one filesystem to another, depending on a condition.

    Arguments:
        src_fs (FS or str): Source filesystem (URL or instance).
        dst_fs (FS or str): Destination filesystem (URL or instance).
        condition (str): Name of the condition to check for each file.
        walker (~fs.walk.Walker, optional): A walker object that will be
            used to scan for files in ``src_fs``. Set this if you only want
            to consider a sub-set of the resources in ``src_fs``.
        on_copy (callable):A function callback called after a single file copy
            is executed. Expected signature is ``(src_fs, src_path, dst_fs,
            dst_path)``.
        workers (int): Use ``worker`` threads to copy data, or ``0`` (default)
            for a single-threaded copy.
        preserve_time (bool): If `True`, try to preserve mtime of the
            resources (defaults to `False`).

    See Also:
        `~fs.copy.copy_file_if` for the full list of supported values for the
        ``condition`` argument.

    """
    return copy_dir_if(
        src_fs,
        "/",
        dst_fs,
        "/",
        condition,
        walker=walker,
        on_copy=on_copy,
        workers=workers,
        preserve_time=preserve_time,
    )


def copy_file(
    src_fs,  # type: Union[FS, Text]
    src_path,  # type: Text
    dst_fs,  # type: Union[FS, Text]
    dst_path,  # type: Text
    preserve_time=False,  # type: bool
):
    # type: (...) -> None
    """Copy a file from one filesystem to another.

    If the destination exists, and is a file, it will be first truncated.

    Arguments:
        src_fs (FS or str): Source filesystem (instance or URL).
        src_path (str): Path to a file on the source filesystem.
        dst_fs (FS or str): Destination filesystem (instance or URL).
        dst_path (str): Path to a file on the destination filesystem.
        preserve_time (bool): If `True`, try to preserve mtime of the
            resource (defaults to `False`).

    """
    copy_file_if(
        src_fs, src_path, dst_fs, dst_path, "always", preserve_time=preserve_time
    )


def copy_file_if_newer(
    src_fs,  # type: Union[FS, Text]
    src_path,  # type: Text
    dst_fs,  # type: Union[FS, Text]
    dst_path,  # type: Text
    preserve_time=False,  # type: bool
):
    # type: (...) -> bool
    """Copy a file from one filesystem to another, checking times.

    .. deprecated:: 2.5.0
       Use `~fs.copy.copy_file_if` with ``condition="newer"`` instead.

    """
    warnings.warn(
        "copy_file_if_newer is deprecated. Use copy_file_if instead.",
        DeprecationWarning,
    )
    return copy_file_if(
        src_fs, src_path, dst_fs, dst_path, "newer", preserve_time=preserve_time
    )


def copy_file_if(
    src_fs,  # type: Union[FS, Text]
    src_path,  # type: Text
    dst_fs,  # type: Union[FS, Text]
    dst_path,  # type: Text
    condition,  # type: Text
    preserve_time=False,  # type: bool
):
    # type: (...) -> bool
    """Copy a file from one filesystem to another, depending on a condition.

    Depending on the value of ``condition``, certain requirements must
    be fulfilled for a file to be copied to ``dst_fs``. The following
    values are supported:

    ``"always"``
        The source file is always copied.
    ``"newer"``
        The last modification time of the source file must be newer than that
        of the destination file. If either file has no modification time, the
        copy is performed always.
    ``"older"``
        The last modification time of the source file must be older than that
        of the destination file. If either file has no modification time, the
        copy is performed always.
    ``"exists"``
        The source file is only copied if a file of the same path already
        exists in ``dst_fs``.
    ``"not_exists"``
        The source file is only copied if no file of the same path already
        exists in ``dst_fs``.

    Arguments:
        src_fs (FS or str): Source filesystem (instance or URL).
        src_path (str): Path to a file on the source filesystem.
        dst_fs (FS or str): Destination filesystem (instance or URL).
        dst_path (str): Path to a file on the destination filesystem.
        condition (str): Name of the condition to check for each file.
        preserve_time (bool): If `True`, try to preserve mtime of the
            resource (defaults to `False`).

    Returns:
        bool: `True` if the file copy was executed, `False` otherwise.

    """
    with manage_fs(src_fs, writeable=False) as _src_fs:
        with manage_fs(dst_fs, create=True) as _dst_fs:
            do_copy = _copy_is_necessary(
                _src_fs, src_path, _dst_fs, dst_path, condition
            )
            if do_copy:
                copy_file_internal(
                    _src_fs,
                    src_path,
                    _dst_fs,
                    dst_path,
                    preserve_time=preserve_time,
                    lock=True,
                )
            return do_copy


def copy_file_atomic(
    src_fs,  # type: Union[FS, Text]
    src_path,  # type: Text
    dst_fs,  # type: Union[FS, Text]
    dst_path,  # type: Text
    on_progress=None,  # type: Optional[_OnProgress]
    chunk_size=_COPY_CHUNK_SIZE,  # type: int
    preserve_time=False,  # type: bool
    allow_non_atomic=False,  # type: bool
    hash_name="sha256",  # type: Text
):
    # type: (...) -> Dict[Text, Any]
    """Copy a file to a temporary path, verify it, then publish it.

    Progress is reported as the number of bytes that the destination
    accepted, not the number of bytes read from the source. The callback
    may return `False` to request cancellation; cancellation and other
    exceptions remove the temporary file created by this call.

    Arguments:
        src_fs (FS or str): Source filesystem (instance or URL).
        src_path (str): Path to a file on the source filesystem.
        dst_fs (FS or str): Destination filesystem (instance or URL).
        dst_path (str): Path to a file on the destination filesystem.
        on_progress (callable, optional): Called as
            ``on_progress(bytes_written, total_size, src_fs, src_path,
            dst_fs, dst_path)`` before copying and after every destination
            write. Return `False` to cancel.
        chunk_size (int): Maximum number of bytes read at once.
        preserve_time (bool): If `True`, copy the source modification time
            after the destination is published.
        allow_non_atomic (bool): If `True`, replace existing files on
            backends that do not advertise atomic replacement using a
            best-effort backup-and-restore fallback. If `False`, such
            backends raise `~fs.errors.Unsupported`.
        hash_name (str): Hash algorithm used to verify the completed
            temporary file.

    Returns:
        dict: A dictionary containing ``bytes_written``, ``size`` and
        ``hash``.

    Raises:
        fs.errors.IllegalDestination: If source and destination identify
            the same file.
        fs.errors.CopyCancelled: If ``on_progress`` returns `False`.
        fs.errors.Unsupported: If an existing destination requires a
            non-atomic fallback and ``allow_non_atomic`` is `False`.

    """
    with manage_fs(src_fs, writeable=False) as _src_fs:
        with manage_fs(dst_fs, create=True, writeable=True) as _dst_fs:
            return _copy_file_atomic_internal(
                _src_fs,
                src_path,
                _dst_fs,
                dst_path,
                on_progress=on_progress,
                chunk_size=chunk_size,
                preserve_time=preserve_time,
                allow_non_atomic=allow_non_atomic,
                hash_name=hash_name,
                lock=True,
            )


def _copy_file_atomic_internal(
    src_fs,
    src_path,
    dst_fs,
    dst_path,
    on_progress=None,
    chunk_size=_COPY_CHUNK_SIZE,
    preserve_time=False,
    allow_non_atomic=False,
    hash_name="sha256",
    lock=False,
):
    # type: (...) -> Dict[Text, Any]
    _src_path = src_fs.validatepath(src_path)
    _dst_path = dst_fs.validatepath(dst_path)
    if src_fs is dst_fs and _src_path == _dst_path:
        raise IllegalDestination(dst_path)

    def _copy_locked():
        # type: () -> Dict[Text, Any]
        if chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer")
        try:
            hashlib.new(hash_name)
        except ValueError:
            raise UnsupportedHash("hash '{}' is not supported".format(hash_name))

        if dst_fs.isdir(_dst_path):
            raise FileExpected(dst_path)
        src_info = src_fs.getinfo(_src_path, ("details",))
        if src_info.is_dir:
            raise FileExpected(src_path)
        total_size = src_info.size

        destination_exists = dst_fs.exists(_dst_path)
        if (
            destination_exists
            and not allow_non_atomic
            and not dst_fs.getmeta().get("supports_atomic_replace", False)
        ):
            raise Unsupported(
                _dst_path,
                msg="destination filesystem does not support atomic replacement",
            )

        destination_directory = dirname(_dst_path)
        temp_name = "{}.tmp".format(basename(_dst_path))
        temp_path = _unique_path(dst_fs, destination_directory, temp_name)

        _report_progress(
            on_progress, 0, total_size, src_fs, src_path, dst_fs, dst_path
        )

        bytes_written = 0
        hash_object = hashlib.new(hash_name)
        temp_created = False
        try:
            with src_fs.openbin(_src_path) as read_file:
                with dst_fs.openbin(temp_path, "x") as write_file:
                    temp_created = True
                    while True:
                        chunk = read_file.read(chunk_size)
                        if not chunk:
                            break
                        bytes_written = _write_all(
                            write_file,
                            chunk,
                            on_progress,
                            total_size,
                            src_fs,
                            src_path,
                            dst_fs,
                            dst_path,
                            bytes_written,
                            hash_object,
                        )
                    write_file.flush()

                if dst_fs.getsize(temp_path) != bytes_written:
                    raise ResourceInvalid(
                        temp_path, msg="copied size verification failed"
                    )

                source_hash = _hash_file(src_fs, _src_path, hash_name)
                temporary_hash = _hash_file(dst_fs, temp_path, hash_name)
                written_hash = hash_object.hexdigest()
                if source_hash != temporary_hash or source_hash != written_hash:
                    raise ResourceInvalid(
                        temp_path, msg="copied hash verification failed"
                    )

                backup_path = None
                if destination_exists:
                    if dst_fs.getmeta().get("supports_atomic_replace", False):
                        dst_fs.replace(temp_path, _dst_path)
                        temp_created = False
                    else:
                        backup_name = "{}.bak".format(basename(_dst_path))
                        backup_path = _unique_path(
                            dst_fs, destination_directory, backup_name
                        )
                        dst_fs.move(_dst_path, backup_path)
                        try:
                            dst_fs.move(temp_path, _dst_path)
                            temp_created = False
                        except Exception:
                            try:
                                dst_fs.move(backup_path, _dst_path)
                            finally:
                                if dst_fs.exists(temp_path):
                                    dst_fs.remove(temp_path)
                                temp_created = False
                            raise
                        try:
                            dst_fs.remove(backup_path)
                        except Exception:
                            if not dst_fs.exists(_dst_path):
                                raise
                else:
                    dst_fs.move(temp_path, _dst_path)
                    temp_created = False

                if preserve_time:
                    src_meta = src_fs.getinfo(_src_path, ("details",))
                    src_details = src_meta.raw.get("details", {})
                    dst_details = {}
                    for value in ("metadata_changed", "modified"):
                        if value in src_details:
                            dst_details[value] = src_details[value]
                    if dst_details:
                        dst_fs.setinfo(_dst_path, {"details": dst_details})
        except Exception:
            if temp_created and dst_fs.exists(temp_path):
                dst_fs.remove(temp_path)
            raise

        return {
            "bytes_written": bytes_written,
            "size": bytes_written,
            "hash": source_hash,
            "hash_name": hash_name,
        }

    if lock:
        with src_fs.lock(), dst_fs.lock():
            return _copy_locked()
    return _copy_locked()


def copy_file_internal(
    src_fs,  # type: FS
    src_path,  # type: Text
    dst_fs,  # type: FS
    dst_path,  # type: Text
    preserve_time=False,  # type: bool
    lock=False,  # type: bool
):
    # type: (...) -> None
    """Copy a file at low level, without calling `manage_fs` or locking.

    If the destination exists, and is a file, it will be first truncated.

    This method exists to optimize copying in loops. In general you
    should prefer `copy_file`.

    Arguments:
        src_fs (FS): Source filesystem.
        src_path (str): Path to a file on the source filesystem.
        dst_fs (FS): Destination filesystem.
        dst_path (str): Path to a file on the destination filesystem.
        preserve_time (bool): If `True`, try to preserve mtime of the
            resource (defaults to `False`).
        lock (bool): Lock both filesystems before copying.

    """
    _src_path = src_fs.validatepath(src_path)
    _dst_path = dst_fs.validatepath(dst_path)
    if src_fs is dst_fs:
        # It's not allowed to copy a file onto itself
        if _src_path == _dst_path:
            raise IllegalDestination(dst_path)
        # Same filesystem, so we can do a potentially optimized copy
        src_fs.copy(src_path, dst_path, overwrite=True, preserve_time=preserve_time)
        return

    def _copy_locked():
        if dst_fs.hassyspath(dst_path):
            with dst_fs.openbin(dst_path, "w") as write_file:
                src_fs.download(src_path, write_file)
        else:
            with src_fs.openbin(src_path) as read_file:
                dst_fs.upload(dst_path, read_file)

        if preserve_time:
            copy_modified_time(src_fs, src_path, dst_fs, dst_path)

    if lock:
        with src_fs.lock(), dst_fs.lock():
            _copy_locked()
    else:
        _copy_locked()


def copy_structure(
    src_fs,  # type: Union[FS, Text]
    dst_fs,  # type: Union[FS, Text]
    walker=None,  # type: Optional[Walker]
    src_root="/",  # type: Text
    dst_root="/",  # type: Text
):
    # type: (...) -> None
    """Copy directories (but not files) from ``src_fs`` to ``dst_fs``.

    Arguments:
        src_fs (FS or str): Source filesystem (instance or URL).
        dst_fs (FS or str): Destination filesystem (instance or URL).
        walker (~fs.walk.Walker, optional): A walker object that will be
            used to scan for files in ``src_fs``. Set this if you only
            want to consider a sub-set of the resources in ``src_fs``.
        src_root (str): Path of the base directory to consider as the root
            of the tree structure to copy.
        dst_root (str): Path to the target root of the tree structure.

    """
    walker = walker or Walker()
    with manage_fs(src_fs) as _src_fs:
        with manage_fs(dst_fs, create=True) as _dst_fs:
            _src_root = _src_fs.validatepath(src_root)
            _dst_root = _dst_fs.validatepath(dst_root)

            # It's not allowed to copy a structure into itself
            if _src_fs == _dst_fs and isbase(_src_root, _dst_root):
                raise IllegalDestination(dst_root)

            with _src_fs.lock(), _dst_fs.lock():
                _dst_fs.makedirs(_dst_root, recreate=True)
                for dir_path in walker.dirs(_src_fs, _src_root):
                    _dst_fs.makedir(
                        combine(_dst_root, frombase(_src_root, dir_path)), recreate=True
                    )


def copy_dir(
    src_fs,  # type: Union[FS, Text]
    src_path,  # type: Text
    dst_fs,  # type: Union[FS, Text]
    dst_path,  # type: Text
    walker=None,  # type: Optional[Walker]
    on_copy=None,  # type: Optional[_OnCopy]
    workers=0,  # type: int
    preserve_time=False,  # type: bool
):
    # type: (...) -> None
    """Copy a directory from one filesystem to another.

    Arguments:
        src_fs (FS or str): Source filesystem (instance or URL).
        src_path (str): Path to a directory on the source filesystem.
        dst_fs (FS or str): Destination filesystem (instance or URL).
        dst_path (str): Path to a directory on the destination filesystem.
        walker (~fs.walk.Walker, optional): A walker object that will be
            used to scan for files in ``src_fs``. Set this if you only
            want to consider a sub-set of the resources in ``src_fs``.
        on_copy (callable, optional):  A function callback called after
            a single file copy is executed. Expected signature is
            ``(src_fs, src_path, dst_fs, dst_path)``.
        workers (int): Use ``worker`` threads to copy data, or ``0`` (default) for
            a single-threaded copy.
        preserve_time (bool): If `True`, try to preserve mtime of the
            resources (defaults to `False`).

    """
    copy_dir_if(
        src_fs,
        src_path,
        dst_fs,
        dst_path,
        "always",
        walker,
        on_copy,
        workers,
        preserve_time=preserve_time,
    )


def copy_dir_if_newer(
    src_fs,  # type: Union[FS, Text]
    src_path,  # type: Text
    dst_fs,  # type: Union[FS, Text]
    dst_path,  # type: Text
    walker=None,  # type: Optional[Walker]
    on_copy=None,  # type: Optional[_OnCopy]
    workers=0,  # type: int
    preserve_time=False,  # type: bool
):
    # type: (...) -> None
    """Copy a directory from one filesystem to another, checking times.

    .. deprecated:: 2.5.0
       Use `~fs.copy.copy_dir_if` with ``condition="newer"`` instead.

    """
    warnings.warn(
        "copy_dir_if_newer is deprecated. Use copy_dir_if instead.", DeprecationWarning
    )
    copy_dir_if(
        src_fs,
        src_path,
        dst_fs,
        dst_path,
        "newer",
        walker,
        on_copy,
        workers,
        preserve_time=preserve_time,
    )


def copy_dir_if(
    src_fs,  # type: Union[FS, Text]
    src_path,  # type: Text
    dst_fs,  # type: Union[FS, Text]
    dst_path,  # type: Text
    condition,  # type: Text
    walker=None,  # type: Optional[Walker]
    on_copy=None,  # type: Optional[_OnCopy]
    workers=0,  # type: int
    preserve_time=False,  # type: bool
):
    # type: (...) -> None
    """Copy a directory from one filesystem to another, depending on a condition.

    Arguments:
        src_fs (FS or str): Source filesystem (instance or URL).
        src_path (str): Path to a directory on the source filesystem.
        dst_fs (FS or str): Destination filesystem (instance or URL).
        dst_path (str): Path to a directory on the destination filesystem.
        condition (str): Name of the condition to check for each file.
        walker (~fs.walk.Walker, optional): A walker object that will be
            used to scan for files in ``src_fs``. Set this if you only want
            to consider a sub-set of the resources in ``src_fs``.
        on_copy (callable):A function callback called after a single file copy
            is executed. Expected signature is ``(src_fs, src_path, dst_fs,
            dst_path)``.
        workers (int): Use ``worker`` threads to copy data, or ``0`` (default) for
            a single-threaded copy.
        preserve_time (bool): If `True`, try to preserve mtime of the
            resources (defaults to `False`).

    See Also:
        `~fs.copy.copy_file_if` for the full list of supported values for the
        ``condition`` argument.

    """
    on_copy = on_copy or (lambda *args: None)
    walker = walker or Walker()
    _src_path = abspath(normpath(src_path))
    _dst_path = abspath(normpath(dst_path))

    from ._bulk import Copier

    copy_structure(src_fs, dst_fs, walker, src_path, dst_path)

    with manage_fs(src_fs, writeable=False) as _src_fs, manage_fs(
        dst_fs, create=True
    ) as _dst_fs:
        with _src_fs.lock(), _dst_fs.lock():
            _thread_safe = is_thread_safe(_src_fs, _dst_fs)
            with Copier(
                num_workers=workers if _thread_safe else 0, preserve_time=preserve_time
            ) as copier:
                for dir_path in walker.files(_src_fs, _src_path):
                    copy_path = combine(_dst_path, frombase(_src_path, dir_path))
                    if _copy_is_necessary(
                        _src_fs, dir_path, _dst_fs, copy_path, condition
                    ):
                        copier.copy(_src_fs, dir_path, _dst_fs, copy_path)
                        on_copy(_src_fs, dir_path, _dst_fs, copy_path)


def _copy_is_necessary(
    src_fs,  # type: FS
    src_path,  # type: Text
    dst_fs,  # type: FS
    dst_path,  # type: Text
    condition,  # type: Text
):
    # type: (...) -> bool

    if condition == "always":
        return True

    elif condition == "newer":
        try:
            src_modified = src_fs.getmodified(src_path)
            dst_modified = dst_fs.getmodified(dst_path)
        except ResourceNotFound:
            return True
        else:
            return (
                src_modified is None
                or dst_modified is None
                or src_modified > dst_modified
            )

    elif condition == "older":
        try:
            src_modified = src_fs.getmodified(src_path)
            dst_modified = dst_fs.getmodified(dst_path)
        except ResourceNotFound:
            return True
        else:
            return (
                src_modified is None
                or dst_modified is None
                or src_modified < dst_modified
            )

    elif condition == "exists":
        return dst_fs.exists(dst_path)

    elif condition == "not_exists":
        return not dst_fs.exists(dst_path)

    else:
        raise ValueError("{} is not a valid copy condition.".format(condition))


def copy_modified_time(
    src_fs,  # type: Union[FS, Text]
    src_path,  # type: Text
    dst_fs,  # type: Union[FS, Text]
    dst_path,  # type: Text
):
    # type: (...) -> None
    """Copy modified time metadata from one file to another.

    Arguments:
        src_fs (FS or str): Source filesystem (instance or URL).
        src_path (str): Path to a directory on the source filesystem.
        dst_fs (FS or str): Destination filesystem (instance or URL).
        dst_path (str): Path to a directory on the destination filesystem.

    """
    namespaces = ("details",)
    with manage_fs(src_fs, writeable=False) as _src_fs:
        with manage_fs(dst_fs, create=True) as _dst_fs:
            src_meta = _src_fs.getinfo(src_path, namespaces)
            src_details = src_meta.raw.get("details", {})
            dst_details = {}
            for value in ("metadata_changed", "modified"):
                if value in src_details:
                    dst_details[value] = src_details[value]
            _dst_fs.setinfo(dst_path, {"details": dst_details})
