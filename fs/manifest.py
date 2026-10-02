"""Generate and compare filesystem content manifests.

A *manifest* is a stable, content-based description of a directory
tree. It is intended for verifying that a directory was migrated
(copied) correctly, without relying on timestamps being preserved.

Each file is recorded with its normalized relative path, size, and a
streaming content hash. Directories and symlinks are recorded as
distinct entry types. Two manifests may be compared with
`compare_manifests`, which reports added, removed, and changed
resources, as well as type conflicts.

Note:
    A manifest is *not* an atomic snapshot. Filesystems do not
    generally provide atomic snapshots of a directory tree, so a
    resource may change while it is being scanned. Such resources are
    detected by comparing metadata before and after reading, retried
    a limited number of times, and finally flagged as *unstable* in
    the manifest. Callers should treat unstable entries as
    inconclusive rather than as proof of a failed migration.

"""

from __future__ import print_function, unicode_literals

import hashlib
import typing

from . import errors
from .path import normpath, relpath

if typing.TYPE_CHECKING:
    from typing import Iterator, List, Optional, Text, Tuple

    from .base import FS


#: Entry type for a regular file.
TYPE_FILE = "file"
#: Entry type for a directory.
TYPE_DIRECTORY = "directory"
#: Entry type for a symlink.
TYPE_SYMLINK = "symlink"

_DEFAULT_CHUNK_SIZE = 1024 * 1024


class ManifestEntry(object):
    """A single resource record within a `Manifest`.

    Attributes:
        path (str): Normalized relative path (forward slashes, no
            leading slash) of the resource, relative to the scanned
            root.
        type (str): One of ``"file"``, ``"directory"``, or
            ``"symlink"``.
        size (int or None): Size of a file in bytes, or `None` for
            non-file resources and unstable records.
        digest (str or None): Hex digest of a file's contents, or
            `None` for non-file resources and files that could not be
            read.
        link_target (str or None): Target of a symlink, or `None` for
            other resource types.
        stable (bool): `False` if the resource changed (or vanished)
            while it was being scanned, in which case the recorded
            values are best-effort and should not be trusted for
            verification.

    """

    __slots__ = ("path", "type", "size", "digest", "link_target", "stable")

    def __init__(
        self, path, type, size=None, digest=None, link_target=None, stable=True
    ):
        # type: (Text, Text, Optional[int], Optional[Text], Optional[Text], bool) -> None  # noqa: E501
        self.path = path
        self.type = type
        self.size = size
        self.digest = digest
        self.link_target = link_target
        self.stable = stable

    def __repr__(self):
        # type: () -> Text
        return (
            "ManifestEntry({!r}, {!r}, size={!r}, digest={!r}, "
            "link_target={!r}, stable={!r})"
        ).format(
            self.path, self.type, self.size, self.digest, self.link_target, self.stable
        )

    def __eq__(self, other):
        # type: (object) -> bool
        """Compare content records (stability is not identity)."""
        if not isinstance(other, ManifestEntry):
            return NotImplemented
        return (
            self.path == other.path
            and self.type == other.type
            and self.size == other.size
            and self.digest == other.digest
            and self.link_target == other.link_target
        )

    def __ne__(self, other):
        # type: (object) -> bool
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def content_matches(self, other):
        # type: (ManifestEntry) -> bool
        """Check if the recorded content of two entries is identical."""
        return (
            self.size == other.size
            and self.digest == other.digest
            and self.link_target == other.link_target
        )


