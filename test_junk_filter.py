import unittest

from fast_filter import clean_signature, matches_keywords


class JunkFilterTests(unittest.TestCase):
    def test_gorilka_post_is_junk(self):
        self.assertFalse(matches_keywords("всім горілочки, хто з нами?"))

    def test_greeting_and_channel_promo_is_junk(self):
        self.assertFalse(matches_keywords("Добрий вечір, підтримайте канал донатом"))

    def test_ppo_report_with_thanks_is_not_junk(self):
        # «Дякуємо» в тексте не должно блокировать реальный отчёт ППО.
        self.assertTrue(matches_keywords("Дякуємо ППО, працює над Києвом"))

    def test_stand_down_with_thanks_is_not_junk(self):
        # Отбой публикуется всегда: благодарность его не блокирует.
        self.assertTrue(matches_keywords("Відбій! Дякуємо за спокій"))

    def test_clean_signature_strips_support_tails(self):
        cleaned = clean_signature("БпЛА на Київ, йдуть з півдня 👉 підтримайте проєкт реквізити в описі")
        self.assertTrue(cleaned.startswith("БпЛА на Київ"))
        self.assertNotIn("підтримайте", cleaned)

    def test_clean_signature_strips_donate_tail(self):
        cleaned = clean_signature("Вибух у Дарницькому районі. Задонатити: mono.link/xxx")
        self.assertNotIn("Задонатити", cleaned)
        self.assertIn("Вибух", cleaned)


if __name__ == "__main__":
    unittest.main()
