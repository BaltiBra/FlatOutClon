"""Lector/extractor de archivos .bfs (bfs1, v2007.03.10 = FlatOut Ultimate Carnage).

Formato portado de la documentacion/codigo de xNyaDev/bfstool (MIT/Apache-2.0).

Uso:
    python bfs.py list    <archivo.bfs> [filtro_substring]
    python bfs.py extract <archivo.bfs> <carpeta_salida> [filtro_substring]
"""
import struct
import sys
import zlib
from pathlib import Path

MAGIC = b"bfs1"
VERSION = 0x20070310
HASH_SIZE = 0x3E5


class Entry:
    __slots__ = ("flags", "copies", "offset", "size", "packed", "crc", "folder", "file", "name")


def _decode_huffman_dict(raw):
    """raw: bytes de pares (value, type). Devuelve {pattern: byte}."""
    nodes = [(raw[i], raw[i + 1]) for i in range(0, len(raw), 2)]
    result = {}
    stack = [(1, 0)]
    while stack:
        key, pos = stack.pop()
        if pos >= len(nodes):
            continue
        value, kind = nodes[pos]
        if kind == 0x80:
            result[key] = value
        else:
            stack.append(((key << 1) | 1, pos + 1))
            stack.append((key << 1, value))
    return result


def _decode_name(data, table, length):
    out = bytearray()
    pattern = 1
    for byte in data:
        for bit in range(8):
            pattern = (pattern << 1) | ((byte >> bit) & 1)
            ch = table.get(pattern)
            if ch is not None:
                out.append(ch)
                pattern = 1
                if len(out) == length:
                    return bytes(out).decode("utf-8", "replace")
    return bytes(out).decode("utf-8", "replace")


def read_archive(f):
    f.seek(0)
    magic, version, header_end, file_count = struct.unpack("<4sIII", f.read(16))
    if magic != MAGIC or version != VERSION:
        raise ValueError(f"No es un bfs1 v2007 (magic={magic!r}, version={version:#x})")
    (hash_size,) = struct.unpack("<I", f.read(4))
    if hash_size != HASH_SIZE:
        raise ValueError(f"hash_size inesperado: {hash_size:#x}")
    f.seek(hash_size * 8, 1)  # tabla hash: no hace falta, los headers son contiguos
    meta_start = hash_size * 8 + 20
    fh_off, name_off, len_off, dict_off, data_off = struct.unpack("<5I", f.read(20))

    bounds = sorted([header_end, fh_off + meta_start, name_off + meta_start,
                     len_off + meta_start, dict_off + meta_start, data_off + meta_start])

    def section(start_rel):
        start = start_rel + meta_start
        end = bounds[bounds.index(start) + 1]
        f.seek(start)
        return f.read(end - start)

    offsets = struct.unpack("<%dI" % (len(section(name_off)) // 4), section(name_off))
    lengths = struct.unpack("<%dH" % (len(section(len_off)) // 2), section(len_off))
    table = _decode_huffman_dict(section(dict_off))
    blob = section(data_off)

    names = []
    for i, (off, ln) in enumerate(zip(offsets, lengths)):
        end = offsets[i + 1] if i + 1 < len(offsets) else len(blob)
        names.append(_decode_name(blob[off:end], table, ln))

    f.seek(fh_off + meta_start)
    entries = []
    for _ in range(file_count):
        e = Entry()
        e.flags, _pad, e.copies, e.offset, e.size, e.packed, e.crc, e.folder, e.file = \
            struct.unpack("<BBHIIIIHH", f.read(24))
        if e.copies:
            f.seek(4 * e.copies, 1)
        e.name = f"{names[e.folder]}/{names[e.file]}"
        entries.append(e)
    return entries


def read_data(f, e):
    f.seek(e.offset)
    raw = f.read(e.packed)
    return zlib.decompress(raw) if e.flags & 1 else raw


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in ("list", "extract"):
        print(__doc__)
        sys.exit(1)
    cmd, path = sys.argv[1], Path(sys.argv[2])
    with open(path, "rb") as f:
        entries = read_archive(f)
        if cmd == "list":
            flt = sys.argv[3].lower() if len(sys.argv) > 3 else ""
            for e in entries:
                if flt in e.name.lower():
                    print(f"{e.size:>10} {e.packed:>10} {'Z' if e.flags & 1 else '-'} {e.name}")
            print(f"# {len(entries)} archivos", file=sys.stderr)
            return
        out = Path(sys.argv[3])
        flt = sys.argv[4].lower() if len(sys.argv) > 4 else ""
        done = bad = 0
        for e in entries:
            if flt not in e.name.lower():
                continue
            dest = out / e.name.replace("\\", "/")
            try:
                data = read_data(f, e)
                if len(data) != e.size:
                    raise ValueError(f"tamaño {len(data)} != {e.size}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                done += 1
            except Exception as ex:  # seguir con el resto y reportar al final
                bad += 1
                print(f"ERROR {e.name}: {ex}", file=sys.stderr)
        print(f"extraidos {done}, errores {bad}")


if __name__ == "__main__":
    main()
