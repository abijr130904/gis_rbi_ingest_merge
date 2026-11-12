from __future__ import annotations

import logging
import os
import shutil
import uuid
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

import pyogrio
from pyogrio import list_layers, read_dataframe, read_info, write_dataframe
from pyproj import CRS
from .detect import normalize_name, safe_output_name
from .dissolve import dissolve_group
from .models import Group, Options, SourceFile
from .validate import crs_equal, validate_group

log = logging.getLogger("shpmerge")


def _discover_gdbs(input_path: Path, output_root: Path) -> list[Path]:
    root = input_path.resolve()
    if root.suffix.casefold() == ".gdb":
        return [root]
    if not root.is_dir():
        raise ValueError("--merge-gdb membutuhkan folder atau satu direktori .gdb")
    output = output_root.resolve()
    found = []
    for current, dirs, _ in os.walk(root):
        folder = Path(current)
        children = [d for d in dirs if (folder / d).suffix.casefold() == ".gdb"]
        found.extend(folder / d for d in children
                     if (folder / d).resolve() != output
                     and output not in (folder / d).resolve().parents)
        dirs[:] = sorted(d for d in dirs if d not in children
                         and (folder / d).resolve() != output)
    return sorted(set(found), key=lambda p: str(p).casefold())


def _write_group(g: Group, o: Options, temporary: Path) -> int:
    try:
        written = 0
        geometry_types = {sf.geometry_type for sf in g.files}
        output_geometry_type = "Unknown" if len(geometry_types) > 1 else next(iter(geometry_types))
        for sf in g.files:
            layer = sf.rel.rsplit("/", 1)[-1]
            for start in range(0, sf.features, o.chunk_size):
                frame = read_dataframe(str(sf.path), layer=layer, skip_features=start,
                                        max_features=o.chunk_size)
                if o.target_crs and sf.crs and not crs_equal(sf.crs, o.target_crs):
                    frame = frame.to_crs(o.target_crs)
                geom = frame.geometry.name
                for name, fam in g.schema:
                    if name not in frame.columns:
                        frame[name] = None
                    if fam == "int":
                        frame[name] = frame[name].astype("Int64") if frame[name].isna().any() else frame[name].astype("int64")
                    elif fam == "float":
                        frame[name] = frame[name].astype("float64")
                    elif fam == "text":
                        frame[name] = frame[name].astype("string")
                columns = [name for name, _ in g.schema]
                if o.add_source_fields:
                    frame["SRC_REGION"] = sf.region_hint[:254]
                    frame["SRC_FILE"] = sf.rel[:254]
                    columns += ["SRC_REGION", "SRC_FILE"]
                frame = frame[columns + [geom]]
                write_dataframe(frame, str(temporary), layer=g.out_name,
                                driver="OpenFileGDB", append=written > 0,
                                geometry_type=output_geometry_type)
                written += len(frame)
                log.info("  %s: %d/%d fitur (total tertulis %d)", sf.rel,
                         min(start + o.chunk_size, sf.features), sf.features, written)
        return written
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def merge_gdb_tree(input_path: Path, output_root: Path, *, categories: list[str] | None = None,
                   dry_run: bool = False, overwrite: bool = False,
                   chunk_size: int = 50_000, target_crs: str | None = None,
                   include_single: bool = False, add_source_fields: bool = True,
                   strict_schema: bool = False, allow_schema_union: bool = False,
                   allow_unknown_crs: bool = False, skip_bad_files: bool = False,
                   dissolve: bool = True, dissolve_ignore: list[str] | None = None,
                   dissolve_overlap: bool = True,
                   dissolve_overlap_min: float = 0.01) -> list[tuple[str, int, str]]:
    """Merge same-named GDB layers using the shared SHP category validation rules."""
    gdbs = _discover_gdbs(input_path, output_root)
    if not gdbs:
        raise FileNotFoundError(f"tidak ditemukan direktori .gdb di {input_path}")
    wanted = {normalize_name(Path(name).stem if Path(name).suffix.casefold() == ".gdb" else name)
              for name in (categories or [])}
    grouped: dict[str, list[SourceFile]] = defaultdict(list)
    for gdb in gdbs:
        try:
            layers = list_layers(str(gdb))
        except Exception as exc:
            log.warning("GDB tidak dapat dibaca %s: %s", gdb, exc)
            continue
        for layer, geometry in layers:
            if geometry is None:
                continue
            key = normalize_name(str(layer))
            if wanted and key not in wanted:
                continue
            sf = SourceFile(path=gdb, rel=f"{gdb.name}/{layer}", stem=str(layer),
                            region_hint=gdb.parent.name, key=key)
            try:
                sf.info = read_info(str(gdb), layer=str(layer))
                if int(sf.info["features"]) < 0:
                    raise ValueError("jumlah fitur tidak dapat dihitung")
            except Exception as exc:
                sf.error = f"TIDAK_BISA_DIBACA: {exc}"
            grouped[key].append(sf)
    missing = sorted(wanted - set(grouped))
    if missing:
        log.warning("Kategori tidak ditemukan: %s", ", ".join(missing))
    if not grouped:
        raise FileNotFoundError("tidak ditemukan layer GDB yang cocok dengan kategori")

    opts = Options(input_dir=input_path, output_dir=output_root, dry_run=dry_run,
                   overwrite=overwrite, target_crs=target_crs,
                   include_single=include_single, add_source_fields=add_source_fields,
                   categories=categories or [], strict_schema=strict_schema,
                   allow_schema_union=allow_schema_union, allow_unknown_crs=allow_unknown_crs,
                   skip_bad_files=skip_bad_files, chunk_size=chunk_size,
                   dissolve=dissolve, dissolve_ignore=dissolve_ignore or [],
                   dissolve_overlap=dissolve_overlap,
                   dissolve_overlap_min=dissolve_overlap_min)
    if target_crs:
        CRS.from_user_input(target_crs)
    groups = [Group(key=k, files=sorted(v, key=lambda f: f.rel.casefold()),
                    out_name=safe_output_name(k)) for k, v in sorted(grouped.items())]
    results = []
    output_root = output_root.resolve()
    if not dry_run:
        output_root.mkdir(parents=True, exist_ok=True)
        mode = pyogrio.list_drivers().get("OpenFileGDB", "")
        if "w" not in mode.casefold():
            raise RuntimeError(f"driver OpenFileGDB tidak mendukung tulis (status={mode!r})")
    for g in groups:
        validate_group(g, opts)
        dest = output_root / f"{g.out_name}.gdb"
        count = g.total_features
        if g.status == "REVIEW_REQUIRED":
            log.warning("Kategori perlu ditinjau %s: %s", g.key, "; ".join(g.reasons))
            continue
        if g.status == "SKIPPED":
            log.info("Kategori dilewati %s: %s", g.key, "; ".join(g.reasons))
            continue
        if g.status == "SINGLE_SOURCE" and not include_single:
            log.info("Kategori %s hanya memiliki satu sumber; dilewati (gunakan --include-single)", g.key)
            continue
        if dry_run:
            results.append((g.key, count, str(dest)))
            continue
        group_options = opts
        if g.status == "SINGLE_SOURCE":
            # SHP copies a single source unchanged, without adding source fields.
            g.schema = g.files[0].schema
            group_options = replace(opts, add_source_fields=False)
        if dest.exists() and not overwrite:
            log.warning("Output sudah ada, kategori dilewati %s: %s (gunakan --overwrite)", g.key, dest)
            continue
        temporary = dest.with_name(f".{dest.stem}.tmp_{uuid.uuid4().hex[:8]}.gdb")
        written = _write_group(g, group_options, temporary)
        if written != count:
            shutil.rmtree(temporary, ignore_errors=True)
            raise RuntimeError(f"fitur tertulis {written} != {count} untuk kategori {g.key}")
        backup = dest.with_name(f".{dest.stem}.backup_{uuid.uuid4().hex[:8]}.gdb")
        moved_old = False
        try:
            if dest.exists():
                os.replace(dest, backup)
                moved_old = True
            os.replace(temporary, dest)
        except Exception as exc:
            rollback_error = None
            if moved_old and not dest.exists():
                try:
                    os.replace(backup, dest)
                except Exception as restore_exc:  # preserve the original failure context
                    rollback_error = restore_exc
            shutil.rmtree(temporary, ignore_errors=True)
            if isinstance(exc, PermissionError):
                detail = (
                    f"Windows menolak mengganti {dest}. FileGDB kemungkinan sedang "
                    "terbuka/dikunci ArcGIS, QGIS, atau proses lain. Tutup semua "
                    "aplikasi yang memakai GDB lalu jalankan ulang dengan --overwrite. "
                    "Output lama dipertahankan."
                )
                if rollback_error:
                    detail += f" Pemulihan backup juga gagal: {rollback_error}; backup: {backup}"
                raise PermissionError(detail) from exc
            if rollback_error:
                raise RuntimeError(
                    f"Gagal memasang output ({exc}); pemulihan GDB lama juga gagal: "
                    f"{rollback_error}. Backup: {backup}"
                ) from exc
            raise
        if moved_old:
            shutil.rmtree(backup, ignore_errors=True)
        g.status, g.output, g.output_features = "MERGED", str(dest), written
        results.append((g.key, written, str(dest)))
        dissolve_group(g, opts, output_root)
    return results
