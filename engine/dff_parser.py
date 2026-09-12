import struct
import numpy as np

# RW chunk type IDs
T_STRUCT = 0x1
T_STRING = 0x2
T_EXTENSION = 0x3
T_TEXTURE = 0x6
T_MATERIAL = 0x7
T_MATLIST = 0x8
T_FRAMELIST = 0xE
T_GEOMETRY = 0xF
T_CLUMP = 0x10
T_ATOMIC = 0x14
T_GEOMETRYLIST = 0x1A
T_UNIQUEID = 0x1B
T_BINMESH = 0x50E

FLAG_TRISTRIP = 0x1
FLAG_POSITIONS = 0x2
FLAG_TEXTURED = 0x4
FLAG_PRELIT = 0x8
FLAG_NORMALS = 0x10
FLAG_LIGHT = 0x20
FLAG_MODMATCOLOR = 0x40
FLAG_TEXTURED2 = 0x80
FLAG_NATIVE = 0x1000000


def read_header(data, pos):
    type_, size, ver = struct.unpack_from('<III', data, pos)
    return type_, size, ver, pos + 12


def parse_chunks(data, start, end):
    """Yield (type, content_bytes, content_start_abs) for top-level chunks in [start,end)."""
    pos = start
    chunks = []
    while pos < end:
        if pos + 12 > end:
            break  # trailing junk, not a full chunk header
        type_, size, ver, cpos = read_header(data, pos)
        if cpos + size > end:
            break  # malformed / trailing junk
        chunks.append((type_, cpos, size))
        pos = cpos + size
    return chunks


def parse_string_chunk(data, pos, size):
    raw = data[pos:pos+size]
    s = raw.split(b'\x00', 1)[0]
    return s.decode('latin-1', errors='replace')


def parse_material(data, pos, size):
    end = pos + size
    chunks = parse_chunks(data, pos, end)
    tex_name = None
    color = (255, 255, 255, 255)
    u_addr, v_addr = 1, 1  # default RW addressing = WRAP if no TEXTURE chunk info
    for (ctype, cpos, csize) in chunks:
        if ctype == T_STRUCT:
            # material struct: flags(4), color(4 bytes rgba), unused(4), textured(4)
            flags, r, g, b, a, unused, is_textured = struct.unpack_from('<I4BII', data, cpos)
            color = (r, g, b, a)
        elif ctype == T_TEXTURE:
            tend = cpos + csize
            tchunks = parse_chunks(data, cpos, tend)
            names = []
            for (tctype, tcpos, tcsize) in tchunks:
                if tctype == T_STRING:
                    names.append(parse_string_chunk(data, tcpos, tcsize))
                elif tctype == T_STRUCT and tcsize >= 4:
                    # RW texture struct: filterMode(u8), uAddressing(u8),
                    # vAddressing(u8), pad(u8). Addressing: 1=WRAP,
                    # 2=MIRROR, 3=CLAMP, 4=BORDER. This is per-texture, set
                    # by whoever authored the model - hardcoding one mode
                    # for every file is wrong: some atlases are laid out
                    # for wrap, others deliberately reuse a mirrored UV
                    # island, and forcing the other mode samples the wrong
                    # part of the atlas (exactly the mismatched
                    # sleeve/leg/foot symptom).
                    _filter, ua, va, _pad = struct.unpack_from('<4B', data, tcpos)
                    if ua: u_addr = ua
                    if va: v_addr = va
            if names:
                tex_name = names[0]
    return {'texture': tex_name, 'color': color, 'u_addr': u_addr, 'v_addr': v_addr}


def parse_matlist(data, pos, size, global_materials):
    """RW MatList struct holds numMaterials followed by an int32 index per
    material: -1 means a real MATERIAL chunk follows next in the stream,
    >=0 means "reuse the material already parsed earlier in this file at
    that global position" (exporters do this to share one texture across
    several geometries without repeating the MATERIAL chunk). Materials
    referenced this way have NO chunk of their own here, so a naive scan
    that just collects T_MATERIAL chunks in order undercounts and
    misaligns everything after the first shared reference - which is what
    was silently scrambling mat_idx -> texture lookups downstream."""
    end = pos + size
    chunks = parse_chunks(data, pos, end)
    indices = []
    material_chunks = []
    for (ctype, cpos, csize) in chunks:
        if ctype == T_STRUCT:
            (num_materials,) = struct.unpack_from('<i', data, cpos)
            if num_materials > 0:
                indices = list(struct.unpack_from('<%di' % num_materials, data, cpos + 4))
        elif ctype == T_MATERIAL:
            material_chunks.append((cpos, csize))
    if not indices:
        # older/malformed files without the index array: assume all-new
        indices = [-1] * len(material_chunks)

    materials = []
    mi = 0
    for idx in indices:
        if idx is not None and idx >= 0:
            if idx < len(global_materials):
                materials.append(global_materials[idx])
            else:
                materials.append({'texture': None, 'color': (255, 255, 255, 255)})
        else:
            if mi < len(material_chunks):
                cpos, csize = material_chunks[mi]
                mi += 1
                mat = parse_material(data, cpos, csize)
            else:
                mat = {'texture': None, 'color': (255, 255, 255, 255)}
            materials.append(mat)
            global_materials.append(mat)
    return materials


