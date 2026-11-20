"""Single source of truth for the Polski Bramy Windows icon."""

from pathlib import Path

from PIL import Image

ICON_SOURCE = Path(__file__).resolve().parent / "assets" / "pb-radar-icon.png"


def red_mark_icon(size: int = 256) -> Image.Image:
    """Keep only the red emblem from the supplied screenshot, with alpha.

    The source has a navy screenshot background, not real transparency.
    Reconstruct antialiased alpha from the original red foreground instead of
    making the background transparent by a crude exact-color replacement.
    """
    source = Image.open(ICON_SOURCE).convert("RGB")
    background = source.getpixel((0, 0))
    red = (202, 24, 25)
    mark = Image.new("RGBA", source.size)
    for y in range(source.height):
        for x in range(source.width):
            rgb = source.getpixel((x, y))
            if rgb[0] <= rgb[1] + 10 or rgb[0] <= rgb[2] + 5:
                continue
            opacity = round(255 * (rgb[0] - background[0]) / (red[0] - background[0]))
            if opacity > 0:
                mark.putpixel((x, y), (*red, min(255, opacity)))
    # Keep original proportions (94x92) and add transparent margins.
    side = max(mark.size)
    square = Image.new("RGBA", (side, side))
    square.paste(mark, ((side - mark.width) // 2, (side - mark.height) // 2))
    return square.resize((size, size), Image.Resampling.LANCZOS)
