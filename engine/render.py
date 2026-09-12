import os
import numpy as np
from PIL import Image
from dff_parser import load_dff


def _build_texture_index(tex_dir):
    """Map lowercased filename (no ext) -> actual file path, so texture lookup
    works regardless of case mismatches (common issue: dff on Windows references
    'Wheel.png' but the extracted file on a Linux VPS is 'wheel.png')."""
    index = {}
    try:
        for fname in os.listdir(tex_dir):
            base, ext = os.path.splitext(fname)
            if ext.lower() == '.png':
                index[base.lower()] = os.path.join(tex_dir, fname)
    except FileNotFoundError:
        pass
    return index


def tristrip_to_triangles(indices):
    """Convert a triangle-strip index list (with degenerate tris allowed) to a flat triangle list (N,3)."""
    tris = []
    idx = indices
    n = len(idx)
    for i in range(n - 2):
        a, b, c = idx[i], idx[i + 1], idx[i + 2]
        if a == b or b == c or a == c:
            continue  # degenerate
        if i % 2 == 0:
            tris.append((a, b, c))
        else:
            tris.append((a, c, b))  # flip winding on odd tris
    return np.array(tris, dtype=np.int64)


def build_mesh(geom):
    """Prefer BINMESH triangle/material data when available - some exporters
    leave the material field in the geometry STRUCT triangles as all-zero and
    only store the real per-triangle material assignment in the BINMESH
    extension. Falling back to STRUCT triangles there silently paints the
    whole mesh with material 0.

    The BINMESH 'tristrip' flag, once decoded correctly (see dff_parser),
    is the source of truth for how to interpret the index buffer. The old
    edge-length heuristic is kept only as a fallback for the rare case of
    genuinely malformed data, where the trusted decoding yields zero usable
    triangles but the other interpretation still produces something."""
    verts = geom['vertices']
    uvs = geom['uvs']
    if geom['binmesh'] is not None:
        all_tris = []
        all_mat = []
        for mesh in geom['binmesh']:
            idx = mesh['indices']
            as_list = idx[:len(idx) - len(idx) % 3].reshape(-1, 3)
            as_strip = tristrip_to_triangles(idx)
            trusted = as_strip if mesh['tristrip'] else as_list
            fallback = as_list if mesh['tristrip'] else as_strip
            if len(trusted) > 0:
                tris = trusted
            else:
                tris = fallback
            if len(tris) == 0:
                continue
            all_tris.append(tris)
            all_mat.append(np.full(len(tris), mesh['mat_idx'], dtype=np.int64))
        if all_tris:
            triangles = np.concatenate(all_tris, axis=0)
            tri_mat = np.concatenate(all_mat, axis=0)
            return verts, uvs, triangles, tri_mat, geom['materials']
    # fall back to struct triangle list (order: v1, v2, v3, matID already parsed)
    return verts, uvs, geom['triangles'], geom['tri_mat'], geom['materials']


def build_scene(parts):
    """Merge all atomic parts (each with its own world rot/pos) into one combined mesh.
    Applies each part's world transform to its vertices and offsets material indices
    so tri_mat correctly indexes into the combined materials list."""
    all_verts, all_uvs, all_tris, all_tri_mat, all_materials = [], [], [], [], []
    vert_offset = 0
    mat_offset = 0

    for part in parts:
        geom = part['geometry']
        verts, uvs, triangles, tri_mat, materials = build_mesh(geom)
        if verts is None or triangles is None or len(triangles) == 0:
            continue
        if uvs is None:
            uvs = np.zeros((len(verts), 2), dtype=np.float32)
        if tri_mat is None:
            tri_mat = np.zeros(len(triangles), dtype=np.int64)
        if not materials:
            materials = [{'texture': None, 'color': (255, 255, 255, 255)}]
            tri_mat = np.zeros(len(triangles), dtype=np.int64)

        rot = part['rot']
        pos = part['pos']
        world_verts = (rot @ verts.T).T + pos

        all_verts.append(world_verts)
        all_uvs.append(uvs)
        all_tris.append(triangles + vert_offset)
        all_tri_mat.append(tri_mat + mat_offset)
        all_materials.extend(materials)

        vert_offset += len(verts)
        mat_offset += len(materials)

    if not all_verts:
        raise ValueError('no renderable geometry found in dff (empty parts)')

    verts = np.concatenate(all_verts, axis=0)
    uvs = np.concatenate(all_uvs, axis=0)
    triangles = np.concatenate(all_tris, axis=0)
    tri_mat = np.concatenate(all_tri_mat, axis=0)
    return verts, uvs, triangles, tri_mat, all_materials


