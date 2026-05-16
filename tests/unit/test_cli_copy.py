"""CLI surface tests for Phase 4: amifuse cp, read --preserve, write meta.

The tests fall into three layers:

  - _parse_image_path: pure function, easy table tests
  - argparse: instantiate the parser, verify the new flags/defaults
  - end-to-end via cmd_cp/cmd_read/cmd_write: monkeypatch
    _create_bridge_from_args to return FakeBridges from test_copy

The end-to-end layer exercises the full argument-handling path (including
the --preserve/--meta-format wiring) without spinning up a real handler.
"""

from __future__ import annotations

import argparse
import io
import json as _json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Bring in the FakeBridge fixtures from the copy engine tests
from tests.unit.test_copy import FakeBridge, _add_dir, _add_file


# ---------------------------------------------------------------------------
# _parse_image_path
# ---------------------------------------------------------------------------


class TestParseImagePath:
    def test_simple_split(self, fuse_mock):
        from amifuse.fuse_fs import _parse_image_path
        assert _parse_image_path("disk.hdf:/Sys/S/Startup-Sequence") == (
            "disk.hdf", "/Sys/S/Startup-Sequence"
        )

    def test_relative_image_and_root_path(self, fuse_mock):
        from amifuse.fuse_fs import _parse_image_path
        assert _parse_image_path("./disk.hdf:/") == ("./disk.hdf", "/")

    def test_path_without_leading_slash_is_normalized(self, fuse_mock):
        from amifuse.fuse_fs import _parse_image_path
        assert _parse_image_path("disk.hdf:Sys/S") == ("disk.hdf", "/Sys/S")

    def test_empty_path_defaults_to_root(self, fuse_mock):
        from amifuse.fuse_fs import _parse_image_path
        assert _parse_image_path("disk.hdf:") == ("disk.hdf", "/")

    def test_missing_colon_errors(self, fuse_mock):
        from amifuse.fuse_fs import _parse_image_path
        with pytest.raises(SystemExit, match="missing ':path'"):
            _parse_image_path("disk.hdf")

    def test_empty_image_errors(self, fuse_mock):
        from amifuse.fuse_fs import _parse_image_path
        with pytest.raises(SystemExit, match="empty image part"):
            _parse_image_path(":/path")

    def test_multiple_colons_splits_on_last(self, fuse_mock):
        """Useful when Amiga path contains a colon — we split on the last one."""
        from amifuse.fuse_fs import _parse_image_path
        # An image filename containing a colon plus an Amiga path
        assert _parse_image_path("dir:weird.hdf:/Sys") == ("dir:weird.hdf", "/Sys")


# ---------------------------------------------------------------------------
# argparse: cp / read --preserve / write --meta-format
# ---------------------------------------------------------------------------


def _build_parser():
    """Reach into main() to construct the same argparse setup it builds.

    main() calls parser.parse_args(argv) and dispatches; for tests we want
    the parser itself to inspect the parsed Namespace. We re-implement the
    factory by calling main() with --help intercepted — but a cleaner
    approach is to invoke main() with argv that just triggers parse_args
    on the cp subcommand and capture the resulting namespace.

    Simplest: parse a known cp argv via main() and intercept by patching
    args.func to a recorder.
    """
    # Lazy import: fuse_mock fixture must have run before fuse_fs imports
    import amifuse.fuse_fs as fuse_fs  # noqa: F401
    # We construct the parser by re-running the relevant setup. Re-using
    # main() directly is simplest.
    return fuse_fs


