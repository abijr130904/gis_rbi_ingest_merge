from __future__ import annotations

import difflib
import re
import unicodedata
from collections import defaultdict

from .models import SourceFile

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul",
             *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def normalize_name(stem: str, aliases: dict[str, str] | None = None) -> str:
    """Normalize formatting and apply optional whole-name category aliases."""
    s = unicodedata.normalize("NFKC", stem).strip()
    if s.lower().endswith(".shp"):
        s = s[:-4]
    s = s.casefold()
    if aliases:
        s = aliases.get(s, s)
        for old, new in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
            s = re.sub(rf"(?<!\w){re.escape(old)}(?!\w)", lambda _: new, s)
    return s


def group_files(files: list[SourceFile], aliases: dict[str, str] | None = None) -> dict[str, list[SourceFile]]:
    groups: dict[str, list[SourceFile]] = defaultdict(list)
    for f in files:
        f.key = normalize_name(f.stem, aliases)
        groups[f.key].append(f)
    return {k: sorted(v, key=lambda x: x.rel.casefold()) for k, v in groups.items()}


def safe_output_name(key: str) -> str:
    name = _INVALID.sub("_", key.upper()).strip(" .")
    if not name or name.lower() in _RESERVED:
        name = "_" + name
    return name


def suggest_similar(keys, threshold: float = 0.85) -> list[dict]:
    """Hanya rekomendasi untuk dibaca manusia. TIDAK pernah memicu merge."""
    ks = sorted(keys)
    out = []
    for i, a in enumerate(ks):
        for b in ks[i + 1:]:
            r = difflib.SequenceMatcher(None, a, b).ratio()
            if r >= threshold:
                out.append({"a": a, "b": b, "ratio": round(r, 3),
                            "note": "mirip, TIDAK digabung otomatis"})
    return out
