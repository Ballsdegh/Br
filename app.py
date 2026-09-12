#!/usr/bin/env python3
"""
FUCKBR Model Viewer — hostable web app.

Run for local/dev use:
    python3 app.py                 # http://127.0.0.1:8765
Run for production (see README_HOSTING.md):
    gunicorn -w 2 -b 0.0.0.0:8000 app:app
"""
from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file, abort

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "engine"))

from engine import session as sess              # noqa: E402
from engine.session import SessionError         # noqa: E402
from engine import mod as mod_engine            # noqa: E402
from engine import textures as tex_engine       # noqa: E402

MAX_UPLOAD_MB = 300

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

sess.start_cleanup_thread()


def _err(message, code=400):
    return jsonify({"ok": False, "error": message}), code


@app.get("/")
def index():
    return render_template("index.html")


# --------------------------------------------------------------------- session

@app.post("/api/session")
def api_create_session():
    s = sess.create_session()
    return jsonify({"ok": True, "session_id": s.id})


@app.post("/api/session/<sid>/textures")
def api_upload_textures(sid):
    try:
        s = sess.get_session(sid)
    except SessionError as e:
        return _err(str(e), 404)
    files = request.files.getlist("file")
    if not files:
        return _err("no files uploaded")
    summary = {"added": 0, "skipped": 0, "failed": 0}
    for f in files:
        result = s.add_texture_archive(f.filename, f.read())
        for k in summary:
            summary[k] += result[k]
    return jsonify({
        "ok": True,
        **summary,
        "texture_count": s.texture_count(),
        "log": s.texture_log[-50:],
    })


@app.post("/api/session/<sid>/models")
def api_upload_models(sid):
    try:
        s = sess.get_session(sid)
    except SessionError as e:
        return _err(str(e), 404)
    files = request.files.getlist("file")
    if not files:
        return _err("no files uploaded")
    summary = {"added": 0, "failed": 0}
    for f in files:
        result = s.add_model_archive(f.filename, f.read())
        for k in summary:
            summary[k] += result[k]
    return jsonify({
        "ok": True,
        **summary,
        "models": s.model_list(),
        "log": s.model_log[-50:],
    })


@app.get("/api/session/<sid>/models")
def api_list_models(sid):
    try:
        s = sess.get_session(sid)
    except SessionError as e:
        return _err(str(e), 404)
    return jsonify({"ok": True, "models": s.model_list(), "texture_count": s.texture_count()})


@app.get("/api/session/<sid>/models/<name>/preview")
def api_model_preview(sid, name):
    try:
        s = sess.get_session(sid)
        data = s.texture_preview(name)
    except SessionError as e:
        return _err(str(e), 404)
    return jsonify({"ok": True, **data})


@app.get("/api/session/<sid>/render/<name>")
def api_render(sid, name):
    try:
        s = sess.get_session(sid)
    except SessionError as e:
        return _err(str(e), 404)
    try:
        az = float(request.args.get("az", 0))
        el = float(request.args.get("el", 12))
        size = int(request.args.get("size", 900))
        size = max(128, min(size, 2000))
        transparent = request.args.get("bg", "transparent") == "transparent"
        png_bytes = s.render_png(name, az, el, size=size, transparent=transparent)
    except SessionError as e:
        return _err(str(e), 404)
    except Exception as e:
        return _err(f"render failed: {e}", 500)
    return send_file(io.BytesIO(png_bytes), mimetype="image/png")


# ---------------------------------------------------------------- legacy tools

TOOL_EXTS = {
    "mod2dff": {".mod"},
    "ani2ifp": {".ani"},
    "cls2col": {".cls"},
    "bpc2zip": {".bpc"},
    "png2btx": {".png"},
    "btx2png": {".btx", ".ktx", ".astc"},
}


@app.get("/tools")
def tools_page():
    return render_template("tools.html")


@app.post("/api/tools/convert")
def api_tools_convert():
    kind = request.form.get("kind", "")
    if kind not in TOOL_EXTS:
        return _err("unknown conversion kind")
    files = request.files.getlist("file")
    if not files:
        return _err("no files uploaded")

    results = []
    logs = []
    for f in files:
        try:
            for name, blob in sess._iter_archive_or_single(f.filename, f.read(), TOOL_EXTS[kind]):
                ext = Path(name).suffix.lower()
                if ext not in TOOL_EXTS[kind]:
                    continue
                stem = Path(name).stem
                out_name, out_bytes = _run_tool(kind, stem, ext, blob)
                results.append((out_name, out_bytes))
                logs.append(f"[OK] {name} -> {out_name}")
        except Exception as e:
            logs.append(f"[ERROR] {f.filename}: {e}")

    if not results:
        return _err("\n".join(logs) or "nothing produced", 500)

    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        seen = {}
        for out_name, out_bytes in results:
            n = seen.get(out_name, 0)
            seen[out_name] = n + 1
            final_name = out_name if n == 0 else f"{Path(out_name).stem}_{n}{Path(out_name).suffix}"
            z.writestr(final_name, out_bytes)
        z.writestr("_conversion_log.txt", "\n".join(logs) + "\n")
    mem.seek(0)
    resp = send_file(mem, mimetype="application/zip", as_attachment=True,
                      download_name="FUCKBR_result.zip")
    resp.headers["X-Info"] = "\n".join(logs)[:2000]
    return resp


def _run_tool(kind, stem, ext, blob):
    import tempfile
    if kind == "mod2dff":
        return f"{stem}.dff", mod_engine.decrypt_mod_to_dff(blob)

    if kind == "ani2ifp":
        data = bytearray(blob)
        x, y = 0x20, 0x04
        data[y:y] = data[x:x + 4]
        del data[x + 4:x + 8]
        return f"{stem}.ifp", bytes(data)

    if kind == "cls2col":
        import struct
        data = bytearray(blob)
        idx, blocks = 0, []
        while True:
            pos = data.find(b"CLST", idx)
            if pos < 0 or pos + 8 > len(data):
                break
            length = struct.unpack_from("<I", data, pos + 4)[0]
            end = min(len(data), pos + 8 + length)
            block = bytearray(data[pos:end])
            block[:4] = b"COL3"
            while len(block) % 4:
                block.append(0)
            struct.pack_into("<I", block, 4, len(block) - 8)
            blocks.append(block)
            idx = pos + 8 + length
        if not blocks:
            raise RuntimeError("CLST blocks not found")
        return f"{stem}.col", b"".join(blocks)

    if kind == "bpc2zip":
        key = b"1cK1a5UF2tU8*G2lW#&%"
        dec = bytes(b ^ key[i % len(key)] for i, b in enumerate(blob))
        return f"{stem}.zip", dec

    if kind == "png2btx":
        with tempfile.TemporaryDirectory(prefix="p2b_") as td:
            src = Path(td) / f"{stem}.png"
            dst = Path(td) / f"{stem}.btx"
            src.write_bytes(blob)
            astc = tex_engine.find_astcenc()
            if not astc:
                raise RuntimeError("PNG->BTX needs astcenc on the host (not bundled)")
            sys.path.insert(0, str(ROOT / "engine"))
            import btx_gui
            btx_gui.png_to_btx(src, dst, astc)
            return dst.name, dst.read_bytes()

    if kind == "btx2png":
        with tempfile.TemporaryDirectory(prefix="b2p_") as td:
            src = Path(td) / f"{stem}{ext}"
            dst = Path(td) / f"{stem}.png"
            src.write_bytes(blob)
            tex_engine.convert_texture_to_png(src, dst)
            return dst.name, dst.read_bytes()

    raise RuntimeError("unsupported kind")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8765, debug=False, threaded=True)