def _wrap_coord(x, mode):
    """Map a real UV coordinate into [0,1] per RW addressing mode, read
    per-material from the TEXTURE chunk (see dff_parser.parse_material):
    1=WRAP (plain repeat), 2=MIRROR (reflect at each integer boundary),
    3/4=CLAMP/BORDER (clamp to edge). This must be read from the file
    per-material, not assumed globally - different models legitimately
    use different modes, and forcing one mode for every file fixes some
    while breaking others (mismatched sleeve/leg/foot colors)."""
    if mode == 2:
        xm = np.mod(x, 2.0)
        return np.where(xm > 1.0, 2.0 - xm, xm)
    elif mode in (3, 4):
        return np.clip(x, 0.0, 1.0)
    else:  # mode == 1 (WRAP) or unknown -> RW's default is wrap
        return np.mod(x, 1.0)


def normalize(v):
    return v / (np.linalg.norm(v) + 1e-9)


def look_at(eye, target, up):
    f = normalize(target - eye)
    s = normalize(np.cross(f, up))
    u = np.cross(s, f)
    R = np.stack([s, u, -f], axis=0)  # 3x3
    t = -R @ eye
    return R, t


def render(dff_path, tex_dir, out_path, width=1400, height=1400,
           azimuth_deg=0, elevation_deg=0, bg_top=(30, 30, 38), bg_bottom=(8, 8, 12),
           flip_v=True, supersample=2, transparent_bg=False, up_axis='auto'):
    render_w, render_h = width * supersample, height * supersample
    _render_impl(dff_path, tex_dir, out_path, render_w, render_h,
                 azimuth_deg, elevation_deg, bg_top, bg_bottom, flip_v,
                 final_size=(width, height), transparent_bg=transparent_bg, up_axis=up_axis)


