"""Parse the bundled peds.txt (GTA:SA style peds.ide) into a lookup table
so uploaded DFF files can be shown with a friendly name instead of a bare
filename, e.g. "TRUTH.dff" -> "#1 TRUTH".

Line format (comma separated, comments start with # and may trail the line):
  modelId, ModelName, TxdName, DefaultPedType, animGroup, carsMask, flag,
  animfile, radio1, radio2
"""
from __future__ import annotations

from pathlib import Path

_CACHE = None


def _parse(path: Path):
    by_txd = {}
    by_model = {}
    text = path.read_text(encoding="utf-8", errors="ignore")
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or "," not in line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        model_id = parts[0]
        if not model_id.isdigit():
            continue
        model_name = parts[1]
        txd_name = parts[2]
        entry = {"id": int(model_id), "model_name": model_name, "txd_name": txd_name}
        by_txd[txd_name.lower()] = entry
        by_model[model_name.lower()] = entry
    return by_txd, by_model


def load(path: Path | None = None):
    global _CACHE
    if _CACHE is None:
        if path is None:
            path = Path(__file__).resolve().parent.parent / "data" / "peds.txt"
        try:
            _CACHE = _parse(Path(path))
        except Exception:
            _CACHE = ({}, {})
    return _CACHE


def lookup(stem: str):
    """Given a DFF filename stem (no extension), try to find a matching ped
    definition by TxdName first, then by ModelName. Returns an entry dict or None."""
    by_txd, by_model = load()
    key = stem.lower()
    return by_txd.get(key) or by_model.get(key)
