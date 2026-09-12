from __future__ import annotations

import io
import shutil
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ENGINE_DIR))

from dff_parser import load_dff          # noqa: E402
from mod import decrypt_mod_to_dff       # noqa: E402
import textures as tex_engine            # noqa: E402
import peds                              # noqa: E402
import render as render_engine           # noqa: E402

SESSIONS_ROOT = Path(__file__).resolve().parent.parent / "sessions"
SESSIONS_ROOT.mkdir(parents=True, exist_ok=True)

SESSION_TTL_SECONDS = 6 * 3600  # auto-cleanup after 6h of inactivity
MODEL_EXTS = {".dff", ".mod"}
TEXTURE_EXTS = {".png", ".btx", ".ktx", ".astc"}

_LOCK = threading.Lock()
_SESSIONS: dict[str, "Session"] = {}


class SessionError(Exception):
    pass


class Session:
    def __init__(self, sid: str):
        self.id = sid
        self.dir = SESSIONS_ROOT / sid
        self.textures_dir = self.dir / "textures"
        self.models_dir = self.dir / "models"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.textures_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.last_used = time.time()
        # model_name -> {"path": Path, "materials": [texture names], "friendly": str|None}
        self.models: dict[str, dict] = {}
        self.texture_log: list[str] = []
        self.model_log: list[str] = []
        self._render_cache: dict[tuple, bytes] = {}

    def touch(self):
        self.last_used = time.time()

    # ---------------------------------------------------------------- textures
    def add_texture_archive(self, filename: str, data: bytes):
        entries = _iter_archive_or_single(filename, data, TEXTURE_EXTS)
        added, skipped, failed = 0, 0, 0
        for name, blob in entries:
            ext = Path(name).suffix.lower()
            if ext not in TEXTURE_EXTS:
                skipped += 1
                continue
            stem = Path(name).stem
            dst = self.textures_dir / f"{stem}.png"
            try:
                if ext == ".png":
                    dst.write_bytes(blob)
                else:
                    with tempfile.TemporaryDirectory(prefix="texin_") as td:
                        src_path = Path(td) / Path(name).name
                        src_path.write_bytes(blob)
                        tex_engine.convert_texture_to_png(src_path, dst)
                added += 1
                self.texture_log.append(f"[OK] {name} -> {dst.name}")
            except Exception as e:
                failed += 1
                self.texture_log.append(f"[ERROR] {name}: {e}")
        return {"added": added, "skipped": skipped, "failed": failed}

    def texture_count(self):
        return len(list(self.textures_dir.glob("*.png")))

    # ------------------------------------------------------------------ models
    def add_model_archive(self, filename: str, data: bytes):
        entries = _iter_archive_or_single(filename, data, MODEL_EXTS)
        added, failed = 0, 0
        for name, blob in entries:
            ext = Path(name).suffix.lower()
            if ext not in MODEL_EXTS:
                continue
            stem = Path(name).stem
            dst = self.models_dir / f"{stem}.dff"
            try:
                if ext == ".mod":
                    dst.write_bytes(decrypt_mod_to_dff(blob))
                else:
                    dst.write_bytes(blob)
                self._register_model(dst)
                added += 1
                self.model_log.append(f"[OK] {name} -> {dst.name}")
            except Exception as e:
                failed += 1
                self.model_log.append(f"[ERROR] {name}: {e}")
        return {"added": added, "failed": failed}

    def _register_model(self, dff_path: Path):
        try:
            parts = load_dff(str(dff_path))
        except Exception as e:
            self.models[dff_path.stem] = {
                "path": dff_path, "materials": [], "friendly": None, "error": str(e),
            }
            return
        tex_names = []
        seen = set()
        for part in parts:
            for m in part["geometry"]["materials"]:
                name = m.get("texture")
                if name and name not in seen:
                    seen.add(name)
                    tex_names.append(name)
        entry = peds.lookup(dff_path.stem)
        self.models[dff_path.stem] = {
            "path": dff_path,
            "materials": tex_names,
            "friendly": entry["model_name"] if entry else None,
            "ped_id": entry["id"] if entry else None,
            "part_count": len(parts),
            "error": None,
        }

    def model_list(self):
        out = []
        for stem, info in sorted(self.models.items()):
            out.append({
                "name": stem,
                "friendly": info.get("friendly"),
                "ped_id": info.get("ped_id"),
                "texture_count": len(info.get("materials", [])),
                "error": info.get("error"),
            })
        return out

    def texture_preview(self, model_name: str):
        info = self.models.get(model_name)
        if not info:
            raise SessionError(f"model not found: {model_name}")
        index = {p.stem.lower(): p for p in self.textures_dir.glob("*.png")}
        found, missing = [], []
        for tname in info["materials"]:
            hit = index.get(tname.lower())
            if hit:
                found.append({"name": tname, "file": hit.name})
            else:
                missing.append(tname)
        return {"found": found, "missing": missing}

    # ------------------------------------------------------------------ render
    def render_png(self, model_name: str, azimuth: float, elevation: float,
                    size: int = 900, transparent: bool = True) -> bytes:
        info = self.models.get(model_name)
        if not info:
            raise SessionError(f"model not found: {model_name}")
        key = (model_name, round(azimuth, 1), round(elevation, 1), size, transparent)
        cached = self._render_cache.get(key)
        if cached is not None:
            return cached
        with tempfile.TemporaryDirectory(prefix="render_") as td:
            out_path = Path(td) / "out.png"
            render_engine.render(
                str(info["path"]), str(self.textures_dir), str(out_path),
                width=size, height=size,
                azimuth_deg=azimuth, elevation_deg=elevation,
                transparent_bg=transparent, supersample=2 if size <= 700 else 1,
            )
            data = out_path.read_bytes()
        if len(self._render_cache) > 64:
            self._render_cache.clear()
        self._render_cache[key] = data
        return data


def _iter_archive_or_single(filename: str, data: bytes, allowed_exts: set[str]):
    """Yields (name, bytes) pairs. If `filename` is a .zip, expands it and
    yields every entry whose extension is in allowed_exts. Otherwise yields
    the single (filename, data) pair as-is (extension is checked by the caller)."""
    ext = Path(filename or "").suffix.lower()
    if ext != ".zip":
        yield (filename or "upload"), data
        return
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename)
            if "__MACOSX" in name.parts or name.name.startswith("."):
                continue
            if name.suffix.lower() not in allowed_exts:
                continue
            yield name.name, zf.read(info)


# --------------------------------------------------------------------- registry

def create_session() -> Session:
    sid = uuid.uuid4().hex[:16]
    with _LOCK:
        s = Session(sid)
        _SESSIONS[sid] = s
    return s


def get_session(sid: str) -> Session:
    with _LOCK:
        s = _SESSIONS.get(sid)
    if s is None:
        raise SessionError("session not found or expired")
    s.touch()
    return s


def _cleanup_loop():
    while True:
        time.sleep(600)
        now = time.time()
        with _LOCK:
            expired = [sid for sid, s in _SESSIONS.items()
                       if now - s.last_used > SESSION_TTL_SECONDS]
            for sid in expired:
                s = _SESSIONS.pop(sid, None)
                if s:
                    shutil.rmtree(s.dir, ignore_errors=True)


def start_cleanup_thread():
    t = threading.Thread(target=_cleanup_loop, daemon=True)
    t.start()
