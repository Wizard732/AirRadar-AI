"""guard_places.py — іменовані місця сторожа (дім, робота, рідні): до MAX_PLACES на користувача.

Живе окремо від «поточної геопозиції» (guard_points): місця — це фіксовані точки з назвою; сторож перевіряє їх усі
і пише з назвою місця. Координати зберігаються лише для місць, які користувач сам додав, і стираються видаленням.
"""
from __future__ import annotations

MAX_PLACES = 5
DEFAULT_RADIUS = 10.0
LABELS = {"home": "🏠 Дім", "work": "💼 Робота", "family": "👪 Рідні"}


def ensure(db) -> None:
    with db._lock:  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001
            "CREATE TABLE IF NOT EXISTS guard_places (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, "
            "name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, radius_km REAL NOT NULL DEFAULT 10, "
            "UNIQUE(user_id, name))")
        db._conn.commit()  # noqa: SLF001


def list_places(db, uid: int) -> list[dict]:
    ensure(db)
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute("SELECT id, user_id, name, lat, lon, radius_km FROM guard_places WHERE user_id=? ORDER BY id", (uid,)).fetchall()  # noqa: SLF001
    return [dict(zip(("id", "user_id", "name", "lat", "lon", "radius_km"), r)) for r in rows]


def all_places(db) -> list[dict]:
    ensure(db)
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute("SELECT id, user_id, name, lat, lon, radius_km FROM guard_places").fetchall()  # noqa: SLF001
    return [dict(zip(("id", "user_id", "name", "lat", "lon", "radius_km"), r)) for r in rows]


def add_place(db, uid: int, name: str, lat: float, lon: float, radius_km: float = DEFAULT_RADIUS) -> tuple[bool, str]:
    """(успіх, повідомлення). Те саме ім'я оновлює точку; понад MAX_PLACES — відмова."""
    ensure(db)
    name = " ".join(name.split())[:30] or "Місце"
    existing = list_places(db, uid)
    if len(existing) >= MAX_PLACES and not any(p["name"] == name for p in existing):
        return False, f"Можна зберегти до {MAX_PLACES} місць. Видали одне в «📍 Мої місця»."
    with db._lock:  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001
            "INSERT INTO guard_places(user_id, name, lat, lon, radius_km) VALUES (?,?,?,?,?) "
            "ON CONFLICT(user_id, name) DO UPDATE SET lat=excluded.lat, lon=excluded.lon, radius_km=excluded.radius_km",
            (uid, name, float(lat), float(lon), float(radius_km)))
        db._conn.commit()  # noqa: SLF001
    return True, name


def remove_place(db, uid: int, pid: int) -> bool:
    ensure(db)
    with db._lock:  # noqa: SLF001
        cur = db._conn.execute("DELETE FROM guard_places WHERE user_id=? AND id=?", (uid, pid))  # noqa: SLF001
        db._conn.execute("DELETE FROM guard_state WHERE user_id=? AND track_key LIKE ?", (uid, f"%|p{pid}"))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    return cur.rowcount > 0


def set_radius(db, uid: int, radius_km: float) -> int:
    """Єдиний радіус для всіх місць користувача."""
    ensure(db)
    with db._lock:  # noqa: SLF001
        cur = db._conn.execute("UPDATE guard_places SET radius_km=? WHERE user_id=?", (float(radius_km), uid))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    return cur.rowcount


def _ensure_flags(db) -> None:
    with db._lock:  # noqa: SLF001
        db._conn.execute("CREATE TABLE IF NOT EXISTS guard_flags (user_id INTEGER PRIMARY KEY, persistent INTEGER NOT NULL DEFAULT 0)")  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001


def persistent_users(db) -> set[int]:
    """Користувачі з «наполегливим режимом»: небезпечне сповіщення повторюється (до 2 разів) і привертає увагу звуком."""
    _ensure_flags(db)
    with db._lock:  # noqa: SLF001
        return {r[0] for r in db._conn.execute("SELECT user_id FROM guard_flags WHERE persistent=1").fetchall()}  # noqa: SLF001


def toggle_persistent(db, uid: int) -> bool:
    _ensure_flags(db)
    new = uid not in persistent_users(db)
    with db._lock:  # noqa: SLF001
        db._conn.execute("INSERT INTO guard_flags(user_id, persistent) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET persistent=excluded.persistent", (uid, int(new)))  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    return new
