"""Draw TIDALOID's app icon (assets/tidaloid.ico): blue rounded square, white equalizer bars + download arrow.

Run:  python tools/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024  # master size, scaled down for each .ico size
OUT = Path(__file__).resolve().parent.parent / "assets" / "tidaloid.ico"


def gradient(size, top, bottom):
    g = Image.new("RGBA", (1, size))
    for y in range(size):
        t = y / (size - 1)
        g.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,))
    return g.resize((size, size))


def draw():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    mask = Image.new("L", (S, S), 0)
    m = int(S * 0.06)
    ImageDraw.Draw(mask).rounded_rectangle([m, m, S - m, S - m], radius=int(S * 0.22), fill=255)
    img.paste(gradient(S, (64, 156, 255), (0, 88, 208)), (0, 0), mask)

    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)
    # equalizer bars
    heights = (0.22, 0.34, 0.16, 0.30, 0.20)   # uneven, like a live equalizer
    bar_w, gap = S * 0.09, S * 0.05
    total = len(heights) * bar_w + (len(heights) - 1) * gap
    x = (S - total) / 2
    base = S * 0.56
    for h in heights:
        d.rounded_rectangle([x, base - S * h, x + bar_w, base], radius=bar_w / 2, fill=white)
        x += bar_w + gap
    # download arrow under the bars
    cx, top, w = S / 2, S * 0.63, S * 0.30
    d.polygon([(cx - w / 2, top), (cx + w / 2, top), (cx, top + S * 0.15)], fill=white)
    d.rounded_rectangle([cx - w / 2, S * 0.81, cx + w / 2, S * 0.85], radius=S * 0.02, fill=white)
    return img


if __name__ == "__main__":
    OUT.parent.mkdir(exist_ok=True)
    icon = draw()
    icon.save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    icon.resize((256, 256), Image.LANCZOS).save(OUT.with_suffix(".png"))
    print("wrote", OUT)
