from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely
from pyogrio import read_dataframe, read_info, write_dataframe
from shapely import STRtree

from .merge import _cast, _new_tmp, existing_outputs, finalize
from .models import Group, Options
from .validate import family

log = logging.getLogger("shpmerge")

DISSOLVE_DIR = "dissolved"
MEASURE_COLS = {"shape_leng", "shape_length", "shape_len", "shape_area"}


def is_polygon_group(g: Group) -> bool:
    return bool(g.files) and "Polygon" in g.files[0].geometry_type


def _polygons_only(geom):
    """Ambil bagian poligon saja (make_valid dapat menghasilkan GeometryCollection)."""
    if geom is None or geom.is_empty or geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    polys = []
    for part in shapely.get_parts(geom):
        if part.geom_type == "Polygon":
            polys.append(part)
        elif part.geom_type == "MultiPolygon":
            polys.extend(part.geoms)
    return shapely.MultiPolygon(polys) if polys else None


def _overlap_components(geoms, min_ratio: float) -> list[int]:
    """Label komponen: bentuk yang saling tumpang tindih (luas irisan > min_ratio x luas
    bentuk terkecil) berbagi label. Hanya bersentuhan di perbatasan TIDAK dihitung."""
    n = len(geoms)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    areas = shapely.area(geoms)
    tree = STRtree(geoms)
    left, right = tree.query(geoms, predicate="intersects")
    for i, j in zip(left, right):
        if i >= j or find(i) == find(j):
            continue
        smaller = min(areas[i], areas[j])
        if smaller <= 0:
            continue
        inter = shapely.area(shapely.intersection(geoms[i], geoms[j]))
        if inter > min_ratio * smaller:
            parent[find(i)] = find(j)
    return [find(i) for i in range(n)]


def _merge_components(res, labels: list[int], key_cols: list[str], count_col: str):
    order, groups = [], {}
    for idx, lab in enumerate(labels):
        if lab not in groups:
            groups[lab] = []
            order.append(lab)
        groups[lab].append(idx)
    base = res.geometry.to_numpy()
    counts = res[count_col].to_numpy()
    attrs, geoms, jml_out, mixed = [], [], [], []
    for lab in order:
        idxs = groups[lab]
        sub = res.iloc[idxs]
        row, is_mixed = {}, 0
        for c in key_cols:
            vals = sub[c]
            if len(idxs) == 1 or vals.nunique(dropna=False) == 1:
                row[c] = vals.iloc[0]
            else:
                row[c] = None
                is_mixed = 1
        attrs.append(row)
        geoms.append(base[idxs[0]] if len(idxs) == 1 else shapely.union_all(base[idxs]))
        jml_out.append(int(counts[idxs].sum()))
        mixed.append(is_mixed)
    df = pd.DataFrame(attrs, columns=key_cols)
    df[count_col] = jml_out
    df["ATR_CAMPUR"] = mixed
    out = gpd.GeoDataFrame(df, geometry=geoms, crs=res.crs)
    gname = res.geometry.name
    if out.geometry.name != gname:
        out = out.rename_geometry(gname)
    out[gname] = out.geometry.apply(_polygons_only)
    return out, int(sum(mixed))


