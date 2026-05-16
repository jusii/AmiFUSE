"""Sidecar metadata I/O for Amiga filesystem files extracted to host trees.

When AmiFUSE extracts files to a host filesystem, the Amiga protection bits,
filenote comments, and modification timestamps don't survive in standard
Linux file metadata. Sidecar files carry them alongside the data so the
metadata can be reapplied on a later reimport.

Two formats are supported, both reusing amitools' parser/emitter code:

  - ``.uaem``  : per-file text sidecar, FS-UAE's directory-mount format.
  - xdfmeta    : one manifest per directory tree, amitools' own format.

The :class:`SidecarRegistry` lets the copy/read/write paths detect which
format is present for a given file without each caller hard-coding the
filename rules.

Detection priority (lower wins):

  1. xdfmeta manifest at the tree root (covers many files in one place)
  2. ``.uaem`` per-file sidecar

Default emission for new extracts:

  - Single-file extract → ``.uaem`` (best FS-UAE interop)
  - Recursive extract   → xdfmeta (one manifest beats thousands of sidecars)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from amitools.fs.MetaInfo import MetaInfo  # type: ignore
from amitools.fs.MetaInfoFSUAE import MetaInfoFSUAE  # type: ignore
from amitools.fs.MetaDB import MetaDB  # type: ignore
from amitools.fs.RootMetaInfo import RootMetaInfo  # type: ignore
from amitools.fs.TimeStamp import TimeStamp  # type: ignore


XDFMETA_MANIFEST_NAME = ".amiga-meta.xdfmeta"


def is_default_meta(meta: MetaInfo) -> bool:
    """Return True when *meta* carries nothing worth preserving.

    A file with all-default protection (``----rwed`` / FIBF mask 0), no
    filenote comment, and no modification timestamp doesn't need a sidecar:
    the host filesystem already captures equivalent state. This matches
    FS-UAE's policy of only writing ``.uaem`` files when the file has
    non-default metadata.

    Note: we don't compare the timestamp to "now" — a real Amiga datestamp
    is meaningful even if it's recent. Only the *absence* of a timestamp
    counts as default.
    """
    mask = meta.get_protect()
    if mask is not None and mask != 0:
        return False
    if meta.get_comment_unicode_str():
        return False
    if meta.get_mod_ts() is not None:
        return False
    return True


class SidecarProvider:
    """Read and write Amiga metadata sidecars in some format.

    Per-file providers (like ``.uaem``) operate purely on a single file's
    sidecar; *tree_root* is ignored. Per-volume providers (like xdfmeta)
    use *tree_root* to locate the shared manifest. Writes to per-volume
    providers accumulate in memory and are persisted by :meth:`flush`.
    """

    name: str = ""
    can_write: bool = True

    def read_meta(
        self,
        host_file: Path,
        tree_root: Optional[Path] = None,
    ) -> Optional[MetaInfo]:
        """Return MetaInfo for *host_file*, or None if no sidecar covers it."""
        raise NotImplementedError

    def write_meta(
        self,
        host_file: Path,
        meta: MetaInfo,
        tree_root: Optional[Path] = None,
    ) -> None:
        """Persist (or buffer) metadata for *host_file*.

        For per-volume providers the write is buffered until :meth:`flush`
        is called on the same *tree_root*; for per-file providers it lands
        on disk immediately.
        """
        raise NotImplementedError

    def flush(self, tree_root: Optional[Path] = None) -> None:
        """Write accumulated metadata for *tree_root* to disk.

        No-op for per-file providers.
        """

    def covers_file(
        self,
        host_file: Path,
        tree_root: Optional[Path] = None,
    ) -> bool:
        """Return True if a sidecar exists that describes *host_file*."""
        return self.read_meta(host_file, tree_root) is not None


class UaemProvider(SidecarProvider):
    """FS-UAE per-file ``.uaem`` sidecar provider.

    For a file ``foo.txt`` the sidecar is ``foo.txt.uaem`` in the same
    directory. *tree_root* is ignored.
    """

    name = "uaem"
    can_write = True
    suffix = ".uaem"

    def __init__(self) -> None:
        self._codec = MetaInfoFSUAE()

    def sidecar_path_for(self, host_file: Path) -> Path:
        return host_file.with_name(host_file.name + self.suffix)

    def read_meta(
        self,
        host_file: Path,
        tree_root: Optional[Path] = None,
    ) -> Optional[MetaInfo]:
        sidecar = self.sidecar_path_for(host_file)
        if not sidecar.exists():
            return None
        return self._codec.load_meta(str(sidecar))

    def write_meta(
        self,
        host_file: Path,
        meta: MetaInfo,
        tree_root: Optional[Path] = None,
    ) -> None:
        sidecar = self.sidecar_path_for(host_file)
        self._codec.save_meta(str(sidecar), meta)


class XdfMetaProvider(SidecarProvider):
    """amitools xdfmeta per-volume manifest provider.

    The manifest lives at ``<tree_root>/.amiga-meta.xdfmeta`` and contains
    one entry per file (path relative to *tree_root*). Writes are
    accumulated in memory keyed by *tree_root* and flushed on :meth:`flush`.

    *tree_root* must be supplied for every operation. Read returns None if
    the manifest is absent or has no entry for that file. The provider can
    serve many independent trees from the same instance.
    """

    name = "xdfmeta"
    can_write = True
    manifest_name = XDFMETA_MANIFEST_NAME

    def __init__(self) -> None:
        # Cache of loaded manifests, keyed by absolute tree_root.
        self._read_cache: Dict[Path, MetaDB] = {}
        # Pending writes, keyed by absolute tree_root.
        self._pending: Dict[Path, MetaDB] = {}

    def manifest_path_for(self, tree_root: Path) -> Path:
        return tree_root / self.manifest_name

    def _normalize_root(self, tree_root: Optional[Path]) -> Path:
        if tree_root is None:
            raise ValueError(
                "XdfMetaProvider requires tree_root for all operations"
            )
        return Path(tree_root).resolve()

    def _relative_key(self, host_file: Path, tree_root: Path) -> str:
        """Return the slash-separated path key for *host_file* under *tree_root*.

        amitools' MetaDB uses Amiga-style forward-slash paths regardless of
        host OS; we normalize accordingly.
        """
        rel = Path(host_file).resolve().relative_to(tree_root)
        return rel.as_posix()

    def _load_manifest(self, tree_root: Path) -> Optional[MetaDB]:
        if tree_root in self._read_cache:
            return self._read_cache[tree_root]
        manifest = self.manifest_path_for(tree_root)
        if not manifest.exists():
            return None
        db = MetaDB()
        db.load(str(manifest))
        self._read_cache[tree_root] = db
        return db

    def _pending_for(self, tree_root: Path) -> MetaDB:
        if tree_root not in self._pending:
            db = MetaDB()
            # Preserve an existing manifest's volume metadata if present.
            existing = self._load_manifest(tree_root)
            if existing is not None:
                root_meta = existing.get_root_meta_info()
                if root_meta is not None:
                    db.set_root_meta_info(root_meta)
                vol_name = existing.get_volume_name()
                if vol_name is not None:
                    db.set_volume_name(vol_name)
                db.set_dos_type(existing.get_dos_type())
            self._pending[tree_root] = db
        return self._pending[tree_root]

    def read_meta(
        self,
        host_file: Path,
        tree_root: Optional[Path] = None,
    ) -> Optional[MetaInfo]:
        root = self._normalize_root(tree_root)
        db = self._load_manifest(root)
        if db is None:
            return None
        try:
            key = self._relative_key(host_file, root)
        except ValueError:
            return None
        return db.get_meta_info(key)

    def write_meta(
        self,
        host_file: Path,
        meta: MetaInfo,
        tree_root: Optional[Path] = None,
    ) -> None:
        root = self._normalize_root(tree_root)
        key = self._relative_key(host_file, root)
        self._pending_for(root).set_meta_info(key, meta)

    def set_volume_info(
        self,
        tree_root: Path,
        volume_name: str,
        create_ts: Optional[TimeStamp] = None,
        disk_ts: Optional[TimeStamp] = None,
        mod_ts: Optional[TimeStamp] = None,
        root_meta: Optional[RootMetaInfo] = None,
    ) -> None:
        """Set volume-level metadata to be written into the manifest header.

        Pass *root_meta* directly, or supply the three TimeStamps to have
        a RootMetaInfo built for you. amitools' xdfmeta format requires all
        three timestamps to be parseable strings; unset timestamps fall back
        to the Amiga epoch (1978-01-01) so the manifest stays readable.
        """
        root = self._normalize_root(tree_root)
        db = self._pending_for(root)
        db.set_volume_name(volume_name)
        if root_meta is None:
            epoch = TimeStamp(days=0, mins=0, ticks=0)
            root_meta = RootMetaInfo(
                create_ts=create_ts or epoch,
                disk_ts=disk_ts or epoch,
                mod_ts=mod_ts or epoch,
            )
        db.set_root_meta_info(root_meta)

    def flush(self, tree_root: Optional[Path] = None) -> None:
        if tree_root is None:
            # Flush all pending roots.
            roots = list(self._pending.keys())
        else:
            roots = [self._normalize_root(tree_root)]
        for root in roots:
            db = self._pending.get(root)
            if db is None:
                continue
            manifest = self.manifest_path_for(root)
            manifest.parent.mkdir(parents=True, exist_ok=True)
            db.save(str(manifest))
            # Refresh the read cache so subsequent reads see the new content.
            self._read_cache[root] = db
            del self._pending[root]


@dataclass(order=True)
class _RegistryEntry:
    priority: int
    provider: SidecarProvider = field(compare=False)


class SidecarRegistry:
    """Priority-ordered registry of sidecar providers.

    Lower priority numbers are tried first when detecting which provider
    has metadata for a given file. The default lineup prefers the per-volume
    manifest (single source of truth) over per-file ``.uaem`` sidecars.
    """

    def __init__(self) -> None:
        self._entries: List[_RegistryEntry] = []

    def register(self, provider: SidecarProvider, priority: int = 50) -> None:
        self._entries.append(_RegistryEntry(priority, provider))
        self._entries.sort()

    def by_name(self, name: str) -> SidecarProvider:
        for entry in self._entries:
            if entry.provider.name == name:
                return entry.provider
        raise KeyError(f"No sidecar provider registered under name {name!r}")

    def names(self) -> List[str]:
        return [e.provider.name for e in self._entries]

    def writers(self) -> List[SidecarProvider]:
        return [e.provider for e in self._entries if e.provider.can_write]

    def detect(
        self,
        host_file: Path,
        tree_root: Optional[Path] = None,
    ) -> Optional[Tuple[SidecarProvider, MetaInfo]]:
        """Find the highest-priority provider with metadata for *host_file*.

        Returns (provider, meta_info) or None. Per-volume providers that
        require a *tree_root* but didn't get one are silently skipped.
        """
        for entry in self._entries:
            provider = entry.provider
            try:
                meta = provider.read_meta(host_file, tree_root)
            except ValueError:
                # Per-volume provider missing required tree_root context.
                continue
            if meta is not None:
                return provider, meta
        return None


def default_registry() -> SidecarRegistry:
    """Build a registry with the standard providers in their default order."""
    reg = SidecarRegistry()
    reg.register(XdfMetaProvider(), priority=10)
    reg.register(UaemProvider(), priority=20)
    return reg
