from __future__ import annotations

from pathlib import Path

from pyogrio import read_info
from pyproj import CRS

from .discovery import missing_components, sibling_size
from .models import SOURCE_FIELDS, Group, Options, SourceFile

SHP_LIMIT = 2_000_000_000  # batas praktis .shp / .dbf (~2 GB)


def crs_equal(a: str, b: str) -> bool:
    if a == b:
        return True
    try:
        ca, cb = CRS.from_user_input(a), CRS.from_user_input(b)
    except Exception:
        return False
    if ca.equals(cb, ignore_axis_order=True):
        return True
    ea, eb = ca.to_epsg(min_confidence=70), cb.to_epsg(min_confidence=70)
    return ea is not None and ea == eb


def family(dtype: str) -> str:
    d = str(dtype)
    if d.startswith(("int", "uint")):
        return "int"
    if d.startswith("float"):
        return "float"
    if d.startswith("datetime"):
        return "date"
    return "text"


def _promote_family(left: str, right: str) -> str:
    """Choose a loss-minimizing common field family for schema union."""
    if left == right:
        return left
    if {left, right} <= {"int", "float"}:
        return "float"
    # Text can represent values from every supported attribute family.
    return "text"


def normalized_geometry_type(geometry_types: list[str] | set[str]) -> str | None:
    """Return one single-part output type for compatible singular/multi variants.

    Different geometry families cannot be converted without changing meaning.
    """
    families = {"Point": "Point", "MultiPoint": "Point",
                "LineString": "LineString", "MultiLineString": "LineString",
                "Polygon": "Polygon", "MultiPolygon": "Polygon"}
    parsed = []
    for raw in geometry_types:
        value = str(raw).strip()
        has_z = value.endswith(" Z") or value.startswith("3D ")
        base = value[:-2] if value.endswith(" Z") else value[3:] if value.startswith("3D ") else value
        if base not in families:
            return None
        parsed.append((base, families[base], has_z))
    if not parsed or len({family for _, family, _ in parsed}) != 1:
        return None
    family = parsed[0][1]
    return family + (" Z" if any(has_z for _, _, has_z in parsed) else "")


def inspect_file(sf: SourceFile, encoding: str | None = None) -> None:
    miss = missing_components(sf.path, sf.components)
    if miss:
        sf.error = "KOMPONEN_HILANG: " + ",".join(miss)
        return
    try:
        sf.info = read_info(str(sf.path), **({"encoding": encoding} if encoding else {}))
        if int(sf.info["features"]) < 0:
            raise ValueError("jumlah fitur tidak dapat dihitung")
    except Exception as exc:  # noqa: BLE001
        sf.info = None
        sf.error = f"TIDAK_BISA_DIBACA: {exc}"
        return
    if sf.components is not None:
        sf.size_shp = int(sf.components.get(".shp", sf.size_shp))
        sf.size_dbf = sibling_size(sf.path, ".dbf", sf.components)
    else:
        sf.size_shp = Path(sf.path).stat().st_size
        sf.size_dbf = sibling_size(sf.path, ".dbf")


def _fingerprint(sf: SourceFile):
    b = sf.info.get("total_bounds")
    bounds = tuple(round(float(x), 6) for x in b) if b is not None else ()
    return (sf.features, bounds, sf.size_shp, sf.size_dbf)


