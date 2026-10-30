"""Тесты threat_parser на реальных формулировках каналов (kiev_nebo, rozvidkaneba,
raketa_trevoga, nebo_raketa, sumyregion). Тексты — копии из боевой БД."""

import unittest

from threat_parser import locate, parse_post

ROUNDUP_SHAHED = """⚠️ → Рух шахедів:

• 2 на Сумщині — Боромля (1), Лебедин (1).

• 9 на Полтавщині — Гадяч (1), Хорол (1), Полтава (1), Кременчук(1).

• 8 на Дніпропетровщині — Лихівка (2), Царичанка (1), Синельникове (1).

✅@raketa_svitlo"""

ROUNDUP_UAV_TIME = """⚠️ → Рух БпЛА станом на 23:39:

• Черкащина (1) – Чигирин (1)

• Сумщина (5) – Миколаївка (2), Недригайлів (1), Ромни (2)

• Київщина (1) – Велика Димерка/Бровари (1)"""

REACTIVE_BLOCKS = """**Чернігівщина:**
2х Реактивні БпЛА курсом на Славутич

**Київщина:**
Реактивний БпЛА курсом на Київ повз Вишгород
Реактивний БпЛА курсом на Обухів

**Дніпропетровщина:**
БпЛА курсом на Дніпро"""


class RoundupTests(unittest.TestCase):
    def test_multi_oblast_roundup_keeps_every_oblast(self):
        post = parse_post(ROUNDUP_SHAHED)
        self.assertEqual(post.kind, "track")
        self.assertEqual(post.oblasts, ["sumska", "poltavska", "dnipropetrovska"])
        self.assertTrue(all(o.weapon == "shahed" for o in post.observations))

    def test_place_counts_and_group_counts(self):
        post = parse_post(ROUNDUP_SHAHED)
        lykhivka = next(o for o in post.observations if o.place == "Лихівка")
        self.assertEqual(lykhivka.count, 2)
        self.assertEqual(lykhivka.group_count, 8)
        self.assertEqual(lykhivka.oblast, "dnipropetrovska")

    def test_known_cities_get_point_coordinates(self):
        post = parse_post(ROUNDUP_SHAHED)
        poltava = next(o for o in post.observations if o.place == "Полтава")
        self.assertEqual(poltava.precision, "place")
        self.assertAlmostEqual(poltava.lat, 49.59, delta=0.2)

    def test_unknown_village_never_gets_invented_coordinates(self):
        post = parse_post("⚠️ → Рух шахедів:\n\n• 1 на Сумщині — Зачепилівка (1).")
        obs = post.observations[0]
        self.assertEqual(obs.place, "Зачепилівка")
        self.assertNotEqual(obs.precision, "place")

    def test_time_and_oblast_headed_bullets(self):
        post = parse_post(ROUNDUP_UAV_TIME)
        self.assertEqual(post.as_of, "23:39")
        self.assertEqual(post.oblasts, ["cherkaska", "sumska", "kyivska"])
        self.assertEqual([o.place for o in post.observations if o.oblast == "sumska"],
                         ["Миколаївка", "Недригайлів", "Ромни"])

    def test_reactive_profile_and_course(self):
        post = parse_post(REACTIVE_BLOCKS)
        self.assertEqual(post.kind, "track")
        by_place = {o.place: o for o in post.observations}
        self.assertEqual(by_place["Славутич"].profile, "reactive")
        self.assertEqual(by_place["Славутич"].count, 2)
        self.assertEqual(by_place["Славутич"].oblast, "chernigivska")
        self.assertEqual(by_place["Вишгород"].relation, "via")
        self.assertEqual(by_place["Обухів"].oblast, "kyivska")
        self.assertEqual(by_place["Дніпро"].profile, "")


class ShortFormTests(unittest.TestCase):
    def test_city_comma_weapon_to_you(self):
        post = parse_post("Чернігів, шахед на місто!")
        self.assertEqual(post.kind, "track")
        obs = post.observations[0]
        self.assertEqual((obs.weapon, obs.place, obs.relation), ("shahed", "Чернігів", "toward"))

    def test_count_word(self):
        post = parse_post("Одеса, дві ракети до вас!")
        self.assertEqual(post.observations[0].count, 2)

    def test_landmark_with_city_hint(self):
        post = parse_post("• ТЕЦ-5 (Київ), бандеролі у вашому напрямку.")
        obs = post.observations[0]
        self.assertEqual(obs.place, "ТЕЦ-5")
        self.assertEqual(obs.precision, "place")
        self.assertAlmostEqual(obs.lat, 50.45, delta=0.2)

    def test_course_to_with_via(self):
        post = parse_post("🛵 1 БпЛА курсом через Гвоздів/Ходосівку (звичайний шахад).")
        self.assertEqual([o.place for o in post.observations], ["Гвоздів", "Ходосівку"])
        self.assertTrue(all(o.relation == "via" for o in post.observations))

    def test_oblast_only_warning_becomes_oblast_observation(self):
        post = parse_post("💥 Сумщина під балістичним обстрілом.")
        self.assertEqual(post.observations[0].oblast, "sumska")
        self.assertEqual(post.observations[0].place, "")

    def test_source_designation_x59(self):
        post = parse_post("🚀 Х-59 на Одесу!")
        self.assertEqual(post.kind, "track")
        self.assertEqual(post.observations[0].oblast, "odeska")


