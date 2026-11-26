"""Minimal zero-dependency QR Code SVG/PNG generator (Byte mode, ECC Level L)."""

from __future__ import annotations

import base64
import numpy as np

# GF(256) with primitive polynomial 0x11D
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_gen_poly(degree: int) -> list[int]:
    poly = [1]
    for i in range(degree):
        next_poly = [0] * (len(poly) + 1)
        factor = _EXP[i]
        for j, coeff in enumerate(poly):
            next_poly[j] ^= coeff
            next_poly[j + 1] ^= _gf_mul(coeff, factor)
        poly = next_poly
    return poly


def _rs_remainder(data: list[int], ec_len: int) -> list[int]:
    gen = _rs_gen_poly(ec_len)
    res = list(data) + [0] * ec_len
    for i in range(len(data)):
        coef = res[i]
        if coef != 0:
            for j in range(1, len(gen)):
                res[i + j] ^= _gf_mul(gen[j], coef)
    return res[len(data) :]


# Version specs for Level L (versions 1..6, single EC block for 1..5, 2 blocks for 6)
_VERSION_PARAMS = {
    1: (21, 19, 7, []),
    2: (25, 34, 10, [6, 18]),
    3: (29, 55, 15, [6, 22]),
    4: (33, 80, 20, [6, 26]),
    5: (37, 108, 26, [6, 30]),
}


