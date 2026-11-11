from __future__ import annotations

import logging
import os
from collections import defaultdict
from pathlib import Path

import pyogrio
from pyogrio import list_layers, read_dataframe, read_info, write_dataframe

from .detect import normalize_name, safe_output_name

log = logging.getLogger("shpmerge")


def merge_gdb_tree(input_path: Path, output_root: Path, *, categories: list[str] | None = None,
                   dry_run: bool = False, overwrite: bool = False,
                   chunk_size: int = 50_000) -> list[tuple[str, int, str]]:
    """Merge same-named spatial layers from FileGDBs into one GDB per layer/category."""
    root = input_path.resolve()
    if root.suffix.casefold() == ".gdb":
        gdbs = [root]
    elif root.is_dir():
        gdbs = []
        output_resolved = output_root.resolve()
        for current, dirs, _ in os.walk(root):
            folder = Path(current)
            found = [d for d in dirs if (folder / d).suffix.casefold() == ".gdb"]
            gdbs.extend(folder / d for d in found
                        if (folder / d).resolve() != output_resolved
                        and output_resolved not in (folder / d).resolve().parents)
            dirs[:] = sorted(d for d in dirs if d not in found
                             and (folder / d).resolve() != output_resolved)
    else:
        raise ValueError("--merge-gdb membutuhkan folder atau satu direktori .gdb")
    gdbs = sorted(set(gdbs), key=lambda p: str(p).casefold())
    if not gdbs:
        raise FileNotFoundError(f"tidak ditemukan direktori .gdb di {root}")

    wanted = {normalize_name(name) for name in (categories or [])}
    groups: dict[str, list[tuple[Path, str, str]]] = defaultdict(list)
    for gdb in gdbs:
        for layer, geometry in list_layers(str(gdb)):
            if geometry is not None and (not wanted or normalize_name(str(layer)) in wanted):
                groups[normalize_name(str(layer))].append((gdb, str(layer), str(geometry)))
    if wanted:
        found = set(groups)
        missing = sorted(wanted - found)
        if missing:
            log.warning("Kategori tidak ditemukan: %s", ", ".join(missing))
    if not groups:
        raise FileNotFoundError("tidak ditemukan layer GDB yang cocok dengan kategori")

    driver_mode = pyogrio.list_drivers().get("OpenFileGDB", "")
    if not dry_run and "w" not in driver_mode.casefold():
        raise RuntimeError(f"driver OpenFileGDB tidak mendukung tulis (status={driver_mode!r})")

    output_root = output_root.resolve()
    results = []
    for key, sources in sorted(groups.items()):
        name = safe_output_name(key)
        dest = output_root / f"{name}.gdb"
        metadata = [(gdb, layer, geom, read_info(str(gdb), layer=layer))
                    for gdb, layer, geom in sources]
        count = sum(int(info["features"]) for _, _, _, info in metadata)
        # Require matching spatial definition and attributes; avoid silently corrupting schema.
        baseline = metadata[0][3]
        base_fields = list(zip(baseline["fields"], baseline["dtypes"]))
        for gdb, layer, geom, info in metadata[1:]:
            fields = list(zip(info["fields"], info["dtypes"]))
            if geom.casefold() != metadata[0][2].casefold() or fields != base_fields:
                raise ValueError(f"skema/geometri berbeda untuk kategori {key!r}: {gdb} / {layer}")
            if (info.get("crs") or None) != (baseline.get("crs") or None):
                raise ValueError(f"CRS berbeda untuk kategori {key!r}: {gdb} / {layer}")
        results.append((key, count, str(dest)))
        if dry_run:
            continue
        if dest.exists():
            if not overwrite:
                raise FileExistsError(f"output sudah ada: {dest} (gunakan --overwrite)")
            import shutil
            shutil.rmtree(dest)
        for gdb, layer, geom, info in metadata:
            feature_count = int(info["features"])
            starts = range(0, feature_count, chunk_size) if feature_count else (0,)
            for start in starts:
                frame = read_dataframe(str(gdb), layer=layer, skip_features=start,
                                       max_features=chunk_size)
                fid = info.get("fid_column")
                if fid and fid in frame.columns:
                    frame = frame.drop(columns=[fid])
                write_dataframe(frame, str(dest), layer=name, driver="OpenFileGDB",
                                append=dest.exists(), geometry_type=geom)
    return results
