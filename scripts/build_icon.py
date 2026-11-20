"""Generate a multi-resolution Windows icon from the user's PB Radar mark."""

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
logo = Image.open(ROOT / "pbradar" / "assets" / "pb-radar-icon.png").convert("RGB")
# Keep the requested dark background, spacing and thin red lines. The supplied
# artwork is 94x92, so pad it rather than stretching its proportions.
side = max(logo.size)
mark = Image.new("RGB", (side, side), logo.getpixel((0, 0)))
mark.paste(logo, ((side - logo.width) // 2, (side - logo.height) // 2))
mark = mark.resize((256, 256), Image.Resampling.LANCZOS)
output = ROOT / "dist" / "PB-Radar.ico"
output.parent.mkdir(exist_ok=True)
mark.save(output, format="ICO", sizes=[(16, 16), (24, 24), (32, 32),
                                        (48, 48), (64, 64), (128, 128), (256, 256)])
print(output)