def parse_binmesh(data, pos, size):
    flags, num_meshes, total_idx = struct.unpack_from('<III', data, pos)
    p = pos + 12
    meshes = []
    for i in range(num_meshes):
        num_idx, mat_idx = struct.unpack_from('<II', data, p)
        p += 8
        idx = np.frombuffer(data, dtype='<u4', count=num_idx, offset=p)
        p += 4 * num_idx
        meshes.append({'mat_idx': mat_idx, 'indices': idx.copy(), 'tristrip': bool(flags & FLAG_TRISTRIP)})
    return meshes


def parse_geometry(data, pos, size, global_materials):
    end = pos + size
    chunks = parse_chunks(data, pos, end)
    result = {'vertices': None, 'uvs': None, 'triangles': None, 'tri_mat': None,
              'materials': [], 'binmesh': None}
    for (ctype, cpos, csize) in chunks:
        if ctype == T_STRUCT:
            p = cpos
            flags, num_tri, num_vert, num_morph = struct.unpack_from('<IIII', data, p)
            p += 16
            num_uv_sets = (flags >> 16) & 0xFF
            if num_uv_sets == 0 and (flags & FLAG_TEXTURED):
                num_uv_sets = 1
            if flags & FLAG_TEXTURED2 and num_uv_sets == 0:
                num_uv_sets = 2

            if flags & FLAG_PRELIT:
                p += 4 * num_vert  # RGBA prelight, skip

            uvs = None
            if num_uv_sets > 0:
                uv_all = np.frombuffer(data, dtype='<f4', count=num_vert * 2 * num_uv_sets, offset=p)
                p += 4 * 2 * num_vert * num_uv_sets
                uv_all = uv_all.reshape(num_uv_sets, num_vert, 2)
                uvs = uv_all[0]  # use first uv set

            triangles = None
            tri_mat = None
            if num_tri > 0:
                tri_raw = np.frombuffer(data, dtype='<u2', count=num_tri * 4, offset=p)
                p += 8 * num_tri
                tri_raw = tri_raw.reshape(num_tri, 4)
                # order: v2, v1, matID, v3
                v2 = tri_raw[:, 0].astype(np.int64)
                v1 = tri_raw[:, 1].astype(np.int64)
                mat = tri_raw[:, 2].astype(np.int64)
                v3 = tri_raw[:, 3].astype(np.int64)
                triangles = np.stack([v1, v2, v3], axis=1)
                tri_mat = mat

            # bounding sphere
            p += 16
            has_pos, has_norm = struct.unpack_from('<II', data, p)
            p += 8

            positions = None
            for morph in range(max(num_morph, 1)):
                if has_pos:
                    pos_arr = np.frombuffer(data, dtype='<f4', count=num_vert * 3, offset=p)
                    p += 4 * 3 * num_vert
                    if positions is None:
                        positions = pos_arr.reshape(num_vert, 3)
                if has_norm:
                    p += 4 * 3 * num_vert  # skip normals, we compute our own

            result['vertices'] = positions
            result['uvs'] = uvs
            result['triangles'] = triangles
            result['tri_mat'] = tri_mat

        elif ctype == T_MATLIST:
            result['materials'] = parse_matlist(data, cpos, csize, global_materials)

        elif ctype == T_EXTENSION:
            eend = cpos + csize
            echunks = parse_chunks(data, cpos, eend)
            for (ectype, ecpos, ecsize) in echunks:
                if ectype == T_BINMESH:
                    result['binmesh'] = parse_binmesh(data, ecpos, ecsize)

    return result


def parse_framelist(data, pos, size):
    """Returns list of {'rot': 3x3 np.array (columns=right,up,at), 'pos': 3-vec, 'parent': int}."""
    end = pos + size
    chunks = parse_chunks(data, pos, end)
    frames = []
    for (ctype, cpos, csize) in chunks:
        if ctype == T_STRUCT:
            p = cpos
            (num_frames,) = struct.unpack_from('<I', data, p)
            p += 4
            for i in range(num_frames):
                vals = struct.unpack_from('<12f', data, p)
                p += 48
                parent_idx, flags = struct.unpack_from('<iI', data, p)
                p += 8
                right = np.array(vals[0:3], dtype=np.float64)
                up = np.array(vals[3:6], dtype=np.float64)
                at = np.array(vals[6:9], dtype=np.float64)
                posv = np.array(vals[9:12], dtype=np.float64)
                rot = np.stack([right, up, at], axis=1)  # columns = basis vectors
                frames.append({'rot': rot, 'pos': posv, 'parent': parent_idx})
            break  # only one struct chunk holds all frame data
    return frames