class Manifest(object):
    """An immutable, content-based description of a directory tree.

    Attributes:
        entries (tuple): `ManifestEntry` records, sorted by path.
        hash_name (str): Name of the hash algorithm used for file
            digests.
        case_insensitive (bool): Whether the scanned filesystem
            reported itself as case-insensitive.
        warnings (tuple): Diagnostic messages, e.g. case conflicts
            detected on a case-insensitive backend.

    """

    def __init__(self, entries, hash_name, case_insensitive=False, warnings=()):
        # type: (List[ManifestEntry], Text, bool, List[Text]) -> None
        self.entries = tuple(
            sorted(entries, key=lambda entry: entry.path)
        )  # type: Tuple[ManifestEntry, ...]
        self.hash_name = hash_name
        self.case_insensitive = case_insensitive
        self.warnings = tuple(sorted(warnings))  # type: Tuple[Text, ...]

    def __repr__(self):
        # type: () -> Text
        return "Manifest({} entries, hash={!r}, stable={!r})".format(
            len(self.entries), self.hash_name, self.stable
        )

    def __iter__(self):
        # type: () -> Iterator[ManifestEntry]
        return iter(self.entries)

    def __len__(self):
        # type: () -> int
        return len(self.entries)

    def __eq__(self, other):
        # type: (object) -> bool
        if not isinstance(other, Manifest):
            return NotImplemented
        return self.hash_name == other.hash_name and self.entries == other.entries

    def __ne__(self, other):
        # type: (object) -> bool
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    @property
    def stable(self):
        # type: () -> bool
        """`True` if every entry was stable while being scanned."""
        return all(entry.stable for entry in self.entries)

    @property
    def unstable(self):
        # type: () -> Tuple[Text, ...]
        """Tuple of paths that changed while being scanned."""
        return tuple(entry.path for entry in self.entries if not entry.stable)

    @property
    def paths(self):
        # type: () -> Tuple[Text, ...]
        """Tuple of all recorded paths, in stable (sorted) order."""
        return tuple(entry.path for entry in self.entries)

    def get(self, path):
        # type: (Text) -> Optional[ManifestEntry]
        """Get the entry for ``path``, or `None` if not recorded."""
        _path = relpath(normpath(path))
        for entry in self.entries:
            if entry.path == _path:
                return entry
        return None


class ManifestDiff(object):
    """The result of comparing two manifests.

    Attributes:
        added (tuple): Paths only present in the new manifest.
        removed (tuple): Paths only present in the old manifest.
        changed (tuple): Paths present in both manifests, with the
            same entry type, but different content records.
        type_conflicts (tuple): Paths present in both manifests, but
            with different entry types (e.g. a file replaced by a
            directory).
        unstable (tuple): Paths flagged as unstable in either
            manifest; comparisons involving these are inconclusive.

    """

    def __init__(self, added, removed, changed, type_conflicts, unstable):
        # type: (List[Text], List[Text], List[Text], List[Text], List[Text]) -> None
        self.added = tuple(sorted(added))  # type: Tuple[Text, ...]
        self.removed = tuple(sorted(removed))  # type: Tuple[Text, ...]
        self.changed = tuple(sorted(changed))  # type: Tuple[Text, ...]
        self.type_conflicts = tuple(sorted(type_conflicts))  # type: Tuple[Text, ...]
        self.unstable = tuple(sorted(unstable))  # type: Tuple[Text, ...]

    def __repr__(self):
        # type: () -> Text
        return (
            "ManifestDiff(added={!r}, removed={!r}, changed={!r}, "
            "type_conflicts={!r}, unstable={!r})"
        ).format(
            self.added, self.removed, self.changed, self.type_conflicts, self.unstable
        )

    @property
    def equal(self):
        # type: () -> bool
        """`True` if the two manifests describe identical trees."""
        return not (self.added or self.removed or self.changed or self.type_conflicts)


def _metadata_signature(info):
    # type: (...) -> Tuple[Optional[int], object]
    """Extract the metadata used to detect changes during a scan."""
    return (
        info.get("details", "size", None),
        info.get("details", "modified", None),
    )


def _hash_file(fs, path, hash_name, retries, chunk_size):
    # type: (FS, Text, Text, int, int) -> Tuple[Optional[Text], Optional[int], bool]
    """Hash a file, retrying while its metadata changes mid-read.

    Returns a tuple of ``(digest, size, stable)``. ``stable`` is
    `False` if the file kept changing (or vanished) after all
    retries, in which case ``digest`` and ``size`` are best-effort
    values from the last attempt.

    """
    digest = None  # type: Optional[Text]
    size = None  # type: Optional[int]
    for _attempt in range(retries + 1):
        try:
            before = _metadata_signature(fs.getinfo(path, namespaces=["details"]))
            hash_object = hashlib.new(hash_name)
            bytes_read = 0
            with fs.openbin(path) as file_obj:
                while True:
                    chunk = file_obj.read(chunk_size)
                    if not chunk:
                        break
                    hash_object.update(chunk)
                    bytes_read += len(chunk)
            after = _metadata_signature(fs.getinfo(path, namespaces=["details"]))
        except errors.ResourceNotFound:
            # The file vanished mid-scan; no point retrying.
            return None, None, False
        digest = hash_object.hexdigest()
        size = bytes_read
        expected_size = before[0]
        if before == after and (expected_size is None or expected_size == bytes_read):
            return digest, size, True
    return digest, size, False


