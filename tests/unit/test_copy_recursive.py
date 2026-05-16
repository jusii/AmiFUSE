"""Unit tests for amifuse.copy.export_tree and import_tree.

Exercises host-tree ↔ image-tree recursive flows with the FakeBridge from
test_copy.py. Verifies content, metadata via sidecars, and sidecar-file
exclusion on import.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from amifuse.copy import (
    ST_LINKFILE,
    CopyProgress,
    export_tree,
    import_tree,
)
from amifuse.sidecar import (
    UaemProvider,
    XDFMETA_MANIFEST_NAME,
    XdfMetaProvider,
)
from amitools.fs.FSString import FSString
from amitools.fs.MetaInfo import MetaInfo
from amitools.fs.ProtectFlags import ProtectFlags
from amitools.fs.TimeStamp import TimeStamp

from tests.unit.test_copy import FakeBridge, FakeNode, _add_dir, _add_file


def _ts(days, mins, ticks):
    return TimeStamp(days=days, mins=mins, ticks=ticks)


# ---------------------------------------------------------------------------
# export_tree
# ---------------------------------------------------------------------------


class TestExportTree:
    def test_extracts_flat_tree(self, tmp_path):
        bridge = FakeBridge()
        _add_dir(bridge, "/Sys")
        _add_file(bridge, "/Sys/a", b"AAA")
        _add_file(bridge, "/Sys/b", b"BBB")

        stats = export_tree(bridge, "/Sys", tmp_path / "out", preserve=False)

        assert (tmp_path / "out" / "a").read_bytes() == b"AAA"
        assert (tmp_path / "out" / "b").read_bytes() == b"BBB"
        assert stats.files_copied == 2
        assert stats.bytes_copied == 6

    def test_extracts_nested_tree(self, tmp_path):
        bridge = FakeBridge()
        _add_dir(bridge, "/Sys")
        _add_dir(bridge, "/Sys/S")
        _add_dir(bridge, "/Sys/C")
        _add_file(bridge, "/Sys/S/Startup-Sequence", b";; script\n")
        _add_file(bridge, "/Sys/C/Copy", b"BINARY")

        export_tree(bridge, "/Sys", tmp_path / "out", preserve=False)

        out = tmp_path / "out"
        assert (out / "S" / "Startup-Sequence").read_bytes() == b";; script\n"
        assert (out / "C" / "Copy").read_bytes() == b"BINARY"

    def test_uaem_sidecars_emitted_per_file(self, tmp_path):
        bridge = FakeBridge()
        _add_dir(bridge, "/d")
        _add_file(bridge, "/d/file", b"x",
                  protection=0x40, comment="note",
                  date_days=5471, date_mins=720, date_ticks=1500)

        export_tree(bridge, "/d", tmp_path / "out",
                    preserve=True, meta_format="uaem")

        assert (tmp_path / "out" / "file").exists()
        assert (tmp_path / "out" / "file.uaem").exists()
        # No xdfmeta manifest in .uaem mode
        assert not (tmp_path / "out" / XDFMETA_MANIFEST_NAME).exists()

    def test_xdfmeta_manifest_emitted_once(self, tmp_path):
        bridge = FakeBridge()
        _add_dir(bridge, "/d")
        _add_file(bridge, "/d/a", b"A", protection=0x40, comment="a-note")
        _add_file(bridge, "/d/b", b"B", protection=0x20, comment="b-note")

        export_tree(bridge, "/d", tmp_path / "out",
                    preserve=True, meta_format="xdfmeta")

        manifest = tmp_path / "out" / XDFMETA_MANIFEST_NAME
        assert manifest.exists()
        content = manifest.read_text()
        assert "a:" in content
        assert "b:" in content
        assert "a-note" in content
        assert "b-note" in content
        # No per-file .uaem in xdfmeta mode
        assert not (tmp_path / "out" / "a.uaem").exists()
        assert not (tmp_path / "out" / "b.uaem").exists()

    def test_xdfmeta_roundtrip_via_provider(self, tmp_path):
        bridge = FakeBridge()
        _add_dir(bridge, "/d")
        _add_file(bridge, "/d/file", b"x",
                  protection=0x40, comment="hello",
                  date_days=5471, date_mins=720, date_ticks=1500)

        export_tree(bridge, "/d", tmp_path / "out",
                    preserve=True, meta_format="xdfmeta")

        provider = XdfMetaProvider()
        meta = provider.read_meta(tmp_path / "out" / "file", tmp_path / "out")
        assert meta is not None
        assert meta.get_protect() == 0x40
        assert meta.get_comment_unicode_str() == "hello"

    def test_uaem_roundtrip_via_provider(self, tmp_path):
        bridge = FakeBridge()
        _add_dir(bridge, "/d")
        _add_file(bridge, "/d/file", b"x",
                  protection=0x40, comment="hi",
                  date_days=5471, date_mins=720, date_ticks=1500)

        export_tree(bridge, "/d", tmp_path / "out",
                    preserve=True, meta_format="uaem")

        provider = UaemProvider()
        meta = provider.read_meta(tmp_path / "out" / "file")
        assert meta is not None
        assert meta.get_protect() == 0x40
        assert meta.get_comment_unicode_str() == "hi"

    def test_default_metadata_skipped(self, tmp_path):
        """No sidecar emitted for files with all-default metadata."""
        bridge = FakeBridge()
        _add_dir(bridge, "/d")
        _add_file(bridge, "/d/plain", b"x")  # no metadata
        _add_file(bridge, "/d/marked", b"y", protection=0x40)

        export_tree(bridge, "/d", tmp_path / "out",
                    preserve=True, meta_format="uaem")

        # 'plain' has no sidecar; 'marked' does
        assert not (tmp_path / "out" / "plain.uaem").exists()
        assert (tmp_path / "out" / "marked.uaem").exists()

    def test_links_skipped(self, tmp_path):
        bridge = FakeBridge()
        _add_dir(bridge, "/d")
        _add_file(bridge, "/d/file", b"x")
        bridge.root.children["d"].children["link"] = FakeNode(
            name="link", dir_type=ST_LINKFILE,
        )

        stats = export_tree(bridge, "/d", tmp_path / "out", preserve=False)

        assert stats.links_skipped == 1
        assert (tmp_path / "out" / "file").exists()
        assert not (tmp_path / "out" / "link").exists()

    def test_creates_output_directory_if_absent(self, tmp_path):
        bridge = FakeBridge()
        _add_dir(bridge, "/d")
        _add_file(bridge, "/d/f", b"x")

        out_path = tmp_path / "does" / "not" / "exist"
        export_tree(bridge, "/d", out_path, preserve=False)

        assert (out_path / "f").exists()

    def test_invalid_meta_format_raises(self, tmp_path):
        bridge = FakeBridge()
        _add_dir(bridge, "/d")
        with pytest.raises(ValueError, match="invalid meta_format"):
            export_tree(bridge, "/d", tmp_path, meta_format="bogus")

    def test_progress_callback_fires(self, tmp_path):
        bridge = FakeBridge()
        _add_dir(bridge, "/d")
        _add_file(bridge, "/d/file", b"x")

        events = []
        export_tree(bridge, "/d", tmp_path / "out",
                    preserve=False,
                    on_progress=lambda e: events.append(e))

        ops = [e.current_op for e in events]
        assert "copy" in ops


# ---------------------------------------------------------------------------
# import_tree
# ---------------------------------------------------------------------------


class TestImportTree:
    def test_imports_flat_tree(self, tmp_path):
        # Build host tree
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a").write_bytes(b"AAA")
        (tmp_path / "src" / "b").write_bytes(b"BBB")

        bridge = FakeBridge()
        import_tree(tmp_path / "src", bridge, "/Sys")

        sys_node = bridge.root.children["Sys"]
        assert bytes(sys_node.children["a"].data) == b"AAA"
        assert bytes(sys_node.children["b"].data) == b"BBB"

    def test_imports_nested_tree(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "S").mkdir()
        (src / "S" / "Startup-Sequence").write_bytes(b";; script\n")
        (src / "C").mkdir()
        (src / "C" / "Copy").write_bytes(b"BINARY")

        bridge = FakeBridge()
        import_tree(src, bridge, "/Sys")

        sys_node = bridge.root.children["Sys"]
        assert bytes(sys_node.children["S"].children["Startup-Sequence"].data) == b";; script\n"
        assert bytes(sys_node.children["C"].children["Copy"].data) == b"BINARY"

    def test_applies_uaem_sidecar_metadata(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "file").write_bytes(b"x")
        provider = UaemProvider()
        provider.write_meta(src / "file", MetaInfo(
            protect_flags=ProtectFlags(0x40),
            mod_ts=_ts(5471, 720, 1500),
            comment=FSString("from-uaem"),
        ))

        bridge = FakeBridge()
        import_tree(src, bridge, "/Sys")

        node = bridge.root.children["Sys"].children["file"]
        assert bytes(node.data) == b"x"
        assert node.protection == 0x40
        assert node.comment == "from-uaem"
        assert node.date_days == 5471

    def test_applies_xdfmeta_manifest_metadata(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "file").write_bytes(b"y")
        provider = XdfMetaProvider()
        provider.write_meta(src / "file", MetaInfo(
            protect_flags=ProtectFlags(0x20),
            mod_ts=_ts(8000, 1000, 500),
            comment=FSString("from-xdfmeta"),
        ), tree_root=src)
        provider.set_volume_info(src, "X")
        provider.flush(src)

        bridge = FakeBridge()
        import_tree(src, bridge, "/Sys")

        node = bridge.root.children["Sys"].children["file"]
        assert node.protection == 0x20
        assert node.comment == "from-xdfmeta"
        # The .amiga-meta.xdfmeta itself was excluded from the import
        assert XDFMETA_MANIFEST_NAME not in bridge.root.children["Sys"].children

    def test_uaem_files_excluded_from_import(self, tmp_path):
        """.uaem sidecars are metadata, not data — they don't end up on the image."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "real").write_bytes(b"data")
        UaemProvider().write_meta(src / "real", MetaInfo(
            protect_flags=ProtectFlags(0x40),
            mod_ts=_ts(1, 0, 0),
            comment=FSString(""),
        ))

        bridge = FakeBridge()
        import_tree(src, bridge, "/Sys")

        sys_node = bridge.root.children["Sys"]
        assert "real" in sys_node.children
        assert "real.uaem" not in sys_node.children

    def test_filename_length_validation(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        long_name = "x" * 50
        (src / long_name).write_bytes(b"x")
        (src / "ok").write_bytes(b"OK")

        bridge = FakeBridge()
        with pytest.raises(OSError, match="filename exceeds"):
            import_tree(src, bridge, "/Sys", max_filename_len=30)

    def test_filename_length_skip_on_error(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        long_name = "x" * 50
        (src / long_name).write_bytes(b"x")
        (src / "ok").write_bytes(b"OK")

        bridge = FakeBridge()
        stats = import_tree(
            src, bridge, "/Sys",
            max_filename_len=30, on_error="skip",
        )

        assert "ok" in bridge.root.children["Sys"].children
        assert long_name not in bridge.root.children["Sys"].children
        assert any("filename exceeds" in e for e in stats.errors)

    def test_on_conflict_skip(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "file").write_bytes(b"NEW")

        bridge = FakeBridge()
        _add_dir(bridge, "/Sys")
        _add_file(bridge, "/Sys/file", b"PRESERVED")

        import_tree(src, bridge, "/Sys", on_conflict="skip")

        assert bytes(bridge.root.children["Sys"].children["file"].data) == b"PRESERVED"

    def test_on_conflict_error_raises(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "file").write_bytes(b"NEW")

        bridge = FakeBridge()
        _add_dir(bridge, "/Sys")
        _add_file(bridge, "/Sys/file", b"OLD")

        with pytest.raises(FileExistsError):
            import_tree(src, bridge, "/Sys", on_conflict="error")

    def test_missing_source_raises(self, tmp_path):
        bridge = FakeBridge()
        with pytest.raises(FileNotFoundError):
            import_tree(tmp_path / "missing", bridge, "/Sys")

    def test_source_must_be_directory(self, tmp_path):
        f = tmp_path / "afile"
        f.write_bytes(b"x")
        bridge = FakeBridge()
        with pytest.raises(NotADirectoryError):
            import_tree(f, bridge, "/Sys")

    def test_flush_called_at_end(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f").write_bytes(b"x")
        bridge = FakeBridge()
        import_tree(src, bridge, "/Sys")
        assert bridge.flushed is True


# ---------------------------------------------------------------------------
# Round-trip: export → import preserves content + metadata
# ---------------------------------------------------------------------------


class TestExportImportRoundTrip:
    @pytest.mark.parametrize("meta_format", ["uaem", "xdfmeta"])
    def test_full_roundtrip_preserves_metadata(self, tmp_path, meta_format):
        # Build a small source image
        src_bridge = FakeBridge()
        _add_dir(src_bridge, "/Sys")
        _add_dir(src_bridge, "/Sys/S")
        _add_file(src_bridge, "/Sys/S/Startup-Sequence", b";; boot\n",
                  protection=0x40, comment="boot script",
                  date_days=5471, date_mins=720, date_ticks=1500)
        _add_file(src_bridge, "/Sys/info", b"info",
                  protection=0x20, comment="readonly")

        # Extract to host
        extract_root = tmp_path / "extracted"
        export_tree(src_bridge, "/Sys", extract_root,
                    preserve=True, meta_format=meta_format)

        # Reimport to a fresh image
        dst_bridge = FakeBridge()
        import_tree(extract_root, dst_bridge, "/Sys")

        sys_node = dst_bridge.root.children["Sys"]
        startup = sys_node.children["S"].children["Startup-Sequence"]
        info = sys_node.children["info"]

        assert bytes(startup.data) == b";; boot\n"
        assert startup.protection == 0x40
        assert startup.comment == "boot script"
        assert startup.date_days == 5471

        assert bytes(info.data) == b"info"
        assert info.protection == 0x20
        assert info.comment == "readonly"
