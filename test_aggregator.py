import asyncio
import unittest

from aggregator import AlertAggregator, PendingAlert
from incident_fusion import IncidentFact


def _fact(weapon: str = "uav", destination: str = "kyivska") -> IncidentFact:
    return IncidentFact(weapon, "imminent", "", destination, "unspecified", None, False, "")


def _alert(weapon: str = "uav", destination: str = "kyivska", source: str = "a",
           confirmation: dict | None = None) -> PendingAlert:
    return PendingAlert(
        text="БпЛА на Київ", source=source, event_ts=1_700_000_000,
        fact=_fact(weapon, destination),
        confirmation=confirmation or {"status": "reported", "sources": 1},
        regions=[destination],
    )


class FlushLogger:
    """Fake async callback-логгер: собирает наборы фlush-ей."""

    def __init__(self):
        self.flushes: list[list[PendingAlert]] = []

    async def __call__(self, items):
        self.flushes.append(list(items))


class AggregatorTests(unittest.TestCase):
    def test_two_sources_same_window_single_flush(self):
        log = FlushLogger()
        agg = AlertAggregator(0.05, log)

        async def run():
            await agg.submit(_alert(source="a"))
            await agg.submit(_alert(source="b"))  # тот же ключ, таймер не сбрасывается
            await asyncio.sleep(0.3)  # дождаться истечения окна

        asyncio.run(run())
        self.assertEqual(len(log.flushes), 1)
        self.assertEqual(len(log.flushes[0]), 2)
        sources = [item.source for item in log.flushes[0]]
        self.assertEqual(sources, ["a", "b"])

    def test_critical_bypass_without_waiting(self):
        log = FlushLogger()
        agg = AlertAggregator(60, log)  # окно огромное — flush только через байпас

        async def run():
            await agg.submit(_alert(weapon="ballistic"))
            await asyncio.sleep(0.05)

        asyncio.run(run())
        self.assertEqual(len(log.flushes), 1)
        self.assertEqual(log.flushes[0][0].fact.weapon_class, "ballistic")

    def test_stand_down_bypass_without_waiting(self):
        log = FlushLogger()
        agg = AlertAggregator(60, log)

        async def run():
            await agg.submit(_alert(weapon="stand_down"))
            await asyncio.sleep(0.05)

        asyncio.run(run())
        self.assertEqual(len(log.flushes), 1)
        self.assertEqual(log.flushes[0][0].fact.weapon_class, "stand_down")

    def test_material_update_bypass(self):
        log = FlushLogger()
        agg = AlertAggregator(60, log)

        async def run():
            await agg.submit(_alert(confirmation={"status": "reported", "material_update": True}))
            await asyncio.sleep(0.05)

        asyncio.run(run())
        self.assertEqual(len(log.flushes), 1)

    def test_aclose_flushes_pending_buffers(self):
        log = FlushLogger()
        agg = AlertAggregator(60, log)  # окно не истечёт — вылить должен aclose

        async def run():
            await agg.submit(_alert(source="a"))
            await agg.submit(_alert(destination="odeska", source="b"))
            await agg.aclose()
            await asyncio.sleep(0.05)

        asyncio.run(run())
        regions = sorted(items[0].fact.destination_region for items in log.flushes)
        self.assertEqual(regions, ["kyivska", "odeska"])
        self.assertEqual(len(agg._buffers), 0)


if __name__ == "__main__":
    unittest.main()