class TestCpParserDefaults:
    def test_cp_required_args(self, fuse_mock, monkeypatch):
        from amifuse import fuse_fs

        captured = {}

        def fake_cmd_cp(args):
            captured["args"] = args

        monkeypatch.setattr(fuse_fs, "cmd_cp", fake_cmd_cp)
        # The parser captures args.func at parse time — patch the dispatch.
        fuse_fs.main(
            ["cp", "src.hdf:/file", "dst.hdf:/file"]
        )

        args = captured["args"]
        assert args.src == "src.hdf:/file"
        assert args.dst == "dst.hdf:/file"
        # Defaults
        assert args.recursive is False
        assert args.preserve is True
        assert args.atomic is True
        assert args.on_error == "abort"
        assert args.partition is None
        assert args.driver is None
        assert args.json is False

    def test_cp_recursive_flag(self, fuse_mock, monkeypatch):
        from amifuse import fuse_fs

        captured = {}
        monkeypatch.setattr(fuse_fs, "cmd_cp", lambda a: captured.setdefault("a", a))
        fuse_fs.main(["cp", "-r", "src:/d", "dst:/d"])
        assert captured["a"].recursive is True

    def test_cp_no_preserve(self, fuse_mock, monkeypatch):
        from amifuse import fuse_fs

        captured = {}
        monkeypatch.setattr(fuse_fs, "cmd_cp", lambda a: captured.setdefault("a", a))
        fuse_fs.main(["cp", "--no-preserve", "src:/f", "dst:/f"])
        assert captured["a"].preserve is False

    def test_cp_no_atomic(self, fuse_mock, monkeypatch):
        from amifuse import fuse_fs

        captured = {}
        monkeypatch.setattr(fuse_fs, "cmd_cp", lambda a: captured.setdefault("a", a))
        fuse_fs.main(["cp", "--no-atomic", "src:/f", "dst:/f"])
        assert captured["a"].atomic is False

    def test_cp_skip_existing(self, fuse_mock, monkeypatch):
        from amifuse import fuse_fs

        captured = {}
        monkeypatch.setattr(fuse_fs, "cmd_cp", lambda a: captured.setdefault("a", a))
        fuse_fs.main(["cp", "--skip-existing", "src:/f", "dst:/f"])
        assert captured["a"].skip_existing is True

    def test_cp_per_side_partition_overrides(self, fuse_mock, monkeypatch):
        from amifuse import fuse_fs

        captured = {}
        monkeypatch.setattr(fuse_fs, "cmd_cp", lambda a: captured.setdefault("a", a))
        fuse_fs.main([
            "cp", "--partition", "DH0",
            "--src-partition", "DH1",
            "--dst-partition", "DH2",
            "src:/", "dst:/",
        ])
        a = captured["a"]
        assert a.partition == "DH0"
        assert a.src_partition == "DH1"
        assert a.dst_partition == "DH2"


class TestReadPreserveParser:
    def test_read_preserve_flag_default_false(self, fuse_mock, monkeypatch):
        from amifuse import fuse_fs

        captured = {}
        monkeypatch.setattr(fuse_fs, "cmd_read", lambda a: captured.setdefault("a", a))
        fuse_fs.main(["read", "img.hdf", "--file", "/foo", "--out", "out"])
        assert captured["a"].preserve is False
        # Default meta_format is auto when --preserve is set; flag exists
        # regardless of preserve so the namespace always has it.
        assert captured["a"].meta_format == "auto"

    def test_read_preserve_set(self, fuse_mock, monkeypatch):
        from amifuse import fuse_fs

        captured = {}
        monkeypatch.setattr(fuse_fs, "cmd_read", lambda a: captured.setdefault("a", a))
        fuse_fs.main(["read", "img.hdf", "--file", "/foo", "--out", "out",
                      "--preserve", "--meta-format", "xdfmeta"])
        a = captured["a"]
        assert a.preserve is True
        assert a.meta_format == "xdfmeta"


class TestWriteMetaParser:
    def test_write_meta_format_defaults(self, fuse_mock, monkeypatch):
        from amifuse import fuse_fs

        captured = {}
        monkeypatch.setattr(fuse_fs, "cmd_write", lambda a: captured.setdefault("a", a))
        fuse_fs.main(["write", "img.hdf", "--file", "/foo", "--in", "out"])
        a = captured["a"]
        assert a.meta_format == "auto"
        assert a.meta_from is None

    def test_write_meta_format_none(self, fuse_mock, monkeypatch):
        from amifuse import fuse_fs

        captured = {}
        monkeypatch.setattr(fuse_fs, "cmd_write", lambda a: captured.setdefault("a", a))
        fuse_fs.main(["write", "img.hdf", "--file", "/foo", "--in", "out",
                      "--meta-format", "none"])
        assert captured["a"].meta_format == "none"

    def test_write_meta_from_path(self, fuse_mock, monkeypatch):
        from amifuse import fuse_fs

        captured = {}
        monkeypatch.setattr(fuse_fs, "cmd_write", lambda a: captured.setdefault("a", a))
        fuse_fs.main(["write", "img.hdf", "--file", "/foo", "--in", "out",
                      "--meta-from", "/tmp/x.uaem"])
        assert captured["a"].meta_from == "/tmp/x.uaem"


# ---------------------------------------------------------------------------
# End-to-end: cmd_cp via FakeBridges
# ---------------------------------------------------------------------------


def _patch_bridges(monkeypatch, src_bridge, dst_bridge):
    """Patch _create_bridge_from_args to return (src, dst) bridges in order."""
    from amifuse import fuse_fs

    calls = [src_bridge, dst_bridge]

    def fake_create(args, command, read_only=True):
        if not calls:
            raise RuntimeError("more bridge requests than expected")
        return calls.pop(0), None

    monkeypatch.setattr(fuse_fs, "_create_bridge_from_args", fake_create)


