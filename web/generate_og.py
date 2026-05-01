"""Generate Tideline OG image (1200x630 PNG).

Static image — generate once, commit, link from OG/Twitter meta tags.
If the visual ever needs an update, re-run this and re-commit.

Output: web/og.png

Run: python web/generate_og.py
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# Brand palette — sandbox warmth on ocean depth
OCEAN_DEEP    = (10,  26,  47)   # #0A1A2F
OCEAN_DARK    = (6,   16,  31)   # #06101F
OCEAN_CARD    = (20,  40,  63)   # #14283F
OCEAN_BORDER  = (42,  64,  96)   # #2A4060
FOAM          = (240, 230, 210)  # #F0E6D2  warm cream
MUTED         = (168, 155, 130)  # #A89B82  muted sand
CYAN_BRIGHT   = (232, 221, 200)  # #E8DDC8  light sand (was cyan)
TEAL          = (184, 149, 106)  # #B8956A  wood
SAND          = (212, 185, 140)  # #D4B98C  mid-sand accent
REEF          = (123, 163, 122)  # #7BA37A  soft sage

W, H = 1200, 630


def vertical_gradient(top, bottom, w, h):
    img = Image.new("RGB", (w, h), top)
    px = img.load()
    for y in range(h):
        f = y / max(h - 1, 1)
        r = int(top[0] + (bottom[0] - top[0]) * f)
        g = int(top[1] + (bottom[1] - top[1]) * f)
        b = int(top[2] + (bottom[2] - top[2]) * f)
        for x in range(w):
            px[x, y] = (r, g, b)
    return img


def add_radial_glow(img, center, color, radius, alpha=0.18):
    """Soft radial glow blob — for the visual interest you see in the live page."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    cx, cy = center
    # Multiple concentric circles for soft falloff
    for i in range(40, 0, -1):
        r = int(radius * i / 40)
        a = int(255 * alpha * (1 - i / 40) ** 2)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*color, a))
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius=18))
    img.paste(overlay, (0, 0), overlay)
    return img


def draw_wave(img, y_center, amplitude, color, width, alpha):
    """Draw a stylized wave path matching the header logo."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    points = []
    for x in range(0, W + 1, 4):
        t = x / W * 4 * math.pi
        y = y_center + amplitude * math.sin(t)
        points.append((x, y))
    for i in range(len(points) - 1):
        draw.line([points[i], points[i+1]], fill=(*color, alpha), width=width)
    img.paste(overlay, (0, 0), overlay)
    return img


def get_font(size, bold=False):
    """Try common Windows + cross-platform fonts; fall back to default."""
    candidates_bold = [
        "C:\\Windows\\Fonts\\segoeuib.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/SFNS.ttf",
    ]
    candidates_reg = [
        "C:\\Windows\\Fonts\\segoeui.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/SFNS.ttf",
    ]
    for path in (candidates_bold if bold else candidates_reg):
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def measure(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def main():
    # Background gradient
    img = vertical_gradient(OCEAN_DEEP, OCEAN_DARK, W, H)

    # Atmospheric glows
    img = add_radial_glow(img, (180, 160), CYAN_BRIGHT, 380, alpha=0.20)
    img = add_radial_glow(img, (1020, 470), TEAL,       420, alpha=0.22)

    # Decorative waves bottom area
    img = draw_wave(img, 530, 14, CYAN_BRIGHT, width=3, alpha=180)
    img = draw_wave(img, 565, 10, TEAL,        width=2, alpha=130)
    img = draw_wave(img, 595,  7, OCEAN_BORDER, width=2, alpha=180)

    draw = ImageDraw.Draw(img)

    # Hand-tuned vertical positions (Pillow textbbox understates real height for some fonts)
    Y_TITLE = 170
    Y_TAGLINE = 360
    Y_MICRO = 430
    Y_DOMAIN = H - 60

    # Wordmark
    title_font = get_font(140, bold=True)
    title = "Tideline"
    tw, _ = measure(draw, title, title_font)
    title_x = (W - tw) // 2
    # Shadow
    draw.text((title_x + 2, Y_TITLE + 4), title, font=title_font, fill=(0, 0, 0, 200))
    draw.text((title_x, Y_TITLE), title, font=title_font, fill=FOAM)

    # Tagline 1 — primary
    sub_font = get_font(40, bold=False)
    sub = "Macro Regime Tracker"
    sw, _ = measure(draw, sub, sub_font)
    draw.text(((W - sw) // 2, Y_TAGLINE), sub, font=sub_font, fill=CYAN_BRIGHT)

    # Tagline 2 — descriptive value prop
    micro_font = get_font(24, bold=False)
    micro = "One trend signal  ·  One descriptive panel  ·  Full audit trail"
    mw, _ = measure(draw, micro, micro_font)
    draw.text(((W - mw) // 2, Y_MICRO), micro, font=micro_font, fill=MUTED)

    # Footer accent (domain placeholder — user edits before deploy)
    accent_font = get_font(20, bold=False)
    accent = "tideline.live"
    aw, _ = measure(draw, accent, accent_font)
    draw.text(((W - aw) // 2, Y_DOMAIN), accent, font=accent_font, fill=MUTED)

    # Save
    out_path = Path(__file__).parent / "og.png"
    img.save(out_path, "PNG", optimize=True)
    size_kb = out_path.stat().st_size // 1024
    print(f"Wrote {out_path} ({size_kb} KB, {W}x{H})")


if __name__ == "__main__":
    main()