def compute_world_transforms(frames):
    """Compose parent-relative frame matrices into world-space matrices."""
    world = [None] * len(frames)

    def resolve(i):
        if world[i] is not None:
            return world[i]
        f = frames[i]
        parent = f['parent']
        if parent is None or parent < 0 or parent >= len(frames) or parent == i:
            w = {'rot': f['rot'], 'pos': f['pos']}
        else:
            pw = resolve(parent)
            w = {'rot': pw['rot'] @ f['rot'], 'pos': pw['rot'] @ f['pos'] + pw['pos']}
        world[i] = w
        return w

    for i in range(len(frames)):
        resolve(i)
    return world


def parse_atomic(data, pos, size):
    frame_idx, geom_idx, flags, unused = struct.unpack_from('<IIII', data, pos)
    return frame_idx, geom_idx


def load_dff(path):
    """Returns list of parts: [{'geometry': geom_dict, 'rot': 3x3, 'pos': 3-vec}, ...]
    Each part already carries its world-space transform (identity if no frame data)."""
    with open(path, 'rb') as f:
        data = f.read()
    top_chunks = parse_chunks(data, 0, len(data))
    clump = None
    for (t, cpos, csize) in top_chunks:
        if t == T_CLUMP:
            clump = (cpos, csize)
            break
    if clump is None:
        # Some exporters write the outer CLUMP size field slightly larger
        # than the bytes actually present (padding/miscount bug in the
        # tool, not real truncation - the inner chunk tree still parses
        # cleanly all the way to EOF). parse_chunks() rejects any chunk
        # whose declared size overruns the buffer, which discards the
        # whole top-level chunk in that case. Retry once by reading the
        # header directly and clamping the size to what's actually
        # available, instead of giving up.
        if len(data) >= 12:
            type_, size, ver, cpos = read_header(data, 0)
            if type_ == T_CLUMP:
                clamped_size = min(size, len(data) - cpos)
                clump = (cpos, clamped_size)
    if clump is None:
        raise ValueError('CLUMP chunk not found among top-level chunks: ' +
                          str([hex(t) for t, _, _ in top_chunks]))
    clump_chunks = parse_chunks(data, clump[0], clump[0] + clump[1])

    geometries = []
    frames = []
    atomics = []  # (frame_idx, geom_idx)
    global_materials = []  # RW shared-material registry, spans the whole file

    for (ctype, cp, cs) in clump_chunks:
        if ctype == T_GEOMETRYLIST:
            gend = cp + cs
            gchunks = parse_chunks(data, cp, gend)
            for (gctype, gcpos, gcsize) in gchunks:
                if gctype == T_GEOMETRY:
                    geometries.append(parse_geometry(data, gcpos, gcsize, global_materials))
        elif ctype == T_FRAMELIST:
            frames = parse_framelist(data, cp, cs)
        elif ctype == T_ATOMIC:
            achunks = parse_chunks(data, cp, cp + cs)
            for (actype, acpos, acsize) in achunks:
                if actype == T_STRUCT:
                    atomics.append(parse_atomic(data, acpos, acsize))
                    break

    if not geometries:
        return []

    world = compute_world_transforms(frames) if frames else []

    parts = []
    if atomics:
        # only trust per-atomic frame transforms when there are multiple atomics
        # (a genuinely multi-part model, e.g. vehicle/object). For the common
        # single-atomic skinned character mesh, the geometry's own vertex
        # positions are already the correct pose - applying the frame matrix
        # in that case can rotate/flip the whole model incorrectly.
        apply_transform = len(atomics) > 1
        for (frame_idx, geom_idx) in atomics:
            if geom_idx < 0 or geom_idx >= len(geometries):
                continue
            geom = geometries[geom_idx]
            if geom['vertices'] is None:
                continue
            if apply_transform and world and 0 <= frame_idx < len(world):
                rot = world[frame_idx]['rot']
                pos = world[frame_idx]['pos']
            else:
                rot = np.eye(3)
                pos = np.zeros(3)
            parts.append({'geometry': geom, 'rot': rot, 'pos': pos})
    else:
        # no atomic info (unusual) - fall back to raw geometries at identity transform
        for geom in geometries:
            if geom['vertices'] is None:
                continue
            parts.append({'geometry': geom, 'rot': np.eye(3), 'pos': np.zeros(3)})

    return parts


if __name__ == '__main__':
    import sys
    geoms = load_dff(sys.argv[1])
    for i, g in enumerate(geoms):
        print(f'--- geometry {i} ---')
        print('vertices:', None if g['vertices'] is None else g['vertices'].shape)
        print('uvs:', None if g['uvs'] is None else g['uvs'].shape)
        print('triangles (struct):', None if g['triangles'] is None else g['triangles'].shape)
        print('materials:', g['materials'])
        if g['binmesh']:
            print('binmesh meshes:', len(g['binmesh']))
            for m in g['binmesh']:
                print('  mat_idx', m['mat_idx'], 'n_idx', len(m['indices']), 'tristrip', m['tristrip'])
