import asyncio
from ai_summarizer import NullSummarizer, make_summarizer


class _S:  # минимальные настройки: для off ничего не читается
    summary_cache_ttl = 0


def test_off_backend_makes_no_calls():
    s = make_summarizer("off", _S(), None)
    assert isinstance(s, NullSummarizer)
    assert asyncio.run(s.summarize("Шахеди на Київ")) == "Шахеди на Київ"
    assert asyncio.run(s.classify("x", "sys")) == ""
    assert asyncio.run(s.healthcheck()) is True
