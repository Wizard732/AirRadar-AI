"""Трекер user-каналов Interests (вынесен из main.run())."""
import asyncio
from types import SimpleNamespace

from airradar.ingestion.interests_channels import InterestsChannelTracker, bare_chat_id, channel_key


class FakeDB:
    def __init__(self, chans): self.chans = chans
    def all_interests_channels(self): return list(self.chans)


class FakeClient:
    def __init__(self, ents): self.ents = ents; self.walks = 0
    async def iter_dialogs(self):
        self.walks += 1
        for e in self.ents:
            yield SimpleNamespace(entity=e)


def test_channel_key_forms():
    assert channel_key("@Name") == "name"
    assert channel_key("-1001234") == "1234"
    assert channel_key("-55") == "55"
    assert channel_key("123") == "123"
    assert channel_key(None) == ""


def test_bare_chat_id():
    assert bare_chat_id(-1001234) == 1234
    assert bare_chat_id(-55) == 55
    assert bare_chat_id(77) == 77


def test_resolve_by_username_and_id_then_no_rewalk():
    ents = [SimpleNamespace(username="News", id=10), SimpleNamespace(username=None, id=20), SimpleNamespace(username="x", id=30)]
    cl = FakeClient(ents); tr = InterestsChannelTracker(cl, FakeDB(["@news", "-10020", "ghost"]))
    asyncio.run(tr.resolve_new())
    assert tr.ids == {10, 20}
    assert tr.is_tracked(-10010) and tr.is_tracked(20) and not tr.is_tracked(30) and not tr.is_tracked(None)
    assert tr.resolved_keys == {"news", "20"}
    # «ghost» ещё не найден — следующий обход снова его ищет; найденные не пересматриваются
    asyncio.run(tr.resolve_new()); assert cl.walks == 2


def test_nothing_missing_skips_walk():
    cl = FakeClient([]); tr = InterestsChannelTracker(cl, FakeDB([]))
    asyncio.run(tr.resolve_new()); assert cl.walks == 0


def test_refresh_loop_survives_errors_and_propagates_cancel():
    class Boom(InterestsChannelTracker):
        calls = 0
        async def resolve_new(self):
            Boom.calls += 1
            if Boom.calls == 1: raise RuntimeError("x")
    async def go():
        t = asyncio.create_task(Boom(None, None).refresh_loop(interval=0))
        await asyncio.sleep(0.05); t.cancel()
        try: await t
        except asyncio.CancelledError: return True
        return False
    assert asyncio.run(go()) and Boom.calls >= 2
