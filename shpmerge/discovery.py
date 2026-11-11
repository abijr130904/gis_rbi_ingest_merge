from __future__ import annotations

import logging
import os
import zipfile
from pathlib import Path

from .models import SourceFile

log = logging.getLogger("shpmerge")

EXCLUDED_DIRS = {".git", ".venv", "venv", "env", "__pycache__", "node_modules",
                 "$recycle.bin", "system volume information"}
TMP_PREFIX = ".shpmerge_tmp_"
REQUIRED_EXTS = (".shx", ".dbf")


def _keep_dir(path: Path, output_dir: Path | None) -> bool:
    name = path.name
    if name.casefold() in EXCLUDED_DIRS or name.startswith(TMP_PREFIX):
        return False
    if output_dir is not None and path.resolve() == output_dir:
        return False
    return True


def _zip_shapefiles(archive: Path, root: Path) -> list[SourceFile]:
    """Find Shapefiles in a ZIP and reference them through GDAL's VSI filesystem."""
    try:
        with zipfile.ZipFile(archive) as zipped:
            files = {info.filename.replace("\\", "/"): info.file_size
                     for info in zipped.infolist() if not info.is_dir()}
    except (zipfile.BadZipFile, OSError) as exc:
        log.warning("Lewati ZIP rusak/tidak valid %s: %s", archive, exc)
        return []

    by_stem: dict[tuple[str, str], dict[str, int]] = {}
    for name, size in files.items():
        parent, filename = name.rsplit("/", 1) if "/" in name else ("", name)
        path = Path(filename)
        key = (parent.casefold(), path.stem.casefold())
        by_stem.setdefault(key, {})[path.suffix.casefold()] = size

    vsi_archive = archive.resolve().as_posix()
    try:
        archive_rel = archive.relative_to(root).as_posix()
    except ValueError:
        archive_rel = archive.name
    results = []
    for name, size in sorted(files.items(), key=lambda item: item[0].casefold()):
        if not name.casefold().endswith(".shp"):
            continue
        parent, filename = name.rsplit("/", 1) if "/" in name else ("", name)
        path = Path(filename)
        components = by_stem.get((parent.casefold(), path.stem.casefold()), {})
        rel = f"{archive_rel}/{name}"
        archive_parent = archive.parent.relative_to(root).parts
        inner_parent = Path(name).parent.parts
        region = archive_parent[0] if archive_parent else (inner_parent[0] if inner_parent else "")
        results.append(SourceFile(
            path=f"/vsizip/{vsi_archive}/{name}",
            rel=rel,
            stem=path.stem,
            region_hint=region,
            size_shp=size,
            size_dbf=components.get(".dbf", 0),
            components=components,
        ))
    return results


def find_shapefiles(input_dir: Path, output_dir: Path | None = None) -> list[SourceFile]:
    """Scan recursively for .shp files on disk and inside ZIPs."""
    root = Path(input_dir).resolve()
    out = Path(output_dir).resolve() if output_dir else None
    results: list[SourceFile] = []
    seen: set[str] = set()
    if root.is_file() and root.suffix.casefold() == ".zip":
        return _zip_shapefiles(root, root.parent)
    for cur, dirs, names in os.walk(root):  # os.walk tidak mengikuti symlink
        cur_p = Path(cur)
        dirs[:] = sorted(d for d in dirs if _keep_dir(cur_p / d, out))
        for n in sorted(names):
            if not n.lower().endswith(".shp"):
                continue
            p = cur_p / n
            ident = os.path.normcase(os.path.realpath(p))
            if ident in seen:
                continue
            seen.add(ident)
            rel = p.relative_to(root)
            results.append(SourceFile(
                path=p,
                rel=rel.as_posix(),
                stem=p.stem,
                region_hint=rel.parts[0] if len(rel.parts) > 1 else "",
            ))
        for n in sorted(names):
            archive = cur_p / n
            if archive.suffix.casefold() == ".zip":
                results.extend(_zip_shapefiles(archive, root))
    log.info("Ditemukan %d file .shp", len(results))
    return results


def missing_components(shp: Path | str, components: dict[str, int] | None = None) -> list[str]:
    if components is not None:
        return [e for e in REQUIRED_EXTS if e not in components]
    shp = Path(shp)
    stem = shp.stem.lower()
    present = {p.suffix.lower() for p in shp.parent.iterdir()
               if p.is_file() and p.stem.lower() == stem}
    return [e for e in REQUIRED_EXTS if e not in present]


def sibling_size(shp: Path | str, ext: str, components: dict[str, int] | None = None) -> int:
    if components is not None:
        return int(components.get(ext.casefold(), 0))
    shp = Path(shp)
    stem = shp.stem.lower()
    for p in shp.parent.iterdir():
        if p.is_file() and p.stem.lower() == stem and p.suffix.lower() == ext:
            return p.stat().st_size
    return 0
