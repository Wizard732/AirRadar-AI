# -*- coding: utf-8 -*-
"""Гард ложных критичных угроз: одиночная «загроза балістики» не летит в чат.

Правило продукта: пост CRITICAL-класса (балістика, крилаті, КАБ…) от одного
источника не публикуется в канал — мониторинговые каналы часто пишут
«загроза балістики» как рутину/фейт. Пост пишется в БД (карта показывает),
а в канал уходит только после подтверждения вторым независимым источником
или официальным каналом. Возвращает прежнее поведение CRITICAL_NEEDS_CONFIRMATION=0.
"""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aggregator import AlertAggregator
from database import Database
from dedup import DedupCache

main = None


def _import_main():
    global main
    if main is None:
        import main as _main
        main = _main
    return main


class FakeSummarizer:
    async def summarize(self, text: str, *a, **kw) -> str:
        return text

    async def classify(self, text: str, prompt: str, max_tokens: int = 30) -> str:
        return ""


class FakePublisher:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text: str, *a, **kw):
        self.sent.append(text)
        return {"chat": {"id": "chat"}, "message_id": 1}

    async def edit(self, chat_id, message_id, text: str, *a, **kw):
        return True


def _event(text: str, ts: datetime, username: str) -> SimpleNamespace:
    return SimpleNamespace(
        message=SimpleNamespace(text=text, date=ts, photo=None),
        chat=SimpleNamespace(username=username),
    )


class CriticalConfirmPipelineTests(unittest.TestCase):
    """Одиночная баллистика — в БД/карту, но не в канал; подтверждённая — в канал."""

    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        _import_main()
        self.publisher = FakePublisher()

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def _aggregator(self) -> AlertAggregator:
        db, publisher = self.db, self.publisher

        async def flush(items):
            await main._publish_items(db, publisher, None, items)

        return AlertAggregator(60, flush, critical_needs_confirmation=True)

    def _process(self, text: str, ts: datetime, username: str, aggregator) -> None:
        dedup = DedupCache(ttl=60)
        asyncio.run(
            main._process_message(
                _event(text, ts, username), FakeSummarizer(), self.publisher,
                dedup, self.db, aggregator=aggregator,
            )
        )

    def test_single_source_ballistic_not_published(self):
        agg = self._aggregator()
        ts = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        self._process("Загроза балістики у Києві", ts, "channel_a", agg)

        # В канале тишина.
        self.assertEqual(self.publisher.sent, [], "одиночная баллистика не публикуется")
        # В журнал угроз и на карту — да (мапа видит, чат нет).
        rows = self.db._conn.execute(
            "SELECT threat_type, region FROM threats"
        ).fetchall()
        self.assertTrue(any(r["threat_type"] == "missile" and r["region"] == "kyivska"
                            for r in rows), "пост обязан попасть в журнал/карту")

    def test_second_source_confirms_and_publishes(self):
        agg = self._aggregator()
        base = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        self._process("Загроза балістики у Києві", base, "channel_a", agg)
        self._process("Балістика летить на Київ", base + timedelta(seconds=90),
                      "channel_b", agg)

        self.assertEqual(len(self.publisher.sent), 1, "подтверждённый инцидент публикуется")
        text = self.publisher.sent[0]
        self.assertIn("| Балістика", text)
        self.assertIn("підтверджено 2 незалежними джерелами", text)
        # Обрывок подписи/имя источника в пост не просачиваются.
        self.assertNotIn("channel_a", text)
        self.assertNotIn("channel_b", text)

    def test_gate_disabled_publishes_single_critical(self):
        """CRITICAL_NEEDS_CONFIRMATION=0 — прежний мгновенный байпас."""
        db, publisher = self.db, self.publisher

        async def flush(items):
            await main._publish_items(db, publisher, None, items)

        agg = AlertAggregator(60, flush)  # гард выключен (по умолчанию)
        ts = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        self._process("Загроза балістики у Києві", ts, "channel_a", agg)
        self.assertEqual(len(publisher.sent), 1, "без гарда — прежнее поведение")


if __name__ == "__main__":
    unittest.main()
