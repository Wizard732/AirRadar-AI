"""hardening.py — захист веб-шару, логів і файлів (без зміни бізнес-логіки).

* RedactFilter — прибирає з логів токени бота, ключі API, Bearer-заголовки та координати з query-рядка;
* harden_file_permissions — .env, Telethon-сесія, БД і бекапи доступні лише власнику (0600/0700);
* RateLimiter + security_middleware — ліміт запитів на IP, захист токена адмін-панелі від підбору,
  ліміт одночасних SSE-з'єднань, безпечні заголовки відповіді;
* client_ip — за замовчуванням адреса з'єднання; заголовок проксі (TRUST_PROXY_HEADER=CF-Connecting-IP тощо) — лише за явним
  налаштуванням, щоб його не можна було підробити напряму.
"""
from __future__ import annotations

import logging
import os
import re
import stat
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- логи
_REDACTIONS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"), "bot<redacted>"),                                   # токен Bot API в URL
    (re.compile(r"\b\d{8,}:AA[A-Za-z0-9_-]{20,}"), "<bot-token-redacted>"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "sk-<redacted>"),                                      # ключі OpenAI-сумісних API
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1<redacted>"),
    (re.compile(r"(?i)(x-admin-token['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]{8,}"), r"\1<redacted>"),
    (re.compile(r"(?i)(\b[\w-]*(?:api[_-]?key|token|secret|password|passwd)['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9._~+/=-]{12,}"), r"\1<redacted>"),
    (re.compile(r"(?i)([?&](?:lat|lon|lng|latitude|longitude)=)-?\d+(?:\.\d+)?"), r"\1<redacted>"),   # координати користувача
]


def redact(text: str) -> str:
    for rx, repl in _REDACTIONS:
        text = rx.sub(repl, text)
    return text


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            msg = record.getMessage()
            red = redact(msg)
            if red != msg:
                record.msg, record.args = red, ()
            if record.exc_info and record.exc_info[1] is not None and not record.exc_text:
                import traceback
                record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
            elif record.exc_text:
                record.exc_text = redact(record.exc_text)
        except (ValueError, TypeError, AttributeError, KeyError, IndexError):   # фільтр ніколи не ламає логування
            pass
        return True


def install_log_redaction() -> None:
    """Повісити фільтр на всі handler-и кореневого логера (і на сам логер)."""
    root = logging.getLogger()
    flt = RedactFilter()
    for h in root.handlers:
        if not any(isinstance(f, RedactFilter) for f in h.filters):
            h.addFilter(flt)
    if not any(isinstance(f, RedactFilter) for f in root.filters):
        root.addFilter(flt)


# ---------------------------------------------------------------- файли
def harden_file_permissions(paths: list[str], *, dir_mode: int = 0o700, file_mode: int = 0o600) -> list[str]:
    """Обмежити права до власника; повертає список виправлених шляхів. Відсутні шляхи ігноруються."""
    fixed = []
    for p in paths:
        try:
            if not p or not os.path.exists(p):
                continue
            want = dir_mode if os.path.isdir(p) else file_mode
            cur = stat.S_IMODE(os.stat(p).st_mode)
            if cur & ~want & 0o777:          # є зайві біти (група/інші)
                os.chmod(p, want)
                fixed.append(p)
        except OSError:
            logger.warning("не вдалося змінити права %s", p, exc_info=True)
    if fixed:
        logger.warning("Виправлено небезпечні права доступу (тепер лише власник): %s", ", ".join(os.path.basename(f) for f in fixed))
    return fixed


def sensitive_paths(db_path: str, session_name: str = "") -> list[str]:
    base = os.path.dirname(os.path.abspath(db_path)) or "."
    out = [os.path.join(base, ".env"), db_path, db_path + "-wal", db_path + "-shm", db_path + "-journal"]
    for stem in {session_name, session_name + "_bot"} - {""}:
        for suf in (".session", ".session-journal"):
            out.append(os.path.join(base, stem + suf))
    out += [os.path.join(base, "backups")]
    return out


# ---------------------------------------------------------------- ліміти
class RateLimiter:
    """Token bucket на ключ (IP). Пам'ять обмежена max_keys (старі ключі витісняються)."""

    def __init__(self, per_minute: float, burst: float | None = None, *, max_keys: int = 20000, clock=time.monotonic) -> None:
        self.rate = per_minute / 60.0
        self.burst = float(burst if burst is not None else max(1.0, per_minute / 4))
        self._b: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._max, self._clock = max_keys, clock

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = self._clock()
        tokens, ts = self._b.pop(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - ts) * self.rate)
        ok = tokens >= cost
        if ok:
            tokens -= cost
        self._b[key] = (tokens, now)
        while len(self._b) > self._max:
            self._b.popitem(last=False)
        return ok


