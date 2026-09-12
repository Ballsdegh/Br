"""
Texture conversion helpers: BTX / KTX -> PNG.

Reuses the bundled kram + PVRTexToolCLI binaries (engine/kram, engine/pvr) so a
Linux host does NOT need a separately-installed `astcenc` binary. This is a
clean re-implementation of the pipeline in the original btx.py, which could
not be imported directly (it calls argparse.parse_args() at import time).
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent

if platform.system() == "Linux":
    KRAM_PATH = ENGINE_DIR / "kram" / "linux" / "kram"
    PVR_PATH = ENGINE_DIR / "pvr" / "linux" / "PVRTexToolCLI"
elif platform.system() == "Windows":
    KRAM_PATH = ENGINE_DIR / "kram" / "win" / "kram.exe"
    PVR_PATH = ENGINE_DIR / "pvr" / "win" / "PVRTexToolCLI.exe"
else:
    KRAM_PATH = None
    PVR_PATH = None


class TextureConvertError(RuntimeError):
    pass


def _run(cmd):
    p = subprocess.run([str(c) for c in cmd], stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        raise TextureConvertError((p.stdout or "").strip() or f"command failed: {cmd[0]}")
    return p.stdout


def bundled_tools_available() -> bool:
    return bool(KRAM_PATH and PVR_PATH and Path(KRAM_PATH).is_file() and Path(PVR_PATH).is_file())


def find_astcenc():
    p = shutil.which("astcenc")
    if p:
        return Path(p)
    home_default = Path.home() / "astcenc"
    if home_default.is_file():
        return home_default
    return None


def btx_or_ktx_to_png(src: Path, dst: Path) -> None:
    """Convert a .btx (4-byte magic + KTX1/ASTC) or plain .ktx file to PNG using
    the bundled kram + PVRTexToolCLI binaries. Raises TextureConvertError on failure."""
    if not bundled_tools_available():
        raise TextureConvertError(
            "kram/PVRTexToolCLI binaries not available for this platform")
    src = Path(src)
    with tempfile.TemporaryDirectory(prefix="tex_") as td:
        td = Path(td)
        base = src.stem
        raw_ktx = td / f"{base}.ktx"
        dec_ktx = td / f"{base}_dec.ktx"

        data = src.read_bytes()
        if src.suffix.lower() == ".btx":
            # BTX = 4-byte custom prefix + real KTX1 container.
            raw_ktx.write_bytes(data[4:])
        else:
            raw_ktx.write_bytes(data)

        _run([KRAM_PATH, "decode", "-i", str(raw_ktx), "-o", str(dec_ktx)])
        _run([PVR_PATH, "-d", "-f", "r8g8b8a8", "-i", str(dec_ktx)])

        produced = None
        for p in td.iterdir():
            if p.name.startswith(base) and p.suffix.lower() == ".png":
                produced = p
                break
        if produced is None:
            raise TextureConvertError("PVRTexToolCLI produced no PNG output")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(produced, dst)


def astc_to_png(src: Path, dst: Path) -> None:
    """Standalone .astc file -> PNG. Requires astcenc (not bundled)."""
    astc = find_astcenc()
    if not astc:
        raise TextureConvertError("standalone .astc needs astcenc, which was not found on this host")
    import sys
    sys.path.insert(0, str(ENGINE_DIR))
    import btx_gui  # local module, safe to import (no top-level argparse)
    btx_gui.astc_to_png(Path(src), Path(dst), astc)


def convert_texture_to_png(src: Path, dst: Path) -> None:
    ext = Path(src).suffix.lower()
    if ext == ".png":
        shutil.copyfile(src, dst)
    elif ext in (".btx", ".ktx"):
        btx_or_ktx_to_png(src, dst)
    elif ext == ".astc":
        astc_to_png(src, dst)
    else:
        raise TextureConvertError(f"unsupported texture format: {ext}")