def validate_group(g: Group, o: Options) -> Group:
    bad = [f for f in g.files if f.error]
    if bad:
        if o.skip_bad_files:
            for f in bad:
                g.skipped.append((f.rel, f.error))
            g.warnings.append(f"PARSIAL: {len(bad)} file dilewati, hasil TIDAK mencakup seluruh sumber")
            g.files = [f for f in g.files if not f.error]
        else:
            g.status = "REVIEW_REQUIRED"
            g.reasons += [f"FILE_TIDAK_VALID: {f.rel} -> {f.error}" for f in bad]
            return g
    if not g.files:
        g.status, _ = "SKIPPED", g.reasons.append("TIDAK_ADA_FILE_VALID")
        return g
    if g.total_features == 0:
        g.status, _ = "SKIPPED", g.reasons.append("SEMUA_FILE_KOSONG")
        return g
    if len(g.files) == 1:
        g.status = "SINGLE_SOURCE"
        return g

    problems: list[str] = []

    # --- CRS (tidak pernah menebak) ---
    known = [f for f in g.files if f.crs]
    unknown = [f for f in g.files if not f.crs]
    if unknown and known:
        problems.append("CRS_CAMPURAN: sebagian file tanpa CRS: "
                        + ", ".join(f.rel for f in unknown[:5]))
    elif unknown:
        if o.target_crs:
            problems.append("CRS_TIDAK_DIKETAHUI: tidak bisa ditransformasi ke --target-crs")
        elif not o.allow_unknown_crs:
            problems.append("CRS_TIDAK_DIKETAHUI: semua file tanpa CRS (gunakan --allow-unknown-crs jika disengaja)")
        else:
            g.warnings.append("CRS tidak diketahui pada semua sumber; output tanpa .prj")
    else:
        distinct: list[str] = []
        for f in known:
            if not any(crs_equal(f.crs, d) for d in distinct):
                distinct.append(f.crs)
        if len(distinct) > 1 and not o.target_crs:
            problems.append(f"CRS_BERBEDA: {len(distinct)} CRS berbeda (gunakan --target-crs)")
        g.crs_out = o.target_crs or known[0].crs

    # --- Geometri ---
    gtypes = sorted({f.geometry_type for f in g.files})
    output_type = normalized_geometry_type(set(gtypes))
    if output_type is None:
        if len(gtypes) > 1 or (gtypes and gtypes[0] not in {"Point", "LineString", "Polygon"}):
            problems.append(f"GEOMETRI_TIDAK_KOMPATIBEL: {gtypes} (keluarga geometri berbeda/tidak dikenal)")
    elif len(gtypes) > 1 or any(t != output_type for t in gtypes):
        detail = f"GEOMETRI_DISERAGAMKAN: {gtypes} -> {output_type}; multipart dipecah"
        if output_type.endswith(" Z") and any(
                not (t.endswith(" Z") or t.startswith("3D ")) for t in gtypes):
            detail += "; koordinat Z sumber 2D diisi 0"
        g.warnings.append(detail)

    # --- Skema ---
    union: dict[str, str] = {}
    for f in g.files:
        for n, d in f.schema:
            fam = family(d)
            union[n] = _promote_family(union[n], fam) if n in union else fam
    ref = g.files[0]
    diffs, type_promotions, hard = [], [], False
    for f in g.files:
        if o.strict_schema and f.schema != ref.schema:
            diffs.append(f"{f.rel}: skema tidak identik dengan {ref.rel}")
            hard = True
            continue
        fam = {n: family(d) for n, d in f.schema}
        missing = [n for n in union if n not in fam]
        promoted = [n for n in fam if fam[n] != union[n]]
        if missing or promoted:
            diffs.append(f"{f.rel}: kolom hilang={missing} promosi_tipe={promoted}")
            if promoted:
                type_promotions.extend(promoted)
    if diffs:
        if o.allow_schema_union and not hard:
            details = "SKEMA_UNION: kolom hilang diisi NULL"
            if type_promotions:
                details += "; tipe dipromosikan (int+float -> float, konflik lain -> text)"
            g.warnings.append(details + " -> " + " | ".join(diffs))
        else:
            problems.append("SKEMA_BERBEDA: " + " | ".join(diffs))
    g.schema = list(union.items())

    # --- Field sumber ---
    if o.add_source_fields:
        clash = [n for n in SOURCE_FIELDS if n in {x.upper() for x in union}]
        if clash:
            problems.append(f"BENTROK_FIELD_SUMBER: kolom {clash} sudah ada (gunakan --no-add-source-fields)")

    # --- Duplikat (hanya peringatan, tidak dihapus) ---
    seen: dict = {}
    for f in g.files:
        if f.features == 0:
            continue
        fp = _fingerprint(f)
        if fp in seen:
            g.warnings.append(f"KEMUNGKINAN_DUPLIKAT: {f.rel} mirip {seen[fp]} (tetap digabung)")
        else:
            seen[fp] = f.rel

    # --- Batas ukuran Shapefile ---
    margin = 0.95 if o.add_source_fields else 1.0
    if (sum(f.size_shp for f in g.files) >= SHP_LIMIT * margin
            or sum(f.size_dbf for f in g.files) >= SHP_LIMIT * margin):
        problems.append("MELEBIHI_BATAS_SHAPEFILE: perkiraan >2 GB; gunakan GeoPackage (.gpkg) atau PostGIS")

    if problems:
        g.status = "REVIEW_REQUIRED"
        g.reasons += problems
    return g
