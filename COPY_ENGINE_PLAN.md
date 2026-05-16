# Amiga-aware Copy Engine — Design & Implementation Plan

This document describes the addition of a metadata-faithful, cross-filesystem
copy engine to AmiFUSE and the supporting sidecar-format I/O layer. The work is
broken into focused PRs that each land independently.

## Goals

- **Metadata-faithful image-to-image copy.** Protection bits, file comments,
  and Amiga datestamps must round-trip exactly across the supported filesystems.
- **Cross-filesystem.** The copy engine operates at the AmigaDOS packet layer,
  so it works between any two filesystems that have a working native handler in
  AmiFUSE: PFS3 ↔ PFS3, PFS3 ↔ FFS, SFS ↔ PFS3, etc.
- **Sidecar interoperability.** Read and write `.uaem` (FS-UAE) and `xdfmeta`
  (amitools) formats so that AmiFUSE plays nicely with the wider Amiga tooling
  ecosystem and host-side staging workflows survive without metadata loss.
- **In-process efficiency.** Recursive copies avoid the kernel FUSE round-trip
  and run with both source and target images open in the same Python process.

## Non-goals

- **Block-level partition cloning.** `dd` already handles this with bit-perfect
  fidelity for same-size, same-FS-type partitions. Documented as the right tool
  for the bulk path; not wrapped in a new subcommand.
- **`.uaefsdb` write support.** WinUAE's binary sidecar format is read-only in
  this plan and read support is itself parked behind a "later if requested"
  flag. Format spec lookup is out of scope.
- **A general installer pipeline.** That orchestration tool builds on top of
  these primitives but lives outside AmiFUSE proper.

## Filesystem support matrix

| Filesystem | Source (read) | Destination (write) | Notes |
|---|---|---|---|
| PFS3 | yes | yes | Primary target |
| FFS (DOS\\0–\\4) | yes | yes | Native handler via `L:FastFileSystem` |
| SFS | yes | yes | Tested with SFS 1.279 |
| BFFS | yes | yes | Tested with bffs handler |
| OD (Optical Disc FS) | yes | n/a | Read-only by design |
| CDFS | yes | n/a | Read-only by design |
| ADF (FFS on floppy) | yes | yes | Same handler as HDF FFS |

Anything that AmiFUSE can mount, the copy engine can use as a source. Anything
AmiFUSE can write (write mode), it can use as a destination. There are no
filesystem-specific code paths in the copy engine itself — the differences live
inside each handler.

### Filename-length caveat

| FS | Max filename length |
|---|---|
| FFS | 30 chars |
| PFS3 | 107 chars |
| SFS | 107 chars |

A PFS3 → FFS copy may fail on overlong source names. The engine **detects this
before the write attempt** and reports the offending paths rather than silently
truncating or letting the handler return an opaque error.

---

## Architecture

Three layered components, bottom up:

```
┌─────────────────────────────────────────────────────────────┐
│ CLI: amifuse cp [-r], amifuse read --preserve, write        │
├─────────────────────────────────────────────────────────────┤
│ Copy engine (amifuse/copy.py)                               │
│  · walk + plan + execute                                    │
│  · two-bridge orchestration                                  │
│  · resumability, atomicity, progress                         │
├──────────────────────┬──────────────────────────────────────┤
│ Sidecar layer        │ Packet senders                       │
│ amifuse/sidecar.py   │ amifuse/startup_runner.py            │
│  · UaemProvider      │  · send_set_protect                  │
│  · XdfMetaProvider   │  · send_set_comment                  │
│  · (UaeFsDbProvider) │  · send_set_date                     │
└──────────────────────┴──────────────────────────────────────┘
                         │
                         ▼
                  HandlerBridge (existing)
                  amitools' MetaInfo, ProtectFlags, TimeStamp
```

The packet senders are the foundation. Sidecar I/O and the copy engine both
build on them, but neither depends on the other — sidecars can be added or
swapped without touching the copy engine.

---

## Component 1: Packet senders

### Packet numbers

Standard AmigaDOS dos.library packet types ([autodocs ref][dos-packets]):

| Packet | Decimal | Purpose |
|---|---|---|
| `ACTION_SET_PROTECT` | 21 | Set file/dir protection bits |
| `ACTION_SET_COMMENT` | 28 | Set file/dir comment (FileNote) |
| `ACTION_SET_DATE` | 34 | Set file/dir modification timestamp |

