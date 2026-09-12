#!/usr/bin/env python3
"""
BTX Converter GUI - single-file local web GUI.

Works on desktop and Termux/Android without Tkinter or third-party Python GUI
packages. Start this file with Python, then open the shown address in a browser.

Features:
  * BTX -> PNG (first/base mip, ASTC 6x6)
  * PNG -> BTX (ASTC 6x6 + full mip chain)
  * batch conversion
  * automatic astcenc discovery
  * no Pillow required
"""

from __future__ import annotations

import html
import json
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve()
DEFAULT_ASTC = Path.home() / "astcenc"

KTX_ID = bytes.fromhex("AB 4B 54 58 20 31 31 BB 0D 0A 1A 0A")
ASTC_MAGIC = bytes.fromhex("13 AB A1 5C")
ASTC6_RGBA = 0x93B4


def find_exe(name):
    p = shutil.which(name)
    if p:
        return Path(p)
    if name == "astcenc" and DEFAULT_ASTC.is_file():
        return DEFAULT_ASTC
    return None


def run(cmd):
    p = subprocess.run([str(x) for x in cmd], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True)
    if p.returncode:
        raise RuntimeError((p.stdout or "").strip() or
                           f"command failed: {p.returncode}")
    return p.stdout


def png_size(path):
    with open(path, "rb") as f:
        if f.read(8) != b"\x89PNG\r\n\x1a\n":
            raise ValueError("Not a PNG file")
        while True:
            h = f.read(8)
            if len(h) != 8:
                raise ValueError("Invalid PNG")
            n, typ = struct.unpack(">I4s", h)
            data = f.read(n)
            f.read(4)
            if typ == b"IHDR":
                return struct.unpack(">II", data[:8])
            if typ == b"IEND":
                break
    raise ValueError("PNG has no IHDR")


