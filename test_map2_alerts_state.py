"""Логіка офіційних тривог нової карти (miniapp/map2/alerts-state.js, data.js): ключі, антидребезг, стани, Київ/Севастополь."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).parent


@unittest.skipUnless(shutil.which("node"), "node не встановлено")
class Map2AlertStateTests(unittest.TestCase):
    def test_node_suite(self):
        r = subprocess.run(["node", "--test", str(ROOT / "miniapp" / "map2" / "test")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-1500:])

    def test_public_client_has_no_private_fields(self):
        """Клієнт карти не звертається до закритих ендпоінтів. Поля цілей (швидкість, курс, прогноз, ETA) дозволені лише
        в модулі цілей, шарі карти й app.js (рішення власника, етап 11); логіка тривог (data.js, alerts-state.js) від них вільна."""
        for name in ("layers.js", "data.js", "app.js", "alerts-state.js", "tracks.js"):
            src = (ROOT / "miniapp" / "map2" / name).read_text(encoding="utf-8")
            for bad in ("/admin/", "/api/threats", "/api/private"):
                self.assertNotIn(bad, src, f"{name}: {bad}")
        for name in ("data.js", "alerts-state.js"):
            src = (ROOT / "miniapp" / "map2" / name).read_text(encoding="utf-8")
            for bad in ("/api/tracks", "speed_kmh", "heading", "forecast", "eta_min"):
                self.assertNotIn(bad, src, f"{name}: {bad}")


if __name__ == "__main__":
    unittest.main()
