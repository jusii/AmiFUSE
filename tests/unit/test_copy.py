"""Unit tests for amifuse.copy module.

Uses an in-memory FakeBridge that satisfies the subset of HandlerBridge
surface the copy engine touches. This lets us exercise the orchestration
logic — walking, recursion, conflict policies, atomicity, metadata
application, progress events — without any vamos/handler dependency.

Real-bridge integration tests will live in tests/integration/ (separate PR
in Phase 6 once the cross-FS test matrix is up).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pytest

from amifuse.copy import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_MAX_FILENAME_LEN,
    ST_FILE,
    ST_LINKDIR,
    ST_LINKFILE,
    ST_SOFTLINK,
    ST_USERDIR,
    CopyProgress,
    CopyStats,
    copy_file,
    copy_tree,
    meta_info_from_fib,
)


# ---------------------------------------------------------------------------
# Fake bridge
# ---------------------------------------------------------------------------


@dataclass
class FakeNode:
    """Single tree node in the FakeBridge volume."""

    name: str
    dir_type: int  # USERDIR=2, FILE=-3, SOFTLINK=3, etc.
    data: bytearray = field(default_factory=bytearray)
    # AmigaDOS metadata
    protection: int = 0
    comment: str = ""
    date_days: int = 0
    date_mins: int = 0
    date_ticks: int = 0
    children: Dict[str, "FakeNode"] = field(default_factory=dict)


class FakeBridge:
    """In-memory implementation of the HandlerBridge surface used by copy.py.

    Implements just enough for the copy engine's lifecycle: stat_path,
    list_dir_path, open_file (read + write+create+trunc), read/write/seek
    handles, close, create_dir/delete_object/rename_object, apply_meta (via
    apply_meta_at_path), locate_path/locate/free_lock, flush_volume.

    Lock BPTRs are synthetic ints that map back to FakeNode references via
    self._locks. Negative ints are never produced.
    """

    def __init__(self, name: str = "Sys"):
        self._volume = name
        self.root = FakeNode(name="", dir_type=ST_USERDIR)
        self._open_files: Dict[int, dict] = {}
        self._next_fh = 1
        self._locks: Dict[int, FakeNode] = {}
        self._next_lock = 1
        # Test instrumentation
        self.applied_meta_calls: List[Tuple[str, dict]] = []
        self.write_enabled = True
        self.flushed = False

    # -- internal helpers ----------------------------------------------------

    def _walk(self, path: str) -> Optional[FakeNode]:
        parts = [p for p in path.split("/") if p]
        node = self.root
        for p in parts:
            if p not in node.children:
                return None
            node = node.children[p]
        return node

    def _alloc_lock(self, node: FakeNode) -> int:
        lock = self._next_lock
        self._next_lock += 1
        self._locks[lock] = node
        return lock

    def _entry_dict(self, node: FakeNode) -> Dict:
        return {
            "dir_type": node.dir_type,
            "size": len(node.data),
            "name": node.name,
            "protection": node.protection,
            "num_blocks": (len(node.data) + 511) // 512,
            "date_days": node.date_days,
            "date_mins": node.date_mins,
            "date_ticks": node.date_ticks,
            "comment": node.comment,
        }

    # -- bridge surface ------------------------------------------------------

    def stat_path(self, path: str) -> Optional[Dict]:
        node = self._walk(path)
        if node is None:
            return None
        if path in ("/", ""):
            return {
                "dir_type": ST_USERDIR,
                "size": 0,
                "name": self._volume,
                "protection": 0,
                "num_blocks": 0,
                "date_days": 0,
                "date_mins": 0,
                "date_ticks": 0,
                "comment": "",
            }
        return self._entry_dict(node)

    def list_dir_path(self, path: str) -> List[Dict]:
        node = self._walk(path)
        if node is None or node.dir_type <= 0:
            return []
        return [self._entry_dict(c) for c in node.children.values()]

    def locate_path(self, path: str) -> Tuple[int, int, List[int]]:
        node = self._walk(path)
        if node is None:
            return 0, 0, []
        lock = self._alloc_lock(node)
        return lock, 0, [lock]

    def locate(self, lock_bptr: int, name: str) -> Tuple[int, int]:
        if lock_bptr == 0 and name == "":
            return self._alloc_lock(self.root), 0
        parent = self._locks.get(lock_bptr, self.root)
        if name == "":
            return self._alloc_lock(parent), 0
        child = parent.children.get(name)
        if child is None:
            return 0, 0
        return self._alloc_lock(child), 0

    def free_lock(self, lock: int) -> None:
        self._locks.pop(lock, None)

    def open_file(self, path: str, flags: int = os.O_RDONLY):
        write_mode = flags & getattr(os, "O_ACCMODE", 3)
        parts = [p for p in path.split("/") if p]
        if not parts:
            return None
        name = parts[-1]
        dir_path = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
        parent = self._walk(dir_path)
        if parent is None:
            return None

        if write_mode != os.O_RDONLY:
            if not self.write_enabled:
                return None
            # Create or truncate
            if flags & os.O_TRUNC or name not in parent.children:
                node = FakeNode(name=name, dir_type=ST_FILE)
                parent.children[name] = node
            else:
                node = parent.children[name]
            mode_str = "w" if (flags & os.O_TRUNC) else "rw"
        else:
            node = parent.children.get(name)
            if node is None or node.dir_type > 0:
                return None
            mode_str = "r"

        fh = self._next_fh
        self._next_fh += 1
        self._open_files[fh] = {"node": node, "offset": 0, "mode": mode_str}
        return fh, self._alloc_lock(parent)

    def read_handle(self, fh: int, size: int) -> bytes:
        info = self._open_files[fh]
        node = info["node"]
        off = info["offset"]
        data = bytes(node.data[off : off + size])
        info["offset"] += len(data)
        return data

    def write_handle(self, fh: int, data: bytes) -> int:
        info = self._open_files[fh]
        node = info["node"]
        off = info["offset"]
        # Extend if writing past end
        if off + len(data) > len(node.data):
            node.data.extend(b"\x00" * (off + len(data) - len(node.data)))
        node.data[off : off + len(data)] = data
        info["offset"] += len(data)
        return len(data)

    def seek_handle(self, fh: int, offset: int, mode=None) -> None:
        self._open_files[fh]["offset"] = offset

    def close_file(self, fh: int) -> None:
        self._open_files.pop(fh, None)

    def create_dir(self, parent_lock_bptr: int, name: str) -> Tuple[int, int]:
        parent = self._locks.get(parent_lock_bptr, self.root)
        if name in parent.children:
            return 0, 203  # ERROR_OBJECT_EXISTS
        node = FakeNode(name=name, dir_type=ST_USERDIR)
        parent.children[name] = node
        return self._alloc_lock(node), 0

    def delete_object(self, parent_lock_bptr: int, name: str) -> Tuple[int, int]:
        parent = self._locks.get(parent_lock_bptr, self.root)
        if name not in parent.children:
            return 0, 205  # ERROR_OBJECT_NOT_FOUND
        del parent.children[name]
        return 1, 0

    def rename_object(
        self,
        src_lock: int,
        src_name: str,
        dst_lock: int,
        dst_name: str,
    ) -> Tuple[int, int]:
        src_parent = self._locks.get(src_lock, self.root)
        dst_parent = self._locks.get(dst_lock, self.root)
        if src_name not in src_parent.children:
            return 0, 205
        if dst_name in dst_parent.children:
            return 0, 203
        node = src_parent.children.pop(src_name)
        node.name = dst_name
        dst_parent.children[dst_name] = node
        return 1, 0

    def apply_meta(self, parent_lock_bptr: int, name: str, meta_info) -> None:
        parent = self._locks.get(parent_lock_bptr, self.root)
        node = parent.children.get(name)
        if node is None:
            raise FileNotFoundError(name)
        mask = meta_info.get_protect()
        if mask is not None:
            node.protection = mask
        comment = meta_info.get_comment_unicode_str()
        node.comment = comment or ""
        ts = meta_info.get_mod_ts()
        if ts is not None:
            node.date_days = ts.days
            node.date_mins = ts.mins
            node.date_ticks = ts.ticks
        self.applied_meta_calls.append((name, {
            "protect": node.protection,
            "comment": node.comment,
            "days": node.date_days,
            "mins": node.date_mins,
            "ticks": node.date_ticks,
        }))

    def apply_meta_at_path(self, path: str, meta_info) -> None:
        parts = [p for p in path.split("/") if p]
        if not parts:
            raise ValueError(path)
        name = parts[-1]
        dir_path = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
        parent = self._walk(dir_path)
        if parent is None:
            raise FileNotFoundError(dir_path)
        lock = self._alloc_lock(parent)
        try:
            self.apply_meta(lock, name, meta_info)
        finally:
            self.free_lock(lock)

    def flush_volume(self) -> None:
        self.flushed = True


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _add_file(bridge: FakeBridge, path: str, content: bytes, **meta):
    """Inject a file with metadata directly into the FakeBridge tree."""
    parts = [p for p in path.split("/") if p]
    parent = bridge.root
    for p in parts[:-1]:
        if p not in parent.children:
            parent.children[p] = FakeNode(name=p, dir_type=ST_USERDIR)
        parent = parent.children[p]
    name = parts[-1]
    node = FakeNode(name=name, dir_type=ST_FILE, data=bytearray(content))
    for k, v in meta.items():
        setattr(node, k, v)
    parent.children[name] = node
    return node


def _add_dir(bridge: FakeBridge, path: str, **meta):
    parts = [p for p in path.split("/") if p]
    parent = bridge.root
    for p in parts[:-1]:
        if p not in parent.children:
            parent.children[p] = FakeNode(name=p, dir_type=ST_USERDIR)
        parent = parent.children[p]
    name = parts[-1]
    node = FakeNode(name=name, dir_type=ST_USERDIR)
    for k, v in meta.items():
        setattr(node, k, v)
    parent.children[name] = node
    return node


# ---------------------------------------------------------------------------
# meta_info_from_fib
# ---------------------------------------------------------------------------


class TestMetaInfoFromFib:
    def test_all_zero_date_returns_none_mod_ts(self):
        meta = meta_info_from_fib({"protection": 0, "date_days": 0,
                                   "date_mins": 0, "date_ticks": 0,
                                   "comment": ""})
        assert meta.get_mod_ts() is None

    def test_non_zero_date_builds_timestamp(self):
        meta = meta_info_from_fib({"protection": 0, "date_days": 5471,
                                   "date_mins": 720, "date_ticks": 1500,
                                   "comment": ""})
        ts = meta.get_mod_ts()
        assert (ts.days, ts.mins, ts.ticks) == (5471, 720, 1500)

    def test_empty_comment_skipped(self):
        meta = meta_info_from_fib({"protection": 0, "comment": ""})
        # apply_meta sees this as "no comment" and skips SET_COMMENT
        assert meta.get_comment_unicode_str() == ""

    def test_non_empty_comment_preserved(self):
        meta = meta_info_from_fib({"protection": 0, "comment": "Hello"})
        assert meta.get_comment_unicode_str() == "Hello"

    def test_protection_propagated(self):
        meta = meta_info_from_fib({"protection": 0x40, "comment": ""})
        assert meta.get_protect() == 0x40


# ---------------------------------------------------------------------------
# copy_file
# ---------------------------------------------------------------------------


class TestCopyFileSingle:
    def test_copies_content_and_metadata(self):
        src = FakeBridge()
        dst = FakeBridge()
        _add_file(src, "/file", b"hello world",
                  protection=0x40, comment="note",
                  date_days=5471, date_mins=720, date_ticks=1500)

        stats = copy_file(src, "/file", dst, "/file", atomic=False)

        assert stats.files_copied == 1
        assert stats.bytes_copied == len(b"hello world")
        assert bytes(dst.root.children["file"].data) == b"hello world"
        assert dst.root.children["file"].protection == 0x40
        assert dst.root.children["file"].comment == "note"
        assert dst.root.children["file"].date_days == 5471

    def test_atomic_write_uses_temp_then_rename(self):
        src = FakeBridge()
        dst = FakeBridge()
        _add_file(src, "/file", b"abc")

        copy_file(src, "/file", dst, "/file", atomic=True)

        # Temp file should be gone after rename
        assert "file" in dst.root.children
        # No leftover .amifuse-tmp.* in destination root
        leftovers = [n for n in dst.root.children if n.startswith(".amifuse-tmp.")]
        assert leftovers == []

    def test_overwrite_false_with_existing_raises(self):
        src = FakeBridge()
        dst = FakeBridge()
        _add_file(src, "/file", b"new")
        _add_file(dst, "/file", b"old")

        with pytest.raises(FileExistsError):
            copy_file(src, "/file", dst, "/file", overwrite=False, atomic=False)

    def test_overwrite_replaces_existing_content(self):
        src = FakeBridge()
        dst = FakeBridge()
        _add_file(src, "/file", b"NEW", protection=0x40)
        _add_file(dst, "/file", b"oldoldold", protection=0x10)

        copy_file(src, "/file", dst, "/file", atomic=False)

        assert bytes(dst.root.children["file"].data) == b"NEW"
        assert dst.root.children["file"].protection == 0x40

    def test_link_is_skipped(self):
        src = FakeBridge()
        dst = FakeBridge()
        src.root.children["link"] = FakeNode(name="link", dir_type=ST_SOFTLINK)

        stats = copy_file(src, "/link", dst, "/link", atomic=False)

        assert stats.files_copied == 0
        assert stats.links_skipped == 1
        assert "link" not in dst.root.children

    def test_directory_source_raises(self):
        src = FakeBridge()
        dst = FakeBridge()
        _add_dir(src, "/somedir")

        with pytest.raises(IsADirectoryError):
            copy_file(src, "/somedir", dst, "/somedir", atomic=False)

    def test_missing_source_raises(self):
        src = FakeBridge()
        dst = FakeBridge()
        with pytest.raises(FileNotFoundError):
            copy_file(src, "/missing", dst, "/missing", atomic=False)

    def test_preserve_false_skips_metadata(self):
        src = FakeBridge()
        dst = FakeBridge()
        _add_file(src, "/file", b"x", protection=0x40, comment="note")

        copy_file(src, "/file", dst, "/file", preserve=False, atomic=False)

        node = dst.root.children["file"]
        assert node.protection == 0
        assert node.comment == ""

    def test_progress_callback_fires_per_chunk(self):
        src = FakeBridge()
        dst = FakeBridge()
        # 3 chunks at chunk_size=4 = 12 bytes
        _add_file(src, "/file", b"abcdefghijkl")

        events = []
        copy_file(src, "/file", dst, "/file",
                  chunk_size=4, atomic=False,
                  on_progress=lambda e: events.append(e))

        copy_events = [e for e in events if e.current_op == "copy"]
        assert len(copy_events) == 3
        # Last copy event should report all bytes done
        assert copy_events[-1].bytes_done_in_file == 12
        assert copy_events[-1].bytes_in_file == 12


# ---------------------------------------------------------------------------
# copy_tree
# ---------------------------------------------------------------------------


class TestCopyTree:
    def test_copies_flat_tree(self):
        src = FakeBridge()
        dst = FakeBridge()
        _add_dir(src, "/SourceDir")
        _add_file(src, "/SourceDir/a", b"AAA")
        _add_file(src, "/SourceDir/b", b"BBB", protection=0x40)

        stats = copy_tree(src, "/SourceDir", dst, "/DstDir", atomic=False)

        assert stats.dirs_copied == 0  # The root itself isn't "copied"; only children
        assert stats.files_copied == 2
        assert stats.bytes_copied == 6
        assert bytes(dst.root.children["DstDir"].children["a"].data) == b"AAA"
        assert dst.root.children["DstDir"].children["b"].protection == 0x40

    def test_copies_nested_tree(self):
        src = FakeBridge()
        dst = FakeBridge()
        _add_dir(src, "/Sys")
        _add_dir(src, "/Sys/S")
        _add_dir(src, "/Sys/C")
        _add_file(src, "/Sys/S/Startup-Sequence", b";; script\n", protection=0x40)
        _add_file(src, "/Sys/C/Copy", b"BINARY")
        _add_file(src, "/Sys/C/Type", b"BINARY2")

        stats = copy_tree(src, "/Sys", dst, "/Sys", atomic=False)

        assert stats.files_copied == 3
        assert stats.dirs_copied == 2  # S, C
        assert "Sys" in dst.root.children
        sys_node = dst.root.children["Sys"]
        assert "S" in sys_node.children
        assert "C" in sys_node.children
        assert (bytes(sys_node.children["S"].children["Startup-Sequence"].data)
                == b";; script\n")
        assert sys_node.children["S"].children["Startup-Sequence"].protection == 0x40

    def test_skips_links_in_tree(self):
        src = FakeBridge()
        dst = FakeBridge()
        _add_dir(src, "/d")
        _add_file(src, "/d/file", b"x")
        src.root.children["d"].children["link"] = FakeNode(
            name="link", dir_type=ST_LINKFILE,
        )

        stats = copy_tree(src, "/d", dst, "/d", atomic=False)

        assert stats.files_copied == 1
        assert stats.links_skipped == 1
        assert "link" not in dst.root.children["d"].children

    def test_on_conflict_skip_leaves_existing(self):
        src = FakeBridge()
        dst = FakeBridge()
        _add_dir(src, "/d")
        _add_file(src, "/d/file", b"NEW")
        _add_dir(dst, "/d")
        _add_file(dst, "/d/file", b"PRESERVED")

        stats = copy_tree(src, "/d", dst, "/d", atomic=False,
                          on_conflict="skip")

        assert stats.files_skipped == 1
        assert stats.files_copied == 0
        assert bytes(dst.root.children["d"].children["file"].data) == b"PRESERVED"

    def test_on_conflict_error_raises(self):
        src = FakeBridge()
        dst = FakeBridge()
        _add_dir(src, "/d")
        _add_file(src, "/d/file", b"NEW")
        _add_dir(dst, "/d")
        _add_file(dst, "/d/file", b"OLD")

        with pytest.raises(FileExistsError):
            copy_tree(src, "/d", dst, "/d", atomic=False,
                      on_conflict="error")

    def test_creates_destination_root_if_absent(self):
        src = FakeBridge()
        dst = FakeBridge()
        _add_dir(src, "/Sys")
        _add_file(src, "/Sys/a", b"A")

        copy_tree(src, "/Sys", dst, "/Sys", atomic=False)

        assert "Sys" in dst.root.children
        assert "a" in dst.root.children["Sys"].children

    def test_filename_length_validation_aborts(self):
        src = FakeBridge()
        dst = FakeBridge()
        long_name = "x" * 50
        _add_dir(src, "/d")
        _add_file(src, f"/d/{long_name}", b"x")

        with pytest.raises(OSError, match="filename exceeds"):
            copy_tree(src, "/d", dst, "/d", atomic=False, max_filename_len=30)

    def test_filename_length_skip_on_error(self):
        src = FakeBridge()
        dst = FakeBridge()
        long_name = "x" * 50
        _add_dir(src, "/d")
        _add_file(src, "/d/ok", b"OK")
        _add_file(src, f"/d/{long_name}", b"BAD")

        stats = copy_tree(src, "/d", dst, "/d", atomic=False,
                          max_filename_len=30, on_error="skip")

        # Long name skipped; short one copied
        assert "ok" in dst.root.children["d"].children
        assert long_name not in dst.root.children["d"].children
        assert any("filename exceeds" in e for e in stats.errors)

    def test_directory_metadata_applied(self):
        src = FakeBridge()
        dst = FakeBridge()
        _add_dir(src, "/Sys", protection=0x40, comment="System dir")
        _add_file(src, "/Sys/a", b"A")

        copy_tree(src, "/Sys", dst, "/Sys", atomic=False)

        assert dst.root.children["Sys"].protection == 0x40
        assert dst.root.children["Sys"].comment == "System dir"

    def test_flush_volume_called_at_end(self):
        src = FakeBridge()
        dst = FakeBridge()
        _add_dir(src, "/d")
        _add_file(src, "/d/x", b"x")

        copy_tree(src, "/d", dst, "/d", atomic=False)

        assert dst.flushed is True

    def test_progress_callback_fires_for_dirs_and_files(self):
        src = FakeBridge()
        dst = FakeBridge()
        _add_dir(src, "/d")
        _add_dir(src, "/d/sub")
        _add_file(src, "/d/file", b"x")
        _add_file(src, "/d/sub/nested", b"y")

        events = []
        copy_tree(src, "/d", dst, "/d", atomic=False,
                  on_progress=lambda e: events.append(e))

        ops = [e.current_op for e in events]
        assert "mkdir" in ops
        assert "copy" in ops

    def test_missing_source_root_raises(self):
        src = FakeBridge()
        dst = FakeBridge()
        with pytest.raises(FileNotFoundError):
            copy_tree(src, "/missing", dst, "/anywhere", atomic=False)

    def test_source_root_must_be_directory(self):
        src = FakeBridge()
        dst = FakeBridge()
        _add_file(src, "/notadir", b"x")
        with pytest.raises(NotADirectoryError):
            copy_tree(src, "/notadir", dst, "/d", atomic=False)


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


class TestAtomicity:
    def test_atomic_temp_name_pattern(self):
        """Verify the temp filename uses the .amifuse-tmp.<pid>.<name> pattern.

        We can't easily observe the temp file existing mid-write without a
        crash hook, so we instead check that after a successful atomic copy
        there's no temp leftover.
        """
        src = FakeBridge()
        dst = FakeBridge()
        _add_file(src, "/file", b"x")

        copy_file(src, "/file", dst, "/file", atomic=True)

        for n in dst.root.children:
            assert not n.startswith(".amifuse-tmp."), \
                f"leftover temp file: {n}"

    def test_atomic_overwrites_existing(self):
        src = FakeBridge()
        dst = FakeBridge()
        _add_file(src, "/file", b"NEW")
        _add_file(dst, "/file", b"OLD")

        copy_file(src, "/file", dst, "/file", atomic=True)

        assert bytes(dst.root.children["file"].data) == b"NEW"
        for n in dst.root.children:
            assert not n.startswith(".amifuse-tmp.")


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------


class TestConstantsSanity:
    def test_dir_entry_types_match_amigaos(self):
        assert ST_USERDIR == 2
        assert ST_SOFTLINK == 3
        assert ST_LINKDIR == 4
        assert ST_FILE == -3
        assert ST_LINKFILE == -4

    def test_default_chunk_size_reasonable(self):
        # Bigger than 64 KiB (the existing cmd_read default) to reduce
        # packet overhead per chunk; small enough to bound memory.
        assert 64 * 1024 <= DEFAULT_CHUNK_SIZE <= 4 * 1024 * 1024

    def test_default_max_filename_len_matches_pfs3(self):
        assert DEFAULT_MAX_FILENAME_LEN == 107
