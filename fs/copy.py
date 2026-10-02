"""Functions for copying resources *between* filesystem.
"""

from __future__ import print_function, unicode_literals

import hashlib
import typing
import uuid

import warnings

from .errors import (
    DestinationExists,
    FileExpected,
    IllegalDestination,
    OperationFailed,
    ResourceNotFound,
    Unsupported,
    UnsupportedHash,
)
from .opener import manage_fs
from .path import abspath, basename, combine, dirname, frombase, isbase, normpath
from .tools import is_thread_safe
from .walk import Walker

if typing.TYPE_CHECKING:
    from typing import Callable, Optional, Text, Union

    from .base import FS

    _OnCopy = Callable[[FS, Text, FS, Text], object]

    _OnProgress = Callable[[int, int], object]


class CopyCancelled(OperationFailed):
    """The copy was cancelled by the progress callback."""

    default_message = "copy cancelled"


class CopyVerificationFailed(OperationFailed):
    """The copied data did not verify against the source."""

    default_message = "copied data failed verification"


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


def copy_file_atomic(
    src_fs,  # type: Union[FS, Text]
    src_path,  # type: Text
    dst_fs,  # type: Union[FS, Text]
    dst_path,  # type: Text
    progress=None,  # type: Optional[_OnProgress]
    chunk_size=None,  # type: Optional[int]
    hash_name="sha256",  # type: Text
    overwrite=True,  # type: bool
    allow_non_atomic=False,  # type: bool
):
    # type: (...) -> int
    """Copy a file to a temporary file on the destination, then publish it.

    The data is first streamed to a uniquely named temporary file in the
    same directory as ``dst_path``. Once every byte has been written and
    the temporary file verifies against the source, it is moved over
    ``dst_path``. A pre-existing destination file is therefore left
    untouched until the copy has completed successfully, and any failure
    (including cancellation) removes only the temporary file created by
    this call.

    Arguments:
        src_fs (FS or str): Source filesystem (instance or URL).
        src_path (str): Path to a file on the source filesystem.
        dst_fs (FS or str): Destination filesystem (instance or URL).
        dst_path (str): Path to a file on the destination filesystem.
        progress (callable, optional): Called as ``progress(written,
            total)`` after bytes have been written to the destination,
            where ``written`` is the number of bytes actually written so
            far and ``total`` is the size of the source file. Return
            ``False`` to cancel the copy, which raises
            `~fs.copy.CopyCancelled`. Exceptions raised by the callback
            propagate after the same cleanup as any other failure.
        chunk_size (int, optional): Number of bytes to copy at a time
            (defaults to 1MB).
        hash_name (str): Name of the `hashlib` algorithm used to verify
            the temporary file against the source (defaults to
            ``"sha256"``).
        overwrite (bool): If `True` (default), an existing destination
            file is replaced when the copy is published. If `False` and
            ``dst_path`` exists, `~fs.errors.DestinationExists` is raised
            before any data is copied.
        allow_non_atomic (bool): Permit publishing on filesystems that
            do not report ``supports_rename`` in their meta. On such
            backends the final move degrades to a copy followed by a
            remove, so replacing an existing destination is *not*
            atomic. Defaults to `False`, which refuses the copy with
            `~fs.errors.Unsupported`.

    Returns:
        int: The number of bytes written and published.

    Raises:
        fs.errors.IllegalDestination: If source and destination are the
            same file.
        fs.errors.FileExpected: If ``dst_path`` is a directory.
        fs.errors.DestinationExists: If ``dst_path`` exists and
            ``overwrite`` is `False`.
        fs.errors.Unsupported: If the destination filesystem cannot
            replace files atomically and ``allow_non_atomic`` is `False`.
        fs.errors.UnsupportedHash: If ``hash_name`` is not supported.
        fs.copy.CopyCancelled: If the ``progress`` callback requests
            cancellation.
        fs.copy.CopyVerificationFailed: If the temporary file does not
            match the source after copying.

    """
    with manage_fs(src_fs, writeable=False) as _src_fs:
        with manage_fs(dst_fs, create=True) as _dst_fs:
            _src_path = _src_fs.validatepath(src_path)
            _dst_path = _dst_fs.validatepath(dst_path)

            if _src_fs is _dst_fs and _src_path == _dst_path:
                raise IllegalDestination(dst_path)
            if _dst_fs.isdir(_dst_path):
                raise FileExpected(dst_path)
            if not overwrite and _dst_fs.exists(_dst_path):
                raise DestinationExists(dst_path)
            if not allow_non_atomic and not _dst_fs.getmeta().get(
                "supports_rename", False
            ):
                raise Unsupported(
                    dst_path,
                    msg="destination filesystem does not support atomic "
                    "rename; pass allow_non_atomic=True to publish "
                    "non-atomically",
                )
            try:
                hash_object = hashlib.new(hash_name)
            except ValueError:
                raise UnsupportedHash(
                    "hash '{}' is not supported".format(hash_name)
                )

            total = _src_fs.getsize(_src_path)
            _chunk_size = 1024 * 1024 if chunk_size is None else chunk_size

            _dst_dir = dirname(_dst_path)
            _dst_name = basename(_dst_path)
            temp_path = None
            for _ in range(100):
                candidate = combine(
                    _dst_dir, ".{}.{}.tmp".format(_dst_name, uuid.uuid4().hex)
                )
                if not _dst_fs.exists(candidate):
                    temp_path = candidate
                    break
            if temp_path is None:
                raise OperationFailed(
                    dst_path, msg="unable to allocate a temporary path"
                )

            written = 0
            published = False
            try:
                with _src_fs.openbin(_src_path) as read_file:
                    with _dst_fs.openbin(temp_path, "w") as write_file:
                        write = write_file.write
                        while True:
                            chunk = read_file.read(_chunk_size)
                            if not chunk:
                                break
                            hash_object.update(chunk)
                            offset = 0
                            while offset < len(chunk):
                                num_written = write(chunk[offset:])
                                if num_written is None:
                                    num_written = len(chunk) - offset
                                if num_written <= 0:
                                    raise OperationFailed(
                                        dst_path,
                                        msg="destination write made no progress",
                                    )
                                offset += num_written
                                written += num_written
                                if (
                                    progress is not None
                                    and progress(written, total) is False
                                ):
                                    raise CopyCancelled(
                                        dst_path,
                                        msg="copy cancelled by progress callback",
                                    )

                if _dst_fs.hash(temp_path, hash_name) != hash_object.hexdigest():
                    raise CopyVerificationFailed(
                        dst_path,
                        msg="hash of temporary file does not match source",
                    )

                _dst_fs.move(temp_path, _dst_path, overwrite=True)
                published = True
            finally:
                if not published:
                    try:
                        _dst_fs.remove(temp_path)
                    except ResourceNotFound:
                        pass

            return written


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
