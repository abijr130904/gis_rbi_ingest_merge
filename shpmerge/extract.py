from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pyogrio
from pyogrio import list_layers, read_dataframe, read_info, write_dataframe

from .detect import normalize_name, safe_output_name

log = logging.getLogger("shpmerge")


def _rar_members(archive: Path) -> list[str]:
    exe = shutil.which("7z") or shutil.which("7za")
    for candidate in (r"C:\Program Files\7-Zip\7z.exe",
                      r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if not exe and Path(candidate).is_file():
            exe = candidate
    if not exe:
        raise RuntimeError("7-Zip diperlukan untuk membaca daftar isi RAR")
    proc = subprocess.run([exe, "l", "-slt", str(archive)], capture_output=True,
                          text=True, errors="replace")
    if proc.returncode >= 2:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "7-Zip gagal membaca arsip")
    archive_names = {str(archive), archive.name, archive.resolve().as_posix()}
    return [match.group(1).replace("\\", "/").strip("/")
            for line in proc.stdout.splitlines()
            if (match := re.match(r"^Path = (.+)$", line))
            and match.group(1) not in archive_names and match.group(1) not in ("", ".")]


def _archive_gdbs(archive: Path) -> list[tuple[str, str]]:
    """Return GDAL VSI datasource and internal path for GDBs in ZIP or RAR."""
    ext = archive.suffix.casefold()
    if ext == ".zip":
        try:
            with zipfile.ZipFile(archive) as zipped:
                members = [name.replace("\\", "/").strip("/")
                           for name in zipped.namelist()]
        except (zipfile.BadZipFile, OSError) as exc:
            raise ValueError(f"file berakhiran .zip bukan ZIP valid atau rusak: {exc}") from exc
        archive_path = archive.resolve().as_posix()
        prefix = f"/vsizip/{archive_path}/"
    elif ext == ".rar":
        members = _rar_members(archive)
        archive_path = archive.resolve().as_posix()
        prefix = f"/vsirar/{{{archive_path}}}/"
    else:
        return []
    gdbs = set()
    for member in members:
        parts = member.split("/")
        for index, part in enumerate(parts):
            if part.casefold().endswith(".gdb") and index < len(parts) - 1:
                gdbs.add("/".join(parts[:index + 1]))
    return [(f"{prefix}{name}", name)
            for name in sorted(gdbs, key=str.casefold)]


def _archive_shapefiles(archive: Path) -> list[tuple[str, str]]:
    """Return VSI datasource and internal path for Shapefiles in a ZIP or RAR."""
    ext = archive.suffix.casefold()
    if ext == ".zip":
        try:
            with zipfile.ZipFile(archive) as zipped:
                members = [name.replace("\\", "/").strip("/")
                           for name in zipped.namelist() if not name.endswith("/")]
        except (zipfile.BadZipFile, OSError) as exc:
            raise ValueError(f"file berakhiran .zip bukan ZIP valid atau rusak: {exc}") from exc
        prefix = f"/vsizip/{archive.resolve().as_posix()}/"
    elif ext == ".rar":
        members = _rar_members(archive)
        prefix = f"/vsirar/{{{archive.resolve().as_posix()}}}/"
    else:
        return []
    return [(f"{prefix}{member}", member) for member in members
            if member.casefold().endswith(".shp")]


def _archive_sources(archive: Path, base_dir: Path) -> list[tuple[Path | str, Path]]:
    sources = [(vsi, base_dir / Path(member).parent)
               for vsi, member in _archive_gdbs(archive)]
    sources.extend((vsi, base_dir / Path(member).parent)
                   for vsi, member in _archive_shapefiles(archive))
    return sources


def _find_sources(root: Path, output_root: Path) -> list[tuple[Path | str, Path]]:
    """Find ordinary GDB folders and GDBs inside ZIP files, without unpacking ZIPs."""
    root = root.resolve()
    if root.suffix.casefold() == ".gdb":
        return [(root, Path())]
    if root.suffix.casefold() == ".shp":
        return [(root, Path())]
    if root.suffix.casefold() in {".zip", ".rar"}:
        return _archive_sources(root, Path(root.stem))

    output_root = output_root.resolve()
    sources: list[tuple[Path | str, Path]] = []
    skipped_archives: list[str] = []
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
            source_file = folder / name
            if source_file.suffix.casefold() == ".shp":
                sources.append((source_file, source_file.parent.relative_to(root)))
                continue
            if source_file.suffix.casefold() not in {".zip", ".rar"}:
                continue
            if (source_file.suffix.casefold() == ".rar"
                    and (match := re.search(r"\.part0*(\d+)\.rar$", source_file.name, re.I))
                    and int(match.group(1)) != 1):
                continue
            try:
                base_dir = source_file.parent.relative_to(root) / source_file.stem
                sources.extend(_archive_sources(source_file, base_dir))
            except Exception as exc:
                skipped_archives.append(f"{source_file}: {exc}")
                log.warning("Lewati arsip %s: %s", source_file, exc)
    if not sources and skipped_archives:
        raise RuntimeError("Tidak ditemukan GDB yang dapat diproses. Arsip bermasalah: "
                           + " | ".join(skipped_archives))
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
            # Source FileGDBs can store their FID as a physical field. Reusing
            # that field on write makes OpenFileGDB interpret its value as the
            # destination FID, which only supports positive 32-bit integers.
            # Let the destination assign fresh FIDs instead.
            fid_column = info.get("fid_column")
            if fid_column and fid_column in frame.columns:
                frame = frame.drop(columns=[fid_column])
            write_dataframe(frame, str(dest), layer=filename, driver="OpenFileGDB",
                            append=start > 0,
                            geometry_type=info.get("geometry_type") if feature_count == 0 else None)
    return results


def extract_gdb_tree(input_path: Path, output_root: Path, *, overwrite: bool = False,
                     dry_run: bool = False, categories: list[str] | None = None,
                     chunk_size: int = 50_000) -> list[tuple[str, int, str]]:
    sources = _find_sources(input_path, output_root)
    if not sources:
        raise FileNotFoundError(f"tidak ditemukan GDB, SHP, atau data spasial dalam arsip: {input_path}")

    driver_mode = pyogrio.list_drivers().get("OpenFileGDB", "")
    if not dry_run and "w" not in driver_mode.casefold():
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
                raise RuntimeError(f"gagal membaca data di dalam arsip {source}: {exc}") from exc
            raise
    return results
