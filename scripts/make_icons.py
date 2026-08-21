"""Generate the PWA icon set from one geometric definition.

The mark is drawn rather than loaded from a font file: a "T" is two rectangles,
so this reproduces byte-identically on any machine and needs no font shipped
with the repo. It mirrors .mark-logo in app.css — the same emerald gradient and
the same letterform the header shows.

Run after changing the palette:  python -m scripts.make_icons
"""

from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw

OUT = pathlib.Path(__file__).resolve().parent.parent / "app" / "static" / "icons"

PRIMARY = (14, 159, 110)       # --primary
PRIMARY_DARK = (7, 132, 90)    # --primary-dark
WHITE = (255, 255, 255)

SS = 4  # supersample factor, then downscale for clean edges


def _gradient(size: int) -> Image.Image:
    """The 145deg emerald gradient, approximated on the diagonal."""
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size - 2)
            px[x, y] = tuple(
                round(a + (b - a) * t) for a, b in zip(PRIMARY, PRIMARY_DARK)
            )
    return img


def _draw_t(canvas: Image.Image, left: float, top_: float, box: float) -> None:
    """The T, proportioned like the header mark, inside the given square."""
    d = ImageDraw.Draw(canvas)
    cx = left + box / 2
    bar_w, bar_h = box * 0.46, box * 0.115
    stem_w = box * 0.135
    top = top_ + box * 0.30
    bottom = top_ + box * 0.70
    r = bar_h * 0.34
    d.rounded_rectangle([cx - bar_w / 2, top, cx + bar_w / 2, top + bar_h], radius=r, fill=WHITE)
    d.rounded_rectangle([cx - stem_w / 2, top, cx + stem_w / 2, bottom], radius=r, fill=WHITE)


def _rounded(size: int, inset: float) -> Image.Image:
    """A rounded-square icon on transparent ground."""
    S = size * SS
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    pad = round(S * inset)
    box = S - 2 * pad

    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [pad, pad, pad + box, pad + box], radius=round(box * 0.30), fill=255
    )
    plate = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    plate.paste(_gradient(S), (0, 0), mask)
    canvas.alpha_composite(plate)

    _draw_t(canvas, pad, pad, box)
    return canvas.resize((size, size), Image.LANCZOS)


def _maskable(size: int) -> Image.Image:
    """Full-bleed background with the mark inside Android's 80% safe zone.

    The background must reach the edge and be a single unbroken fill: a
    launcher may crop this to a circle, a squircle, or a rounded square, and
    any inner plate shows up as a seam under the crop.
    """
    S = size * SS
    canvas = Image.new("RGBA", (S, S))
    canvas.paste(_gradient(S))
    safe = S * 0.62
    _draw_t(canvas, (S - safe) / 2, (S - safe) / 2, safe)
    return canvas.resize((size, size), Image.LANCZOS)


def _flatten(img: Image.Image, bg=(255, 255, 255)) -> Image.Image:
    """iOS ignores alpha and composites on black, which looks broken."""
    out = Image.new("RGB", img.size, bg)
    out.paste(img, (0, 0), img)
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    written = []

    for size in (192, 512):
        p = OUT / f"icon-{size}.png"
        _rounded(size, inset=0.06).save(p)
        written.append(p)

    for size in (192, 512):
        p = OUT / f"maskable-{size}.png"
        _maskable(size).save(p)
        written.append(p)

    # iOS home screen. No alpha, and iOS applies its own rounding.
    p = OUT / "apple-touch-icon.png"
    _flatten(_rounded(180, inset=0.0)).save(p)
    written.append(p)

    for p in written:
        print(f"  {p.name:24} {p.stat().st_size:>6} bytes")


if __name__ == "__main__":
    main()
