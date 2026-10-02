"""Tools for building and comparing directory manifests.

A *manifest* is a content-based description of a directory tree: every
file is recorded with its normalized relative path, size, and a
streaming digest of its contents, while directories and symlinks are
recorded as distinct entry types. Manifests make it possible to verify
a migrated or copied directory tree without relying on timestamps
happening to match.

A manifest is **not** an atomic snapshot of the filesystem. Ordinary
filesystems may be mutated concurrently while a scan is in progress.
To cope with this, file metadata is checked before and after the
contents are hashed; if the metadata changed, the read is retried a
limited number of times, and entries that never stabilize are flagged
as *unstable* rather than silently trusted.
"""

from __future__ import print_function, unicode_literals

import hashlib
import typing

from . import errors
from .path import abspath, combine, normpath

if typing.TYPE_CHECKING:
    from typing import Iterator, List, Optional, Text, Tuple

    from .base import FS


class EntryType(object):
    """Constants for the type of a `ManifestEntry`."""

    FILE = "file"  # type: Text
    DIRECTORY = "directory"  # type: Text
    SYMLINK = "symlink"  # type: Text


class ManifestEntry(object):
    """A single resource recorded in a `Manifest`.

    Attributes:
        path (str): Normalized path relative to the scanned root,
            using forward slashes and no leading slash.
        type (str): One of `EntryType.FILE`, `EntryType.DIRECTORY`,
            or `EntryType.SYMLINK`.
        size (int, optional): Size of the hashed contents in bytes
            (files only).
        digest (str, optional): Hex digest of the file contents
            (files only).
        target (str, optional): Symlink target (symlinks only).
        stable (bool): `False` if the resource kept changing while it
            was being scanned, meaning the recorded content could not
            be verified.
        attempts (int): Number of read attempts made for this entry.
    """

    __slots__ = ("path", "type", "size", "digest", "target", "stable", "attempts")

    def __init__(
        self,
        path,  # type: Text
        type,  # type: Text
        size=None,  # type: Optional[int]
        digest=None,  # type: Optional[Text]
        target=None,  # type: Optional[Text]
        stable=True,  # type: bool
        attempts=0,  # type: int
    ):
        # type: (...) -> None
        self.path = path
        self.type = type
        self.size = size
        self.digest = digest
        self.target = target
        self.stable = stable
        self.attempts = attempts

    def __repr__(self):
        # type: () -> Text
        return "ManifestEntry({!r}, {!r}, size={!r}, digest={!r}, target={!r}, stable={!r})".format(
            self.path, self.type, self.size, self.digest, self.target, self.stable
        )

    @property
    def content_key(self):
        # type: () -> Tuple[Text, Text, Optional[int], Optional[Text], Optional[Text]]
        """tuple: The identity of the recorded content.

        Two entries from different filesystems (or different scans)
        describe the same content if and only if their content keys
        are equal. Stability metadata is deliberately excluded.
        """
        return (self.path, self.type, self.size, self.digest, self.target)

    def __eq__(self, other):
        # type: (object) -> bool
        if not isinstance(other, ManifestEntry):
            return NotImplemented
        return self.content_key == other.content_key

    def __ne__(self, other):
        # type: (object) -> bool
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result


