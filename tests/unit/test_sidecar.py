"""Unit tests for amifuse.sidecar module.

These tests use the real vendored amitools — sidecar.py is independent of
fuse_fs so it doesn't need the fuse_mock fixture.
"""

from pathlib import Path

import pytest

from amitools.fs.FSString import FSString
from amitools.fs.MetaInfo import MetaInfo
from amitools.fs.ProtectFlags import ProtectFlags
from amitools.fs.TimeStamp import TimeStamp

from amifuse.sidecar import (
    SidecarRegistry,
    UaemProvider,
    XdfMetaProvider,
    XDFMETA_MANIFEST_NAME,
    default_registry,
    is_default_meta,
)


def _meta(mask=0, comment=None, days=0, mins=0, ticks=0, have_ts=True):
    """Build a MetaInfo with the requested fields, omitting ones marked None."""
    pf = ProtectFlags(mask)
    ts = TimeStamp(days=days, mins=mins, ticks=ticks) if have_ts else None
    cm = FSString(comment) if comment is not None else None
    return MetaInfo(protect_flags=pf, mod_ts=ts, comment=cm)


# ---------------------------------------------------------------------------
# is_default_meta
# ---------------------------------------------------------------------------


class TestIsDefaultMeta:
    def test_all_default_returns_true(self):
        # mask=0 (----rwed), no comment, no timestamp
        meta = MetaInfo(protect_flags=ProtectFlags(0), mod_ts=None, comment=None)
        assert is_default_meta(meta) is True

    def test_non_default_protection_returns_false(self):
        # script bit set
        meta = MetaInfo(protect_flags=ProtectFlags(0x40), mod_ts=None, comment=None)
        assert is_default_meta(meta) is False

    def test_non_empty_comment_returns_false(self):
        meta = MetaInfo(
            protect_flags=ProtectFlags(0),
            mod_ts=None,
            comment=FSString("note"),
        )
        assert is_default_meta(meta) is False

    def test_timestamp_present_returns_false(self):
        meta = MetaInfo(
            protect_flags=ProtectFlags(0),
            mod_ts=TimeStamp(days=1, mins=0, ticks=0),
            comment=None,
        )
        assert is_default_meta(meta) is False

    def test_empty_comment_treated_as_default(self):
        """FSString("") has no content worth preserving."""
        meta = MetaInfo(
            protect_flags=ProtectFlags(0),
            mod_ts=None,
            comment=FSString(""),
        )
        assert is_default_meta(meta) is True


# ---------------------------------------------------------------------------
# UaemProvider
# ---------------------------------------------------------------------------


class TestUaemProvider:
    def test_sidecar_path_appends_suffix(self, tmp_path):
        provider = UaemProvider()
        host_file = tmp_path / "Startup-Sequence"
        assert provider.sidecar_path_for(host_file) == (
            tmp_path / "Startup-Sequence.uaem"
        )

    def test_read_returns_none_when_sidecar_absent(self, tmp_path):
        provider = UaemProvider()
        host_file = tmp_path / "missing"
        host_file.write_text("data")
        assert provider.read_meta(host_file) is None

    def test_roundtrip_preserves_metadata(self, tmp_path):
        provider = UaemProvider()
        host_file = tmp_path / "file"
        host_file.write_text("contents")

        # ticks=500 = 10 sec into the minute; whole-second granularity that
        # round-trips losslessly through .uaem (see EXAMPLES note below).
        original = _meta(mask=0x40, comment="Some note", days=5471, mins=720, ticks=500)
        provider.write_meta(host_file, original)
        loaded = provider.read_meta(host_file)

        assert loaded is not None
        assert loaded.get_protect() == 0x40
        assert loaded.get_comment_unicode_str() == "Some note"
        ts = loaded.get_mod_ts()
        assert (ts.days, ts.mins, ts.ticks) == (5471, 720, 500)

    def test_roundtrip_with_latin1_filename_in_comment(self, tmp_path):
        provider = UaemProvider()
        host_file = tmp_path / "file"
        host_file.touch()

        # Comment with non-ASCII Latin-1 chars common in old Amiga annotations
        provider.write_meta(host_file, _meta(comment="Café"))
        loaded = provider.read_meta(host_file)
        assert loaded.get_comment_unicode_str() == "Café"

    def test_write_creates_sidecar_alongside_file(self, tmp_path):
        provider = UaemProvider()
        host_file = tmp_path / "subdir" / "f.txt"
        host_file.parent.mkdir()
        host_file.write_text("x")

        provider.write_meta(host_file, _meta(mask=0x40))

        sidecar = host_file.with_name("f.txt.uaem")
        assert sidecar.exists()

    def test_flush_is_a_noop(self, tmp_path):
        """Per-file providers persist on write_meta; flush does nothing."""
        provider = UaemProvider()
        provider.flush()  # must not raise
        provider.flush(tmp_path)  # must not raise


