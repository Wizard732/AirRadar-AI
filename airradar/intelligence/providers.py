"""providers.py — єдина обгортка над LLM-провайдерами: таймаут, retry, fallback, circuit breaker, денний бюджет.

Обгортає існуючі `AISummarizer` (Ollama) / `GroqSummarizer`: вони при збої повертають ОРИГІНАЛ, тому
«деградація» визначається як відповідь, що збігається з входом. Вихід LLM проходить `ai_guard.validate_output`.
LLM нічого не вирішує щодо прав/політики: результат — лише текст, який далі йде через safety_policy.
Статистика (виклики/збої/fallback/бюджет) доступна через `stats()` для дашборда.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from airradar.security import ai_guard

logger = logging.getLogger(__name__)


@dataclass
class _State:
    name: str
    daily_limit: int = 0                 # 0 = без ліміту (локальна модель)
    calls: int = 0
    failures: int = 0
    consecutive: int = 0
    open_until: float = 0.0
    day: int = 0
    latencies: list = field(default_factory=list)


class ResilientSummarizer:
    def __init__(self, primary, fallback=None, *, names=("primary", "fallback"), retries: int = 0, backoff_s: float = 0.5,
                 timeout_s: float = 45.0, primary_daily_limit: int = 0, fallback_daily_limit: int = 0,
                 breaker_threshold: int = 5, breaker_cooldown_s: float = 60.0, clock=time.time):
        self._providers = [(primary, _State(names[0], primary_daily_limit))]
        if fallback is not None:
            self._providers.append((fallback, _State(names[1], fallback_daily_limit)))
        self._retries, self._backoff, self._timeout = retries, backoff_s, timeout_s
        self._thr, self._cool, self._clock = breaker_threshold, breaker_cooldown_s, clock
        self.fallbacks_used = 0
        self.total_degraded = 0

    # --- бюджет / breaker ---
    def _roll_day(self, st: _State) -> None:
        day = int(self._clock() // 86400)
        if st.day != day:
            st.day, st.calls = day, 0

    def _available(self, st: _State) -> bool:
        self._roll_day(st)
        if st.daily_limit and st.calls >= st.daily_limit:
            return False                       # контроль вартості/квоти
        return self._clock() >= st.open_until

    def _failed(self, st: _State) -> None:
        st.failures += 1
        st.consecutive += 1
        if st.consecutive >= self._thr:
            st.open_until = self._clock() + self._cool
            st.consecutive = 0
            logger.warning("AI provider %s: breaker відкрито на %ds", st.name, int(self._cool))

    async def summarize(self, text: str) -> str:
        cleaned, flags = ai_guard.sanitize_input(text.strip())
        if not cleaned:
            return text
        if flags:
            logger.warning("AI guard: %s", ",".join(flags))
        for idx, (prov, st) in enumerate(self._providers):
            if not self._available(st):
                continue
            for attempt in range(self._retries + 1):
                st.calls += 1
                t0 = self._clock()
                try:
                    out = await asyncio.wait_for(prov.summarize(cleaned), self._timeout)
                except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
                    out = None
                    logger.warning("AI %s: %s", st.name, type(exc).__name__)   # без тексту запиту й ключів
                st.latencies = (st.latencies + [int((self._clock() - t0) * 1000)])[-200:]
                ok = out is not None and out.strip() != cleaned and ai_guard.validate_output(out)[0]
                if ok:
                    st.consecutive = 0
                    if idx:
                        self.fallbacks_used += 1
                    return out
                self._failed(st)
                if attempt < self._retries:
                    await asyncio.sleep(self._backoff * (2 ** attempt))
        self.total_degraded += 1
        return text                              # як і раніше: публікація не зупиняється, йде оригінал

    async def classify(self, text: str, system_prompt: str, max_tokens: int = 30) -> str:
        for prov, st in self._providers:
            if not self._available(st):
                continue
            st.calls += 1
            try:
                out = await asyncio.wait_for(prov.classify(text, system_prompt, max_tokens), self._timeout)
            except Exception:  # noqa: BLE001
                out = ""
            if out:
                return out
            self._failed(st)
        return ""

    async def healthcheck(self) -> bool:
        return await self._providers[0][0].healthcheck()

    async def aclose(self) -> None:
        for prov, _ in self._providers:
            await prov.aclose()

    def stats(self) -> dict:
        def pct(v, p):
            return sorted(v)[min(len(v) - 1, int(p * (len(v) - 1)))] if v else None
        return {"providers": [{"name": s.name, "calls_today": s.calls, "daily_limit": s.daily_limit or None, "failures": s.failures,
                               "breaker_open": self._clock() < s.open_until, "p50_ms": pct(s.latencies, .5), "p95_ms": pct(s.latencies, .95)}
                              for _, s in self._providers],
                "fallbacks_used": self.fallbacks_used, "degraded_to_original": self.total_degraded}