class Manifest(object):
    """An ordered, content-based description of a directory tree.

    Entries are always iterated in sorted path order, so equivalent
    trees scanned from different backends produce identical sequences
    of content records.
    """

    def __init__(
        self,
        entries,  # type: List[ManifestEntry]
        case_insensitive=False,  # type: bool
        case_conflicts=(),  # type: List[Tuple[Text, Text]]
    ):
        # type: (...) -> None
        self._entries = tuple(sorted(entries, key=lambda entry: entry.path))
        self.case_insensitive = case_insensitive
        self.case_conflicts = tuple(case_conflicts)
        self._map = {entry.path: entry for entry in self._entries}

    def __repr__(self):
        # type: () -> Text
        return "Manifest({} entries, {} unstable)".format(len(self), len(self.unstable))

    def __iter__(self):
        # type: () -> Iterator[ManifestEntry]
        return iter(self._entries)

    def __len__(self):
        # type: () -> int
        return len(self._entries)

    def __getitem__(self, path):
        # type: (Text) -> ManifestEntry
        """Get the entry for a normalized relative path."""
        return self._map[path]

    def __contains__(self, path):
        # type: (Text) -> bool
        return path in self._map

    @property
    def entries(self):
        # type: () -> Tuple[ManifestEntry, ...]
        """tuple: All entries, sorted by path."""
        return self._entries

    @property
    def unstable(self):
        # type: () -> Tuple[Text, ...]
        """tuple: Paths of entries that changed during the scan."""
        return tuple(entry.path for entry in self._entries if not entry.stable)

    def content_keys(self):
        # type: () -> Tuple[Tuple, ...]
        """tuple: Content keys of all entries, in stable order."""
        return tuple(entry.content_key for entry in self._entries)

    def __eq__(self, other):
        # type: (object) -> bool
        if not isinstance(other, Manifest):
            return NotImplemented
        return self.content_keys() == other.content_keys()

    def __ne__(self, other):
        # type: (object) -> bool
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def diff(self, other):
        # type: (Manifest) -> ManifestDiff
        """Compare this manifest to another; see `diff_manifests`."""
        return diff_manifests(self, other)


class ManifestDiff(object):
    """The result of comparing two manifests.

    Attributes:
        added (tuple): Paths present only in the new manifest.
        removed (tuple): Paths present only in the old manifest.
        changed (tuple): Paths present in both with the same entry
            type but different content.
        type_conflicts (tuple): Paths present in both but with
            different entry types (e.g. a file replaced by a
            directory).
    """

    def __init__(
        self,
        added=(),  # type: Tuple[Text, ...]
        removed=(),  # type: Tuple[Text, ...]
        changed=(),  # type: Tuple[Text, ...]
        type_conflicts=(),  # type: Tuple[Text, ...]
    ):
        # type: (...) -> None
        self.added = tuple(added)
        self.removed = tuple(removed)
        self.changed = tuple(changed)
        self.type_conflicts = tuple(type_conflicts)

    def __repr__(self):
        # type: () -> Text
        return (
            "ManifestDiff(added={!r}, removed={!r}, changed={!r}, "
            "type_conflicts={!r})"
        ).format(self.added, self.removed, self.changed, self.type_conflicts)

    @property
    def is_empty(self):
        # type: () -> bool
        """bool: `True` if the two manifests describe the same tree."""
        return not (self.added or self.removed or self.changed or self.type_conflicts)

    def __bool__(self):
        # type: () -> bool
        return not self.is_empty

    __nonzero__ = __bool__


def _fingerprint(fs, path):
    # type: (FS, Text) -> Optional[Tuple[Optional[int], object]]
    """Get (size, modified) metadata for a path, or `None` if it vanished."""
    try:
        info = fs.getinfo(path, namespaces=["details"])
    except errors.ResourceNotFound:
        return None
    return (info.get("details", "size", None), info.get("details", "modified", None))


def _hash_file(fs, path, hash_name, chunk_size):
    # type: (FS, Text, Text, int) -> Tuple[Text, int]
    """Stream a file's contents through a hash, returning (digest, size)."""
    try:
        hash_object = hashlib.new(hash_name)
    except ValueError:
        raise errors.UnsupportedHash("hash '{}' is not supported".format(hash_name))
    size = 0
    with fs.openbin(path) as binary_file:
        while True:
            chunk = binary_file.read(chunk_size)
            if not chunk:
                break
            hash_object.update(chunk)
            size += len(chunk)
    return hash_object.hexdigest(), size


def _scan_file(fs, path, rel_path, hash_name, retries, chunk_size):
    # type: (FS, Text, Text, Text, int, int) -> ManifestEntry
    """Hash a file, retrying while its metadata keeps changing."""
    attempts = 0
    digest = None  # type: Optional[Text]
    size = None  # type: Optional[int]
    while True:
        attempts += 1
        before = _fingerprint(fs, path)
        try:
            digest, size = _hash_file(fs, path, hash_name, chunk_size)
        except errors.ResourceNotFound:
            digest, size = None, None
        after = _fingerprint(fs, path)
        if before is not None and before == after:
            return ManifestEntry(
                rel_path, EntryType.FILE, size=size, digest=digest,
                stable=True, attempts=attempts,
            )
        if attempts > retries:
            return ManifestEntry(
                rel_path, EntryType.FILE, size=size, digest=digest,
                stable=False, attempts=attempts,
            )


