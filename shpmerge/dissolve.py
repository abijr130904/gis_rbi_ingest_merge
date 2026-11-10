from __future__ import annotations

import logging
import shutil
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


<<<<<<< HEAD
def _merge_components(res, labels: list[int], key_cols: list[str]):
=======
def _aligned_ratio(geom):
    """Porsi panjang garis batas bersama yang lurus sejajar sumbu (ciri garis lembar)."""
    total = aligned = 0.0
    for part in shapely.get_parts(geom):
        if part.geom_type != "LineString":
            continue
        d = np.diff(np.asarray(part.coords)[:, :2], axis=0)
        seg = np.hypot(d[:, 0], d[:, 1])
        ok = (np.abs(d[:, 0]) <= SEAM_TOL) | (np.abs(d[:, 1]) <= SEAM_TOL)
        total += float(seg.sum())
        aligned += float(seg[ok].sum())
    return aligned / total if total > 0 else None


def _thin_overlap(inter) -> bool:
    """Irisan berupa poligon sangat tipis (sliver) di sepanjang garis pertemuan."""
    area = length = 0.0
    for part in shapely.get_parts(inter):
        if part.geom_type == "Polygon":
            area += part.area
            length += part.length
    return area > 0 and length > 0 and (2 * area / length) < SEAM_NEAR


def _seam_pairs(geoms, sigs, srcs, cmp_cols):
    """Pasangan dari file berbeda yang menempel di garis lembar dengan atribut identik."""
    snapped = shapely.set_precision(geoms, SEAM_SNAP)
    tree = STRtree(snapped)
    left, right = tree.query(snapped, predicate="intersects")
    pairs, blocked, diffcols = [], 0, Counter()
    for i, j in zip(left, right):
        i, j = int(i), int(j)
        if i >= j or srcs[i] == srcs[j]:
            continue
        inter = shapely.intersection(snapped[i], snapped[j])
        r = _aligned_ratio(inter)
        line_seam = r is not None and r >= SEAM_ALIGNED
        if not (line_seam or _thin_overlap(inter)):
            continue
        if sigs[i] == sigs[j]:
            pairs.append((i, j))
        elif line_seam:
            blocked += 1
            diffcols.update(c for c, a, b in zip(cmp_cols, sigs[i], sigs[j]) if a != b)
    near = 0
    l2, r2 = tree.query(snapped, predicate="dwithin", distance=SEAM_NEAR)
    for i, j in zip(l2, r2):
        i, j = int(i), int(j)
        if (i < j and srcs[i] != srcs[j] and sigs[i] == sigs[j]
                and not shapely.intersects(snapped[i], snapped[j])):
            near += 1
    return pairs, blocked, diffcols, near, snapped


def _merge_components(res, labels, key_cols, join_cols, count_col, multi_geoms=None):
>>>>>>> 84c4ee6 (fix : read zip)
    order, groups = [], {}
    for idx, lab in enumerate(labels):
        if lab not in groups:
            groups[lab] = []
            order.append(lab)
        groups[lab].append(idx)
<<<<<<< HEAD
    geoms_all = res.geometry.to_numpy()
    jml_all = res["JML_FITUR"].to_numpy()
    attrs, geoms, jml, mixed = [], [], [], []
=======
    base = res.geometry.to_numpy()
    uni = multi_geoms if multi_geoms is not None else base
    jml = res[count_col].to_numpy()
    prev = res["ATR_CAMPUR"].to_numpy() if "ATR_CAMPUR" in res.columns else np.zeros(len(res), dtype=int)
    attrs, geoms, jml_out, mixed = [], [], [], []
>>>>>>> 84c4ee6 (fix : read zip)
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
        geoms.append(geoms_all[idxs[0]] if len(idxs) == 1 else shapely.union_all(geoms_all[idxs]))
        jml.append(int(jml_all[idxs].sum()))
        mixed.append(is_mixed)
    df = pd.DataFrame(attrs, columns=key_cols)
<<<<<<< HEAD
    df["JML_FITUR"] = jml
=======
    df[count_col] = jml_out
>>>>>>> 84c4ee6 (fix : read zip)
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
    ddir = out_dir / DISSOLVE_DIR
    ddir.mkdir(parents=True, exist_ok=True)
    if existing_outputs(ddir, name) and not o.overwrite:
        g.warnings.append("DISSOLVE_DILEWATI: hasil dissolve sudah ada (gunakan --overwrite)")
        return
    tmp = _new_tmp(ddir)
    try:
        gdf = read_dataframe(g.output)
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