class TestCmdCpEndToEnd:
    def test_single_file_copy(self, fuse_mock, monkeypatch, tmp_path):
        """End-to-end exercise of cp single-file via FakeBridges."""
        from amifuse import fuse_fs

        src = FakeBridge()
        dst = FakeBridge()
        _add_file(src, "/file", b"hello", protection=0x40)

        # Make image files exist so the early existence check passes
        (tmp_path / "src.hdf").touch()
        (tmp_path / "dst.hdf").touch()

        _patch_bridges(monkeypatch, src, dst)

        fuse_fs.main([
            "cp",
            f"{tmp_path}/src.hdf:/file",
            f"{tmp_path}/dst.hdf:/file",
            "--no-atomic",
        ])

        assert "file" in dst.root.children
        assert bytes(dst.root.children["file"].data) == b"hello"
        assert dst.root.children["file"].protection == 0x40

    def test_recursive_copy(self, fuse_mock, monkeypatch, tmp_path, capsys):
        from amifuse import fuse_fs

        src = FakeBridge()
        dst = FakeBridge()
        _add_dir(src, "/Sys")
        _add_file(src, "/Sys/a", b"AAA")
        _add_file(src, "/Sys/b", b"BB", protection=0x40)

        (tmp_path / "src.hdf").touch()
        (tmp_path / "dst.hdf").touch()
        _patch_bridges(monkeypatch, src, dst)

        fuse_fs.main([
            "cp", "-r", "--no-atomic",
            f"{tmp_path}/src.hdf:/Sys",
            f"{tmp_path}/dst.hdf:/Sys",
        ])

        assert "Sys" in dst.root.children
        assert "a" in dst.root.children["Sys"].children
        assert "b" in dst.root.children["Sys"].children
        assert dst.root.children["Sys"].children["b"].protection == 0x40

        captured = capsys.readouterr()
        assert "Files copied: 2" in captured.out
        assert "Bytes copied: 5" in captured.out

    def test_json_output(self, fuse_mock, monkeypatch, tmp_path, capsys):
        from amifuse import fuse_fs

        src = FakeBridge()
        dst = FakeBridge()
        _add_file(src, "/file", b"x")
        (tmp_path / "src.hdf").touch()
        (tmp_path / "dst.hdf").touch()
        _patch_bridges(monkeypatch, src, dst)

        fuse_fs.main([
            "cp", "--json", "--no-atomic",
            f"{tmp_path}/src.hdf:/file",
            f"{tmp_path}/dst.hdf:/file",
        ])

        result = _json.loads(capsys.readouterr().out)
        assert result["status"] == "ok"
        assert result["command"] == "cp"
        assert result["files_copied"] == 1
        assert result["bytes_copied"] == 1

    def test_missing_image_error_json(self, fuse_mock, monkeypatch, tmp_path, capsys):
        from amifuse import fuse_fs

        # No file created → IMAGE_NOT_FOUND
        _patch_bridges(monkeypatch, FakeBridge(), FakeBridge())

        with pytest.raises(SystemExit):
            fuse_fs.main([
                "cp", "--json",
                f"{tmp_path}/missing-src.hdf:/file",
                f"{tmp_path}/missing-dst.hdf:/file",
            ])

        result = _json.loads(capsys.readouterr().out)
        assert result["status"] == "error"
        assert result["error"]["code"] == "IMAGE_NOT_FOUND"
        assert "missing-src.hdf" in result["error"]["message"]


# ---------------------------------------------------------------------------
# read --preserve emits sidecar
# ---------------------------------------------------------------------------