# ---------------------------------------------------------------------------
# XdfMetaProvider
# ---------------------------------------------------------------------------


class TestXdfMetaProvider:
    def test_requires_tree_root(self, tmp_path):
        provider = XdfMetaProvider()
        host_file = tmp_path / "f"
        with pytest.raises(ValueError, match="tree_root"):
            provider.read_meta(host_file, None)
        with pytest.raises(ValueError, match="tree_root"):
            provider.write_meta(host_file, _meta(), None)

    def test_read_returns_none_when_manifest_absent(self, tmp_path):
        provider = XdfMetaProvider()
        host_file = tmp_path / "f"
        host_file.touch()
        assert provider.read_meta(host_file, tmp_path) is None

    def test_write_then_flush_creates_manifest(self, tmp_path):
        provider = XdfMetaProvider()
        host_file = tmp_path / "f"
        host_file.touch()

        provider.write_meta(host_file, _meta(mask=0x40, comment="note"), tmp_path)

        # Manifest should not exist yet — writes are buffered.
        manifest = tmp_path / XDFMETA_MANIFEST_NAME
        assert not manifest.exists()

        # set_volume_info is required because MetaDB.save references the
        # volume's root meta when writing the header.
        provider.set_volume_info(tmp_path, volume_name="Test")
        provider.flush(tmp_path)

        assert manifest.exists()
        # Manifest should reference our file
        content = manifest.read_text()
        assert "f:" in content
        assert "note" in content

    def test_roundtrip_via_fresh_provider_reads_back_metadata(self, tmp_path):
        """Write with one provider instance; read with a fresh one."""
        writer = XdfMetaProvider()
        host_file = tmp_path / "Startup-Sequence"
        host_file.touch()

        writer.write_meta(
            host_file,
            _meta(mask=0x40, comment="The note", days=5471, mins=720, ticks=500),
            tmp_path,
        )
        writer.set_volume_info(tmp_path, volume_name="Sys")
        writer.flush(tmp_path)

        reader = XdfMetaProvider()
        loaded = reader.read_meta(host_file, tmp_path)

        assert loaded is not None
        assert loaded.get_protect() == 0x40
        assert loaded.get_comment_unicode_str() == "The note"
        ts = loaded.get_mod_ts()
        assert (ts.days, ts.mins, ts.ticks) == (5471, 720, 500)

    def test_relative_path_uses_forward_slash(self, tmp_path):
        """Manifest keys are stored as Amiga-style forward-slash paths."""
        provider = XdfMetaProvider()
        host_file = tmp_path / "S" / "Startup-Sequence"
        host_file.parent.mkdir()
        host_file.touch()

        provider.write_meta(host_file, _meta(comment="hi"), tmp_path)
        provider.set_volume_info(tmp_path, volume_name="X")
        provider.flush(tmp_path)

        manifest_text = (tmp_path / XDFMETA_MANIFEST_NAME).read_text()
        # Should use forward slash regardless of host OS
        assert "S/Startup-Sequence" in manifest_text

    def test_files_outside_tree_root_return_none(self, tmp_path):
        """A file outside the manifest's tree returns None silently."""
        provider = XdfMetaProvider()
        # Set up a populated manifest
        inside = tmp_path / "inside"
        inside.touch()
        provider.write_meta(inside, _meta(comment="x"), tmp_path)
        provider.set_volume_info(tmp_path, "X")
        provider.flush(tmp_path)

        outside = tmp_path.parent / "outside-file"
        assert provider.read_meta(outside, tmp_path) is None

    def test_multiple_files_in_one_manifest(self, tmp_path):
        provider = XdfMetaProvider()

        # Three files with distinct metadata
        for name, mask, comment in [
            ("a", 0x10, "comment-a"),
            ("b", 0x40, "comment-b"),
            ("c", 0, "comment-c"),
        ]:
            f = tmp_path / name
            f.touch()
            provider.write_meta(f, _meta(mask=mask, comment=comment), tmp_path)
        provider.set_volume_info(tmp_path, "X")
        provider.flush(tmp_path)

        # Re-read with a fresh provider; verify all three entries
        fresh = XdfMetaProvider()
        for name, mask, comment in [
            ("a", 0x10, "comment-a"),
            ("b", 0x40, "comment-b"),
            ("c", 0, "comment-c"),
        ]:
            loaded = fresh.read_meta(tmp_path / name, tmp_path)
            assert loaded is not None
            assert loaded.get_protect() == mask
            assert loaded.get_comment_unicode_str() == comment


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestSidecarRegistry:
    def test_register_orders_by_priority(self):
        reg = SidecarRegistry()
        late = UaemProvider()
        early = XdfMetaProvider()
        reg.register(late, priority=50)
        reg.register(early, priority=10)
        assert reg.names() == ["xdfmeta", "uaem"]

    def test_default_registry_lineup(self):
        reg = default_registry()
        assert reg.names() == ["xdfmeta", "uaem"]

    def test_by_name_returns_provider(self):
        reg = default_registry()
        assert reg.by_name("uaem").name == "uaem"
        assert reg.by_name("xdfmeta").name == "xdfmeta"

    def test_by_name_raises_on_unknown(self):
        reg = default_registry()
        with pytest.raises(KeyError, match="uaefsdb"):
            reg.by_name("uaefsdb")

    def test_detect_prefers_xdfmeta_when_both_present(self, tmp_path):
        """If both formats have metadata for a file, manifest wins."""
        host_file = tmp_path / "f"
        host_file.touch()

        # Lay down a .uaem
        uaem = UaemProvider()
        uaem.write_meta(host_file, _meta(comment="from-uaem"))

        # Lay down an xdfmeta covering the same file (different comment)
        xdf = XdfMetaProvider()
        xdf.write_meta(host_file, _meta(comment="from-xdfmeta"), tmp_path)
        xdf.set_volume_info(tmp_path, "X")
        xdf.flush(tmp_path)

        reg = default_registry()
        match = reg.detect(host_file, tmp_path)
        assert match is not None
        provider, meta = match
        assert provider.name == "xdfmeta"
        assert meta.get_comment_unicode_str() == "from-xdfmeta"

    def test_detect_falls_through_to_uaem_when_no_manifest(self, tmp_path):
        host_file = tmp_path / "f"
        host_file.touch()
        UaemProvider().write_meta(host_file, _meta(comment="only-uaem"))

        reg = default_registry()
        match = reg.detect(host_file, tmp_path)
        assert match is not None
        provider, meta = match
        assert provider.name == "uaem"
        assert meta.get_comment_unicode_str() == "only-uaem"

    def test_detect_returns_none_when_no_sidecar(self, tmp_path):
        host_file = tmp_path / "naked"
        host_file.touch()
        reg = default_registry()
        assert reg.detect(host_file, tmp_path) is None

    def test_detect_without_tree_root_skips_per_volume_providers(self, tmp_path):
        """When tree_root isn't supplied, manifest providers are skipped, not errored."""
        host_file = tmp_path / "f"
        host_file.touch()
        UaemProvider().write_meta(host_file, _meta(comment="x"))

        reg = default_registry()
        match = reg.detect(host_file, tree_root=None)
        assert match is not None
        # Should fall through to uaem since xdfmeta silently skipped
        assert match[0].name == "uaem"