<<<<<<< HEAD
        # tahap 2: satukan bentuk yang saling tumpang tindih
        n_rows = len(res)
        if o.dissolve_overlap and n_rows > 1:
            labels = _overlap_components(res.geometry.to_numpy(), o.dissolve_overlap_min)
            n_comp = len(set(labels))
            if n_comp < n_rows:
                res, n_mixed = _merge_components(res, labels, key_cols)
=======
        # tahap 2: garis lembar antar-file (sisi lurus + atribut identik)
        if o.dissolve_seams and len(res) > 1:
            cmp_cols = [c for c in key_cols if c.casefold() not in SOURCE_COLS]
            sigs = [tuple(None if pd.isna(v) else v for v in row)
                    for row in res[cmp_cols].astype(object).to_numpy()]
            srcs = res["SRC_FILE"].to_numpy()
            pairs, blocked, diffcols, near, snapped = _seam_pairs(
                res.geometry.to_numpy(), sigs, srcs, cmp_cols)
            if pairs:
                before = len(res)
                res, _ = _merge_components(res, _labels(len(res), pairs), key_cols,
                                           SOURCE_COLS, count_col, snapped)
                joined = joined or len(res) < before
                g.warnings.append(f"DISSOLVE: {len(pairs)} garis lembar antar-file disatukan "
                                  f"({before} -> {len(res)} bentuk)")
            if blocked:
                top = ", ".join(f"{c} ({k})" for c, k in diffcols.most_common(5))
                g.warnings.append(
                    f"DISSOLVE: {blocked} pasangan antar-file menempel di garis lurus tetapi atribut "
                    f"berbeda, tidak disatukan. Kolom penyebab: {top or '-'} "
                    "(jika kolom itu hanya metadata, gunakan --dissolve-ignore)")
            if near:
                g.warnings.append(f"DISSOLVE: {near} pasangan hampir menempel (celah < {SEAM_NEAR:g} derajat) "
                                  "dengan atribut identik, tidak disatukan")

        # tahap 3: satukan bentuk yang saling tumpang tindih
        n_rows = len(res)
        if o.dissolve_overlap and n_rows > 1:
            pairs = _overlap_pairs(res.geometry.to_numpy(), o.dissolve_overlap_min)
            if pairs:
                labels = _labels(n_rows, pairs)
                res, n_mixed = _merge_components(res, labels, key_cols, SOURCE_COLS, count_col)
                joined = joined or len(res) < n_rows
>>>>>>> 84c4ee6 (fix : read zip)
                g.warnings.append(
                    f"DISSOLVE: {n_rows} bentuk -> {n_comp} setelah fitur yang saling tumpang tindih "
                    f"disatukan (irisan > {o.dissolve_overlap_min:.0%} luas bentuk terkecil); "
                    f"{n_mixed} hasil beratribut campur (kolom dikosongkan, lihat ATR_CAMPUR)")
        if "ATR_CAMPUR" not in res.columns:
            res["ATR_CAMPUR"] = 0

        info = read_info(g.output)
        fams = {str(n): family(dt) for n, dt in zip(info["fields"], info["dtypes"])}
        for c in key_cols:
<<<<<<< HEAD
            if fams.get(c) == "int":
                res[c] = _cast(res[c], "int")
=======
            fam = fams.get(c)
            if fam in ("int", "float", "date", "text"):
                width = dtype_bits(dts[c]) if (o.is_gdb and fam == "int") else 64
                res[c] = _cast(res[c], fam, width)
        if o.is_gdb:  # hindari kolom 64 bit pada GDB
            res[count_col] = res[count_col].astype("int32")
            res["ATR_CAMPUR"] = res["ATR_CAMPUR"].astype("int32")
>>>>>>> 84c4ee6 (fix : read zip)

        tmp_shp = tmp / f"{name}.shp"
        write_dataframe(res, str(tmp_shp), driver="ESRI Shapefile", encoding="UTF-8")
        if int(read_info(str(tmp_shp))["features"]) != len(res):
            raise ValueError("verifikasi hasil dissolve gagal")
        finalize(tmp, ddir, name, o.overwrite)
        g.dissolved_output = str(ddir / f"{name}.shp")
        g.dissolved_features = len(res)
        g.warnings.append(f"DISSOLVE: {n_in} fitur -> {len(res)} fitur "
                          "(per file sumber dan atribut identik, plus tumpang tindih)")
        log.info("Dissolve %s: %d fitur -> %d (%s)", g.key, n_in, len(res), g.dissolved_output)
    except Exception as exc:  # noqa: BLE001
        g.warnings.append(f"DISSOLVE_GAGAL: {type(exc).__name__}: {exc}")
        log.error("Dissolve gagal %s: %s", g.key, exc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)