[dos-packets]: https://wiki.amigaos.net/wiki/DOS_Packets

### Signatures

Add to `amifuse/startup_runner.py`, alongside the existing `send_set_file_size`
at line 1194:

```python
def send_set_protect(self, state, parent_lock_bptr: int,
                     name_bstr_bptr: int, mask: int) -> None
def send_set_comment(self, state, parent_lock_bptr: int,
                     name_bstr_bptr: int, comment_bstr_bptr: int) -> None
def send_set_date(self, state, parent_lock_bptr: int,
                  name_bstr_bptr: int, days: int, mins: int, ticks: int) -> None
```

Each function:
- Allocates the packet, fills in arguments per the dos.library packet layout
- Sends it through `state.fs_handler_port`
- The matching reply arrives via `_run_until_replies()` like other packets
- Returns when reply arrives; raises on handler crash

### HandlerBridge thin wrappers

In `amifuse/fuse_fs.py` (the `HandlerBridge` class), add:

```python
def set_protect(self, path: str, mask: int) -> bool
def set_comment(self, path: str, comment: str) -> bool
def set_date(self, path: str, ts: TimeStamp) -> bool
def apply_meta(self, path: str, meta_info: MetaInfo) -> None  # all three above
```

`apply_meta` is the unified entry point used by the copy engine — saves the
caller from coordinating three separate calls.

### Constants