def _render_impl(dff_path, tex_dir, out_path, width, height,
                  azimuth_deg, elevation_deg, bg_top, bg_bottom, flip_v, final_size,
                  transparent_bg=False, up_axis='z'):
    parts = load_dff(dff_path)
    if not parts:
        raise ValueError(f'no atomics/geometry found in {dff_path}')
    verts, uvs, triangles, tri_mat, materials = build_scene(parts)

    # permute axes so that `up_axis` ends up as the Z (up) column.
    # 'auto' picks the axis with the largest extent as up (a standing humanoid
    # is tallest along its up axis). Of the two remaining axes, shoulder-to-
    # shoulder width is reliably larger than chest depth, so the larger of
    # the two becomes left-right (render X) and the smaller becomes
    # front-back / depth (render Y, the axis the camera looks along) - this
    # is what actually makes the character face the camera instead of
    # showing a profile.
    if up_axis == 'auto':
        extent = verts.max(axis=0) - verts.min(axis=0)
        up_idx = int(np.argmax(extent))
        remaining = [a for a in range(3) if a != up_idx]
        if extent[remaining[0]] >= extent[remaining[1]]:
            right_idx, depth_idx = remaining[0], remaining[1]
        else:
            right_idx, depth_idx = remaining[1], remaining[0]
        perm = [right_idx, depth_idx, up_idx]
        verts = verts[:, perm]
        # an odd permutation mirrors the mesh (determinant -1), which flips
        # triangle winding and breaks backface culling (holes / inside-out
        # faces show through). Detect that and negate the left-right axis to
        # restore a proper (non-mirrored) orientation - this only swaps
        # left/right, which is visually negligible for a roughly symmetric
        # standing character, unlike negating the forward axis (would show
        # the character's back) or the up axis (would flip it upside down).
        is_odd = (perm[0] > perm[1]) ^ (perm[0] > perm[2]) ^ (perm[1] > perm[2])
        if is_odd:
            verts = verts * np.array([1.0, -1.0, 1.0])
    else:
        axis_map = {'x': 0, 'y': 1, 'z': 2}
        up_idx = axis_map[up_axis]
        perm = {0: [1, 2, 0], 1: [2, 0, 1], 2: [0, 1, 2]}[up_idx]
        verts = verts[:, perm]

    # load textures for each material (fallback to flat material color, then magenta,
    # if a texture is missing or the material has none — never crash the whole batch)
    tex_index = _build_texture_index(tex_dir)
    tex_images = []
    for m in materials:
        name = m['texture']
        img_arr = None
        if name:
            path = tex_index.get(name.lower())
            if path:
                try:
                    img = Image.open(path).convert('RGBA')
                    img_arr = np.asarray(img).astype(np.float32) / 255.0
                except Exception:
                    img_arr = None
            else:
                print(f'  [warn] текстура не найдена: {name}.png')
        if img_arr is None:
            r, g, b, a = m.get('color', (255, 0, 255, 255))
            img_arr = np.zeros((2, 2, 4), dtype=np.float32)
            img_arr[:, :, 0] = r / 255.0
            img_arr[:, :, 1] = g / 255.0
            img_arr[:, :, 2] = b / 255.0
            img_arr[:, :, 3] = 1.0
        tex_images.append(img_arr)

    # --- center & scale model ---
    center = (verts.min(axis=0) + verts.max(axis=0)) / 2.0
    v = verts - center
    scale = 1.0 / np.max(np.linalg.norm(v, axis=1))
    v = v * scale

    # --- vertex normals (area-weighted average of adjacent face normals) for smooth shading ---
    p0_ = v[triangles[:, 0]]
    p1_ = v[triangles[:, 1]]
    p2_ = v[triangles[:, 2]]
    raw_fn = np.cross(p1_ - p0_, p2_ - p0_)  # not normalized -> magnitude = 2*area, used as weight
    vert_normal = np.zeros_like(v)
    np.add.at(vert_normal, triangles[:, 0], raw_fn)
    np.add.at(vert_normal, triangles[:, 1], raw_fn)
    np.add.at(vert_normal, triangles[:, 2], raw_fn)
    vn_len = np.linalg.norm(vert_normal, axis=1, keepdims=True)
    vert_normal = vert_normal / np.where(vn_len < 1e-9, 1, vn_len)

    # --- camera setup (model space is Z-up, GTA convention) ---
    az = np.radians(azimuth_deg)
    el = np.radians(elevation_deg)
    dist = 2.6
    eye = np.array([
        dist * np.cos(el) * np.sin(az),
        -dist * np.cos(el) * np.cos(az),
        dist * np.sin(el)
    ])
    target = np.array([0.0, 0.0, 0.0])
    up = np.array([0.0, 0.0, 1.0])
    R, t = look_at(eye, target, up)

    v_cam = (R @ v.T).T + t  # camera space, looking down -Z

    # perspective projection
    fov = np.radians(28)
    f_scale = 1.0 / np.tan(fov / 2)
    aspect = width / height
    near = 0.1

    z = -v_cam[:, 2]
    z = np.clip(z, near, None)
    x_ndc = (f_scale / aspect) * v_cam[:, 0] / z
    y_ndc = f_scale * v_cam[:, 1] / z

    sx = (x_ndc * 0.5 + 0.5) * width
    sy = (1 - (y_ndc * 0.5 + 0.5)) * height

    # --- auto-frame: rescale/recenter screen coords so the model fills the
    # frame nicely with a margin, regardless of exact camera distance/fov ---
    pad = 0.86  # fraction of frame the model's bbox should occupy
    minx_, maxx_ = sx.min(), sx.max()
    miny_, maxy_ = sy.min(), sy.max()
    bw, bh = maxx_ - minx_, maxy_ - miny_
    cx, cy = (minx_ + maxx_) / 2, (miny_ + maxy_) / 2
    fit_scale = min((width * pad) / bw, (height * pad) / bh)
    sx = (sx - cx) * fit_scale + width / 2
    sy = (sy - cy) * fit_scale + height / 2

    # --- lighting setup: per-vertex smooth (Gouraud) lighting ---
    light_dir1 = normalize(np.array([0.5, -0.6, 0.9]))   # key light from upper front
    light_dir2 = normalize(np.array([-0.6, 0.4, 0.3]))   # fill light
    ambient = 0.38

    vdiff1 = np.clip(np.dot(vert_normal, light_dir1), 0, 1)
    vdiff2 = np.clip(np.dot(vert_normal, light_dir2), 0, 1) * 0.35
    vert_light = np.clip(ambient + vdiff1 * 0.85 + vdiff2, 0, 1.35)

    # --- framebuffer ---
    color_buf = np.zeros((height, width, 3), dtype=np.float32)
    alpha_buf = np.zeros((height, width), dtype=np.float32)
    depth_buf = np.full((height, width), np.inf, dtype=np.float32)

    # background vertical gradient (or fully transparent)
    for row in range(height):
        tfrac = row / (height - 1)
        col = np.array(bg_top) * (1 - tfrac) + np.array(bg_bottom) * tfrac
        color_buf[row, :, :] = col / 255.0
    alpha_buf[:, :] = 0.0 if transparent_bg else 1.0

    invz = 1.0 / z  # for perspective-correct interpolation

    n_tris = len(triangles)

    # --- backface culling: skip triangles facing away from the camera ---
    va = v_cam[triangles[:, 0]]
    vb = v_cam[triangles[:, 1]]
    vc = v_cam[triangles[:, 2]]
    cam_normal = np.cross(vb - va, vc - va)
    front_facing = cam_normal[:, 2] > 0

    for i in range(n_tris):
        if not front_facing[i]:
            continue
        ia, ib, ic = triangles[i]
        xs = sx[[ia, ib, ic]]
        ys = sy[[ia, ib, ic]]

        minx = max(int(np.floor(xs.min())), 0)
        maxx = min(int(np.ceil(xs.max())), width - 1)
        miny = max(int(np.floor(ys.min())), 0)
        maxy = min(int(np.ceil(ys.max())), height - 1)
        if minx > maxx or miny > maxy:
            continue

        x0, y0 = xs[0], ys[0]
        x1, y1 = xs[1], ys[1]
        x2, y2 = xs[2], ys[2]
        denom = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(denom) < 1e-9:
            continue

        px, py = np.meshgrid(np.arange(minx, maxx + 1), np.arange(miny, maxy + 1))
        px = px.astype(np.float32) + 0.5
        py = py.astype(np.float32) + 0.5

        w0 = ((y1 - y2) * (px - x2) + (x2 - x1) * (py - y2)) / denom
        w1 = ((y2 - y0) * (px - x2) + (x0 - x2) * (py - y2)) / denom
        w2 = 1 - w0 - w1

        eps = -1e-4
        mask = (w0 >= eps) & (w1 >= eps) & (w2 >= eps)
        if not np.any(mask):
            continue

        ivz = invz[[ia, ib, ic]]
        pz = 1.0 / (w0 * ivz[0] + w1 * ivz[1] + w2 * ivz[2] + 1e-12)

        zbuf_region = depth_buf[miny:maxy + 1, minx:maxx + 1]
        closer = mask & (pz < zbuf_region)
        if not np.any(closer):
            continue

        # perspective-correct UV
        uv0, uv1, uv2 = uvs[ia], uvs[ib], uvs[ic]
        u = (w0 * uv0[0] * ivz[0] + w1 * uv1[0] * ivz[1] + w2 * uv2[0] * ivz[2]) * pz
        vv = (w0 * uv0[1] * ivz[0] + w1 * uv1[1] * ivz[1] + w2 * uv2[1] * ivz[2]) * pz

        tex = tex_images[tri_mat[i]]
        th, tw = tex.shape[0], tex.shape[1]
        mat = materials[tri_mat[i]]
        u_addr = mat.get('u_addr', 1)
        v_addr = mat.get('v_addr', 1)
        # UV outside [0,1] is not an error - it's a deliberate addressing
        # mode set per-texture by the exporter (WRAP/MIRROR/CLAMP, parsed
        # from the TEXTURE chunk in dff_parser). Using the wrong mode for
        # a given material samples the wrong part of the atlas (the
        # "wrong skin/glove" patches and diagonal stretch artifacts).
        u_wrapped = _wrap_coord(u, u_addr)
        tv_coord = vv if flip_v else (1 - vv)
        tv_wrapped = _wrap_coord(tv_coord, v_addr)
        tu = np.clip((u_wrapped * tw).astype(np.int64), 0, tw - 1)
        tvv = np.clip((tv_wrapped * th).astype(np.int64), 0, th - 1)

        sampled = tex[tvv, tu]  # (h,w,4)
        samp_rgb = sampled[:, :, :3]
        samp_a = sampled[:, :, 3]

        # perspective-correct interpolation of per-vertex lighting (Gouraud)
        lt = vert_light[[ia, ib, ic]]
        light = (w0 * lt[0] * ivz[0] + w1 * lt[1] * ivz[1] + w2 * lt[2] * ivz[2]) * pz
        shaded = np.clip(samp_rgb * light[:, :, None], 0, 1)

        opaque_enough = closer & (samp_a > 0.5)
        if not np.any(opaque_enough):
            continue

        zbuf_region[opaque_enough] = pz[opaque_enough]

        color_region = color_buf[miny:maxy + 1, minx:maxx + 1]
        color_region[opaque_enough] = shaded[opaque_enough]

        alpha_region = alpha_buf[miny:maxy + 1, minx:maxx + 1]
        alpha_region[opaque_enough] = 1.0

    depth_buf_out = depth_buf  # not saved, just used during render

    img = np.clip(color_buf, 0, 1)
    img8 = (img * 255).astype(np.uint8)
    if transparent_bg:
        a8 = np.clip(alpha_buf, 0, 1)
        a8 = (a8 * 255).astype(np.uint8)
        rgba = np.dstack([img8, a8])
        out_img = Image.fromarray(rgba, mode='RGBA')
    else:
        out_img = Image.fromarray(img8, mode='RGB')
    if final_size is not None and final_size != (width, height):
        out_img = out_img.resize(final_size, Image.LANCZOS)
    out_img.save(out_path)
    print('saved', out_path, 'triangles:', n_tris)


if __name__ == '__main__':
    import sys
    render(sys.argv[1], sys.argv[2], sys.argv[3])