def encode_qr_matrix(text: str) -> np.ndarray:
    """Encode UTF-8 text into a boolean numpy QR matrix (True = dark module)."""
    payload = text.encode("utf-8")
    ver = None
    for v, (sz, data_cw, ec_cw, _) in _VERSION_PARAMS.items():
        # Byte mode header (4 bits) + char count (8 bits for v1..9) + payload*8 + 4 terminator
        if len(payload) + 2 <= data_cw:
            ver = v
            break
    if ver is None:
        raise ValueError("Payload too long for QR v1..v5")

    size, data_cw, ec_cw, align_pos = _VERSION_PARAMS[ver]

    # Build bit stream
    bits: list[int] = []

    def push_bits(val: int, length: int) -> None:
        for b in range(length - 1, -1, -1):
            bits.append((val >> b) & 1)

    push_bits(0b0100, 4)  # Byte mode
    push_bits(len(payload), 8)
    for byte in payload:
        push_bits(byte, 8)
    # Terminator (up to 4 zeros)
    term = min(4, data_cw * 8 - len(bits))
    push_bits(0, term)
    while len(bits) % 8 != 0:
        bits.append(0)

    data_bytes = []
    for i in range(0, len(bits), 8):
        val = 0
        for b in bits[i : i + 8]:
            val = (val << 1) | b
        data_bytes.append(val)

    pad_bytes = [0xEC, 0x11]
    idx = 0
    while len(data_bytes) < data_cw:
        data_bytes.append(pad_bytes[idx % 2])
        idx += 1

    ec_bytes = _rs_remainder(data_bytes, ec_cw)
    all_bytes = data_bytes + ec_bytes
    all_bits = []
    for byte in all_bytes:
        for b in range(7, -1, -1):
            all_bits.append((byte >> b) & 1)

    mat = np.zeros((size, size), dtype=np.int8)
    func = np.zeros((size, size), dtype=bool)

    def set_func(r: int, c: int, val: int) -> None:
        if 0 <= r < size and 0 <= c < size:
            mat[r, c] = val
            func[r, c] = True

    def place_finder(r0: int, c0: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if not (0 <= r < size and 0 <= c < size):
                    continue
                if dr in (-1, 7) or dc in (-1, 7):
                    set_func(r, c, 0)
                elif dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4):
                    set_func(r, c, 1)
                else:
                    set_func(r, c, 0)

    place_finder(0, 0)
    place_finder(0, size - 7)
    place_finder(size - 7, 0)

    # Alignment patterns
    if align_pos:
        for ar in align_pos:
            for ac in align_pos:
                if func[ar, ac]:
                    continue
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        val = 1 if (abs(dr) == 2 or abs(dc) == 2 or (dr == 0 and dc == 0)) else 0
                        set_func(ar + dr, ac + dc, val)

    # Timing patterns
    for i in range(8, size - 8):
        if not func[6, i]:
            set_func(6, i, 1 - (i % 2))
        if not func[i, 6]:
            set_func(i, 6, 1 - (i % 2))

    # Dark module & reserve format info
    set_func(size - 8, 8, 1)
    for i in range(9):
        if not func[8, i]:
            set_func(8, i, 0)
        if not func[i, 8]:
            set_func(i, 8, 0)
    for i in range(8):
        if not func[8, size - 1 - i]:
            set_func(8, size - 1 - i, 0)
        if not func[size - 1 - i, 8]:
            set_func(size - 1 - i, 8, 0)

    # Place data bits in zig-zag
    bit_idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for dc in (0, -1):
                c = col + dc
                if not func[r, c]:
                    bit = all_bits[bit_idx] if bit_idx < len(all_bits) else 0
                    mat[r, c] = bit
                    bit_idx += 1
        col -= 2
        upward = not upward

    # Mask 0 ((r + c) % 2 == 0) -> evaluate simple penalty across all 8 masks
    masks = [
        lambda r, c: (r + c) % 2 == 0,
        lambda r, c: r % 2 == 0,
        lambda r, c: c % 3 == 0,
        lambda r, c: (r + c) % 3 == 0,
        lambda r, c: (r // 2 + c // 3) % 2 == 0,
        lambda r, c: ((r * c) % 2 + (r * c) % 3) == 0,
        lambda r, c: (((r * c) % 2 + (r * c) % 3) % 2) == 0,
        lambda r, c: (((r + c) % 2 + (r * c) % 3) % 2) == 0,
    ]

    best_mat = None
    best_mask = 0
    best_score = 10**9

    for m_id, fn in enumerate(masks):
        cand = mat.copy()
        for r in range(size):
            for c in range(size):
                if not func[r, c] and fn(r, c):
                    cand[r, c] ^= 1
        _apply_format(cand, size, m_id)
        score = _penalty(cand, size)
        if score < best_score:
            best_score = score
            best_mask = m_id
            best_mat = cand

    return best_mat.astype(bool)  # type: ignore[union-attr]


def _apply_format(mat: np.ndarray, size: int, mask_id: int) -> None:
    # Level L (01) + 3-bit mask_id -> 15-bit BCH + XOR 0x5412
    data = (0b01 << 3) | mask_id
    rem = data << 10
    gen = 0b10100110111
    for i in range(4, -1, -1):
        if rem & (1 << (i + 10)):
            rem ^= gen << i
    fmt = ((data << 10) | rem) ^ 0b101010000010010

    bits = [(fmt >> i) & 1 for i in range(15)]
    # Primary around top-left
    coords1 = [
        (8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
        (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8),
    ]
    for i, (r, c) in enumerate(coords1):
        mat[r, c] = bits[14 - i]
    # Secondary split: bottom-left col 8 gets MSB bits[14..8], top-right row 8 gets bits[7..0]
    for i in range(7):
        mat[size - 1 - i, 8] = bits[14 - i]
    for i in range(8):
        mat[8, size - 8 + i] = bits[7 - i]


def _penalty(mat: np.ndarray, size: int) -> int:
    score = 0
    # 5+ same color in row/col
    for r in range(size):
        run = 1
        for c in range(1, size):
            if mat[r, c] == mat[r, c - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)
    for c in range(size):
        run = 1
        for r in range(1, size):
            if mat[r, c] == mat[r - 1, c]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)
    return score


def generate_qr_data_uri(text: str, box_size: int = 8, border: int = 2) -> str:
    """Return a clean SVG data URI for the encoded QR code."""
    mat = encode_qr_matrix(text)
    size = mat.shape[0]
    total = (size + border * 2) * box_size
    rects = []
    for r in range(size):
        for c in range(size):
            if mat[r, c]:
                x = (c + border) * box_size
                y = (r + border) * box_size
                rects.append(f'<rect x="{x}" y="{y}" width="{box_size}" height="{box_size}"/>')
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {total}" '
        f'width="{total}" height="{total}" shape-rendering="crispEdges">'
        f'<rect width="100%" height="100%" fill="#ffffff"/>'
        f'<g fill="#16222c">{"".join(rects)}</g></svg>'
    )
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"
