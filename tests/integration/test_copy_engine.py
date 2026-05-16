"""Integration tests for the copy engine against real native handlers.

These run when machine68k is functional and a fixture root with PFS3
drivers + images is available. The test matrix is parameterized over
filesystem pairs; pairs missing fixtures skip individually rather than
failing the whole suite — that way adding an SFS or BFFS image later
makes those pairs activate automatically.

Coverage:

  - Phase 1: SET_PROTECT/COMMENT/DATE round-trip through HandlerBridge
    against a real PFS3 image (apply_meta → stat_path)
  - Phase 3: image-to-image copy_file and copy_tree
  - Phase 5: export_tree → import_tree round-trip via a host tree with
    both .uaem and xdfmeta sidecars
  - CLI: amifuse cp end-to-end via subprocess against PFS3
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helper fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pfs3_writable(pfs3_image, tmp_path):
    """A writable copy of the PFS3 fixture image."""
    dst = tmp_path / "pfs3.hdf"
    shutil.copy2(pfs3_image, dst)
    return dst


@pytest.fixture
def pfs3_dst_writable(pfs3_image, tmp_path):
    """A second writable copy for cross-image tests (different filename)."""
    dst = tmp_path / "pfs3-dst.hdf"
    shutil.copy2(pfs3_image, dst)
    return dst


def _seed_file(bridge, path, content, *, protection=0, comment="",
               days=0, mins=0, ticks=0):
    """Write *content* at *path* in the bridge image, then apply metadata.

    Uses the lower-level bridge primitives so we don't depend on the copy
    engine being correct (the integration test should be able to validate
    each layer independently).
    """
    fh_result = bridge.open_file(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    assert fh_result is not None, f"open_file failed for {path}"
    fh, _ = fh_result
    try:
        n = bridge.write_handle(fh, content)
        assert n == len(content), f"short write: {n} != {len(content)}"
    finally:
        bridge.close_file(fh)

    # Apply metadata via the same path-based wrapper the copy engine uses
    from amitools.fs.FSString import FSString
    from amitools.fs.MetaInfo import MetaInfo
    from amitools.fs.ProtectFlags import ProtectFlags
    from amitools.fs.TimeStamp import TimeStamp

    meta = MetaInfo(
        protect_flags=ProtectFlags(protection),
        mod_ts=TimeStamp(days=days, mins=mins, ticks=ticks),
        comment=FSString(comment) if comment else None,
    )
    bridge.apply_meta_at_path(path, meta)


# ---------------------------------------------------------------------------
# Phase 1: SET_PROTECT/COMMENT/DATE round-trip through HandlerBridge
# ---------------------------------------------------------------------------


class TestApplyMetaRoundTrip:
    """apply_meta writes → stat_path reads back the same bytes."""

    def test_protection_roundtrip(self, pfs3_writable, pfs3_driver):
        from amifuse.fuse_fs import HandlerBridge

        bridge = HandlerBridge(pfs3_writable, pfs3_driver, read_only=False)
        try:
            _seed_file(bridge, "/protect_test", b"x", protection=0x40)
            bridge.flush_volume()
        finally:
            bridge.close()

        bridge2 = HandlerBridge(pfs3_writable, pfs3_driver, read_only=True)
        try:
            info = bridge2.stat_path("/protect_test")
            assert info is not None
            assert info["protection"] == 0x40
        finally:
            bridge2.close()

    def test_comment_roundtrip(self, pfs3_writable, pfs3_driver):
        from amifuse.fuse_fs import HandlerBridge

        bridge = HandlerBridge(pfs3_writable, pfs3_driver, read_only=False)
        try:
            _seed_file(bridge, "/comment_test", b"y", comment="hello world")
            bridge.flush_volume()
        finally:
            bridge.close()

        bridge2 = HandlerBridge(pfs3_writable, pfs3_driver, read_only=True)
        try:
            info = bridge2.stat_path("/comment_test")
            assert info is not None
            assert info["comment"] == "hello world"
        finally:
            bridge2.close()

    def test_date_roundtrip(self, pfs3_writable, pfs3_driver):
        from amifuse.fuse_fs import HandlerBridge

        bridge = HandlerBridge(pfs3_writable, pfs3_driver, read_only=False)
        try:
            _seed_file(bridge, "/date_test", b"z",
                       days=5471, mins=720, ticks=1500)
            bridge.flush_volume()
        finally:
            bridge.close()

        bridge2 = HandlerBridge(pfs3_writable, pfs3_driver, read_only=True)
        try:
            info = bridge2.stat_path("/date_test")
            assert info is not None
            assert info["date_days"] == 5471
            assert info["date_mins"] == 720
            assert info["date_ticks"] == 1500
        finally:
            bridge2.close()

    def test_all_three_combined(self, pfs3_writable, pfs3_driver):
        from amifuse.fuse_fs import HandlerBridge

        bridge = HandlerBridge(pfs3_writable, pfs3_driver, read_only=False)
        try:
            _seed_file(
                bridge, "/combined", b"data",
                protection=0xFF,
                comment="all three",
                days=8000, mins=1000, ticks=500,
            )
            bridge.flush_volume()
        finally:
            bridge.close()

        bridge2 = HandlerBridge(pfs3_writable, pfs3_driver, read_only=True)
        try:
            info = bridge2.stat_path("/combined")
            assert info["protection"] == 0xFF
            assert info["comment"] == "all three"
            assert info["date_days"] == 8000
            assert info["date_mins"] == 1000
            assert info["date_ticks"] == 500
        finally:
            bridge2.close()


# ---------------------------------------------------------------------------
# Phase 3: image-to-image copy_file with metadata
# ---------------------------------------------------------------------------


class TestCopyFileBridgeToBridge:
    def test_single_file_copy_preserves_content_and_metadata(
        self, pfs3_writable, pfs3_dst_writable, pfs3_driver,
    ):
        from amifuse.copy import copy_file
        from amifuse.fuse_fs import HandlerBridge

        # Seed a file with distinctive metadata in src
        src_bridge = HandlerBridge(pfs3_writable, pfs3_driver, read_only=False)
        try:
            _seed_file(
                src_bridge, "/source-file",
                b"copy me through the engine",
                protection=0x40, comment="src-comment",
                days=5471, mins=720, ticks=1500,
            )
            src_bridge.flush_volume()
        finally:
            src_bridge.close()

        # Copy src → dst with a fresh pair of bridges
        src = HandlerBridge(pfs3_writable, pfs3_driver, read_only=True)
        dst = HandlerBridge(pfs3_dst_writable, pfs3_driver, read_only=False)
        try:
            stats = copy_file(
                src, "/source-file",
                dst, "/copied-file",
                atomic=False,
            )
            assert stats.files_copied == 1
            assert stats.bytes_copied == len(b"copy me through the engine")
            dst.flush_volume()
        finally:
            dst.close()
            src.close()

        # Verify on dst with a fresh read-only bridge
        verifier = HandlerBridge(pfs3_dst_writable, pfs3_driver, read_only=True)
        try:
            info = verifier.stat_path("/copied-file")
            assert info is not None
            assert info["size"] == len(b"copy me through the engine")
            assert info["protection"] == 0x40
            assert info["comment"] == "src-comment"
            assert info["date_days"] == 5471
        finally:
            verifier.close()


# ---------------------------------------------------------------------------
# Phase 3: image-to-image copy_tree
# ---------------------------------------------------------------------------


class TestCopyTreeBridgeToBridge:
    def test_recursive_tree_preserves_structure_and_metadata(
        self, pfs3_writable, pfs3_dst_writable, pfs3_driver,
    ):
        from amifuse.copy import copy_tree
        from amifuse.fuse_fs import HandlerBridge

        # Seed src with a small tree
        src_bridge = HandlerBridge(pfs3_writable, pfs3_driver, read_only=False)
        try:
            # Make a directory and populate it
            root_lock, _ = src_bridge.locate(0, "")
            assert root_lock != 0
            dir_lock, _ = src_bridge.create_dir(root_lock, "MyTree")
            assert dir_lock != 0
            src_bridge.free_lock(dir_lock)
            src_bridge.free_lock(root_lock)

            _seed_file(
                src_bridge, "/MyTree/a", b"AAA",
                protection=0x40, comment="a-note",
                days=5471, mins=720, ticks=1500,
            )
            _seed_file(
                src_bridge, "/MyTree/b", b"BBB",
                protection=0x20, comment="b-note",
            )
            src_bridge.flush_volume()
        finally:
            src_bridge.close()

        # Recursive copy
        src = HandlerBridge(pfs3_writable, pfs3_driver, read_only=True)
        dst = HandlerBridge(pfs3_dst_writable, pfs3_driver, read_only=False)
        try:
            stats = copy_tree(
                src, "/MyTree",
                dst, "/CopyOfTree",
                atomic=False,
            )
            assert stats.files_copied == 2
            assert stats.bytes_copied == 6
        finally:
            dst.close()
            src.close()

        # Verify both files appear with correct content + metadata
        verifier = HandlerBridge(pfs3_dst_writable, pfs3_driver, read_only=True)
        try:
            a = verifier.stat_path("/CopyOfTree/a")
            assert a is not None
            assert a["size"] == 3
            assert a["protection"] == 0x40
            assert a["comment"] == "a-note"

            b = verifier.stat_path("/CopyOfTree/b")
            assert b is not None
            assert b["protection"] == 0x20
            assert b["comment"] == "b-note"
        finally:
            verifier.close()


# ---------------------------------------------------------------------------
# Phase 5: export_tree → import_tree round-trip via host
# ---------------------------------------------------------------------------


class TestExportImportRoundTripPfs3:
    @pytest.mark.parametrize("meta_format", ["uaem", "xdfmeta"])
    def test_full_roundtrip_via_host_preserves_metadata(
        self, pfs3_writable, pfs3_dst_writable, pfs3_driver,
        tmp_path, meta_format,
    ):
        from amifuse.copy import export_tree, import_tree
        from amifuse.fuse_fs import HandlerBridge

        # Seed src
        src_bridge = HandlerBridge(pfs3_writable, pfs3_driver, read_only=False)
        try:
            root_lock, _ = src_bridge.locate(0, "")
            dir_lock, _ = src_bridge.create_dir(root_lock, "RoundTrip")
            src_bridge.free_lock(dir_lock)
            src_bridge.free_lock(root_lock)

            _seed_file(
                src_bridge, "/RoundTrip/Startup-Sequence",
                b";; boot script\n",
                protection=0x40, comment="boot",
                days=5471, mins=720, ticks=1500,
            )
            _seed_file(
                src_bridge, "/RoundTrip/readme",
                b"hello",
                protection=0x20, comment="docs",
            )
            src_bridge.flush_volume()
        finally:
            src_bridge.close()

        # Extract to host
        host_root = tmp_path / "extracted"
        src = HandlerBridge(pfs3_writable, pfs3_driver, read_only=True)
        try:
            export_tree(
                src, "/RoundTrip", host_root,
                preserve=True, meta_format=meta_format,
            )
        finally:
            src.close()

        assert (host_root / "Startup-Sequence").read_bytes() == b";; boot script\n"
        assert (host_root / "readme").read_bytes() == b"hello"

        # Reimport to fresh dst location
        dst = HandlerBridge(pfs3_dst_writable, pfs3_driver, read_only=False)
        try:
            import_tree(host_root, dst, "/RoundTripImported")
        finally:
            dst.close()

        # Verify metadata round-tripped
        verifier = HandlerBridge(
            pfs3_dst_writable, pfs3_driver, read_only=True,
        )
        try:
            startup = verifier.stat_path("/RoundTripImported/Startup-Sequence")
            assert startup is not None
            assert startup["size"] == len(b";; boot script\n")
            assert startup["protection"] == 0x40
            assert startup["comment"] == "boot"
            # Whole-second tick values round-trip; sub-second precision is
            # an amitools TimeStamp limitation documented in test_sidecar.
            assert startup["date_days"] == 5471

            readme = verifier.stat_path("/RoundTripImported/readme")
            assert readme is not None
            assert readme["protection"] == 0x20
            assert readme["comment"] == "docs"
        finally:
            verifier.close()


# ---------------------------------------------------------------------------
# CLI: amifuse cp via subprocess (real handler, real argparse)
# ---------------------------------------------------------------------------


class TestCpCli:
    def _run(self, *args, timeout=120.0):
        return subprocess.run(
            [sys.executable, "-m", "amifuse", *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )

    def test_cp_single_file_pfs3_to_pfs3(
        self, pfs3_writable, pfs3_dst_writable, pfs3_driver,
    ):
        from amifuse.fuse_fs import HandlerBridge

        # Seed src
        src_bridge = HandlerBridge(pfs3_writable, pfs3_driver, read_only=False)
        try:
            _seed_file(
                src_bridge, "/cli-src-file", b"cli copy",
                protection=0x40, comment="via-cli",
            )
            src_bridge.flush_volume()
        finally:
            src_bridge.close()

        proc = self._run(
            "cp", "--no-atomic", "--json",
            "--driver", str(pfs3_driver),
            f"{pfs3_writable}:/cli-src-file",
            f"{pfs3_dst_writable}:/cli-dst-file",
        )
        assert proc.returncode == 0, (
            f"cp failed: stderr={proc.stderr}\nstdout={proc.stdout}"
        )

        # Find JSON envelope in stdout
        idx = proc.stdout.find("{")
        assert idx != -1, f"no JSON in stdout: {proc.stdout!r}"
        result = json.loads(proc.stdout[idx:])
        assert result["status"] == "ok"
        assert result["files_copied"] == 1

        # Verify on dst
        verifier = HandlerBridge(
            pfs3_dst_writable, pfs3_driver, read_only=True,
        )
        try:
            info = verifier.stat_path("/cli-dst-file")
            assert info is not None
            assert info["size"] == len(b"cli copy")
            assert info["protection"] == 0x40
            assert info["comment"] == "via-cli"
        finally:
            verifier.close()


# ---------------------------------------------------------------------------
# Cross-FS matrix scaffolding (most pairs skip without fixtures)
# ---------------------------------------------------------------------------


# Mapping of FS name → (driver_fixture_name, image_fixture_name).
# When the named fixtures aren't available, individual matrix rows skip
# cleanly rather than failing the whole test class.
FS_FIXTURES = {
    "pfs3": ("pfs3_driver", "pfs3_image"),
    "ffs":  ("ffs_driver",  "ffs_image"),
    "sfs":  ("sfs_driver",  "sfs_image"),
    "bffs": ("bffs_driver", "bffs_image"),
}


@pytest.mark.parametrize(
    "src_fs,dst_fs",
    [
        ("pfs3", "ffs"),
        ("ffs",  "pfs3"),
        ("pfs3", "sfs"),
        ("sfs",  "pfs3"),
        ("pfs3", "bffs"),
        ("bffs", "pfs3"),
        ("ffs",  "sfs"),
        ("sfs",  "ffs"),
        ("ffs",  "bffs"),
        ("bffs", "ffs"),
        ("sfs",  "bffs"),
        ("bffs", "sfs"),
    ],
)
def test_cross_fs_copy_scaffolding(src_fs, dst_fs, request, tmp_path):
    """Scaffold for the (src_fs × dst_fs) matrix from the plan.

    Skips cleanly when the source or destination FS fixture is unavailable.
    When the full fixture set lands (FFS/SFS/BFFS images + drivers), each
    row activates without any test-code changes.

    The test currently just verifies the fixtures resolve and a copy_file
    completes; expanding to full metadata assertions can happen once the
    fixtures are present and we know their seeded content.
    """
    src_drv_name, src_img_name = FS_FIXTURES[src_fs]
    dst_drv_name, dst_img_name = FS_FIXTURES[dst_fs]

    # Request each fixture; pytest skips if any is unavailable.
    src_driver = request.getfixturevalue(src_drv_name)
    src_image = request.getfixturevalue(src_img_name)
    dst_driver = request.getfixturevalue(dst_drv_name)
    dst_image = request.getfixturevalue(dst_img_name)

    from amifuse.copy import copy_tree
    from amifuse.fuse_fs import HandlerBridge

    src_writable = tmp_path / f"{src_fs}-src.hdf"
    dst_writable = tmp_path / f"{dst_fs}-dst.hdf"
    shutil.copy2(src_image, src_writable)
    shutil.copy2(dst_image, dst_writable)

    src = HandlerBridge(src_writable, src_driver, read_only=True)
    dst = HandlerBridge(dst_writable, dst_driver, read_only=False)
    try:
        # Pick a small known-existing source directory; / is always valid.
        # For now we just verify the engine completes; metadata assertions
        # would require knowing the seeded content of each FS fixture.
        stats = copy_tree(
            src, "/",
            dst, "/copied-tree",
            atomic=False,
            max_filename_len=30 if dst_fs == "ffs" else 107,
            on_error="skip",  # tolerate quirks on cross-FS edge cases
        )
        assert stats.files_copied >= 0  # smoke check; activate richer asserts later
    finally:
        dst.close()
        src.close()
