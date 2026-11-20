import unittest
from pathlib import Path

from PIL import Image


class BrandingTests(unittest.TestCase):
    def test_both_supplied_brand_assets_are_packaged(self):
        assets = Path(__file__).resolve().parents[1] / "pbradar" / "assets"
        for name in ("polski-bramy-logo.png", "polski-bramy-mark.png"):
            with self.subTest(name=name), Image.open(assets / name) as img:
                self.assertEqual(img.size, (604, 258))
                self.assertEqual(img.mode, "RGBA")
                self.assertTrue(img.getchannel("A").getbbox())


if __name__ == "__main__":
    unittest.main()