class GeocodeTests(unittest.TestCase):
    def test_short_stem_does_not_match_other_town(self):
        # «буч» — основа Бучі; Бучач (Тернопільщина) — другое место.
        geo = locate("Бучач", "kyivska")
        self.assertTrue(geo is None or abs(geo.lon - 30.2) > 1, "Бучач не должен превращаться в Бучу")
        self.assertAlmostEqual(locate("Бучач", "ternopilska").lon, 25.39, delta=0.1)

    def test_village_accusative_form_resolves_via_gazetteer(self):
        geo = locate("Дубовʼязівку", "sumska")
        self.assertIsNotNone(geo)
        self.assertEqual(geo.oblast, "sumska")
        self.assertAlmostEqual(geo.lat, 51.14, delta=0.1)

    def test_compound_name_in_accusative(self):
        geo = locate("Білу Церкву", "kyivska")
        self.assertAlmostEqual(geo.lat, 49.8, delta=0.1)

    def test_city_filed_under_neighbouring_oblast_still_resolves(self):
        geo = locate("Славутич", "chernigivska")
        self.assertIsNotNone(geo)
        self.assertEqual(geo.precision, "place")


class NoiseTests(unittest.TestCase):
    def test_reported_speech_news_is_not_a_threat(self):
        post = parse_post("⚡️Росія запланувала виготовити 30 тисяч «Шахедів», — ГУР\n\nРосія запланувала у 2025 році виробити, повідомив заступник голови розвідки.")
        self.assertEqual(post.kind, "news")
        self.assertFalse(post.mappable)

    def test_attack_on_russian_region_is_foreign(self):
        post = parse_post("🔥Московську область масовано атакували дрони: під ударом ЛВДС \"Володарська\"")
        self.assertEqual(post.kind, "foreign")
        self.assertFalse(post.mappable)

    def test_launch_warning_from_russian_city_stays_an_alert(self):
        post = parse_post("Загроза балістики з Брянська і Курська")
        self.assertEqual(post.kind, "alert")
        self.assertFalse(post.mappable)

    def test_shootdown_statistics(self):
        self.assertEqual(parse_post("💥Вночі/Вранці силами ППО ЗБИТО/ПОДАВЛЕНО:\n\n117 із 160 БпЛА різних типів.").kind, "stats")
        self.assertEqual(parse_post("⚜️Силами ППО збито/подавлено 710/753 БпЛА").kind, "stats")

    def test_donation_post(self):
        self.assertEqual(parse_post("Допоможіть хлопцям з ОМБр! Моно Банка https://send.monobank.ua/jar/x шахед").kind, "promo")


class AllClearTests(unittest.TestCase):
    def test_genitive_otboy_is_all_clear(self):
        self.assertEqual(parse_post("✅ Початок відбою тривог — балістична небезпека минула.").kind, "all_clear")
        self.assertEqual(parse_post("🟢 Сигнал по балістиці скасовано.").kind, "all_clear")

    def test_until_the_all_clear_is_not_all_clear(self):
        post = parse_post("Пуски не зафіксовано! До відбою знаходимось у безпечних місцях!")
        self.assertNotEqual(post.kind, "all_clear")

    def test_plain_siren_is_alert(self):
        self.assertEqual(parse_post("Тривога в Києві").kind, "alert")


class NewsNotAlertTests(unittest.TestCase):
    def test_celebrity_shelter_story_is_news_not_fpv(self):
        text = ("Зірка «Гри престолів» Ніколай Костер-Вальдау відвідав Київ та спустився в укриття під час повітряної тривоги. "
                "Через загрозу російських ударів йому довелося спускатися в укриття.")
        self.assertEqual(parse_post(text, "x").kind, "news")

    def test_short_keywords_do_not_match_inside_words(self):
        from weapon_classes import classify_weapon
        self.assertNotEqual(classify_weapon("Йому довелося спускатися в укриття"), "fpv")
        self.assertEqual(classify_weapon("Скат на Київ"), "fpv")


class AirForcePostFormatTests(unittest.TestCase):
    def test_city_with_prefix_is_resolved(self):
        p = parse_post('Баражуючий боєприпас "Бандероль" на м. Запоріжжя з півночі.')
        self.assertEqual(p.kind, "track")
        self.assertTrue(any(o.place == "Запоріжжя" and o.precision == "place" for o in p.observations))

    def test_course_suffix_not_in_place(self):
        p = parse_post('Баражуючий боєприпас "Бандероль" на Харківщині в районі Шарівки курс південний.')
        self.assertFalse(any("курс" in o.place for o in p.observations))

    def test_guided_bomb_launches_are_kab(self):
        p = parse_post("Пуски керованих авіаційних бомб ворожою тактичною авіацією на Дніпропетровщину.")
        self.assertEqual(p.kind, "track")
        self.assertTrue(any(o.weapon == "kab" for o in p.observations))


if __name__ == "__main__":
    unittest.main()