def _find_case_conflicts(paths):
    # type: (List[Text]) -> List[Tuple[Text, Text]]
    """Find pairs of paths that differ only by letter case."""
    seen = {}
    conflicts = []
    for path in sorted(paths):
        key = path.lower()
        if key in seen and seen[key] != path:
            conflicts.append((seen[key], path))
        else:
            seen.setdefault(key, path)
    return conflicts


def build_manifest(fs, path="/", hash_name="sha256", retries=2, chunk_size=1024 * 1024):
    # type: (FS, Text, Text, int, int) -> Manifest
    """Build a manifest describing the tree at ``path``.

    Arguments:
        fs (FS): A filesystem instance.
        path (str): Root of the tree to scan. Defaults to the
            filesystem root.
        hash_name (str): A `hashlib` algorithm name used to digest
            file contents. Defaults to ``"sha256"``.
        retries (int): Number of times to re-read a file whose
            metadata changes while it is being hashed, before flagging
            the entry as unstable.
        chunk_size (int): Number of bytes to read at a time when
            hashing files.

    Returns:
        Manifest: the scanned manifest. Entries are sorted by path.
        Entries that could not be read consistently are flagged via
        `ManifestEntry.stable` and listed in `Manifest.unstable`;
        paths that collide on case-insensitive backends are listed in
        `Manifest.case_conflicts`.

    Note:
        A manifest is not an atomic snapshot; a plain filesystem may
        change while it is scanned. Always check `Manifest.unstable`
        before trusting a comparison.
    """
    root_path = abspath(normpath(path))
    entries = []  # type: List[ManifestEntry]

    def visit(dir_path, rel_prefix):
        # type: (Text, Text) -> None
        for info in fs.scandir(dir_path, namespaces=["details", "link"]):
            rel_path = combine(rel_prefix, info.name) if rel_prefix else info.name
            target = info.get("link", "target", None)
            if target is not None:
                entries.append(
                    ManifestEntry(rel_path, EntryType.SYMLINK, target=target)
                )
            elif info.is_dir:
                entries.append(ManifestEntry(rel_path, EntryType.DIRECTORY))
                visit(combine(dir_path, info.name), rel_path)
            else:
                entries.append(
                    _scan_file(
                        fs,
                        combine(dir_path, info.name),
                        rel_path,
                        hash_name,
                        retries,
                        chunk_size,
                    )
                )

    visit(root_path, "")
    case_insensitive = bool(fs.getmeta().get("case_insensitive", False))
    case_conflicts = _find_case_conflicts([entry.path for entry in entries])
    return Manifest(
        entries, case_insensitive=case_insensitive, case_conflicts=case_conflicts
    )


def diff_manifests(old, new):
    # type: (Manifest, Manifest) -> ManifestDiff
    """Compare two manifests.

    Arguments:
        old (Manifest): The reference manifest.
        new (Manifest): The manifest to verify against ``old``.

    Returns:
        ManifestDiff: paths that were added, removed, changed in
        content, or changed in entry type. Results involving unstable
        entries should be treated as unreliable, since their content
        was never read consistently.
    """
    old_map = {entry.path: entry for entry in old}
    new_map = {entry.path: entry for entry in new}
    added = tuple(sorted(path for path in new_map if path not in old_map))
    removed = tuple(sorted(path for path in old_map if path not in new_map))
    changed = []
    type_conflicts = []
    for path in sorted(set(old_map).intersection(new_map)):
        old_entry = old_map[path]
        new_entry = new_map[path]
        if old_entry.type != new_entry.type:
            type_conflicts.append(path)
        elif old_entry.content_key != new_entry.content_key:
            changed.append(path)
    return ManifestDiff(
        added=added,
        removed=removed,
        changed=tuple(changed),
        type_conflicts=tuple(type_conflicts),
    )