class FailureBlocker:
    """Блокує IP після max_fail невдалих спроб за window_s (підбір токена адмін-панелі)."""

    def __init__(self, max_fail: int = 8, window_s: int = 300, block_s: int = 900, *, max_keys: int = 20000, clock=time.monotonic) -> None:
        self.max_fail, self.window, self.block = max_fail, window_s, block_s
        self._fails: OrderedDict[str, list[float]] = OrderedDict()
        self._until: dict[str, float] = {}
        self._max, self._clock = max_keys, clock

    def blocked(self, key: str) -> bool:
        until = self._until.get(key)
        if until is None:
            return False
        if self._clock() >= until:
            self._until.pop(key, None)
            return False
        return True

    def fail(self, key: str) -> None:
        now = self._clock()
        hist = [t for t in self._fails.pop(key, []) if now - t < self.window] + [now]
        self._fails[key] = hist
        if len(hist) >= self.max_fail:
            self._until[key] = now + self.block
            self._fails.pop(key, None)
        while len(self._fails) > self._max:
            self._fails.popitem(last=False)
        if len(self._until) > self._max:
            self._until.pop(next(iter(self._until)))


def client_ip(request) -> str:  # noqa: ANN001
    """Адреса клієнта. Заголовок проксі береться лише за TRUST_PROXY_HEADER (інакше його легко підробити)."""
    hdr = os.getenv("TRUST_PROXY_HEADER", "").strip()
    if hdr:
        v = request.headers.get(hdr, "")
        if v:
            return v.split(",")[0].strip()[:64]
    return (request.remote or "unknown")[:64]


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(self), camera=(), microphone=(), payment=(), usb=()",
    "Cross-Origin-Resource-Policy": "cross-origin",     # API/дані публічні й читаються з Pages/Mini App
}
# Mini App відкривається у вебвью Telegram і в iframe web.telegram.org; свій же сайт вбудовує map2 у app.html
FRAME_ANCESTORS = "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org https://t.me"


def make_security_middleware(api_limiter: RateLimiter | None = None, admin_blocker: FailureBlocker | None = None, sse_per_ip: int = 4):
    """aiohttp-middleware: ліміти + заголовки. Імпорт aiohttp всередині, щоб модуль лишався легким."""
    from aiohttp import web

    api_limiter = api_limiter or RateLimiter(per_minute=int(os.getenv("API_RATE_PER_MIN", "120")), burst=int(os.getenv("API_RATE_BURST", "40")))
    admin_blocker = admin_blocker or FailureBlocker()
    sse_now: dict[str, int] = {}

    def _headers(resp, request) -> None:
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        ct = resp.headers.get("Content-Type", "")
        if "html" in ct:
            resp.headers.setdefault("Content-Security-Policy", FRAME_ANCESTORS)
        if request.path.startswith("/admin/"):
            resp.headers["Cache-Control"] = "no-store"
        if request.secure or request.headers.get("X-Forwarded-Proto", "") == "https":
            resp.headers.setdefault("Strict-Transport-Security", "max-age=15552000")

    @web.middleware
    async def security(request, handler):  # noqa: ANN001
        ip, path = client_ip(request), request.path
        deny = None
        if path.startswith("/admin/"):
            if admin_blocker.blocked(ip):
                deny = web.Response(status=429, text="too many attempts", headers={"Retry-After": "900"})
        elif path == "/api/stream":
            if sse_now.get(ip, 0) >= sse_per_ip:
                deny = web.Response(status=429, text="too many streams", headers={"Retry-After": "30"})
        elif path.startswith("/api/") and not api_limiter.allow(ip):
            deny = web.json_response({"error": "rate limit"}, status=429, headers={"Retry-After": "30"})
        if deny is not None:
            _headers(deny, request)
            return deny
        counted = path == "/api/stream"
        if counted:
            sse_now[ip] = sse_now.get(ip, 0) + 1
        try:
            try:
                resp = await handler(request)
            except web.HTTPException as exc:
                resp = exc
            if path.startswith("/admin/") and resp.status in (401, 403):
                admin_blocker.fail(ip)
            _headers(resp, request)
            if isinstance(resp, web.HTTPException):
                raise resp
            return resp
        finally:
            if counted:
                sse_now[ip] = max(0, sse_now.get(ip, 1) - 1)
                if not sse_now[ip]:
                    sse_now.pop(ip, None)

    return security
