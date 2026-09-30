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


class CriticalConfirmGateTests(unittest.TestCase):
    """Гард ложных CRITICAL: одиночный пост не публикуется до подтверждения."""

    def test_single_source_critical_held_until_window_expiry(self):
        """Одиночная «загроза балістики» не байпасит и не публикуется вовсе."""
        log = FlushLogger()
        agg = AlertAggregator(0.05, log, critical_needs_confirmation=True)

        async def run():
            await agg.submit(_alert(weapon="ballistic", source="solo"))
            await asyncio.sleep(0.05)  # ещё в окне
            self.assertEqual(log.flushes, [], "байпас для одиночного CRITICAL закрыт")
            await asyncio.sleep(0.3)  # окно истекло

        asyncio.run(run())
        self.assertEqual(log.flushes, [], "окно без подтверждения — тишина")
        self.assertEqual(agg._buffers, {}, "буфер вылит (пост отброшен)")

    def test_corroborated_critical_bypasses(self):
        """2-й независимый источник → corroborated → мгновенный флаш набора."""
        log = FlushLogger()
        agg = AlertAggregator(60, log, critical_needs_confirmation=True)

        async def run():
            await agg.submit(_alert(weapon="ballistic", source="a",
                                    confirmation={"status": "reported", "sources": 1}))
            await agg.submit(_alert(weapon="ballistic", source="b",
                                    confirmation={"status": "corroborated", "sources": 2,
                                                  "material_update": True}))
            await asyncio.sleep(0.05)

        asyncio.run(run())
        self.assertEqual(len(log.flushes), 1)
        self.assertEqual(len(log.flushes[0]), 2, "оба поста инцидента уходят одним набором")

    def test_official_critical_bypasses(self):
        """Официальное подтверждение одиночки проходит сразу."""
        log = FlushLogger()
        agg = AlertAggregator(60, log, critical_needs_confirmation=True)

        async def run():
            await agg.submit(_alert(weapon="ballistic",
                                    confirmation={"status": "officially_confirmed"}))
            await asyncio.sleep(0.05)

        asyncio.run(run())
        self.assertEqual(len(log.flushes), 1)

    def test_non_critical_unaffected_by_gate(self):
        """БПЛА/отбой/обновления при гарде ведут себя как раньше."""
        log = FlushLogger()
        agg = AlertAggregator(60, log, critical_needs_confirmation=True)

        async def run():
            await agg.submit(_alert(weapon="uav"))       # HIGH — обычное окно
            await agg.submit(_alert(weapon="stand_down"))
            await asyncio.sleep(0.05)

        asyncio.run(run())
        self.assertEqual(len(log.flushes), 1)
        self.assertEqual(log.flushes[0][0].fact.weapon_class, "stand_down")

    def test_gate_disabled_keeps_old_bypass(self):
        """critical_needs_confirmation=False — прежнее мгновенное поведение."""
        log = FlushLogger()
        agg = AlertAggregator(60, log)  # дефолт без гарда

        async def run():
            await agg.submit(_alert(weapon="ballistic", source="solo"))
            await asyncio.sleep(0.05)

        asyncio.run(run())
        self.assertEqual(len(log.flushes), 1)

    def test_window_expiry_with_confirmed_sibling_publishes_all(self):
        """Одиночка + подтверждённый в одном окне → публикуется всё набором."""
        log = FlushLogger()
        agg = AlertAggregator(0.05, log, critical_needs_confirmation=True)

        async def run():
            await agg.submit(_alert(weapon="ballistic", source="a",
                                    confirmation={"status": "reported", "sources": 1}))
            # «b» — вторая группа того же инцидента, статус уже corroborated.
            await agg.submit(_alert(weapon="ballistic", source="b",
                                    confirmation={"status": "corroborated", "sources": 2}))
            await asyncio.sleep(0.3)  # окно истекает: в наборе есть подтверждение

        asyncio.run(run())
        self.assertEqual(len(log.flushes), 1)
        self.assertEqual(len(log.flushes[0]), 2)


if __name__ == "__main__":
    unittest.main()