Add to the existing constants block near `ACTION_RENAME_OBJECT = 17`
([startup_runner.py:244](amifuse/startup_runner.py#L244)):

```python
ACTION_SET_PROTECT = 21
ACTION_SET_COMMENT = 28
ACTION_SET_DATE    = 34
```

---

## Component 2: Sidecar layer

### Format inventory

| Format | Provider class | Read | Write | amitools source |
|---|---|---|---|---|
| `.uaem` | `UaemProvider` | yes | yes | `MetaInfoFSUAE` |
| `xdfmeta` | `XdfMetaProvider` | yes | yes | `MetaDB` |
| `.uaefsdb` | `UaeFsDbProvider` | future | no | not in amitools |
| `.amiga.json` | (deferred) | — | — | — |

Both supported formats reuse the **vendored amitools parser/emitter directly**,
not a reimplementation. `MetaInfoFSUAE.parse_data` (at
[amitools/amitools/fs/MetaInfoFSUAE.py:27](amitools/amitools/fs/MetaInfoFSUAE.py#L27))
is the canonical `.uaem` parser; `MetaDB.load`/`save` is the canonical
`xdfmeta` reference.

### Unified internal type

Reuse amitools' `MetaInfo` (`amitools/amitools/fs/MetaInfo.py`) end to end. It
already holds:

- `ProtectFlags` (FIBF mask)
- `TimeStamp` (days/mins/ticks triple)
- `FSString` comment

No new dataclass. Translation between `MetaInfo` and the packet senders is the
only adapter code that needs writing.

### Provider interface

```python
class SidecarProvider(Protocol):
    name: str
    can_write: bool

    def detect(self, file_path: Path) -> Path | None: ...
    def read(self, sidecar_path: Path) -> MetaInfo: ...
    def write(self, file_path: Path, meta: MetaInfo) -> None: ...
```

### Registry

```python
class SidecarRegistry:
    def __init__(self): self._providers = []
    def register(self, provider, priority: int): ...
    def detect_one(self, file_path: Path) -> tuple[SidecarProvider, Path] | None
    def by_name(self, name: str) -> SidecarProvider
```

Detection order on auto:
1. `xdfmeta` (per-directory manifest — beats per-file when both exist)
2. `.uaem`
3. `.uaefsdb`

The detect-first-wins rule prevents conflicts. If both `.uaem` and an
`xdfmeta` manifest exist for the same file, the manifest wins because it was
the more recent deliberate choice.

### Write defaults

- Single-file `amifuse read --preserve`: write `.uaem`. Most FS-UAE-friendly.
- Recursive `amifuse read -r --preserve`: write `xdfmeta`. One manifest per
  extracted tree, no per-file clutter.
- Either default overridable with `--meta-format {uaem|xdfmeta|json|auto|none}`.

### When NOT to emit a sidecar

Skip sidecar emission when the file has **all-default metadata**: protection
`----rwed` (FIBF mask `0x00`), no comment, datestamp within the last few
seconds of the build (i.e. matches "the host filesystem's idea of now"). This
matches FS-UAE's behavior and prevents the typical Workbench-install case from
producing 4000 useless sidecar files.

### Volume-level metadata

`xdfmeta` carries `RootMetaInfo` (volume name, dos_type, create_ts, disk_ts,
mod_ts). The copy engine preserves these on bulk extract and applies them on
import. `.uaem` has no representation for volume metadata; that's a minor
fidelity loss when using `.uaem` for bulk extract.

---

## Component 3: Copy engine

### File: `amifuse/copy.py`

Public API:

```python
def copy_file(
    src_bridge: HandlerBridge, src_path: str,
    dst_bridge: HandlerBridge, dst_path: str,
    preserve: bool = True,
    overwrite: bool = True,
) -> CopyStats

def copy_tree(
    src_bridge: HandlerBridge, src_root: str,
    dst_bridge: HandlerBridge, dst_root: str,
    preserve: bool = True,
    on_progress: Callable[[CopyProgress], None] | None = None,
    on_conflict: Literal["overwrite", "skip", "error"] = "overwrite",
    on_error: Literal["abort", "skip"] = "abort",
) -> CopyStats
```

`CopyStats` reports files copied, bytes copied, files skipped, errors, elapsed
time. `CopyProgress` is emitted per file with `{current_path, copied_bytes,
total_bytes_known, current_index, total_count_known}`.

### Algorithm

```
1. Validate src_root exists on src_bridge; validate dst_bridge is writable.
2. Plan phase:
   - Recursively walk src using existing list_dir_path()
   - For each entry record (path, type, size, MetaInfo)
   - Detect filename length issues vs dst FS limits (warn or fail per policy)
   - Build a sorted list — directories before their children
3. Execute phase, per entry:
   a. Directory:
      - Create on dst (ACTION_CREATE_DIR or equivalent through bridge)
      - apply_meta(dst_bridge, path, src_meta)
   b. File:
      - Open file handle on src
      - Open file handle on dst with O_WRONLY|O_CREAT|O_TRUNC
      - Stream in 256 KiB chunks (TODO: tune)
      - close both
      - apply_meta(dst_bridge, path, src_meta)
   c. Soft link (PFS3): warn and skip in MVP; tracked as follow-up.
   d. On error: per on_error policy (abort or skip)
4. Emit final CopyStats.
```

### Atomicity

Per file: write into `<dst_path>.amifuse-tmp.<pid>`, apply metadata, then
`ACTION_RENAME_OBJECT` into final name. Crash leaves a `.amifuse-tmp.*` stub,
not a half-written real file. Caller can sweep `.amifuse-tmp.*` on recovery.

### Resumability

`on_conflict="skip"` skips files where target exists with matching size **and**
matching mod_ts (within 1 tick). For installer iteration ("re-run after I added
a package"), this avoids redoing the bulk of work.

Skip semantics are deliberately conservative: any difference in size or mod_ts
triggers a fresh copy. We do *not* checksum to detect identical content; that's
the user's job to verify with `amifuse verify` post-copy.

### Progress

Two layers: high-cardinality byte-level updates (every chunk) buffered and
emitted at most every 100 ms, and file-level events emitted on each open/close.
Text output is human-readable progress bar via stderr; `--json` emits
line-delimited JSON to stdout.

### Two-bridge resource handling

- Both bridges are created with separate vamos instances. Memory roughly
  doubles vs single-bridge.
- Both bridges share the same Python process; no IPC, no FUSE.
- Bridges are closed in `try/finally`; `flush_volume` on dst before close.
- If creating dst bridge fails (e.g. dst image not writable), src bridge is
  closed cleanly.

### Chunk size tuning

Initial: 256 KiB. Default for `cmd_read`/`cmd_write` is currently 64 KiB
([fuse_fs.py:3473](amifuse/fuse_fs.py#L3473)). Bigger reduces packet overhead
proportionally. Will benchmark across all supported FSes in the test suite
and pick a number; configurable via `AMIFUSE_COPY_CHUNK_SIZE` env var for
experimentation.

---

## CLI surface

### New: `amifuse cp`

```
amifuse cp [-r|--recursive] [--preserve|--no-preserve]
           [--meta-format {auto|uaem|xdfmeta|json|none}]
           [--overwrite|--skip-existing|--error-on-existing]
           [--on-error {abort|skip}]
           [--chunk-size BYTES]
           [--debug] [--json]
           SRC DST
```

Where `SRC` and `DST` are of the form:
- `<image>:<amiga-path>` — refers to a path inside an Amiga image
- `<host-path>` — refers to a host filesystem path (only valid in
  read/write subcommands, not in cp)

Examples:

```sh
# Image-to-image, single file
amifuse cp src.hdf:S/Startup-Sequence dst.hdf:S/Startup-Sequence

# Image-to-image, recursive whole partition
amifuse cp -r src.hdf:DH0/ dst.hdf:DH0/

# Image-to-image, skip-existing for installer iteration
amifuse cp -r --skip-existing src.hdf:Workbench/ dst.hdf:Sys/Workbench/
```

### Extended: `amifuse read`

New flags:

```
amifuse read --preserve [--meta-format {auto|uaem|xdfmeta|json}]
             [-r|--recursive] [--manifest]
             IMAGE:PATH [-o OUTPATH]
```

- `--preserve`: emit sidecar(s) alongside extracted files.
- `--meta-format`: which format. `auto` picks `.uaem` for single-file, `xdfmeta`
  for recursive.
- `--manifest`: force `xdfmeta` regardless of `--meta-format`.

### Extended: `amifuse write`

```
amifuse write [--meta-format {auto|uaem|xdfmeta|json|none}]
              [--meta-from PATH]
              [-r|--recursive]
              IMAGE:PATH --in HOSTPATH
```

- Default behavior: auto-detect sidecar (any provider) and apply.
- `--meta-format none`: ignore sidecars even if present.
- `--meta-format <name>`: require that specific format, error if absent.
- `--meta-from PATH`: read metadata from an arbitrary file (overrides
  auto-detection).
- `-r`: recursively import host tree into image; uses the same sidecar
  detection rules per file.

### Behavior tables

**`read --preserve` default emission:**

| Mode | Default format |
|---|---|
| single file | `.uaem` |
| `-r` recursive | `xdfmeta` |
| `-r --manifest` | `xdfmeta` (forced) |
| `--meta-format X` | as specified |

**`write` default detection:**

Auto walks the registry in priority order; first match wins. No surprise: if
the user `cp`'d a file alongside its `.uaem` sidecar from one host tree to
another, the import just works.

---

## Edge cases and their resolutions

| Case | Behavior |
|---|---|
| Source file has zero-byte content | Empty file created on dst, metadata applied normally |
| Source directory is empty (no children) | Directory created on dst, metadata applied |
| Source has only-default metadata | Sidecar emission skipped on extract; full metadata still copied between images |
| Source filename exceeds dst FS limit | Plan phase reports it; default policy is `abort`. `--on-error skip` walks past |
| Filename case collision on host (extract) | Plan detects, warns, refuses to extract without `--allow-case-collisions` |
| Source has a soft link (PFS3) | Skipped with warning in MVP; tracked as phase-2 work |
| Dest already has the path (file) | Per `--overwrite|--skip-existing|--error-on-existing` |
| Mid-copy disk full | `write_handle` returns partial, engine aborts current file, leaves `.amifuse-tmp.*` stub, reports failure |
| Mid-copy handler crash | Engine aborts entire copy, reports last completed path, returns nonzero exit |
| Source FS is read-only (CDFS, OD) | Allowed as source; dst writability still validated separately |
| Source uses BCPL-style links (FFS) | Same as PFS3 soft links: warn and skip in MVP |
| Latin-1 character in filename | `FSString` round-trips Latin-1 ↔ UTF-8 losslessly; tested explicitly |
| Latin-1 character in comment | Same |
| Sidecar exists but is corrupted | Read fails loudly with file path and line; copy aborts unless `--on-error skip` |

---

## Test plan

Tests live in `tests/unit/test_copy.py`, `tests/unit/test_sidecar.py`, and
`tests/integration/test_copy_*.py`. Integration tests use the existing fixture
infrastructure at `tests/integration/conftest.py`.

### Unit tests — packet senders

`tests/unit/test_packet_senders.py`:

- `send_set_protect` writes correct packet bytes
- `send_set_comment` handles empty comment, ASCII, Latin-1, max-length
- `send_set_date` encodes Amiga (days, mins, ticks) correctly
- All three reject after handler crash with informative error
- Reply parsing: success path, DOS error codes (212, 213, etc.) propagated

### Unit tests — MetaInfo bridging

`tests/unit/test_meta_bridge.py`:

- `MetaInfo` → packet-sender args: bit-for-bit
- `MetaInfo` from `EXAMINE_OBJECT` reply round-trips through senders identically
- `apply_meta` calls all three senders; failure in one aborts and reports which

### Unit tests — sidecar layer

`tests/unit/test_sidecar.py`:

- `UaemProvider.read` parses every example from `MetaInfoFSUAE` docstring
- `UaemProvider.write` produces output byte-identical to amitools' `save_meta`
- `XdfMetaProvider` round-trips multiple files in one manifest
- Registry priority: `.uaem` and `xdfmeta` both present → `xdfmeta` wins
- `detect_one` returns `None` for files with no sidecar
- Default-metadata heuristic: skips emission for plain `----rwed` + no comment
- Corrupted sidecar: clear error with file path and offending line

### Unit tests — copy engine (in-memory bridges)

`tests/unit/test_copy.py` using mock `HandlerBridge`:

- `copy_file`: file with metadata round-trips through mock packets
- `copy_tree`: depth-first walk hits directories before their children
- Atomicity: temp filename pattern correct; rename on success only
- Resumability: `skip-existing` correctly identifies same/different files
- Progress callback fires per-file and per-chunk with correct counts
- Error policies: `abort` stops on first failure; `skip` continues
- Empty source: returns valid CopyStats with zero counts

### Integration tests — round-trip across FS types

`tests/integration/test_copy_roundtrip.py`:

Parameterized over `(src_fs, dst_fs)` for every pair where both can write:

```
PFS3 → PFS3   FFS → FFS    SFS → SFS    BFFS → BFFS
PFS3 → FFS    FFS → PFS3   PFS3 → SFS   SFS → PFS3
PFS3 → BFFS   BFFS → PFS3  FFS → SFS    SFS → FFS
FFS → BFFS    BFFS → FFS   SFS → BFFS   BFFS → SFS
```

Plus read-only sources:

```
CDFS → PFS3   OD → PFS3   ADF(FFS) → PFS3
```

Each test:
1. Builds a small src image with N files containing diverse metadata
   (different protections, comments, ancient timestamps, modern timestamps,
   nested dirs, Latin-1 filenames, Latin-1 comments)
2. Runs `copy_tree(src, dst)`
3. Verifies every file's content is byte-identical
4. Verifies every file's `MetaInfo` is bit-identical (compares
   `protect_bits`, `comment`, `days/mins/ticks`)
5. Verifies directory metadata also matches

### Integration tests — sidecar round-trip

`tests/integration/test_sidecar_roundtrip.py`:

Per supported `.uaem`/`xdfmeta`:

1. Extract image with `amifuse read -r --preserve --meta-format X`
2. Mutate one file's content on host (sidecar untouched)
3. Reimport with `amifuse write -r`
4. Verify: file content matches mutation; metadata matches original (since
   sidecar was preserved)

### Integration tests — cross-format

`tests/integration/test_cross_format.py`:

1. Extract image A with `.uaem` sidecars
2. Convert `.uaem` to `xdfmeta` using a helper utility
3. Reimport using `xdfmeta` provider
4. Compare with direct image-to-image copy: metadata must be identical

### Integration tests — bulk size

`tests/integration/test_copy_large.py`:

Marked `@pytest.mark.slow` (excluded from quick CI):

- Build a synthetic 100 MB PFS3 image with ~3000 files mimicking a Workbench
  install (mix of small config files, larger binaries, many directories)
- Time `copy_tree` PFS3 → PFS3 — pass criterion: completes in <30 s on dev
  machine (hard target; will be set after benchmarking)
- Verify metadata fidelity on 100 random sampled files

### Integration tests — error conditions

`tests/integration/test_copy_errors.py`:

- Disk full mid-copy: target image deliberately undersized
- Source filename exceeds dst FS limit (PFS3 → FFS with a 50-char name):
  detected in plan phase, listed in error
- Handler crash mid-copy: simulated by sending bad packet; copy aborts cleanly
- Permission-denied on dst: writable destination becomes RO mid-copy
- Corrupted sidecar: aborts with file:line reference

### Round-trip property test

`tests/unit/test_meta_roundtrip_property.py`, using `hypothesis`:

Generate random valid `MetaInfo` instances. For each:
- Encode through `UaemProvider.write` → re-read with `UaemProvider.read` →
  compare. Must be bit-identical.
- Same for `XdfMetaProvider`.
- Cross-format: encode as `.uaem`, decode, re-encode as `xdfmeta`, decode.
  Compare with original. Must match.

This catches subtle drift in date/protection encoding that example-based tests
miss.

### CI integration

- Quick suite (PR checks): unit tests + a small subset of integration tests
  (one FS pair).
- Full suite (nightly / pre-release): all integration cross-FS pairs + sidecar
  round-trips + size benchmark.

---

## Implementation phases

Each phase is a self-contained PR with its own tests. Phases land in order;
each is reviewable on its own.

### Phase 1: Packet senders

- Add `ACTION_SET_PROTECT/COMMENT/DATE` constants
- Implement `send_set_protect/comment/date` in `startup_runner.py`
- Add `set_protect/set_comment/set_date/apply_meta` to `HandlerBridge`
- Unit tests for senders
- Unit tests for `MetaInfo` ↔ sender args

PR size: small. Foundation for everything else.

### Phase 2: Sidecar layer

- `amifuse/sidecar.py`: `SidecarRegistry`, `SidecarProvider` protocol
- `UaemProvider` (thin wrapper around `MetaInfoFSUAE`)
- `XdfMetaProvider` (thin wrapper around `MetaDB`)
- Default-metadata heuristic
- Unit tests for the registry and both providers
- Property test for round-trip

PR size: small-medium.

### Phase 3: Copy engine

- `amifuse/copy.py`: `copy_file`, `copy_tree`, `CopyStats`, `CopyProgress`
- Two-bridge orchestration with proper cleanup
- Atomicity via temp file + rename
- Progress callback infrastructure
- Filename-length validation in plan phase
- Soft link detection + warn-and-skip in MVP
- Unit tests with mock bridges
- Integration test: PFS3 → PFS3 round-trip with metadata

PR size: medium.

### Phase 4: CLI integration

- `amifuse cp` subcommand
- `--preserve` flag on `amifuse read` (single file mode first)
- `--meta-format` and `--meta-from` flags on `amifuse write`
- Help text and `--json` output for all new flags
- Integration tests for CLI surface

PR size: small-medium.

### Phase 5: Recursive read/write

- `amifuse read -r --preserve` produces host tree with sidecars
- `amifuse write -r` consumes host tree with sidecar auto-detect
- `--manifest` flag for forced `xdfmeta` mode
- Integration tests for cross-format round-trip
- Integration tests for sidecar round-trip per format

PR size: medium.

### Phase 6: Cross-FS test matrix

- The full `(src_fs × dst_fs)` test matrix
- Bulk-size benchmark test
- Performance tuning (chunk size, pipelining) based on benchmark results

PR size: medium. Mostly test code; small adjustments to engine based on what
benchmarks reveal.

### Phase 7: Polish, deferred items

Three items, landed (or explicitly deferred) as separate small PRs:

**7a — Soft link copy (DONE).** `ACTION_MAKE_LINK` (24) and
`ACTION_READ_LINK` (29) plumbed through HandlerLauncher + HandlerBridge.
The copy engine reads the source link's target via `read_link` and
recreates the link on dst via `make_link(..., soft=True)`. Hard links
(`ST_LINKDIR` / `ST_LINKFILE`) remain skipped — translating a target
lock across two independent handler images requires a cross-image path
bridge that's out of scope. `copy_links=True` is the default; pass
`copy_links=False` to revert to the original warn-and-skip MVP behavior.

**7b — `.amiga.json` provider (DONE).** Per-file JSON sidecar with an
explicit, lossless schema. Raw FIBF mask and (days, minutes, ticks)
triple are the source of truth; the human-readable HSPARWED string and
ISO-ish date are present for editability and ignored on parse. Wired
into `default_registry()` at priority 30 (after xdfmeta and uaem).
Unlike `.uaem`, the JSON format preserves sub-second tick precision
losslessly — it encodes the integer triple directly.

**7c — `.uaefsdb` read provider (DEFERRED, not in scope).**
WinUAE's binary sidecar format is not implemented. Reasons:

  - The format is undocumented; spec extraction requires reading
    WinUAE's C++ source for the directory-mount filesystem handler.
  - The `.uaefsdb` files are a per-volume binary DB with hostname/path
    fields and per-file records — replicating it correctly without
    breaking existing WinUAE volumes is a meaningful effort.
  - The realistic *value* is bounded: any directory-mount user of WinUAE
    can use FS-UAE's `.uaem` format instead (FS-UAE is strict superset
    of WinUAE in directory-mount semantics), or convert at extract time.
  - For the installer pipeline that motivated this work, neither WinUAE
    nor `.uaefsdb` is in the loop — the build path runs on Linux with
    AmiFUSE talking to native handlers.

If anyone hits a workflow that needs ingesting an existing WinUAE
directory tree's `.uaefsdb` metadata, the format can be reverse-engineered
and a new provider class slotted into the registry without touching the
engine. The pluggable provider architecture means this is a contained,
non-blocking gap.

**Performance tuning (deferred to fixture-equipped CI).** The plan
called for benchmark-driven chunk-size and pipelining tuning. With the
existing PFS3 fixture this is a one-environment data point at best;
the meaningful version of this work requires the cross-FS fixture set
and a representative installer-style workload. Tracked as a follow-up
for whoever lands FFS/SFS/BFFS fixtures (the cross-FS scaffolding in
Phase 6 activates automatically once they appear).

---

## Open questions worth resolving before phase 1

1. **What's the existing `HandlerBridge.create_dir` / equivalent for the copy
   engine to call when making a destination directory tree?** I haven't
   audited this yet. If absent, add it in phase 1 alongside the senders.

2. **Does AmiFUSE's bridge currently expose `EXAMINE_OBJECT` results with all
   the metadata fields parsed?** The existing `stat_path` returns a dict with
   `protection`; need to verify `comment` and full timestamp (days/mins/ticks
   triple) are also captured. If not, extend `_parse_fib` to read them.

3. **Two-bridge memory footprint.** Worth measuring before phase 3 to set
   expectations. Two pfs3aio instances in vamos memory should be tolerable,
   but it's an unknown.

4. **Atomicity vs handler caching.** Some handlers may not flush metadata
   updates without an explicit `ACTION_FLUSH`. We may need to flush after each
   file or batch by directory. Profiling decides.

5. **Cross-FS `apply_meta` failure modes.** If the destination FS doesn't
   support a particular bit (rare, but in principle possible), what's the
   right behavior — silently drop, warn, fail? Tentative: warn and drop, but
   make it configurable via `--strict`.

---

## What's intentionally out of scope

- **`amifuse mv` / rename across images.** A copy + delete primitive, useful
  but not foundational. Trivial to add once `copy_file` works.
- **`amifuse sync` / rsync-style differential.** Could be built on
  `copy_tree(on_conflict="skip")` plus checksum comparison. Separate work.
- **Hard links / softlinks (full).** MVP only warns. Real link handling needs
  packet-level work for `ACTION_MAKE_LINK` (24).
- **Cross-platform sidecar concerns.** Linux is the target; macOS/Windows may
  have file-naming or xattr quirks that need separate work.
- **Performance optimization beyond chunk size.** Pipelined reads, parallel
  writes, asynchronous metadata flushes — all interesting, none needed for
  MVP.

---

## Success criteria

The work is done when:

1. `amifuse cp -r src.hdf:DH0/ dst.hdf:DH0/` produces a destination tree
   that's metadata-identical to the source for any supported FS pair.
2. `amifuse read -r --preserve src.hdf:DH0/ ./extracted/ && amifuse write -r
   ./extracted/ dst.hdf:DH0/` round-trips losslessly.
3. The full cross-FS test matrix passes in CI.
4. A 100 MB / 3000-file PFS3 → PFS3 recursive copy completes in under 30
   seconds on the reference dev machine.
5. The implementation is documented in `README.md`, `TESTING.md`, and a
   command reference page.

At that point, an external orchestrator (an installer, a backup tool, a
sync utility) can build any AmigaOS image content workflow on these primitives.
