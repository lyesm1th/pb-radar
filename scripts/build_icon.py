"""Generate a multi-resolution Windows icon from the user's PB Radar mark."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pbradar.branding import red_mark_icon

mark = red_mark_icon(256)
output = ROOT / "dist" / "PB-Radar.ico"
output.parent.mkdir(exist_ok=True)
mark.save(output, format="ICO", sizes=[(16, 16), (24, 24), (32, 32),
                                        (48, 48), (64, 64), (128, 128), (256, 256)])
print(output)