def mip_sizes(w, h):
    out = []
    while True:
        out.append((w, h))
        if w == 1 and h == 1:
            return out
        w = max(1, w // 2)
        h = max(1, h // 2)


def make_mips(src, work):
    sizes = mip_sizes(*png_size(src))
    magick = find_exe("magick") or find_exe("convert")
    if not magick:
        raise RuntimeError(
            "ImageMagick not found. On Termux run: pkg install imagemagick")
    result = []
    for i, (w, h) in enumerate(sizes):
        out = work / f"mip_{i}.png"
        run([magick, str(src), "-resize", f"{w}x{h}!", str(out)])
        result.append(out)
    return result


def astc_file_header(width, height, block=(6, 6, 1)):
    # ASTC .astc header:
    # magic(4), blockdim_x/y/z(3), xsize/y/z each 3 bytes.
    bx, by, bz = block
    h = bytearray(16)
    h[0:4] = ASTC_MAGIC
    h[4] = bx
    h[5] = by
    h[6] = bz
    h[7:10] = int(width).to_bytes(3, "little")
    h[10:13] = int(height).to_bytes(3, "little")
    h[13:16] = (1).to_bytes(3, "little")
    return bytes(h)


def build_ktx(width, height, payloads):
    header = KTX_ID + struct.pack(
        "<13I",
        0, 1, 0, ASTC6_RGBA, ASTC6_RGBA,
        width, height, 0,
        0, 1, len(payloads), 0
    )
    body = bytearray(header)
    for payload in payloads:
        body += struct.pack("<I", len(payload))
        body += payload
        body += b"\0" * ((4 - len(payload) % 4) % 4)
    return bytes(body)


def png_to_btx(src, dst, astc):
    src = Path(src)
    dst = Path(dst)
    w, h = png_size(src)
    with tempfile.TemporaryDirectory(prefix="png2btx_") as td:
        work = Path(td)
        mips = make_mips(src, work)
        payloads = []
        for i, mip in enumerate(mips):
            a = work / f"mip_{i}.astc"
            run([astc, "-cl", str(mip), str(a), "6x6", "-fast"])
            raw = a.read_bytes()
            if len(raw) < 16 or raw[:4] != ASTC_MAGIC:
                raise RuntimeError("astcenc produced an invalid ASTC file")
            payloads.append(raw[16:])
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"\0\0\0\0" + build_ktx(w, h, payloads))
    return f"{w}x{h}, {len(payloads)} mip levels"


def parse_btx(path):
    data = Path(path).read_bytes()
    if len(data) < 68:
        raise ValueError("BTX is too small")
    ktx = data[4:]
    if ktx[:12] != KTX_ID:
        raise ValueError("Not a BTX/KTX1 file (bad KTX identifier)")
    vals = struct.unpack_from("<13I", ktx, 12)
    endianness, gl_type, gl_type_size, gl_format, internal, base, width, height, depth, arrays, faces, levels, kv = vals
    if levels < 1:
        levels = 1
    pos = 64 + kv
    payloads = []
    sizes = []
    for level in range(levels):
        if pos + 4 > len(ktx):
            raise ValueError(f"Missing mip {level}")
        n = struct.unpack_from("<I", ktx, pos)[0]
        pos += 4
        if pos + n > len(ktx):
            raise ValueError(f"Truncated mip {level}")
        payloads.append(ktx[pos:pos+n])
        sizes.append((max(1, width >> level), max(1, height >> level)))
        pos += n
        pos += (4 - n % 4) % 4
    return width, height, internal, payloads, sizes



def astc_to_png(src, dst, astc):
    """Decode a standalone .astc file using its own 16-byte ASTC header."""
    src, dst = Path(src), Path(dst)
    data = src.read_bytes()
    if len(data) < 16 or data[:4] != ASTC_MAGIC:
        raise ValueError("Not a standalone ASTC file (bad ASTC identifier)")
    bx, by, bz = data[4], data[5], data[6]
    if bz != 1:
        raise ValueError(f"Unsupported ASTC Z block dimension: {bz}")
    width = int.from_bytes(data[7:10], "little")
    height = int.from_bytes(data[10:13], "little")
    if not width or not height:
        raise ValueError("Invalid ASTC dimensions")
    dst.parent.mkdir(parents=True, exist_ok=True)
    run([str(astc), "-dl", str(src), str(dst)])
    return f"{width}x{height}, ASTC {bx}x{by}"

def btx_to_png(src, dst, astc):
    width, height, internal, payloads, sizes = parse_btx(src)
    # The supplied BTX uses ASTC 6x6 RGBA (GL enum 0x93B4 / 37812).
    if internal != ASTC6_RGBA:
        raise ValueError(
            f"Unsupported GL internal format {internal}; expected {ASTC6_RGBA} (ASTC 6x6 RGBA)")
    payload = payloads[0]
    with tempfile.TemporaryDirectory(prefix="btx2png_") as td:
        td = Path(td)
        astc_file = td / "base.astc"
        astc_file.write_bytes(astc_file_header(width, height) + payload)
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        # PNG is supported by astcenc's image loader.
        run([astc, "-dl", str(astc_file), str(dst)])
    return f"{width}x{height}, {len(payloads)} mip levels"


PAGE = r"""<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BTX Converter</title>
<style>
body{font-family:system-ui,sans-serif;max-width:760px;margin:0 auto;padding:20px;background:#111;color:#eee}
.card{background:#1d1d1d;border-radius:14px;padding:18px;margin:14px 0}
h1{margin-top:0} button{padding:12px 18px;border:0;border-radius:10px;cursor:pointer}
input{max-width:100%}.drop{border:2px dashed #666;border-radius:12px;padding:28px;text-align:center;margin:12px 0}
#log{white-space:pre-wrap;background:#090909;padding:12px;border-radius:10px;min-height:80px}
small{color:#aaa}
</style>
</head>
<body>
<h1>BTX Converter</h1>
<p>PNG ↔ BTX · ASTC 6×6 · работает через браузер, без Tkinter.</p>

<div class="card">
<h2>BTX → PNG</h2>
<input id="btx" type="file" accept=".btx">
<br><br><button onclick="convert('btx')">Конвертировать</button>
</div>

<div class="card">
<h2>PNG → BTX</h2>
<input id="png" type="file" accept=".png,image/png">
<br><br><button onclick="convert('png')">Конвертировать</button>
</div>

<div class="card">
<h3>astcenc</h3>
<input id="astc" placeholder="/path/to/astcenc" style="width:100%;box-sizing:border-box">
<small>На Termux обычно: /data/data/com.termux/files/home/astcenc</small>
</div>

<div class="card"><h3>Результат</h3><div id="log">Готово.</div></div>

<script>
async function convert(kind){
 const file=(kind==='btx'?document.getElementById('btx'):document.getElementById('png')).files[0];
 if(!file){alert('Выбери файл');return}
 const fd=new FormData();
 fd.append('file',file);
 fd.append('kind',kind);
 fd.append('astc',document.getElementById('astc').value);
 document.getElementById('log').textContent='Конвертация...';
 try{
  const r=await fetch('/convert',{method:'POST',body:fd});
  if(!r.ok) throw new Error(await r.text());
  const blob=await r.blob();
  const cd=r.headers.get('Content-Disposition')||'';
  const m=cd.match(/filename="([^"]+)"/);
  const name=m?m[1]:(kind==='btx'?file.name.replace(/\\.btx$/i,'.png'):file.name.replace(/\\.png$/i,'.btx'));
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download=name;
  a.click(); URL.revokeObjectURL(a.href);
  document.getElementById('log').textContent='Готово: '+name;
 }catch(e){document.getElementById('log').textContent='Ошибка: '+e}
}
</script>
</body></html>"""


def parse_multipart(body, content_type):
    # Minimal multipart parser sufficient for browser FormData uploads.
    boundary = content_type.split("boundary=", 1)[1]
    boundary = boundary.strip().strip('"').encode()
    marker = b"--" + boundary
    parts = body.split(marker)
    fields = {}
    for part in parts[1:]:
        if part.startswith(b"--"):
            break
        part = part.lstrip(b"\r\n")
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        headers = {}
        for line in head.split(b"\r\n"):
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.lower().strip()] = v.strip()
        disp = headers.get(b"content-disposition", b"").decode("latin1")
        name = None
        filename = None
        for item in disp.split(";"):
            item = item.strip()
            if item.startswith("name="):
                name = item.split("=", 1)[1].strip('"')
            elif item.startswith("filename="):
                filename = item.split("=", 1)[1].strip('"')
        if name:
            fields[name] = (filename, data)
    return fields


