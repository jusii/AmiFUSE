"""In-process two-bridge recursive copy engine with metadata preservation.

The copy engine speaks AmigaDOS packets through two HandlerBridge instances
running in the same Python process. Source files are read via the source
bridge, written to the destination bridge, and metadata (protection bits,
filenote comments, datestamps) is applied via the Phase 1 SET_* packet
senders. No FUSE/kernel round-trip is involved, so the throughput ceiling
is the handler's own packet-processing rate plus host disk I/O.

The engine is filesystem-agnostic: any pair of filesystems with working
native handlers in AmiFUSE works (PFS3 ↔ PFS3, PFS3 ↔ FFS, SFS → PFS3,
read-only CDFS/OD as sources, etc.). Differences live inside each handler.

Atomicity: writes go to a temp filename (``.amifuse-tmp.<pid>.<name>``)
which is renamed to the final name only after content + metadata are in
place. A crash mid-copy leaves a temp stub, never a half-written real
file. Disable with ``atomic=False`` if the temp+rename overhead matters
more than the safety.

Progress: callers receive :class:`CopyProgress` events per file via the
``on_progress`` callback. Use this to drive progress bars or to log slow
files in installer pipelines.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from amitools.fs.FSString import FSString  # type: ignore
from amitools.fs.MetaInfo import MetaInfo  # type: ignore
from amitools.fs.ProtectFlags import ProtectFlags  # type: ignore
from amitools.fs.TimeStamp import TimeStamp  # type: ignore


# AmigaDOS DirEntryType values from <dos/dos.h>
ST_ROOT = 1
ST_USERDIR = 2
ST_SOFTLINK = 3
ST_LINKDIR = 4  # hard link to a directory
ST_FILE = -3
ST_LINKFILE = -4  # hard link to a file

LINK_TYPES = frozenset({ST_SOFTLINK, ST_LINKDIR, ST_LINKFILE})

# Conservative default — PFS3 / SFS / BFFS allow up to 107 chars; FFS only 30.
# Callers targeting FFS should pass ``max_filename_len=30``.
DEFAULT_MAX_FILENAME_LEN = 107

# I/O chunk size. Larger reduces packet overhead at the cost of memory.
DEFAULT_CHUNK_SIZE = 256 * 1024


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CopyStats:
    """Aggregate results of a copy_tree / copy_file invocation."""

    files_copied: int = 0
    dirs_copied: int = 0
    bytes_copied: int = 0
    files_skipped: int = 0
    links_skipped: int = 0
    errors: List[str] = field(default_factory=list)
    elapsed_secs: float = 0.0


@dataclass
class CopyProgress:
    """Per-file progress event passed to the on_progress callback."""

    current_path: str
    current_op: str  # "scan" | "mkdir" | "copy" | "meta" | "rename" | "skip"
    bytes_in_file: int = 0
    bytes_done_in_file: int = 0
    files_done: int = 0


# ---------------------------------------------------------------------------
# MetaInfo construction from a parsed FIB
# ---------------------------------------------------------------------------


def meta_info_from_fib(entry: Dict) -> MetaInfo:
    """Build an amitools MetaInfo from an extended _parse_fib dict.

    The dict must carry the fields _parse_fib produces after the Phase 3
    extension: ``protection``, ``date_days``, ``date_mins``, ``date_ticks``,
    ``comment``. Empty comments stay None so apply_meta skips emitting an
    empty SET_COMMENT packet.

    A datestamp is always set on the result — an all-zero (days/mins/ticks)
    input becomes a TimeStamp at the Amiga epoch (1978-01-01), which is
    the natural sentinel for "never explicitly dated". Sidecar formats
    (.uaem, xdfmeta) require a parseable timestamp, so we can't leave
    mod_ts as None.
    """
    protect = ProtectFlags(entry.get("protection", 0))
    days = entry.get("date_days", 0)
    mins = entry.get("date_mins", 0)
    ticks = entry.get("date_ticks", 0)
    ts = TimeStamp(days=days, mins=mins, ticks=ticks)
    comment_text = entry.get("comment", "")
    comment = FSString(comment_text) if comment_text else None
    return MetaInfo(protect_flags=protect, mod_ts=ts, comment=comment)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _entry_is_dir(entry: Dict) -> bool:
    dt = entry.get("dir_type", 0)
    # Positive dir_type means directory (USERDIR=2, ROOT=1), but
    # ST_SOFTLINK=3 is also positive — exclude links explicitly.
    return dt > 0 and dt not in LINK_TYPES


def _entry_is_link(entry: Dict) -> bool:
    return entry.get("dir_type", 0) in LINK_TYPES


def _posix_join(parent: str, child: str) -> str:
    """Join an Amiga-style absolute path with a child name."""
    if not parent or parent == "/":
        return "/" + child
    return parent.rstrip("/") + "/" + child


def _temp_name(name: str) -> str:
    return f".amifuse-tmp.{os.getpid()}.{name}"


def _split_path(path: str):
    """Split an absolute Amiga path into (dir, basename).

    Mirrors AmigaFuseFS._split_path so the copy engine doesn't depend on
    the FUSE class.
    """
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "/", ""
    name = parts[-1]
    dir_path = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
    return dir_path, name


# ---------------------------------------------------------------------------
# Public copy API
# ---------------------------------------------------------------------------


def copy_file(
    src_bridge,
    src_path: str,
    dst_bridge,
    dst_path: str,
    *,
    preserve: bool = True,
    atomic: bool = True,
    overwrite: bool = True,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    on_progress: Optional[Callable[[CopyProgress], None]] = None,
) -> CopyStats:
    """Copy one file from src to dst, preserving Amiga metadata.

    Both paths are absolute Amiga paths inside their respective images.
    Returns a CopyStats describing the single-file outcome.

    overwrite=False raises FileExistsError if dst exists.
    atomic=True writes to a temp file and renames atomically on success.
    preserve=True applies protection/comment/datestamp via apply_meta.
    """
    start = time.monotonic()
    stats = CopyStats()

    src_info = src_bridge.stat_path(src_path)
    if src_info is None:
        raise FileNotFoundError(f"source not found: {src_path}")
    if _entry_is_dir(src_info):
        raise IsADirectoryError(f"source is a directory: {src_path}")
    if _entry_is_link(src_info):
        stats.links_skipped += 1
        stats.elapsed_secs = time.monotonic() - start
        return stats

    dst_existing = dst_bridge.stat_path(dst_path)
    if dst_existing is not None and not overwrite:
        raise FileExistsError(f"destination exists: {dst_path}")

    # Decide actual on-disk name during write
    dst_dir, dst_name = _split_path(dst_path)
    write_name = _temp_name(dst_name) if atomic else dst_name
    write_path = _posix_join(dst_dir, write_name)

    # If the temp file or final exists from a prior failed run, remove it.
    if atomic:
        leftover = dst_bridge.stat_path(write_path)
        if leftover is not None:
            _unlink_path(dst_bridge, write_path)
    if overwrite and dst_existing is not None and not atomic:
        _unlink_path(dst_bridge, dst_path)

    # Stream content
    bytes_copied = 0
    src_fh_result = src_bridge.open_file(src_path)
    if src_fh_result is None:
        raise OSError(f"failed to open source: {src_path}")
    src_fh, _ = src_fh_result
    try:
        dst_fh_result = dst_bridge.open_file(
            write_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        )
        if dst_fh_result is None:
            raise OSError(f"failed to open destination for write: {write_path}")
        dst_fh, _ = dst_fh_result
        try:
            src_bridge.seek_handle(src_fh, 0)
            total = src_info.get("size", 0)
            while True:
                chunk = src_bridge.read_handle(src_fh, chunk_size)
                if not chunk:
                    break
                n = dst_bridge.write_handle(dst_fh, chunk)
                if n < 0:
                    raise OSError(
                        f"write failed at offset {bytes_copied} for {dst_path}"
                    )
                if n < len(chunk):
                    raise OSError(
                        f"partial write {n}/{len(chunk)} (disk full?) at "
                        f"{bytes_copied} for {dst_path}"
                    )
                bytes_copied += n
                if on_progress is not None:
                    on_progress(
                        CopyProgress(
                            current_path=dst_path,
                            current_op="copy",
                            bytes_in_file=total,
                            bytes_done_in_file=bytes_copied,
                        )
                    )
        finally:
            dst_bridge.close_file(dst_fh)
    finally:
        src_bridge.close_file(src_fh)

    # Apply metadata to the temp/final file
    if preserve:
        meta = meta_info_from_fib(src_info)
        target_meta_path = write_path
        dst_bridge.apply_meta_at_path(target_meta_path, meta)
        if on_progress is not None:
            on_progress(
                CopyProgress(current_path=dst_path, current_op="meta")
            )

    # Atomic rename into final location
    if atomic:
        if dst_existing is not None:
            # Remove any pre-existing target before renaming over it
            _unlink_path(dst_bridge, dst_path)
        _rename_path(dst_bridge, write_path, dst_path)
        if on_progress is not None:
            on_progress(
                CopyProgress(current_path=dst_path, current_op="rename")
            )

    stats.files_copied = 1
    stats.bytes_copied = bytes_copied
    stats.elapsed_secs = time.monotonic() - start
    return stats


def copy_tree(
    src_bridge,
    src_root: str,
    dst_bridge,
    dst_root: str,
    *,
    preserve: bool = True,
    atomic: bool = True,
    on_conflict: str = "overwrite",  # "overwrite" | "skip" | "error"
    on_error: str = "abort",  # "abort" | "skip"
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_filename_len: int = DEFAULT_MAX_FILENAME_LEN,
    on_progress: Optional[Callable[[CopyProgress], None]] = None,
) -> CopyStats:
    """Recursively copy src_root to dst_root, preserving metadata.

    Both roots must be absolute Amiga paths. ``dst_root`` may or may not
    exist; if absent, it is created. The recursion does a depth-first
    walk; directories are created on dst before their children are copied.

    on_conflict controls behavior when a destination entry already exists:
      "overwrite" — replace it (default)
      "skip"      — leave dst as-is, record as skipped
      "error"     — raise FileExistsError

    on_error controls behavior when a single file's copy fails:
      "abort" — re-raise the error (default)
      "skip"  — record in stats.errors and continue
    """
    if on_conflict not in ("overwrite", "skip", "error"):
        raise ValueError(f"invalid on_conflict: {on_conflict!r}")
    if on_error not in ("abort", "skip"):
        raise ValueError(f"invalid on_error: {on_error!r}")

    start = time.monotonic()
    stats = CopyStats()

    src_root_info = src_bridge.stat_path(src_root)
    if src_root_info is None:
        raise FileNotFoundError(f"source root not found: {src_root}")
    if not _entry_is_dir(src_root_info):
        raise NotADirectoryError(f"source root is not a directory: {src_root}")

    # Ensure dst_root exists; apply source root's metadata to it whether
    # we just created it or it already existed (caller asked for a full
    # copy, so root metadata is part of that).
    dst_root_info = dst_bridge.stat_path(dst_root)
    if dst_root_info is None:
        _create_dir_path(dst_bridge, dst_root)
    elif not _entry_is_dir(dst_root_info):
        raise NotADirectoryError(
            f"destination root exists and is not a directory: {dst_root}"
        )
    if preserve and dst_root != "/":
        root_meta = meta_info_from_fib(src_root_info)
        dst_bridge.apply_meta_at_path(dst_root, root_meta)

    # Walk
    _copy_dir_contents(
        src_bridge, src_root,
        dst_bridge, dst_root,
        stats=stats,
        preserve=preserve,
        atomic=atomic,
        on_conflict=on_conflict,
        on_error=on_error,
        chunk_size=chunk_size,
        max_filename_len=max_filename_len,
        on_progress=on_progress,
    )

    # Flush destination after a successful tree copy
    dst_bridge.flush_volume()

    stats.elapsed_secs = time.monotonic() - start
    return stats


# ---------------------------------------------------------------------------
# Recursive driver
# ---------------------------------------------------------------------------


def _copy_dir_contents(
    src_bridge, src_dir,
    dst_bridge, dst_dir,
    *,
    stats: CopyStats,
    preserve: bool,
    atomic: bool,
    on_conflict: str,
    on_error: str,
    chunk_size: int,
    max_filename_len: int,
    on_progress: Optional[Callable[[CopyProgress], None]],
) -> None:
    entries = src_bridge.list_dir_path(src_dir)
    for entry in entries:
        name = entry.get("name", "")
        if not name:
            continue
        if len(name) > max_filename_len:
            msg = (
                f"filename exceeds destination FS limit "
                f"({len(name)} > {max_filename_len}): {_posix_join(src_dir, name)}"
            )
            stats.errors.append(msg)
            if on_error == "abort":
                raise OSError(msg)
            continue

        src_child = _posix_join(src_dir, name)
        dst_child = _posix_join(dst_dir, name)

        if _entry_is_link(entry):
            stats.links_skipped += 1
            if on_progress is not None:
                on_progress(
                    CopyProgress(current_path=src_child, current_op="skip")
                )
            continue

        is_dir = _entry_is_dir(entry)
        try:
            if is_dir:
                _copy_one_dir(
                    src_bridge, src_child,
                    dst_bridge, dst_dir, name, dst_child,
                    entry=entry,
                    stats=stats,
                    preserve=preserve,
                    atomic=atomic,
                    on_conflict=on_conflict,
                    on_error=on_error,
                    chunk_size=chunk_size,
                    max_filename_len=max_filename_len,
                    on_progress=on_progress,
                )
            else:
                _copy_one_file(
                    src_bridge, src_child,
                    dst_bridge, dst_child,
                    stats=stats,
                    preserve=preserve,
                    atomic=atomic,
                    on_conflict=on_conflict,
                    chunk_size=chunk_size,
                    on_progress=on_progress,
                )
        except FileExistsError:
            raise  # always propagates regardless of on_error
        except Exception as exc:
            stats.errors.append(f"{src_child}: {exc}")
            if on_error == "abort":
                raise


def _copy_one_dir(
    src_bridge, src_path,
    dst_bridge, dst_parent, name, dst_path,
    *,
    entry: Dict,
    stats: CopyStats,
    preserve: bool,
    atomic: bool,
    on_conflict: str,
    on_error: str,
    chunk_size: int,
    max_filename_len: int,
    on_progress: Optional[Callable[[CopyProgress], None]],
) -> None:
    dst_existing = dst_bridge.stat_path(dst_path)
    if dst_existing is not None:
        if _entry_is_dir(dst_existing):
            # Directory already exists — recurse into it; apply meta only if
            # we're overwriting.
            if on_conflict == "error":
                raise FileExistsError(f"destination dir exists: {dst_path}")
            # Skip and overwrite both recurse; metadata application differs.
            if on_conflict == "overwrite" and preserve:
                meta = meta_info_from_fib(entry)
                dst_bridge.apply_meta_at_path(dst_path, meta)
        else:
            # Existing entry is a file. We don't replace files with dirs in
            # MVP — that's a confusing semantic. Treat as an error.
            raise FileExistsError(
                f"destination exists as file, source is dir: {dst_path}"
            )
    else:
        # Create directory under parent
        new_lock = _create_dir_under(dst_bridge, dst_parent, name)
        if new_lock == 0:
            raise OSError(f"create_dir failed for {dst_path}")
        # We don't need the new lock for child operations (we use paths);
        # free it immediately.
        dst_bridge.free_lock(new_lock)
        if preserve:
            meta = meta_info_from_fib(entry)
            dst_bridge.apply_meta_at_path(dst_path, meta)

    stats.dirs_copied += 1
    if on_progress is not None:
        on_progress(CopyProgress(current_path=dst_path, current_op="mkdir"))

    _copy_dir_contents(
        src_bridge, src_path,
        dst_bridge, dst_path,
        stats=stats,
        preserve=preserve,
        atomic=atomic,
        on_conflict=on_conflict,
        on_error=on_error,
        chunk_size=chunk_size,
        max_filename_len=max_filename_len,
        on_progress=on_progress,
    )


def _copy_one_file(
    src_bridge, src_path,
    dst_bridge, dst_path,
    *,
    stats: CopyStats,
    preserve: bool,
    atomic: bool,
    on_conflict: str,
    chunk_size: int,
    on_progress: Optional[Callable[[CopyProgress], None]],
) -> None:
    dst_existing = dst_bridge.stat_path(dst_path)
    if dst_existing is not None:
        if on_conflict == "error":
            raise FileExistsError(f"destination exists: {dst_path}")
        if on_conflict == "skip":
            stats.files_skipped += 1
            if on_progress is not None:
                on_progress(
                    CopyProgress(current_path=dst_path, current_op="skip")
                )
            return

    file_stats = copy_file(
        src_bridge, src_path,
        dst_bridge, dst_path,
        preserve=preserve,
        atomic=atomic,
        overwrite=True,  # caller-level conflict policy already handled above
        chunk_size=chunk_size,
        on_progress=on_progress,
    )
    stats.files_copied += file_stats.files_copied
    stats.bytes_copied += file_stats.bytes_copied
    stats.links_skipped += file_stats.links_skipped


# ---------------------------------------------------------------------------
# Bridge helpers (path-based wrappers around lock-based primitives)
# ---------------------------------------------------------------------------


def _create_dir_path(bridge, path: str) -> int:
    """Create a directory at *path*, returning the new lock BPTR.

    Caller is responsible for freeing the returned lock.
    """
    dir_path, name = _split_path(path)
    if not name:
        raise ValueError(f"cannot create root: {path}")
    parent_lock, _, locks = bridge.locate_path(dir_path)
    if dir_path == "/" and parent_lock == 0:
        parent_lock, _ = bridge.locate(0, "")
        if parent_lock:
            locks.append(parent_lock)
    if parent_lock == 0:
        # Parent doesn't exist — try to mkdir -p
        _create_dir_path(bridge, dir_path)
        parent_lock, _, locks = bridge.locate_path(dir_path)
        if parent_lock == 0:
            raise OSError(f"cannot create parent for: {path}")
    try:
        new_lock, res2 = bridge.create_dir(parent_lock, name)
        if new_lock == 0:
            raise OSError(f"create_dir failed for {path}: res2={res2}")
        return new_lock
    finally:
        for l in reversed(locks):
            bridge.free_lock(l)


def _create_dir_under(bridge, parent_path: str, name: str) -> int:
    """Create a directory named *name* under *parent_path*.

    Returns the new lock BPTR; caller frees.
    """
    parent_lock, _, locks = bridge.locate_path(parent_path)
    if parent_path == "/" and parent_lock == 0:
        parent_lock, _ = bridge.locate(0, "")
        if parent_lock:
            locks.append(parent_lock)
    if parent_lock == 0:
        raise OSError(f"parent dir not found: {parent_path}")
    try:
        new_lock, res2 = bridge.create_dir(parent_lock, name)
        if new_lock == 0:
            raise OSError(
                f"create_dir failed for {parent_path}/{name}: res2={res2}"
            )
        return new_lock
    finally:
        for l in reversed(locks):
            bridge.free_lock(l)


def _unlink_path(bridge, path: str) -> None:
    dir_path, name = _split_path(path)
    parent_lock, _, locks = bridge.locate_path(dir_path)
    if dir_path == "/" and parent_lock == 0:
        parent_lock, _ = bridge.locate(0, "")
        if parent_lock:
            locks.append(parent_lock)
    if parent_lock == 0:
        raise FileNotFoundError(f"parent dir not found: {dir_path}")
    try:
        res1, res2 = bridge.delete_object(parent_lock, name)
        if res1 == 0:
            raise OSError(f"delete failed for {path}: res2={res2}")
    finally:
        for l in reversed(locks):
            bridge.free_lock(l)


def _rename_path(bridge, src_path: str, dst_path: str) -> None:
    src_dir, src_name = _split_path(src_path)
    dst_dir, dst_name = _split_path(dst_path)
    src_lock, _, src_locks = bridge.locate_path(src_dir)
    dst_lock, _, dst_locks = bridge.locate_path(dst_dir)
    try:
        res1, res2 = bridge.rename_object(src_lock, src_name, dst_lock, dst_name)
        if res1 == 0:
            raise OSError(
                f"rename failed {src_path} -> {dst_path}: res2={res2}"
            )
    finally:
        for l in reversed(src_locks):
            bridge.free_lock(l)
        for l in reversed(dst_locks):
            bridge.free_lock(l)


# ---------------------------------------------------------------------------
# Image ↔ Host tree operations
# ---------------------------------------------------------------------------


def export_tree(
    src_bridge,
    src_root: str,
    dst_path: Path,
    *,
    preserve: bool = True,
    meta_format: str = "uaem",  # "uaem" | "xdfmeta"
    on_progress: Optional[Callable[[CopyProgress], None]] = None,
    on_error: str = "abort",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> CopyStats:
    """Recursively extract *src_root* from an image to *dst_path* on the host.

    Sidecars are emitted alongside files (per-file ``.uaem``) or as a single
    ``.amiga-meta.xdfmeta`` manifest at the tree root, depending on
    *meta_format*. The default-metadata heuristic skips sidecar emission
    for files with no comment, default protection, and no timestamp.

    *dst_path* is created if absent.
    """
    from .sidecar import (  # local import: amitools-dependent
        UaemProvider,
        XdfMetaProvider,
        is_default_meta,
    )

    if meta_format not in ("uaem", "xdfmeta"):
        raise ValueError(f"invalid meta_format: {meta_format!r}")
    if on_error not in ("abort", "skip"):
        raise ValueError(f"invalid on_error: {on_error!r}")

    start = time.monotonic()
    stats = CopyStats()

    src_root_info = src_bridge.stat_path(src_root)
    if src_root_info is None:
        raise FileNotFoundError(f"source root not found: {src_root}")
    if not _entry_is_dir(src_root_info):
        raise NotADirectoryError(f"source root is not a directory: {src_root}")

    dst_path = Path(dst_path).resolve()
    dst_path.mkdir(parents=True, exist_ok=True)

    provider = UaemProvider() if meta_format == "uaem" else XdfMetaProvider()

    # Record the source root's metadata too. For xdfmeta this becomes a
    # manifest entry whose relative key is the root itself; for .uaem we
    # skip — directory-level uaem sidecars don't have an established
    # naming convention.
    if preserve:
        root_meta = meta_info_from_fib(src_root_info)
        if not is_default_meta(root_meta) and meta_format == "xdfmeta":
            provider.write_meta(dst_path, root_meta, dst_path)

    _export_dir_contents(
        src_bridge,
        src_root,
        dst_path,
        provider=provider,
        meta_format=meta_format,
        tree_root=dst_path,
        stats=stats,
        preserve=preserve,
        on_progress=on_progress,
        on_error=on_error,
        chunk_size=chunk_size,
    )

    if meta_format == "xdfmeta" and preserve:
        # Single-volume manifest needs at least the volume name and a
        # placeholder root meta to be parseable when re-read.
        try:
            volume_name = src_bridge.volume_name()
        except Exception:
            volume_name = "Extracted"
        provider.set_volume_info(dst_path, volume_name)
        provider.flush(dst_path)

    stats.elapsed_secs = time.monotonic() - start
    return stats


def _export_dir_contents(
    src_bridge,
    src_dir: str,
    dst_dir: Path,
    *,
    provider,
    meta_format: str,
    tree_root: Path,
    stats: CopyStats,
    preserve: bool,
    on_progress: Optional[Callable[[CopyProgress], None]],
    on_error: str,
    chunk_size: int,
) -> None:
    from .sidecar import is_default_meta

    entries = src_bridge.list_dir_path(src_dir)
    for entry in entries:
        name = entry.get("name", "")
        if not name:
            continue

        src_child = _posix_join(src_dir, name)
        dst_child = dst_dir / name

        if _entry_is_link(entry):
            stats.links_skipped += 1
            if on_progress is not None:
                on_progress(CopyProgress(current_path=src_child, current_op="skip"))
            continue

        try:
            if _entry_is_dir(entry):
                dst_child.mkdir(parents=True, exist_ok=True)
                stats.dirs_copied += 1
                if preserve:
                    meta = meta_info_from_fib(entry)
                    if not is_default_meta(meta) and meta_format == "xdfmeta":
                        provider.write_meta(dst_child, meta, tree_root)
                if on_progress is not None:
                    on_progress(CopyProgress(
                        current_path=str(dst_child), current_op="mkdir",
                    ))
                _export_dir_contents(
                    src_bridge,
                    src_child,
                    dst_child,
                    provider=provider,
                    meta_format=meta_format,
                    tree_root=tree_root,
                    stats=stats,
                    preserve=preserve,
                    on_progress=on_progress,
                    on_error=on_error,
                    chunk_size=chunk_size,
                )
            else:
                _export_one_file(
                    src_bridge,
                    src_child,
                    dst_child,
                    entry=entry,
                    provider=provider,
                    meta_format=meta_format,
                    tree_root=tree_root,
                    stats=stats,
                    preserve=preserve,
                    on_progress=on_progress,
                    chunk_size=chunk_size,
                )
        except Exception as exc:
            stats.errors.append(f"{src_child}: {exc}")
            if on_error == "abort":
                raise


def _export_one_file(
    src_bridge,
    src_path: str,
    dst_path: Path,
    *,
    entry: Dict,
    provider,
    meta_format: str,
    tree_root: Path,
    stats: CopyStats,
    preserve: bool,
    on_progress: Optional[Callable[[CopyProgress], None]],
    chunk_size: int,
) -> None:
    from .sidecar import is_default_meta

    fh_result = src_bridge.open_file(src_path)
    if fh_result is None:
        raise OSError(f"failed to open source: {src_path}")
    fh_addr, _ = fh_result
    bytes_written = 0
    try:
        src_bridge.seek_handle(fh_addr, 0)
        total = entry.get("size", 0)
        with open(dst_path, "wb") as out_fd:
            while True:
                chunk = src_bridge.read_handle(fh_addr, chunk_size)
                if not chunk:
                    break
                out_fd.write(chunk)
                bytes_written += len(chunk)
                if on_progress is not None:
                    on_progress(CopyProgress(
                        current_path=str(dst_path),
                        current_op="copy",
                        bytes_in_file=total,
                        bytes_done_in_file=bytes_written,
                    ))
    finally:
        src_bridge.close_file(fh_addr)

    stats.files_copied += 1
    stats.bytes_copied += bytes_written

    if preserve:
        meta = meta_info_from_fib(entry)
        if not is_default_meta(meta):
            if meta_format == "uaem":
                provider.write_meta(dst_path, meta)
            else:
                provider.write_meta(dst_path, meta, tree_root)


def import_tree(
    src_path: Path,
    dst_bridge,
    dst_root: str,
    *,
    preserve: bool = True,
    sidecar_registry=None,
    atomic: bool = True,
    on_conflict: str = "overwrite",
    on_error: str = "abort",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_filename_len: int = DEFAULT_MAX_FILENAME_LEN,
    on_progress: Optional[Callable[[CopyProgress], None]] = None,
) -> CopyStats:
    """Recursively import a host tree at *src_path* into the image at *dst_root*.

    Sidecars next to files are auto-detected via *sidecar_registry* (default:
    :func:`amifuse.sidecar.default_registry`) and their metadata is applied
    to the image entries. ``.uaem`` and ``.amiga-meta.xdfmeta`` sidecar files
    themselves are excluded from the import — they are metadata about other
    files, not files to copy into the image.
    """
    from .sidecar import default_registry, XDFMETA_MANIFEST_NAME

    if on_conflict not in ("overwrite", "skip", "error"):
        raise ValueError(f"invalid on_conflict: {on_conflict!r}")
    if on_error not in ("abort", "skip"):
        raise ValueError(f"invalid on_error: {on_error!r}")

    src_path = Path(src_path).resolve()
    if not src_path.exists():
        raise FileNotFoundError(f"source path not found: {src_path}")
    if not src_path.is_dir():
        raise NotADirectoryError(f"source path is not a directory: {src_path}")

    start = time.monotonic()
    stats = CopyStats()
    tree_root = src_path
    reg = sidecar_registry or default_registry()

    # Ensure dst_root exists on the image
    dst_root_info = dst_bridge.stat_path(dst_root)
    if dst_root_info is None:
        _create_dir_path(dst_bridge, dst_root)
    elif not _entry_is_dir(dst_root_info):
        raise NotADirectoryError(
            f"destination root exists and is not a directory: {dst_root}"
        )

    _import_dir_contents(
        src_path,
        dst_bridge,
        dst_root,
        registry=reg,
        tree_root=tree_root,
        stats=stats,
        preserve=preserve,
        atomic=atomic,
        on_conflict=on_conflict,
        on_error=on_error,
        chunk_size=chunk_size,
        max_filename_len=max_filename_len,
        on_progress=on_progress,
    )

    dst_bridge.flush_volume()
    stats.elapsed_secs = time.monotonic() - start
    return stats


def _is_sidecar_filename(name: str) -> bool:
    """Return True for files that hold metadata about other files, not data."""
    from .sidecar import XDFMETA_MANIFEST_NAME
    if name == XDFMETA_MANIFEST_NAME:
        return True
    if name.endswith(".uaem"):
        return True
    return False


def _import_dir_contents(
    src_dir: Path,
    dst_bridge,
    dst_dir: str,
    *,
    registry,
    tree_root: Path,
    stats: CopyStats,
    preserve: bool,
    atomic: bool,
    on_conflict: str,
    on_error: str,
    chunk_size: int,
    max_filename_len: int,
    on_progress: Optional[Callable[[CopyProgress], None]],
) -> None:
    for entry in sorted(src_dir.iterdir()):
        name = entry.name
        if _is_sidecar_filename(name):
            continue
        if len(name) > max_filename_len:
            msg = (
                f"filename exceeds destination FS limit "
                f"({len(name)} > {max_filename_len}): {entry}"
            )
            stats.errors.append(msg)
            if on_error == "abort":
                raise OSError(msg)
            continue

        dst_child = _posix_join(dst_dir, name)

        try:
            if entry.is_dir():
                existing = dst_bridge.stat_path(dst_child)
                if existing is None:
                    new_lock = _create_dir_under(dst_bridge, dst_dir, name)
                    dst_bridge.free_lock(new_lock)
                elif not _entry_is_dir(existing):
                    raise FileExistsError(
                        f"destination exists as file, source is dir: {dst_child}"
                    )
                # Apply metadata if a sidecar covers the directory
                if preserve:
                    match = registry.detect(entry, tree_root=tree_root)
                    if match is not None:
                        _, meta = match
                        dst_bridge.apply_meta_at_path(dst_child, meta)
                stats.dirs_copied += 1
                if on_progress is not None:
                    on_progress(CopyProgress(
                        current_path=dst_child, current_op="mkdir",
                    ))
                _import_dir_contents(
                    entry,
                    dst_bridge,
                    dst_child,
                    registry=registry,
                    tree_root=tree_root,
                    stats=stats,
                    preserve=preserve,
                    atomic=atomic,
                    on_conflict=on_conflict,
                    on_error=on_error,
                    chunk_size=chunk_size,
                    max_filename_len=max_filename_len,
                    on_progress=on_progress,
                )
            elif entry.is_file():
                _import_one_file(
                    entry,
                    dst_bridge,
                    dst_child,
                    registry=registry,
                    tree_root=tree_root,
                    stats=stats,
                    preserve=preserve,
                    atomic=atomic,
                    on_conflict=on_conflict,
                    chunk_size=chunk_size,
                    on_progress=on_progress,
                )
            # Skip other host entry types (symlinks, devices)
        except FileExistsError:
            raise
        except Exception as exc:
            stats.errors.append(f"{entry}: {exc}")
            if on_error == "abort":
                raise


def _import_one_file(
    src_path: Path,
    dst_bridge,
    dst_path: str,
    *,
    registry,
    tree_root: Path,
    stats: CopyStats,
    preserve: bool,
    atomic: bool,
    on_conflict: str,
    chunk_size: int,
    on_progress: Optional[Callable[[CopyProgress], None]],
) -> None:
    existing = dst_bridge.stat_path(dst_path)
    if existing is not None:
        if on_conflict == "error":
            raise FileExistsError(f"destination exists: {dst_path}")
        if on_conflict == "skip":
            stats.files_skipped += 1
            if on_progress is not None:
                on_progress(CopyProgress(current_path=dst_path, current_op="skip"))
            return

    # Write content
    dst_dir, dst_name = _split_path(dst_path)
    write_name = _temp_name(dst_name) if atomic else dst_name
    write_path = _posix_join(dst_dir, write_name)

    if atomic:
        leftover = dst_bridge.stat_path(write_path)
        if leftover is not None:
            _unlink_path(dst_bridge, write_path)
    if existing is not None and not atomic:
        _unlink_path(dst_bridge, dst_path)

    bytes_written = 0
    src_size = src_path.stat().st_size

    dst_fh_result = dst_bridge.open_file(
        write_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    )
    if dst_fh_result is None:
        raise OSError(f"failed to open destination for write: {write_path}")
    dst_fh, _ = dst_fh_result
    try:
        with open(src_path, "rb") as in_fd:
            while True:
                chunk = in_fd.read(chunk_size)
                if not chunk:
                    break
                n = dst_bridge.write_handle(dst_fh, chunk)
                if n < 0:
                    raise OSError(
                        f"write failed at offset {bytes_written} for {dst_path}"
                    )
                if n < len(chunk):
                    raise OSError(
                        f"partial write {n}/{len(chunk)} (disk full?) at "
                        f"{bytes_written} for {dst_path}"
                    )
                bytes_written += n
                if on_progress is not None:
                    on_progress(CopyProgress(
                        current_path=dst_path,
                        current_op="copy",
                        bytes_in_file=src_size,
                        bytes_done_in_file=bytes_written,
                    ))
    finally:
        dst_bridge.close_file(dst_fh)

    # Apply metadata if sidecar covers this file
    if preserve:
        match = registry.detect(src_path, tree_root=tree_root)
        if match is not None:
            _, meta = match
            dst_bridge.apply_meta_at_path(write_path, meta)

    # Atomic rename
    if atomic:
        if existing is not None:
            _unlink_path(dst_bridge, dst_path)
        _rename_path(dst_bridge, write_path, dst_path)

    stats.files_copied += 1
    stats.bytes_copied += bytes_written
