"""Create a branded ICO from the existing transparent Polski Bramy mark."""

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
logo = Image.open(ROOT / "pbradar" / "assets" / "polski-bramy-mark.png").convert("RGBA")
mark = logo.crop((45, 56, 190, 201))
output = ROOT / "dist" / "PB-Radar.ico"
output.parent.mkdir(exist_ok=True)
mark.save(output, format="ICO", sizes=[(16, 16), (24, 24), (32, 32),
                                        (48, 48), (64, 64), (128, 128)])
print(output)
