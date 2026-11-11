"""Регресії парсера на реальних дописах каналів (Суми, Київ, Одеса): раніше всі ці рядки давали «Неклассифицированный пост»."""
import time

from threat_parser import parse_post


def _obs(text, source="x", ctx=None):
    return parse_post(text, source, ctx).observations


def test_kab_inflections_and_slash_places():
    o = _obs("Пуск КАБів у бік Юнаківка/Хотінь", "sumyliketop")
    assert [x.weapon for x in o] == ["kab", "kab"]
    assert {x.place for x in o} == {"Юнаківка", "Хотінь"} and all(x.oblast == "sumska" and x.precision == "place" for x in o)


def test_same_name_villages_prefer_channel_oblast():
    o = _obs("💣КАБи повз Писарівку курсом на Стецьківку, Радьківку.\nСуми район в укриття!", "sumyregion")
    assert len(o) == 3 and all(x.oblast == "sumska" and x.precision == "place" for x in o)


def test_oblast_level_warning_from_local_channel():
    o = _obs("⚠️Заходять на пуски КАБ по Сумському району⚠️", "sumyregion")
    assert len(o) == 1 and o[0].weapon == "kab" and o[0].oblast == "sumska" and o[0].precision == "oblast"
    assert _obs("⚠️Заходять на пуски КАБ по Сумському району⚠️", "somechannel") == []   # без регіонального контексту — не вигадуємо


def test_place_first_line():
    o = _obs("Шостка/район - уважно по шахедах ⚠️", "sumyliketop")
    assert len(o) == 1 and o[0].place == "Шостка" and o[0].weapon == "shahed" and o[0].oblast == "sumska"


def test_between_two_places_and_list_with_ta():
    o = _obs("+ Реактивний БпЛА маневрує між Козельцем та Калитою")
    assert any(x.place == "Калитою" and x.precision == "place" for x in o) and len(o) == 2
    o = _obs("Реактивні БпЛА з Чорного моря у напрямку Південного та Затоки.")
    assert any(x.place == "Затоки" and x.oblast == "odeska" for x in o)


def test_launches_without_weapon_keep_direction():
    o = _obs("Повторні пуски у бік Юнаківка/Хотінь", "sumyliketop")
    assert {x.place for x in o} == {"Юнаківка", "Хотінь"} and all(x.weapon == "unknown" for x in o)
    assert not any(x.place == "пуски" for x in _obs("Авіація на пуски у бік Сумського району", "sumyliketop"))


def test_aviation_activity_is_oblast_threat_for_local_channel():
    o = _obs("⚠️Активність СУ-34/35⚠️", "sumyregion")
    assert len(o) == 1 and o[0].weapon == "tac_aviation" and o[0].oblast == "sumska"


def test_bare_continuation_inherits_weapon_from_same_channel_only():
    ctx = {}
    parse_post("Реактивні БпЛА курсом на Київ", "kiev_nebo", ctx)
    o = _obs("На Димерку/Бровари", "kiev_nebo", ctx)
    assert [x.weapon for x in o] == ["uav", "uav"] and {x.place for x in o} == {"Димерку", "Бровари"}
    assert _obs("На Димерку/Бровари", "other_channel", ctx) == []
    ctx["kiev_nebo"] = (time.time() - 3600, "uav", "")          # застарілий контекст не діє
    assert _obs("На Димерку/Бровари", "kiev_nebo", ctx) == []


def test_no_false_positives_from_new_rules():
    for t in ("В кабинете мэра встреча", "Данія оголосила про новий пакет допомоги Україні на 643 мільйони",
              "Ланцет знищено", "Пуск застосунку оновлено, нова версія вже доступна"):
        assert _obs(t, "sumyliketop") == [], t
