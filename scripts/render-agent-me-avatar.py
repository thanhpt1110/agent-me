#!/usr/bin/env python3
"""Render PNG workspace icons for the agent-me avatar.

The canonical source for README/dashboard is `assets/agent-me-avatar.svg`.
This renderer creates PNG files for places that do not accept SVG, such as
Slack workspace/app icons.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
STATIC = ROOT / "src" / "agent_me" / "dashboard" / "static"

def lerp(a: int, b: int, t: float) -> int:
    return int(a + (b - a) * t)


def draw_gradient_bg(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pix = img.load()
    for y in range(size):
        for x in range(size):
            nx = x / max(size - 1, 1)
            ny = y / max(size - 1, 1)
            t = (nx * 0.35 + ny * 0.65)
            r = lerp(17, 0, t)
            g = lerp(23, 0, t)
            b = lerp(17, 0, t)
            pix[x, y] = (r, g, b, 255)
    mask = Image.new("L", (size, size), 0)
    m = ImageDraw.Draw(mask)
    radius = int(size * 0.215)
    inset = int(size * 0.047)
    m.rounded_rectangle((inset, inset, size - inset, size - inset), radius=radius, fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.alpha_composite(img)
    out.putalpha(mask)
    return out


def sc(v: float, size: int) -> int:
    return round(v * size / 1024)


def draw_line(
    draw: ImageDraw.ImageDraw,
    points: list[tuple[int, int]],
    fill: tuple[int, int, int, int],
    width: int,
) -> None:
    draw.line(points, fill=fill, width=width, joint="curve")
    r = max(width // 2, 6)
    for x, y in points[1:-1]:
        draw.ellipse(
            (x - r, y - r, x + r, y + r),
            fill=(10, 10, 10, 255),
            outline=fill,
            width=max(width // 2, 4),
        )


def render(size: int = 1024) -> Image.Image:
    img = draw_gradient_bg(size)
    draw = ImageDraw.Draw(img)

    green = (118, 185, 0, 235)
    lime = (207, 255, 64, 220)
    dark = (5, 8, 5, 255)

    # Circuit traces.
    draw_line(
        draw,
        [
            (sc(128, size), sc(354, size)),
            (sc(276, size), sc(354, size)),
            (sc(352, size), sc(430, size)),
            (sc(470, size), sc(430, size)),
        ],
        green,
        sc(10, size),
    )
    draw_line(
        draw,
        [
            (sc(896, size), sc(354, size)),
            (sc(748, size), sc(354, size)),
            (sc(672, size), sc(430, size)),
            (sc(554, size), sc(430, size)),
        ],
        (118, 185, 0, 185),
        sc(10, size),
    )
    draw_line(
        draw,
        [
            (sc(164, size), sc(704, size)),
            (sc(314, size), sc(704, size)),
            (sc(380, size), sc(638, size)),
            (sc(496, size), sc(638, size)),
        ],
        (165, 222, 21, 150),
        sc(10, size),
    )
    draw_line(
        draw,
        [
            (sc(860, size), sc(704, size)),
            (sc(710, size), sc(704, size)),
            (sc(644, size), sc(638, size)),
            (sc(528, size), sc(638, size)),
        ],
        (165, 222, 21, 150),
        sc(10, size),
    )
    draw.line(
        [(sc(512, size), sc(162, size)), (sc(512, size), sc(268, size))],
        fill=(118, 185, 0, 195),
        width=sc(14, size),
    )
    draw.ellipse(
        (sc(480, size), sc(114, size), sc(544, size), sc(178, size)),
        fill=dark,
        outline=lime,
        width=sc(12, size),
    )

    # Outer rings.
    draw.rounded_rectangle(
        (sc(76, size), sc(76, size), sc(948, size), sc(948, size)),
        radius=sc(198, size),
        outline=green,
        width=sc(18, size),
    )
    draw.rounded_rectangle(
        (sc(114, size), sc(114, size), sc(910, size), sc(910, size)),
        radius=sc(172, size),
        outline=(207, 255, 64, 74),
        width=max(sc(2, size), 1),
    )

    # Text-free autonomous robot mark.
    robot = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rd = ImageDraw.Draw(robot)
    rd.ellipse(
        (sc(254, size), sc(416, size), sc(770, size), sc(828, size)),
        fill=(118, 185, 0, 58),
    )
    rd.rounded_rectangle(
        (sc(288, size), sc(274, size), sc(736, size), sc(692, size)),
        radius=sc(132, size),
        fill=dark,
        outline=green,
        width=sc(24, size),
    )
    rd.rounded_rectangle(
        (sc(338, size), sc(384, size), sc(686, size), sc(538, size)),
        radius=sc(62, size),
        fill=(7, 16, 7, 255),
        outline=(118, 185, 0, 235),
        width=sc(10, size),
    )
    rd.pieslice(
        (sc(388, size), sc(430, size), sc(636, size), sc(554, size)),
        190,
        350,
        fill=(118, 185, 0, 255),
    )
    rd.ellipse(
        (sc(428, size), sc(444, size), sc(476, size), sc(492, size)),
        fill=(234, 255, 186, 255),
    )
    rd.ellipse(
        (sc(548, size), sc(444, size), sc(596, size), sc(492, size)),
        fill=(234, 255, 186, 255),
    )
    rd.line(
        [(sc(444, size), sc(590, size)), (sc(580, size), sc(590, size))],
        fill=(118, 185, 0, 210),
        width=sc(16, size),
    )
    rd.line(
        [(sc(354, size), sc(328, size)), (sc(292, size), sc(270, size))],
        fill=green,
        width=sc(22, size),
    )
    rd.line(
        [(sc(670, size), sc(328, size)), (sc(732, size), sc(270, size))],
        fill=green,
        width=sc(22, size),
    )
    rd.ellipse(
        (sc(244, size), sc(222, size), sc(312, size), sc(290, size)),
        fill=dark,
        outline=lime,
        width=sc(12, size),
    )
    rd.ellipse(
        (sc(712, size), sc(222, size), sc(780, size), sc(290, size)),
        fill=dark,
        outline=lime,
        width=sc(12, size),
    )
    rd.polygon(
        [
            (sc(348, size), sc(678, size)),
            (sc(274, size), sc(782, size)),
            (sc(750, size), sc(782, size)),
            (sc(676, size), sc(678, size)),
        ],
        fill=dark,
        outline=green,
    )
    rd.line(
        [
            (sc(348, size), sc(678, size)),
            (sc(274, size), sc(782, size)),
            (sc(750, size), sc(782, size)),
            (sc(676, size), sc(678, size)),
        ],
        fill=green,
        width=sc(22, size),
        joint="curve",
    )
    rd.line(
        [(sc(374, size), sc(804, size)), (sc(650, size), sc(804, size))],
        fill=lime,
        width=sc(10, size),
    )
    # Four status chips encode the user's 1110 handle motif without visible text.
    for cx, lit in ((428, True), (486, True), (544, True), (602, False)):
        fill = lime if lit else dark
        outline = lime if lit else green
        rd.ellipse(
            (sc(cx - 18, size), sc(728, size), sc(cx + 18, size), sc(764, size)),
            fill=fill,
            outline=outline,
            width=sc(8, size),
        )

    glow = robot.filter(ImageFilter.GaussianBlur(sc(14, size)))
    glow_mask = Image.new("RGBA", (size, size), (118, 185, 0, 110))
    glow_mask.putalpha(glow.split()[-1])
    img.alpha_composite(glow_mask)
    img.alpha_composite(robot)

    # Bottom and top bracket accents.
    draw = ImageDraw.Draw(img)
    draw.line(
        [(sc(244, size), sc(262, size)), (sc(296, size), sc(210, size)),
         (sc(728, size), sc(210, size)), (sc(780, size), sc(262, size))],
        fill=lime,
        width=sc(18, size),
        joint="curve",
    )
    draw.line(
        [(sc(244, size), sc(842, size)), (sc(296, size), sc(894, size)),
         (sc(728, size), sc(894, size)), (sc(780, size), sc(842, size))],
        fill=(118, 185, 0, 160),
        width=sc(18, size),
        joint="curve",
    )
    return img


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    STATIC.mkdir(parents=True, exist_ok=True)
    img = render(1024)
    outputs = {
        ASSETS / "agent-me-avatar-1024.png": img,
        ASSETS / "agent-me-avatar-512.png": img.resize((512, 512), Image.Resampling.LANCZOS),
        STATIC / "agent-me-avatar-512.png": img.resize((512, 512), Image.Resampling.LANCZOS),
    }
    for path, image in outputs.items():
        image.save(path, "PNG", optimize=True)
        print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