class Handler(BaseHTTPRequestHandler):
    server_version = "BTXGUI/1.0"

    def log_message(self, fmt, *args):
        print("[HTTP]", fmt % args)

    def do_GET(self):
        if urlparse(self.path).path != "/":
            self.send_error(404)
            return
        data = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if urlparse(self.path).path != "/convert":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n)
            fields = parse_multipart(body, self.headers.get("Content-Type", ""))
            filename, data = fields["file"]
            kind = fields.get("kind", (None, b""))[1].decode()
            astc_text = fields.get("astc", (None, b""))[1].decode().strip()
            astc = Path(astc_text) if astc_text else find_exe("astcenc")
            if not astc or not astc.is_file():
                raise RuntimeError("astcenc не найден. Укажи путь в поле astcenc.")
            safe = Path(filename or "input").name
            with tempfile.TemporaryDirectory(prefix="btxgui_") as td:
                td = Path(td)
                src = td / safe
                src.write_bytes(data)
                if kind == "btx":
                    out = td / (src.stem + ".png")
                    info = btx_to_png(src, out, astc)
                elif kind == "png":
                    out = td / (src.stem + ".btx")
                    info = png_to_btx(src, out, astc)
                else:
                    raise ValueError("Unknown conversion")
                result = out.read_bytes()
                name = out.name

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("X-BTX-Info", info)
            self.send_header("Content-Length", str(len(result)))
            self.end_headers()
            self.wfile.write(result)
        except Exception as e:
            msg = ("ERROR: " + str(e)).encode("utf-8", "replace")
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="BTX Converter web GUI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--astcenc", default=None)
    args = ap.parse_args()

    if args.astcenc:
        global DEFAULT_ASTC
        DEFAULT_ASTC = Path(args.astcenc).expanduser()

    astc = find_exe("astcenc")
    if astc:
        print(f"astcenc: {astc}")
    else:
        print("WARNING: astcenc not found yet; set it in the browser.")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"\nBTX GUI: {url}")
    print("Stop: Ctrl+C")
    if args.host == "127.0.0.1":
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
