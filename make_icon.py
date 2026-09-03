"""Generate the cc-manager icon.

    python make_icon.py

Design: a stack of session rows, each with its status dot, on the app's own
dark tile. It echoes what the app actually shows - a list of sessions with
live/stopped/idle states - rather than a generic terminal glyph.

Drawn rather than generated: an icon has to survive being 16px in a taskbar,
where diffusion output turns to mush. Renders at 8x and downsamples, so the
small sizes stay clean. Writes a multi-resolution .ico (16-256) plus a PNG.
"""
import pathlib

from PIL import Image, ImageDraw

BASE = pathlib.Path(__file__).resolve().parent
OUT_ICO = BASE / "cc_manager.ico"
OUT_PNG = BASE / "cc_manager.png"

SS = 8                        # supersample factor
SIZE = 256

# straight out of the GUI's palette, so the icon and the window agree
BG = (11, 15, 20, 255)        # #0b0f14 app background
TILE_EDGE = (31, 41, 55, 255)  # #1f2937 panel border
ROW = (31, 41, 55, 255)       # unselected row
ROW_SEL = (31, 58, 95, 255)   # #1f3a5f selection
BAR = (107, 114, 128, 255)    # #6b7280 muted text
BAR_SEL = (229, 231, 235, 255)  # #e5e7eb bright text

LIVE = (74, 222, 128, 255)    # #4ade80
STOPPED = (251, 191, 36, 255)  # #fbbf24
IDLE = (107, 114, 128, 255)   # #6b7280


def build(size):
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img, "RGBA")

    # Below ~32px there is not enough room for three rows plus their gaps -
    # rendered at 16 they smear into one grey block and the dots vanish. The
    # small frames drop to two fatter rows with less inset, which still reads
    # as a list while keeping the status dots visible.
    tiny = size <= 32

    # rounded dark tile, same corner radius feel as the LED Studio icon
    pad = S * 0.02
    d.rounded_rectangle([pad, pad, S - pad, S - pad],
                        radius=S * 0.22, fill=BG,
                        outline=TILE_EDGE, width=max(1, int(S * 0.012)))

    rows = [
        (LIVE, True),      # the selected, running one
        (STOPPED, False),
        (IDLE, False),
    ]
    if tiny:
        rows = [(LIVE, True), (IDLE, False)]

    inset = 0.105 if tiny else 0.155
    left = S * inset
    right = S - S * inset
    row_h = S * (0.235 if tiny else 0.150)
    gap = S * (0.085 if tiny else 0.055)
    total = len(rows) * row_h + (len(rows) - 1) * gap
    y = (S - total) / 2

    dot_r = row_h * (0.290 if tiny else 0.235)
    dot_cx = left + dot_r * 1.5

    for colour, selected in rows:
        # row plate, so the dot has something to sit on at small sizes
        d.rounded_rectangle([left, y, right, y + row_h],
                            radius=row_h * 0.34,
                            fill=ROW_SEL if selected else ROW)

        cy = y + row_h / 2
        d.ellipse([dot_cx - dot_r, cy - dot_r, dot_cx + dot_r, cy + dot_r],
                  fill=colour)

        # the "title", a bar whose length varies so it reads as text
        bar_x0 = dot_cx + dot_r * 2.2
        bar_w = (right - bar_x0) * (0.86 if selected else 0.62)
        bar_h = row_h * 0.185
        d.rounded_rectangle([bar_x0, cy - bar_h / 2,
                             bar_x0 + bar_w, cy + bar_h / 2],
                            radius=bar_h / 2,
                            fill=BAR_SEL if selected else BAR)
        y += row_h + gap

    return img.resize((size, size), Image.LANCZOS)


def main():
    sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = [build(s) for s in sizes]
    frames[-1].save(OUT_PNG)

    # append_images matters: given only `sizes`, Pillow ignores the frames
    # entirely and downsamples the one image it was called on, so the
    # simplified small layouts above would never reach the file.
    frames[-1].save(OUT_ICO, format="ICO",
                    sizes=[(s, s) for s in sizes],
                    append_images=frames[:-1])

    check = Image.open(OUT_ICO)
    have = sorted(check.info.get("sizes", []))
    print(f"wrote {OUT_ICO.name}  ({', '.join(str(w) for w, _ in have)})")
    print(f"wrote {OUT_PNG.name}")


if __name__ == "__main__":
    main()