def dissolve_group(g: Group, o: Options, out_dir: Path) -> None:
    """Dissolve ke <output>\\dissolved\\NAMA.shp:
    tahap 1 per file sumber + atribut identik; tahap 2 menyatukan bentuk yang tumpang tindih.
    Kegagalan hanya menjadi peringatan; status MERGED tidak diubah."""
    if not (o.dissolve and g.output and is_polygon_group(g)):
        return
    name = g.out_name
    is_gdb = Path(g.output).suffix.casefold() == ".gdb"
    # GDB merge output already occupies <name>.gdb in out_dir, so place the
    # dissolved GDB beside it under a distinct name. Shapefile behavior stays
    # in the existing dissolved subfolder.
    ddir = out_dir if is_gdb else out_dir / DISSOLVE_DIR
    ddir.mkdir(parents=True, exist_ok=True)
    dissolved_name = f"{name}_dissolved" if is_gdb else name
    dissolved_gdb = ddir / f"{dissolved_name}.gdb"
    if ((dissolved_gdb.exists() if is_gdb else bool(existing_outputs(ddir, name)))
            and not o.overwrite):
        g.warnings.append("DISSOLVE_DILEWATI: hasil dissolve sudah ada (gunakan --overwrite)")
        return
    tmp = _new_tmp(ddir)
    try:
        gdf = read_dataframe(g.output, **({"layer": name} if is_gdb else {}))
        gname = gdf.geometry.name
        n_in = len(gdf)
        if "SRC_FILE" not in gdf.columns:
            if len(g.files) != 1:
                raise ValueError("kolom SRC_FILE tidak ada; dissolve per wilayah butuh field sumber "
                                 "(jangan pakai --no-add-source-fields)")
            gdf["SRC_FILE"] = g.files[0].rel[:254]

        keep = gdf.geometry.notna() & ~gdf.geometry.is_empty
        if not keep.all():
            g.warnings.append(f"DISSOLVE: {int((~keep).sum())} fitur tanpa geometri tidak ikut")
            gdf = gdf[keep]
        if gdf.empty:
            raise ValueError("tidak ada geometri yang bisa di-dissolve")
        if gdf.geometry.has_z.any():
            g.warnings.append("DISSOLVE: koordinat Z dibuang pada hasil dissolve (2D)")

        geom = gdf.geometry.force_2d().copy()
        bad = ~geom.is_valid
        if bad.any():
            g.warnings.append(f"DISSOLVE: {int(bad.sum())} geometri invalid diperbaiki (make_valid) "
                              "hanya pada file dissolve; file gabungan tidak diubah")
            geom.loc[bad] = geom.loc[bad].make_valid()

        ignore = MEASURE_COLS | {c.casefold() for c in o.dissolve_ignore}
        attr_cols = [c for c in gdf.columns if c != gname]
        key_cols = [c for c in attr_cols if c.casefold() not in ignore]
        dropped = [c for c in attr_cols if c.casefold() in ignore]
        if dropped:
            g.warnings.append("DISSOLVE: kolom tidak dibawa/dibandingkan: " + ", ".join(dropped))

        # Use a separate counter field if the input already has JML_FITUR.
        # Keeping the source attribute in key_cols preserves its original values.
        used = {c.casefold() for c in key_cols}
        count_col = "JML_FITUR" if "jml_fitur" not in used else "JML_FITUR_DISS"
        suffix = 2
        while count_col.casefold() in used:
            count_col = f"JML_FITUR_DISS_{suffix}"
            suffix += 1

        # tahap 1: per file sumber + atribut identik
        d = gdf[key_cols].copy()
        d[gname] = geom
        d = gpd.GeoDataFrame(d, geometry=gname, crs=gdf.crs)
        d[count_col] = 1
        res = d.dissolve(by=key_cols, aggfunc={count_col: "sum"}, as_index=False, dropna=False)
        res[gname] = res.geometry.apply(_polygons_only)
        res = res[res.geometry.notna() & ~res.geometry.is_empty].reset_index(drop=True)
        if res.empty:
            raise ValueError("hasil dissolve kosong")

        # tahap 2: satukan bentuk yang saling tumpang tindih
        n_rows = len(res)
        if o.dissolve_overlap and n_rows > 1:
            labels = _overlap_components(res.geometry.to_numpy(), o.dissolve_overlap_min)
            n_comp = len(set(labels))
            if n_comp < n_rows:
                res, n_mixed = _merge_components(res, labels, key_cols, count_col)
                g.warnings.append(
                    f"DISSOLVE: {n_rows} bentuk -> {n_comp} setelah fitur yang saling tumpang tindih "
                    f"disatukan (irisan > {o.dissolve_overlap_min:.0%} luas bentuk terkecil); "
                    f"{n_mixed} hasil beratribut campur (kolom dikosongkan, lihat ATR_CAMPUR)")
        if "ATR_CAMPUR" not in res.columns:
            res["ATR_CAMPUR"] = 0

        info = read_info(g.output, **({"layer": name} if is_gdb else {}))
        fams = {str(n): family(dt) for n, dt in zip(info["fields"], info["dtypes"])}
        for c in key_cols:
            if fams.get(c) == "int":
                res[c] = _cast(res[c], "int")

        if is_gdb:
            tmp_output = tmp / f"{dissolved_name}.gdb"
            write_dataframe(res, str(tmp_output), layer=name, driver="OpenFileGDB")
            info = read_info(str(tmp_output), layer=name)
        else:
            tmp_output = tmp / f"{name}.shp"
            write_dataframe(res, str(tmp_output), driver="ESRI Shapefile", encoding="UTF-8")
            info = read_info(str(tmp_output))
        if int(info["features"]) != len(res):
            raise ValueError("verifikasi hasil dissolve gagal")
        if is_gdb:
            final = dissolved_gdb
            backup = ddir / f".{dissolved_name}.backup_{uuid.uuid4().hex[:8]}.gdb"
            moved_old = False
            try:
                if final.exists():
                    if not o.overwrite:
                        raise FileExistsError(f"hasil dissolve sudah ada: {final}")
                    os.replace(final, backup)
                    moved_old = True
                os.replace(tmp_output, final)
            except Exception:
                if moved_old and not final.exists():
                    os.replace(backup, final)
                raise
            if moved_old:
                shutil.rmtree(backup, ignore_errors=True)
        else:
            finalize(tmp, ddir, name, o.overwrite)
            final = ddir / f"{name}.shp"
        g.dissolved_output = str(final)
        g.dissolved_features = len(res)
        g.warnings.append(f"DISSOLVE: {n_in} fitur -> {len(res)} fitur "
                          "(per file sumber dan atribut identik, plus tumpang tindih)")
        log.info("Dissolve %s: %d fitur -> %d (%s)", g.key, n_in, len(res), g.dissolved_output)
    except Exception as exc:  # noqa: BLE001
        g.warnings.append(f"DISSOLVE_GAGAL: {type(exc).__name__}: {exc}")
        log.error("Dissolve gagal %s: %s", g.key, exc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
