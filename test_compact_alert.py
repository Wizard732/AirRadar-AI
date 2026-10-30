import unittest

from alert_renderer import compact_roundup

ROUNDUP = """⚠️ → Рух шахедів:

• 2 на Сумщині — Боромля (1), Лебедин (1).

• 9 на Полтавщині — Гадяч (1), Хорол (1), Полтава (1), Кременчук(1), Нові Санжари (1), Карлівка (2), Кобеляки (1).

✅@raketa_svitlo"""


class CompactRoundupTests(unittest.TestCase):
    def test_one_line_per_oblast_with_totals(self):
        lines = compact_roundup(ROUNDUP).splitlines()
        self.assertEqual(lines[0], "Сумщина · 2: Боромля, Лебедин")
        self.assertTrue(lines[1].startswith("Полтавщина · 9: Гадяч, Хорол"))

    def test_long_lists_are_truncated_with_remainder(self):
        self.assertIn("+1", compact_roundup(ROUNDUP).splitlines()[1])

    def test_no_channel_signature_or_header_noise(self):
        out = compact_roundup(ROUNDUP)
        self.assertNotIn("raketa_svitlo", out)
        self.assertNotIn("Рух шахедів", out)

    def test_course_blocks_show_direction_and_via(self):
        out = compact_roundup("**Чернігівщина:**\n2х Реактивні БпЛА курсом на Славутич\n\n**Київщина:**\nРеактивний БпЛА курсом на Київ повз Вишгород")
        self.assertIn("2× → Славутич", out)
        self.assertIn("через Вишгород", out)

    def test_single_short_post_is_left_untouched(self):
        self.assertEqual(compact_roundup("3 БпЛА на Путивль"), "")
        self.assertEqual(compact_roundup("Загроза балістики"), "")


if __name__ == "__main__":
    unittest.main()
