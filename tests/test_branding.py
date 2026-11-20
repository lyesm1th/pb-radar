import unittest
from pathlib import Path

from PIL import Image
from pbradar.branding import red_mark_icon
from pbradar.gui import dark_theme_logo


class BrandingTests(unittest.TestCase):
    def test_windows_shortcut_uses_only_red_mark_on_transparent_background(self):
        icon_path = Path(__file__).resolve().parents[1] / "pbradar" / "assets" / "pb-radar-icon.png"
        with Image.open(icon_path) as icon:
            self.assertEqual(icon.size, (94, 92))
        mark = red_mark_icon(256)
        self.assertEqual(mark.mode, "RGBA")
        self.assertEqual(mark.size, (256, 256))
        self.assertEqual(mark.getpixel((0, 0))[3], 0)
        self.assertEqual(mark.getpixel((255, 255))[3], 0)
        red_pixels = sum(count for count, (r, g, b, a) in mark.getcolors(256 * 256)
                         if a > 200 and r > 150 and r > g * 2 and r > b * 2)
        self.assertGreater(red_pixels, 1200)

    def test_logo_is_transparent_and_readable_on_dark_header(self):
        logo = dark_theme_logo()
        self.assertEqual(logo.mode, "RGBA")
        self.assertEqual(logo.getpixel((0, 0))[3], 0)
        colors = logo.getcolors(maxcolors=logo.width * logo.height)
        red_emblem = sum(n for n, (r, g, b, a) in colors if a > 200 and r > g * 1.8 and r > b * 1.8)
        white_text = sum(n for n, (r, g, b, a) in colors if a > 200 and min(r, g, b) > 220)
        self.assertGreater(red_emblem, 1000)
        self.assertGreater(white_text, 1000)

    def test_both_supplied_brand_assets_are_packaged(self):
        assets = Path(__file__).resolve().parents[1] / "pbradar" / "assets"
        for name in ("polski-bramy-logo.png", "polski-bramy-mark.png"):
            with self.subTest(name=name), Image.open(assets / name) as img:
                self.assertEqual(img.size, (604, 258))
                self.assertEqual(img.mode, "RGBA")
                self.assertTrue(img.getchannel("A").getbbox())


if __name__ == "__main__":
    unittest.main()
