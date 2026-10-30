import unittest

from publish_dedup import PublishDedup
from threat_parser import parse_post


class DedupTests(unittest.TestCase):
    def test_ballistic_pack_is_one_post_until_new_region(self):
        d = PublishDedup(); t = 1000.0
        self.assertTrue(d.allow("ballistic", None, ["kyivska"], t))
        for k in range(4):   # ещё 4 канала про то же
            self.assertFalse(d.allow("ballistic", None, ["kyivska"], t + 10 + k))
        self.assertFalse(d.allow("ballistic", None, ["unknown"], t + 20))
        self.assertTrue(d.allow("ballistic", None, ["odeska"], t + 30), "новая область — новый пост")
        self.assertTrue(d.allow("ballistic", None, ["kyivska"], t + 20 * 60), "через 10+ мин — снова можно")

    def test_uav_roundup_repeat_is_suppressed_but_new_places_pass(self):
        d = PublishDedup(); t = 5000.0
        a = parse_post("Реактивний БпЛА\nЧернігівщина: курсом на Куликівку\nПолтавщина: курсом на Хорол, Кобеляки", "x")
        b = parse_post("Реактивний БпЛА\nЧернігівщина: курсом на Куликівку\nПолтавщина: курсом на Хорол, Кобеляки", "y")
        c = parse_post("Реактивний БпЛА\nЧеркащина: курсом на Драбів\nПолтавщина: курсом на Глобине", "x")
        self.assertTrue(d.allow("uav", a, [], t))
        self.assertFalse(d.allow("uav", b, [], t + 480))
        self.assertTrue(d.allow("uav", c, [], t + 500))

    def test_generic_standdown_rate_limited(self):
        d = PublishDedup()
        self.assertTrue(d.allow("stand_down", None, [], 100.0))
        for k in range(1, 5):
            self.assertFalse(d.allow("stand_down", None, [], 100.0 + 60 * k))
        self.assertTrue(d.allow("stand_down", None, [], 100.0 + 21 * 60))


if __name__ == "__main__":
    unittest.main()