class TestReadPreserveSidecar:
    def test_uaem_sidecar_written_for_non_default_meta(
        self, fuse_mock, monkeypatch, tmp_path
    ):
        from amifuse import fuse_fs

        bridge = FakeBridge()
        _add_file(bridge, "/file", b"x",
                  protection=0x40, comment="hello",
                  date_days=5471, date_mins=720, date_ticks=1500)
        (tmp_path / "img.hdf").touch()

        def fake_create(args, command, read_only=True):
            return bridge, None

        monkeypatch.setattr(fuse_fs, "_create_bridge_from_args", fake_create)

        out_path = tmp_path / "extracted"
        fuse_fs.main([
            "read", str(tmp_path / "img.hdf"),
            "--file", "/file", "--out", str(out_path),
            "--preserve",
        ])

        sidecar = tmp_path / "extracted.uaem"
        assert out_path.exists()
        assert sidecar.exists()
        content = sidecar.read_text()
        # The protection string for FIBF mask 0x40 has 's' bit visible
        assert "s" in content[:8]
        assert "hello" in content

    def test_recursive_read_extracts_tree_with_xdfmeta_manifest(
        self, fuse_mock, monkeypatch, tmp_path
    ):
        """read -r --preserve produces a host tree with xdfmeta manifest by default."""
        from amifuse import fuse_fs
        from amifuse.sidecar import XDFMETA_MANIFEST_NAME, XdfMetaProvider

        bridge = FakeBridge()
        _add_dir(bridge, "/Sys")
        _add_file(bridge, "/Sys/a", b"AAA",
                  protection=0x40, comment="note-a",
                  date_days=5471, date_mins=720, date_ticks=1500)
        _add_dir(bridge, "/Sys/S")
        _add_file(bridge, "/Sys/S/Startup-Sequence", b";; boot\n",
                  protection=0x20, comment="boot script")

        (tmp_path / "img.hdf").touch()
        monkeypatch.setattr(
            fuse_fs, "_create_bridge_from_args",
            lambda a, c, read_only=True: (bridge, None),
        )

        out_dir = tmp_path / "out"
        fuse_fs.main([
            "read", str(tmp_path / "img.hdf"),
            "-r", "--file", "/Sys", "--out", str(out_dir),
            "--preserve",
        ])

        assert (out_dir / "a").read_bytes() == b"AAA"
        assert (out_dir / "S" / "Startup-Sequence").read_bytes() == b";; boot\n"
        # xdfmeta is the default for recursive extract
        assert (out_dir / XDFMETA_MANIFEST_NAME).exists()

        # Verify metadata is reachable via the manifest
        provider = XdfMetaProvider()
        meta_a = provider.read_meta(out_dir / "a", out_dir)
        assert meta_a is not None
        assert meta_a.get_protect() == 0x40
        assert meta_a.get_comment_unicode_str() == "note-a"

    def test_recursive_read_with_uaem_format(
        self, fuse_mock, monkeypatch, tmp_path
    ):
        """read -r --preserve --meta-format uaem produces per-file sidecars instead."""
        from amifuse import fuse_fs

        bridge = FakeBridge()
        _add_dir(bridge, "/d")
        _add_file(bridge, "/d/file", b"x",
                  protection=0x40, comment="hi")

        (tmp_path / "img.hdf").touch()
        monkeypatch.setattr(
            fuse_fs, "_create_bridge_from_args",
            lambda a, c, read_only=True: (bridge, None),
        )

        out_dir = tmp_path / "out"
        fuse_fs.main([
            "read", str(tmp_path / "img.hdf"),
            "-r", "--file", "/d", "--out", str(out_dir),
            "--preserve", "--meta-format", "uaem",
        ])

        assert (out_dir / "file").exists()
        assert (out_dir / "file.uaem").exists()

    def test_no_sidecar_for_default_metadata(
        self, fuse_mock, monkeypatch, tmp_path
    ):
        """File with default protection/no comment/no timestamp gets no sidecar."""
        from amifuse import fuse_fs

        bridge = FakeBridge()
        _add_file(bridge, "/plain", b"x")  # protection=0, comment="", date=0
        (tmp_path / "img.hdf").touch()

        def fake_create(args, command, read_only=True):
            return bridge, None

        monkeypatch.setattr(fuse_fs, "_create_bridge_from_args", fake_create)

        out_path = tmp_path / "extracted"
        fuse_fs.main([
            "read", str(tmp_path / "img.hdf"),
            "--file", "/plain", "--out", str(out_path),
            "--preserve",
        ])

        assert out_path.exists()
        assert not (tmp_path / "extracted.uaem").exists()


# ---------------------------------------------------------------------------
# write applies sidecar metadata
# ---------------------------------------------------------------------------


