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

    def test_clean_signature_strips_decorated_channel_footer(self):
        # «✙[ Розвідка неба ](https://t.me/rozvidkaneba)✙» — подпись источника
        # не должна попадать в посты, карту и дедупликацию.
        cleaned = clean_signature("Шахед на Дарницький район ✙[ Розвідка неба ](https://t.me/rozvidkaneba)✙")
        self.assertEqual(cleaned, "Шахед на Дарницький район")

    def test_clean_signature_strips_bare_decorated_name(self):
        cleaned = clean_signature("БпЛА на Київ ✙ Розвідка неба ✙")
        self.assertEqual(cleaned, "БпЛА на Київ")

    def test_clean_signature_strips_tme_link(self):
        cleaned = clean_signature("Курс на Київ https://t.me/rozvidkaneba")
        self.assertNotIn("t.me", cleaned)
        self.assertIn("Курс на Київ", cleaned)

    def test_clean_signature_strips_truncated_footer_live_sample(self):
        # Живой кейс из прод-поста: подпись двумя строками, вторая — обрывок
        # декоративной ссылки «✙[» без имени канала (лимит длины срезал хвост).
        sample = (
            "Київщина:\nРеактивний БпЛА курсом на Ржищів\n"
            "3х Реактивні БпЛА курсом на Солоне\n"
            "✙ Розвідка неба ✙\n✙["
        )
        cleaned = clean_signature(sample)
        self.assertNotIn("озвідка", cleaned)
        self.assertNotIn("✙", cleaned)
        self.assertIn("Ржищів", cleaned)
        self.assertTrue(cleaned.endswith("Солоне"))

    def test_clean_signature_strips_bare_residue_tail(self):
        # Обрывок подписи в конце без имени канала: «✙✙», «✙[», «]».
        for tail in ("✙✙", "✙[", "✙ ]"):
            cleaned = clean_signature(f"БпЛА на Київ\n{tail}")
            self.assertEqual(cleaned, "БпЛА на Київ", tail)


if __name__ == "__main__":
    unittest.main()
