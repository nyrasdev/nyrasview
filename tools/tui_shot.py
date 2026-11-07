import os, pty, sys, time, select, struct, fcntl, termios, pyte
from PIL import Image, ImageDraw, ImageFont
W, H = 150, 42
pid, fd = pty.fork()
if pid == 0:
    os.environ["TERM"] = "xterm-256color"
    os.execvp("nyrasview", ["nyrasview"])
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", H, W, 0, 0))
scr = pyte.Screen(W, H); st = pyte.ByteStream(scr)
def pump(t):
    end = time.time() + t
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try: st.feed(os.read(fd, 65536))
            except OSError: return
pump(5)
os.write(fd, b"\x1b[B"); pump(3)
os.write(fd, b"\x1b[A"); pump(3)
BG = (11, 11, 15); FG = (225, 225, 232)
NAMED = {"black": (20, 20, 24), "red": (239, 68, 68), "green": (34, 197, 94), "brown": (234, 179, 8), "yellow": (234, 179, 8),
         "blue": (59, 130, 246), "magenta": (217, 70, 239), "cyan": (34, 211, 238), "white": (225, 225, 232)}
def col(c, default):
    if c == "default": return default
    if c in NAMED: return NAMED[c]
    try: return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except Exception: return default
font = ImageFont.truetype("/usr/share/fonts/TTF/JetBrainsMonoNerdFontMono-Regular.ttf", 26)
bold = ImageFont.truetype("/usr/share/fonts/TTF/JetBrainsMonoNerdFontMono-Bold.ttf", 26)
cw = int(font.getlength("M")); ch = 36; pad = 36
img = Image.new("RGB", (W * cw + pad * 2, H * ch + pad * 2 + 44), (24, 24, 30))
d = ImageDraw.Draw(img)
d.rounded_rectangle([0, 0, img.width - 1, img.height - 1], 22, fill=BG)
for i, c in enumerate([(239, 68, 68), (234, 179, 8), (34, 197, 94)]):
    d.ellipse([pad + i * 30, 18, pad + i * 30 + 16, 34], fill=c)
for y in range(H):
    row = scr.buffer[y]
    for x in range(W):
        c = row[x]
        fg, bg = col(c.fg, FG), col(c.bg, BG)
        if c.reverse: fg, bg = bg, fg
        px, py = pad + x * cw, pad + 44 + y * ch
        if bg != BG: d.rectangle([px, py, px + cw, py + ch], fill=bg)
        if c.data.strip():
            d.text((px, py + 4), c.data, font=bold if c.bold else font, fill=fg)
img.save(sys.argv[1])
os.write(fd, b"q"); pump(0.5)
