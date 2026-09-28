import unittest

from publisher import build_linked_text


class BuildLinkedTextTests(unittest.TestCase):
    def test_link_wraps_brand_line(self):
        text = "🔴 Одеська обл. | БПЛА\n🕒 11:24\nтекст\n📡 Джерело: AirRadar AI"
        result = build_linked_text(text, "https://t.me/AirRadarAI")
        # Тело сохранено, бренд-строка обёрнута в ссылку.
        self.assertTrue(result.startswith("🔴 Одеська обл. | БПЛА\n"))
        self.assertIn('href="https://t.me/AirRadarAI"', result)
        self.assertIn("📡 Джерело: AirRadar AI</a>", result)

    def test_body_is_escaped(self):
        text = "пост с <b>тегом</b> и [скобкой]\n📡 Джерело: AirRadar AI"
        result = build_linked_text(text, "https://t.me/AirRadarAI")
        self.assertIn("&lt;b&gt;", result)
        self.assertNotIn("<b>", result)

    def test_no_promo_url_returns_none(self):
        text = "пост\n📡 Джерело: AirRadar AI"
        self.assertIsNone(build_linked_text(text, ""))
        self.assertIsNone(build_linked_text(text, "   "))

    def test_text_without_brand_line_returns_none(self):
        self.assertIsNone(build_linked_text("просто текст", "https://t.me/AirRadarAI"))

    def test_brand_only_text_returns_none(self):
        # rpartition не находит \n → plain fallback.
        self.assertIsNone(build_linked_text("📡 Джерело: AirRadar AI", "https://t.me/AirRadarAI"))


if __name__ == "__main__":
    unittest.main()
