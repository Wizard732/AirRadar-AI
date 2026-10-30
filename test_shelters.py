import unittest

import shelters

FIX = [
    {"type": "node", "lat": 50.4510, "lon": 30.5234, "tags": {"amenity": "shelter", "name": "Укриття №5"}},
    {"type": "way", "center": {"lat": 50.4520, "lon": 30.5300}, "tags": {"building": "bunker"}},
    {"type": "node", "lat": 50.4510, "lon": 30.5234, "tags": {"name": "Укриття дубль"}},          # дубль координат
    {"type": "node", "lat": 50.4502, "lon": 30.5235, "tags": {"amenity": "shelter", "access": "private"}},
    {"type": "node", "lat": 50.4600, "lon": 30.5400, "tags": {"amenity": "shelter", "disused": "yes"}},
    {"type": "relation", "tags": {"amenity": "shelter"}},                                          # без координат
]


class ShelterTests(unittest.TestCase):
    def test_parse_filters_dedupes_and_sorts(self):
        res = shelters.parse(FIX, 50.4501, 30.5234)
        self.assertEqual([r["name"] for r in res], ["Укриття №5", ""])
        self.assertTrue(res[0]["m"] < res[1]["m"] < 1000)
        self.assertEqual(res[1]["kind"], "бункер")

    def test_format_and_links(self):
        txt = shelters.format_text(shelters.parse(FIX, 50.4501, 30.5234), "🏠 Дім")
        self.assertIn("Укриття №5", txt); self.assertIn("хв пішки", txt); self.assertIn("🏠 Дім", txt)
        self.assertIn("недоступний", shelters.format_text(None))
        self.assertIn("не знайдено", shelters.format_text([]))
        links = shelters.search_links(50.45, 30.52)
        self.assertIn("google.com/maps/search", links["google"])
        self.assertIn("destination=50.450000,30.520000", shelters.route_link(50.45, 30.52))


PHOTON_FIX = [
    {"geometry": {"coordinates": [30.5234, 50.4510]}, "properties": {"name": "Укриття", "osm_value": "shelter", "street": "Йорданська вулиця", "housenumber": "9"}},
    {"geometry": {"coordinates": [30.5234, 50.4510]}, "properties": {"name": "Укриття", "osm_value": "shelter"}},   # дубль
    {"geometry": {"coordinates": [30.5300, 50.4520]}, "properties": {"name": "Паркінг-сховище", "osm_value": "yes"}},
    {"geometry": {"coordinates": [30.5300, 50.4560]}, "properties": {"name": "Магазин", "osm_value": "shop"}},        # не укриття
    {"geometry": {"coordinates": [35.0, 48.0]}, "properties": {"name": "Укриття далеко", "osm_value": "shelter"}},     # >6 км
]


class PhotonTests(unittest.TestCase):
    def test_parse_photon(self):
        res = shelters.parse_photon(PHOTON_FIX, 50.4501, 30.5234)
        self.assertEqual([r["name"] for r in res], ["Укриття, Йорданська вулиця 9", "Паркінг-сховище"])
        self.assertTrue(res[0]["m"] < res[1]["m"])


class LocalTests(unittest.TestCase):
    def test_local_dataset_covers_kyiv_and_is_instant(self):
        import asyncio
        res = asyncio.run(shelters.nearest(50.4501, 30.5234))
        self.assertTrue(res and len(res) >= 3, "у центрі Києва має бути кілька укриттів")
        self.assertTrue(all(r["m"] <= shelters.LOCAL_MAX_M for r in res))
        self.assertEqual(res, sorted(res, key=lambda r: r["m"]))
        self.assertEqual(shelters.local_nearest(0.0, 0.0), [])


if __name__ == "__main__":
    unittest.main()
