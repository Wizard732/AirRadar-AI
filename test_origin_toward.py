"""Область «звідки» не регион тревоги; «курсом на X» даёт приблизительную цель на карте."""
import unittest

from threat_parser import parse_post
from track_engine import build_tracks


class OriginAndTowardTests(unittest.TestCase):
    def test_origin_oblast_is_not_alert_region(self):
        p = parse_post("**Харківщина:**\nРеактивний БпЛА курсом на Барвінкове з Донеччини", "x")
        self.assertEqual(p.oblasts, ["kharkivska"])
        self.assertEqual(p.origins, ["donetska"])

    def test_toward_becomes_approx_track(self):
        p = parse_post("Реактивний БпЛА курсом на Барвінкове з Донеччини\nХарківщина", "x")
        rows = [dict(o.__dict__, event_ts=1_000_000, as_of="", source="x", id=i) for i, o in enumerate(p.observations)]
        tracks = build_tracks(rows, 1_000_060, window_s=3600)
        self.assertEqual(len(tracks), 1)
        t = tracks[0]
        self.assertEqual(t.last.precision, "approx")
        self.assertEqual(t.speed_source, "typical")
        self.assertIsNotNone(t.heading)


if __name__ == "__main__":
    unittest.main()