# ---------------------------------------------------------------------------
# Round-trip property test
# ---------------------------------------------------------------------------


class TestRoundTripExamples:
    """Concrete round-trip cases that catch subtle drift in date/protect/comment.

    Hypothesis-based property testing is on the TODO list; these example-based
    cases cover the common bit patterns and known edge cases for now.
    """

    # NOTE on ticks: amitools' TimeStamp drops sub-second precision when
    # round-tripping through .uaem (the format stores seconds + centiseconds,
    # but amitools' on-disk write uses ts.ticks, which becomes (secs*50) after
    # parse). Test data uses ticks that are multiples of 50 (whole-second
    # granularity), which is the precision the format preserves losslessly.
    EXAMPLES = [
        # (mask, comment, days, mins, ticks, description)
        (0x00, "",        0, 0, 0,    "all defaults"),
        (0x40, "",        5471, 720, 1500, "script bit + timestamp"),
        (0x20, "Pure",    0, 0, 0,    "pure bit only"),
        (0x10, "Archive", 8000, 1439, 2950, "archive bit"),
        (0xFF, "All flags", 1, 1, 50, "all special bits set"),
        (0x00, "Latin-1: Café & Müller", 5000, 600, 0, "non-ASCII comment"),
        (0x00, "x" * 80,  100, 0, 0,  "long comment"),
    ]

    @pytest.mark.parametrize(
        "mask,comment,days,mins,ticks,description",
        EXAMPLES,
        ids=[e[-1] for e in EXAMPLES],
    )
    def test_uaem_roundtrip(self, tmp_path, mask, comment, days, mins, ticks, description):
        provider = UaemProvider()
        host_file = tmp_path / "f"
        host_file.touch()

        original = _meta(mask=mask, comment=comment, days=days, mins=mins, ticks=ticks)
        provider.write_meta(host_file, original)
        loaded = provider.read_meta(host_file)

        assert loaded.get_protect() == mask, f"protect mismatch ({description})"
        # .uaem encodes/decodes UTF-8; comment should round-trip
        assert loaded.get_comment_unicode_str() == comment, (
            f"comment mismatch ({description})"
        )
        ts = loaded.get_mod_ts()
        assert (ts.days, ts.mins, ts.ticks) == (days, mins, ticks), (
            f"timestamp mismatch ({description})"
        )

    @pytest.mark.parametrize(
        "mask,comment,days,mins,ticks,description",
        EXAMPLES,
        ids=[e[-1] for e in EXAMPLES],
    )
    def test_xdfmeta_roundtrip(self, tmp_path, mask, comment, days, mins, ticks, description):
        provider = XdfMetaProvider()
        host_file = tmp_path / "f"
        host_file.touch()

        original = _meta(mask=mask, comment=comment, days=days, mins=mins, ticks=ticks)
        provider.write_meta(host_file, original, tmp_path)
        provider.set_volume_info(tmp_path, "X")
        provider.flush(tmp_path)

        fresh = XdfMetaProvider()
        loaded = fresh.read_meta(host_file, tmp_path)

        assert loaded.get_protect() == mask, f"protect mismatch ({description})"
        assert loaded.get_comment_unicode_str() == comment, (
            f"comment mismatch ({description})"
        )
        ts = loaded.get_mod_ts()
        assert (ts.days, ts.mins, ts.ticks) == (days, mins, ticks), (
            f"timestamp mismatch ({description})"
        )
