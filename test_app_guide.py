import unittest

import menu_screens as ms
from test_bot_ui import _button_datas, _button_rows


def _urls(rows):
    return [getattr(b, "url", None) or getattr(getattr(b, "type", None), "url", "") for r in _button_rows(rows) for b in r]


class AppGuideTests(unittest.TestCase):
    def test_every_page_fits_telegram_limit_and_is_navigable(self):
        for i in range(len(ms.GUIDE)):
            text, rows = ms.guide_screen(i)
            self.assertLess(len(text), 4096)
            datas = _button_datas(rows)
            self.assertIn("app", datas)       # назад до розділу «Додаток»
            if i < len(ms.GUIDE) - 1:
                self.assertIn(f"app:g:{i + 1}", datas)

    def test_download_link_present_even_without_map_url(self):
        _, rows = ms.guide_screen(0)
        urls = _urls(rows)
        self.assertIn(ms.APK_URL, urls)

    def test_out_of_range_page_is_clamped(self):
        self.assertEqual(ms.guide_screen(99)[0], ms.guide_screen(len(ms.GUIDE) - 1)[0])


if __name__ == "__main__":
    unittest.main()