class TestWriteAppliesSidecar:
    def test_uaem_sidecar_applied_automatically(
        self, fuse_mock, monkeypatch, tmp_path
    ):
        """write should pick up a .uaem next to --in and apply metadata."""
        from amifuse import fuse_fs
        from amifuse.sidecar import UaemProvider
        from amitools.fs.MetaInfo import MetaInfo
        from amitools.fs.ProtectFlags import ProtectFlags
        from amitools.fs.FSString import FSString
        from amitools.fs.TimeStamp import TimeStamp

        bridge = FakeBridge()
        # Stage host source file plus its sidecar
        src_path = tmp_path / "src.txt"
        src_path.write_bytes(b"hello")
        meta = MetaInfo(
            protect_flags=ProtectFlags(0x40),
            mod_ts=TimeStamp(days=5471, mins=720, ticks=1500),
            comment=FSString("from sidecar"),
        )
        UaemProvider().write_meta(src_path, meta)

        (tmp_path / "img.hdf").touch()
        monkeypatch.setattr(
            fuse_fs, "_create_bridge_from_args",
            lambda a, c, read_only=True: (bridge, None),
        )

        fuse_fs.main([
            "write", str(tmp_path / "img.hdf"),
            "--file", "/foo", "--in", str(src_path),
        ])

        # The fake bridge should now have a file with the metadata applied
        node = bridge.root.children["foo"]
        assert bytes(node.data) == b"hello"
        assert node.protection == 0x40
        assert node.comment == "from sidecar"
        assert node.date_days == 5471

    def test_meta_format_none_skips_sidecar(
        self, fuse_mock, monkeypatch, tmp_path
    ):
        from amifuse import fuse_fs
        from amifuse.sidecar import UaemProvider
        from amitools.fs.MetaInfo import MetaInfo
        from amitools.fs.ProtectFlags import ProtectFlags
        from amitools.fs.FSString import FSString

        bridge = FakeBridge()
        src_path = tmp_path / "src.txt"
        src_path.write_bytes(b"hi")
        from amitools.fs.TimeStamp import TimeStamp
        UaemProvider().write_meta(
            src_path,
            MetaInfo(
                protect_flags=ProtectFlags(0x40),
                mod_ts=TimeStamp(days=1, mins=0, ticks=0),
                comment=FSString("ignored"),
            ),
        )

        (tmp_path / "img.hdf").touch()
        monkeypatch.setattr(
            fuse_fs, "_create_bridge_from_args",
            lambda a, c, read_only=True: (bridge, None),
        )

        fuse_fs.main([
            "write", str(tmp_path / "img.hdf"),
            "--file", "/foo", "--in", str(src_path),
            "--meta-format", "none",
        ])

        # Bytes written, but metadata stays at default
        node = bridge.root.children["foo"]
        assert bytes(node.data) == b"hi"
        assert node.protection == 0
        assert node.comment == ""

    def test_recursive_write_imports_tree_with_sidecars(
        self, fuse_mock, monkeypatch, tmp_path
    ):
        """write -r <host-dir> imports a tree, applying .uaem sidecars per file."""
        from amifuse import fuse_fs
        from amifuse.sidecar import UaemProvider
        from amitools.fs.MetaInfo import MetaInfo
        from amitools.fs.ProtectFlags import ProtectFlags
        from amitools.fs.FSString import FSString
        from amitools.fs.TimeStamp import TimeStamp

        src = tmp_path / "extracted"
        src.mkdir()
        (src / "a").write_bytes(b"AAA")
        UaemProvider().write_meta(src / "a", MetaInfo(
            protect_flags=ProtectFlags(0x40),
            mod_ts=TimeStamp(days=5471, mins=720, ticks=1500),
            comment=FSString("note-a"),
        ))
        (src / "S").mkdir()
        (src / "S" / "Startup-Sequence").write_bytes(b";; boot\n")

        bridge = FakeBridge()
        (tmp_path / "img.hdf").touch()
        monkeypatch.setattr(
            fuse_fs, "_create_bridge_from_args",
            lambda a, c, read_only=True: (bridge, None),
        )

        fuse_fs.main([
            "write", str(tmp_path / "img.hdf"),
            "-r", "--file", "/Sys", "--in", str(src),
        ])

        sys_node = bridge.root.children["Sys"]
        assert bytes(sys_node.children["a"].data) == b"AAA"
        assert sys_node.children["a"].protection == 0x40
        assert sys_node.children["a"].comment == "note-a"
        # Sidecar .uaem was excluded from the import
        assert "a.uaem" not in sys_node.children
        assert "S" in sys_node.children
        assert "Startup-Sequence" in sys_node.children["S"].children

    def test_meta_format_uaem_requires_sidecar_present(
        self, fuse_mock, monkeypatch, tmp_path
    ):
        """--meta-format uaem with no .uaem alongside source → error."""
        from amifuse import fuse_fs

        bridge = FakeBridge()
        src_path = tmp_path / "src.txt"
        src_path.write_bytes(b"x")
        # No sidecar created

        (tmp_path / "img.hdf").touch()
        monkeypatch.setattr(
            fuse_fs, "_create_bridge_from_args",
            lambda a, c, read_only=True: (bridge, None),
        )

        with pytest.raises(SystemExit):
            fuse_fs.main([
                "write", str(tmp_path / "img.hdf"),
                "--file", "/foo", "--in", str(src_path),
                "--meta-format", "uaem",
            ])