def generate_manifest(fs, path="/", hash_name="sha256", retries=2, chunk_size=None):
    # type: (FS, Text, Text, int, Optional[int]) -> Manifest
    """Generate a content manifest of a directory tree.

    Arguments:
        fs (FS): A filesystem instance.
        path (str): Path to a directory on the filesystem to scan.
        hash_name (str): Hash algorithm for file digests; one of the
            algorithms supported by the `hashlib` module, e.g.
            ``"md5"`` or ``"sha256"``.
        retries (int): Number of times to re-read a file whose
            metadata changes while it is being hashed, before
            flagging it as unstable.
        chunk_size (int, optional): Number of bytes to read at a time
            (defaults to 1 MiB).

    Returns:
        Manifest: A manifest with entries in stable (sorted) order.

    Raises:
        fs.errors.UnsupportedHash: If the requested hash is not
            supported.
        fs.errors.DirectoryNotFound: If ``path`` does not exist.

    Note:
        A manifest is not an atomic snapshot of the filesystem.
        Resources that change during the scan are flagged as unstable
        (see `ManifestEntry.stable` and `Manifest.stable`).

    """
    try:
        hashlib.new(hash_name)
    except ValueError:
        raise errors.UnsupportedHash("hash '{}' is not supported".format(hash_name))
    _chunk_size = _DEFAULT_CHUNK_SIZE if chunk_size is None else chunk_size
    case_insensitive = bool(fs.getmeta().get("case_insensitive", False))

    entries = []  # type: List[ManifestEntry]
    for resource_path, info in fs.walk.info(path, namespaces=["details", "link"]):
        rel_path = relpath(normpath(resource_path))
        link_target = info.get("link", "target", None)
        if link_target is not None:
            entries.append(
                ManifestEntry(rel_path, TYPE_SYMLINK, link_target=link_target)
            )
        elif info.is_dir:
            entries.append(ManifestEntry(rel_path, TYPE_DIRECTORY))
        else:
            digest, size, stable = _hash_file(
                fs, resource_path, hash_name, retries, _chunk_size
            )
            entries.append(
                ManifestEntry(
                    rel_path, TYPE_FILE, size=size, digest=digest, stable=stable
                )
            )

    warnings = []  # type: List[Text]
    if case_insensitive:
        by_folded = {}  # type: dict
        for entry in entries:
            by_folded.setdefault(entry.path.lower(), []).append(entry.path)
        for folded, paths in by_folded.items():
            if len(paths) > 1:
                warnings.append(
                    "case conflict on case-insensitive filesystem: {}".format(
                        ", ".join(sorted(paths))
                    )
                )

    return Manifest(
        entries,
        hash_name,
        case_insensitive=case_insensitive,
        warnings=warnings,
    )


def compare_manifests(old, new):
    # type: (Manifest, Manifest) -> ManifestDiff
    """Compare two manifests.

    Arguments:
        old (Manifest): The reference manifest.
        new (Manifest): The manifest to check against ``old``.

    Returns:
        ManifestDiff: The differences between the two manifests.

    Raises:
        ValueError: If the manifests use different hash algorithms.

    """
    if old.hash_name != new.hash_name:
        raise ValueError(
            "cannot compare manifests hashed with {!r} and {!r}".format(
                old.hash_name, new.hash_name
            )
        )
    old_map = {entry.path: entry for entry in old.entries}
    new_map = {entry.path: entry for entry in new.entries}

    added = [path for path in new_map if path not in old_map]
    removed = [path for path in old_map if path not in new_map]
    changed = []
    type_conflicts = []
    for path in old_map:
        if path not in new_map:
            continue
        old_entry = old_map[path]
        new_entry = new_map[path]
        if old_entry.type != new_entry.type:
            type_conflicts.append(path)
        elif not old_entry.content_matches(new_entry):
            changed.append(path)

    unstable = {entry.path for entry in old.entries if not entry.stable}
    unstable |= {entry.path for entry in new.entries if not entry.stable}

    return ManifestDiff(added, removed, changed, type_conflicts, unstable)
