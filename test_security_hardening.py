# -*- coding: utf-8 -*-
"""Захист: редакція логів, права файлів, ліміти запитів, захист адмін-токена, безпечні заголовки."""
import asyncio
import logging
import os
import stat

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from airradar.security import hardening as h

FAKE_BOT = "123456789:AAE" + "x" * 30           # синтетичні значення, не справжні секрети
FAKE_KEY = "sk-" + "Q7rT" * 8


# ---- логи ----
def test_redact_removes_secrets_and_coordinates():
    cases = {
        f"POST https://api.telegram.org/bot{FAKE_BOT}/sendMessage failed": "x" * 30,
        f"key {FAKE_KEY}": "Q7rTQ7rT",
        "Authorization: Bearer abcdef0123456789abcdef": "abcdef0123456789",
        "X-Admin-Token: supersecrettokenvalue123": "supersecrettokenvalue123",
        "GET /api/risk?lat=50.4501&lon=30.5234&radius=10": "50.4501",
        "CVC_API_KEY=abcdef0123456789xyz": "abcdef0123456789xyz",
    }
    for text, secret in cases.items():
        out = h.redact(text)
        assert secret not in out, (text, out)
    assert "radius=10" in h.redact("GET /api/risk?lat=1.5&lon=2.5&radius=10"), "нерелевантні параметри лишаються"


def test_log_filter_scrubs_message_and_traceback():
    logger = logging.getLogger("sec_test"); logger.setLevel(logging.INFO); logger.propagate = False
    lines = []
    class H(logging.Handler):
        def emit(self, record): lines.append(self.format(record))
    hd = H(); hd.setFormatter(logging.Formatter("%(message)s")); hd.addFilter(h.RedactFilter()); logger.addHandler(hd)
    logger.info(f"url https://api.telegram.org/bot{FAKE_BOT}/getMe")
    try:
        raise RuntimeError(f"ClientError https://api.telegram.org/bot{FAKE_BOT}/sendMessage")
    except RuntimeError:
        logger.exception("boom")
    joined = "\n".join(lines)
    assert "x" * 30 not in joined and "bot<redacted>" in joined


# ---- файли ----
def test_harden_file_permissions(tmp_path):
    env = tmp_path / ".env"; env.write_text("X=1"); os.chmod(env, 0o644)
    bk = tmp_path / "backups"; bk.mkdir(); os.chmod(bk, 0o755)
    ok = tmp_path / "ok.db"; ok.write_text(""); os.chmod(ok, 0o600)
    fixed = h.harden_file_permissions([str(env), str(bk), str(ok), str(tmp_path / "missing")])
    assert set(fixed) == {str(env), str(bk)}
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600 and stat.S_IMODE(os.stat(bk).st_mode) == 0o700


# ---- ліміти ----
def test_rate_limiter_bucket_and_refill():
    t = [0.0]; rl = h.RateLimiter(per_minute=60, burst=3, clock=lambda: t[0])
    assert [rl.allow("a") for _ in range(4)] == [True, True, True, False]
    assert rl.allow("b"), "інший ключ незалежний"
    t[0] += 2; assert rl.allow("a") and rl.allow("a") and not rl.allow("a")     # 1 токен/с
    small = h.RateLimiter(per_minute=60, burst=1, max_keys=3, clock=lambda: t[0])
    for i in range(10): small.allow(str(i))
    assert len(small._b) <= 3, "пам'ять обмежена"


def test_failure_blocker_blocks_then_releases():
    t = [0.0]; fb = h.FailureBlocker(max_fail=3, window_s=60, block_s=100, clock=lambda: t[0])
    for _ in range(3): fb.fail("ip")
    assert fb.blocked("ip") and not fb.blocked("other")
    t[0] += 101; assert not fb.blocked("ip")


# ---- middleware на справжньому aiohttp ----
def _client_app(**mw):
    async def ok(request): return web.json_response({"ok": True})
    async def html(request): return web.Response(text="<html></html>", content_type="text/html")
    async def admin(request): return web.Response(status=401, text="unauthorized")
    async def sse(request):
        await asyncio.sleep(0.3); return web.Response(text="x")
    app = web.Application(middlewares=[h.make_security_middleware(**mw)])
    app.router.add_get("/api/x", ok); app.router.add_get("/", html); app.router.add_get("/admin/a", admin); app.router.add_get("/api/stream", sse)
    return app


def _run(coro):
    return asyncio.run(coro)


def test_security_headers_and_csp_only_for_html():
    async def go():
        async with TestClient(TestServer(_client_app())) as c:
            r = await c.get("/api/x"); j = dict(r.headers)
            r2 = await c.get("/"); hh = dict(r2.headers)
            r3 = await c.get("/nope")
            return j, hh, dict(r3.headers), r3.status
    j, hh, nf, st = _run(go())
    assert j["X-Content-Type-Options"] == "nosniff" and j["Referrer-Policy"] == "no-referrer" and "geolocation=(self)" in j["Permissions-Policy"]
    assert "Content-Security-Policy" not in j and "frame-ancestors" in hh["Content-Security-Policy"] and "telegram.org" in hh["Content-Security-Policy"]
    assert st == 404 and nf["X-Content-Type-Options"] == "nosniff", "заголовки є і на 404"


def test_api_rate_limit_returns_429_with_retry_after():
    async def go():
        rl = h.RateLimiter(per_minute=6, burst=2)
        async with TestClient(TestServer(_client_app(api_limiter=rl))) as c:
            codes = [(await c.get("/api/x")).status for _ in range(4)]
            r = await c.get("/api/x")
            return codes, r.status, r.headers.get("Retry-After")
    codes, last, retry = _run(go())
    assert codes[:2] == [200, 200] and 429 in codes and last == 429 and retry == "30"


def test_admin_bruteforce_is_blocked():
    async def go():
        fb = h.FailureBlocker(max_fail=3, window_s=60, block_s=900)
        async with TestClient(TestServer(_client_app(admin_blocker=fb))) as c:
            return [(await c.get("/admin/a")).status for _ in range(5)]
    assert _run(go()) == [401, 401, 401, 429, 429]


def test_sse_connections_per_ip_capped():
    async def go():
        async with TestClient(TestServer(_client_app(sse_per_ip=2))) as c:
            rs = await asyncio.gather(*[c.get("/api/stream") for _ in range(4)])
            return sorted(r.status for r in rs)
    assert _run(go()) == [200, 200, 429, 429]


def test_client_ip_only_trusts_configured_header(monkeypatch):
    class Req:
        remote = "10.0.0.5"; headers = {"CF-Connecting-IP": "203.0.113.9", "X-Forwarded-For": "1.2.3.4"}
    monkeypatch.delenv("TRUST_PROXY_HEADER", raising=False)
    assert h.client_ip(Req()) == "10.0.0.5", "без налаштування заголовки проксі ігноруються (їх можна підробити)"
    monkeypatch.setenv("TRUST_PROXY_HEADER", "CF-Connecting-IP")
    assert h.client_ip(Req()) == "203.0.113.9"


def test_clean_signature_has_no_quadratic_blowup():
    """Регресія: «стіна» з переносів/пропусків не повинна блокувати цикл подій (було ~0.5 с на 4000 символів)."""
    import time
    from fast_filter import clean_signature
    for text in ("\n" * 4000, " " * 4000 + "x", "\t" * 4000, "a\n" * 2000, " \n" * 2000, "Шахед " * 650):
        t0 = time.perf_counter(); clean_signature(text)
        assert time.perf_counter() - t0 < 0.15, repr(text[:10])
