from __future__ import annotations

import logging
import os
import shutil
import zipfile
from pathlib import Path

import pyogrio
from pyogrio import list_layers, read_dataframe, read_info, write_dataframe

from .detect import normalize_name, safe_output_name

log = logging.getLogger("shpmerge")


def _zip_gdbs(archive: Path) -> list[tuple[str, str]]:
    """Return VSI datasource and internal path for each FileGDB in a ZIP."""
    with zipfile.ZipFile(archive) as zipped:
        members = [name.replace("\\", "/").strip("/") for name in zipped.namelist()]
    gdbs = set()
    for member in members:
        parts = member.split("/")
        for index, part in enumerate(parts):
            if part.casefold().endswith(".gdb") and index < len(parts) - 1:
                gdbs.add("/".join(parts[:index + 1]))
    archive_path = archive.resolve().as_posix()
    return [(f"/vsizip/{archive_path}/{name}", name)
            for name in sorted(gdbs, key=str.casefold)]


def _find_sources(root: Path, output_root: Path) -> list[tuple[Path | str, Path]]:
    """Find ordinary GDB folders and GDBs inside ZIP files, without unpacking ZIPs."""
    root = root.resolve()
    if root.suffix.casefold() == ".gdb":
        return [(root, Path())]
    if root.suffix.casefold() == ".zip":
        return [(vsi, Path(root.stem) / Path(member).parent)
                for vsi, member in _zip_gdbs(root)]

    output_root = output_root.resolve()
    sources: list[tuple[Path | str, Path]] = []
    for current, dirs, files in os.walk(root):
        folder = Path(current)
        kept = []
        for name in dirs:
            child = folder / name
            if child.suffix.casefold() == ".gdb":
                sources.append((child, child.parent.relative_to(root)))
            elif child.resolve() != output_root:
                kept.append(name)
        dirs[:] = kept
        for name in files:
            archive = folder / name
            if archive.suffix.casefold() != ".zip":
                continue
            for vsi, member in _zip_gdbs(archive):
                sources.append((vsi, archive.parent.relative_to(root) / archive.stem
                                / Path(member).parent))
    return sorted(sources, key=lambda item: str(item[0]).casefold())


def _extract_one(source: Path | str, output_dir: Path, *, overwrite: bool,
                 dry_run: bool, categories: list[str] | None,
                 chunk_size: int) -> list[tuple[str, int, str]]:
    source_ref = str(source)
    layers = sorted((str(name), geom) for name, geom in list_layers(source_ref)
                    if geom is not None)
    wanted = {normalize_name(name) for name in (categories or [])}
    if wanted:
        layers = [(name, geom) for name, geom in layers
                  if normalize_name(name) in wanted]

    planned_names: dict[str, str] = {}
    for layer, _ in layers:
        filename = safe_output_name(layer)
        folded = filename.casefold()
        if folded in planned_names:
            raise ValueError(f"nama output bentrok: {planned_names[folded]} dan {layer}")
        planned_names[folded] = layer

    results = []
    for layer, _ in layers:
        filename = safe_output_name(layer)
        dest = output_dir / f"{filename}.gdb"
        info = read_info(source_ref, layer=layer)
        feature_count = int(info["features"])
        if dest.exists() and not overwrite:
            try:
                existing = [(str(name), geom) for name, geom in list_layers(str(dest))
                            if geom is not None]
                if (len(existing) == 1 and existing[0][0].casefold() == filename.casefold()
                        and int(read_info(str(dest), layer=existing[0][0])["features"]) == feature_count):
                    results.append((layer, feature_count, str(dest)))
                    continue
            except Exception:
                pass
            raise FileExistsError(
                f"output sudah ada tetapi belum lengkap: {dest} (gunakan --overwrite untuk mengulang)"
            )

        results.append((layer, feature_count, str(dest)))
        if dry_run:
            continue
        output_dir.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)

        starts = range(0, feature_count, chunk_size) if feature_count else (0,)
        for start in starts:
            frame = read_dataframe(source_ref, layer=layer, skip_features=start,
                                   max_features=chunk_size)
            write_dataframe(frame, str(dest), layer=filename, driver="OpenFileGDB",
                            append=start > 0,
                            geometry_type=info.get("geometry_type") if feature_count == 0 else None)
    return results


def extract_gdb_tree(input_path: Path, output_root: Path, *, overwrite: bool = False,
                     dry_run: bool = False, categories: list[str] | None = None,
                     chunk_size: int = 50_000) -> list[tuple[str, int, str]]:
    sources = _find_sources(input_path, output_root)
    if not sources:
        raise FileNotFoundError(f"tidak ditemukan GDB atau GDB di dalam ZIP: {input_path}")

    driver_mode = pyogrio.list_drivers().get("OpenFileGDB", "")
    if "w" not in driver_mode.casefold():
        raise RuntimeError(
            "GDAL pada environment ini tidak mendukung penulisan FileGDB "
            f"(OpenFileGDB={driver_mode!r}); gunakan GDAL/Pyogrio dengan driver OpenFileGDB writable."
        )

    results = []
    for source, relative_dir in sources:
        try:
            found = _extract_one(source, output_root / relative_dir, overwrite=overwrite,
                                 dry_run=dry_run, categories=categories, chunk_size=chunk_size)
            results.extend(found)
            if not found:
                log.info("Tidak ada layer spasial yang cocok pada %s", source)
        except Exception as exc:
            if isinstance(source, str):
                raise RuntimeError(f"gagal membaca GDB di dalam ZIP {source}: {exc}") from exc
            raise
    return results
