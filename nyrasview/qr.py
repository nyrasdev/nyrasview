CAP = {1: (19, 1, 7), 2: (34, 1, 10), 3: (55, 1, 15), 4: (80, 1, 20), 5: (108, 1, 26), 6: (136, 2, 18)}

EXP = [0] * 512
LOG = [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def _mul(a, b):
    return 0 if a == 0 or b == 0 else EXP[LOG[a] + LOG[b]]


def _rs(data, n):
    gen = [1]
    for i in range(n):
        nxt = [0] * (len(gen) + 1)
        for j, g in enumerate(gen):
            nxt[j] ^= g
            nxt[j + 1] ^= _mul(g, EXP[i])
        gen = nxt
    rem = [0] * n
    for d in data:
        f = d ^ rem[0]
        rem = rem[1:] + [0]
        for j in range(n):
            rem[j] ^= _mul(gen[j + 1], f)
    return rem


def _codewords(text):
    raw = text.encode("utf-8")
    for v, (dc, blocks, ec) in CAP.items():
        if len(raw) + 2 <= dc:
            break
    else:
        raise ValueError("text too long for a small QR code")
    bits = [0, 1, 0, 0] + [(len(raw) >> i) & 1 for i in range(7, -1, -1)]
    for b in raw:
        bits += [(b >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, dc * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0
    while len(data) < dc:
        data.append(0xEC if pad % 2 == 0 else 0x11)
        pad += 1
    size = dc // blocks
    chunks = [data[i * size:(i + 1) * size] for i in range(blocks)]
    ecs = [_rs(c, ec) for c in chunks]
    out = []
    for i in range(size):
        out += [c[i] for c in chunks]
    for i in range(ec):
        out += [e[i] for e in ecs]
    return v, out


def matrix(text):
    v, words = _codewords(text)
    n = 17 + 4 * v
    m = [[0] * n for _ in range(n)]
    fn = [[False] * n for _ in range(n)]

    def put(x, y, dark):
        m[y][x] = 1 if dark else 0
        fn[y][x] = True

    for i in range(n):
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < n and 0 <= y < n:
                    d = max(abs(dx), abs(dy))
                    put(x, y, d not in (2, 4))
    if v >= 2:
        c = n - 7
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                put(c + dx, c + dy, max(abs(dx), abs(dy)) != 1)
    data = 1 << 3
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    bits = ((data << 10) | rem) ^ 0x5412

    def b(i):
        return (bits >> i) & 1
    for i in range(6):
        put(8, i, b(i))
    put(8, 7, b(6))
    put(8, 8, b(7))
    put(7, 8, b(8))
    for i in range(9, 15):
        put(14 - i, 8, b(i))
    for i in range(8):
        put(n - 1 - i, 8, b(i))
    for i in range(8, 15):
        put(8, n - 15 + i, b(i))
    put(8, n - 8, True)
    stream = [(w >> (7 - k)) & 1 for w in words for k in range(8)]
    i = 0
    right = n - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(n):
            for j in range(2):
                x = right - j
                up = ((right + 1) & 2) == 0
                y = n - 1 - vert if up else vert
                if not fn[y][x]:
                    bit = stream[i] if i < len(stream) else 0
                    i += 1
                    m[y][x] = bit ^ (1 if (x + y) % 2 == 0 else 0)
        right -= 2
    return m


def svg(text, scale=8, quiet=4, dark="#000", light="#fff"):
    m = matrix(text)
    n = len(m)
    size = (n + quiet * 2) * scale
    rects = "".join(f'<rect x="{(x + quiet) * scale}" y="{(y + quiet) * scale}" width="{scale}" height="{scale}"/>'
                    for y in range(n) for x in range(n) if m[y][x])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}" shape-rendering="crispEdges">'
            f'<rect width="100%" height="100%" fill="{light}"/><g fill="{dark}">{rects}</g></svg>')


def terminal(text, quiet=2):
    m = matrix(text)
    n = len(m)
    grid = [[0] * (n + quiet * 2) for _ in range(quiet)] + [[0] * quiet + row + [0] * quiet for row in m] + [[0] * (n + quiet * 2) for _ in range(quiet)]
    if len(grid) % 2:
        grid.append([0] * len(grid[0]))
    lines = []
    for y in range(0, len(grid), 2):
        line = ""
        for x in range(len(grid[0])):
            top, bot = not grid[y][x], not grid[y + 1][x]
            line += "█" if top and bot else "▀" if top else "▄" if bot else " "
        lines.append(line)
    return "\n".join(lines)
