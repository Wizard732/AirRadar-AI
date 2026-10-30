"""threat_parser.py — структурный разбор постов мониторинговых каналов.

Зачем: старый конвейер сворачивал пост в ОДИН регион и ОДИН тип. Сводка
«Рух шахедів: • 4 на Сумщині — Суми, Низи • 9 на Полтавщині — Гадяч …»
превращалась в случайную область, а новости («нардеп заявив…», «збито 117 із
160») и атаки по России попадали на карту как живые угрозы.

Здесь пост раскладывается на наблюдения: (тип, количество, область, населённый
пункт, отношение к нему — над/курсом на/через/откуда, координаты). Из них
карта строит треки: вектор, скорость и направление считаются по смене
положения между сводками (track_speed.py), а не выдумываются.

Модуль детерминированный и чистый (без БД/сети): тестируется на реальных
постах из tests_data/. Неопределённость остаётся пустой: нет координат — нет
точки, нет числа — count=None.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from city_coords import CITIES, REGION_CENTROIDS
import gazetteer
from weapon_classes import classify_weapon

# --- области --------------------------------------------------------------

# Основы названий областей (нижний регистр, все падежи UA/RU через общий корень).
OBLAST_STEMS: dict[str, tuple[str, ...]] = {
    "kyivska": ("київщин", "киевщин", "київськ обл", "киевск обл", "київська область"),
    "sumska": ("сумщин", "сумськ обл", "сумск обл", "сумська область"),
    "chernigivska": ("чернігівщин", "черниговщин", "чернігівськ обл", "чернігівська область"),
    "poltavska": ("полтавщин", "полтавськ обл", "полтавська область"),
    "kharkivska": ("харківщин", "харьковщин", "харківськ обл", "харківська область"),
    "dnipropetrovska": ("дніпропетровщин", "днепропетровщин", "дніпровщин", "дніпропетровськ обл"),
    "zaporizka": ("запоріжж", "запорожь", "запорізьк обл", "запорізька область"),
    "khersonska": ("херсонщин", "херсонськ обл", "херсонська область"),
    "mykolaivska": ("миколаївщин", "николаевщин", "миколаївськ обл", "миколаївська область"),
    "odeska": ("одещин", "одешин", "одещ", "одесщин", "одеськ обл", "одеська область"),
    "kirovohradska": ("кіровоградщин", "кировоградщин", "кіровоградськ обл", "кіровоградська область"),
    "cherkaska": ("черкащин", "черкасщин", "черкаськ обл", "черкаська область"),
    "vinnytska": ("вінниччин", "винничин", "вінницьк обл", "вінницька область"),
    "zhytomyrska": ("житомирщин", "житомирськ обл", "житомирська область"),
    "rivnenska": ("рівненщин", "ровенщин", "рівненськ обл", "рівненська область"),
    "volynska": ("волин", "волынь", "волинськ обл", "волинська область"),
    "khmelnytska": ("хмельниччин", "хмельнитчин", "хмельницьк обл", "хмельницька область"),
    "ternopilska": ("тернопільщин", "тернопольщин", "тернопільськ обл", "тернопільська область"),
    "lvivska": ("львівщин", "львовщин", "львівськ обл", "львівська область"),
    "zakarpatska": ("закарпатт", "закарпаття", "закарпатськ обл"),
    "ivano_frankivska": ("прикарпатт", "івано-франківщин", "франківщин", "івано-франківськ обл"),
    "chernivetska": ("буковин", "чернівеччин", "чернівецьк обл"),
    "donetska": ("донеччин", "донетчин", "донецьк обл", "донецька область"),
    "luhanska": ("луганщин", "луганськ обл", "луганська область"),
    "crimea": ("крим",),
}

# Чужая территория: пост о них — не угроза нашим областям.
FOREIGN_STEMS: tuple[str, ...] = (
    "московськ", "москов", "мо́сков", "підмосков", "бєлгородськ", "белгородськ", "бєлгородщин",
    "курськ", "курщин", "брянськ", "брянщин", "воронезьк", "ростовськ", "краснодарськ",
    "волгоградськ", "ленінградськ", "татарстан", "саратовськ", "самарськ", "липецьк",
    "орловськ", "тамбовськ", "смоленськ", "нижегородськ", "рязанськ", "тульськ", "калузьк",
    "бєларус", "білорус", "беларус", "території рф", "території росії", "в росії", "у росії",
)

_OBLAST_RE: list[tuple[re.Pattern[str], str]] = []
for _slug, _stems in OBLAST_STEMS.items():
    for _stem in _stems:
        _m = re.match(r"^(\S+) (?:обл|область)$", _stem)
        if _m:  # «Сумській області», «Запорізька область»: прилагательное склоняется
            _OBLAST_RE.append((re.compile(r"(?<!\w)" + re.escape(_m.group(1)) + r"\w*\s+обл"), _slug))
        else:
            _OBLAST_RE.append((re.compile(r"(?<!\w)" + re.escape(_stem)), _slug))
# Длинные основы раньше коротких (например «крим» — самая общая).
_OBLAST_RE.sort(key=lambda pair: -len(pair[0].pattern))

_FOREIGN_RE = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(s) for s in FOREIGN_STEMS) + ")")


def oblast_in(text: str) -> str:
    """Slug области, названной в тексте (первая по тексту), '' если нет."""
    lowered = text.lower()
    best_pos, best = None, ""
    for pattern, slug in _OBLAST_RE:
        match = pattern.search(lowered)
        if match and (best_pos is None or match.start() < best_pos):
            best_pos, best = match.start(), slug
    return best


# --- геокодинг ------------------------------------------------------------

_APOS = re.compile(r"[ʼ’`´‘']")


def _norm(text: str) -> str:
    text = _APOS.sub("'", text.lower().replace("ё", "е")).strip()
    text = re.sub(r"^(?:м|с|смт|селище|місто|село)\.?\s+", "", text)
    text = re.sub(r"^(\w)\.\s*(?=\w)", "", text)  # «Н.Піщане» → «піщане»
    return text.strip(" .,:;!?-–—")


# Города и ориентиры без записи в city_coords. Только то, в чём уверены.
_EXTRA_PLACES: dict[str, tuple[str, float, float, tuple[str, ...]]] = {
    "Київ": ("kyivska", 50.450, 30.523, ("київ", "києв", "столиц", "киев")),
}

_CASE_ENDINGS = frozenset(gazetteer._ENDINGS)

_PLACE_INDEX: list[tuple[str, str, str, float, float]] = []  # (стем, название, область, lat, lon)
for _name, _obl, _lat, _lon, _keys in list(CITIES.values()) + [
    (n, v[0], v[1], v[2], v[3]) for n, v in _EXTRA_PLACES.items()
]:
    for _key in _keys:
        _PLACE_INDEX.append((_norm(_key), _name, _obl, _lat, _lon))
_PLACE_INDEX.sort(key=lambda row: -len(row[0]))


@dataclass(frozen=True)
class Geo:
    name: str
    oblast: str
    lat: float
    lon: float
    precision: str  # 'place' — пункт найден, 'approx' — совпадений несколько, 'oblast' — центроид
    alts: tuple[tuple[float, float], ...] = ()  # координаты всех кандидатов при неоднозначности


def _locate_core(place: str, oblast: str = "") -> Geo | None:
    """Координаты места. Подстрока без границы слова давала «буч» ⊂ «Бучач»:
    допускаем хвост ≤3 символов (падежное окончание), а при известной области
    отбрасываем города другой области."""
    norm = _norm(place)
    if not norm:
        return None
    first_word = norm.split(" ")[0]
    fallback: Geo | None = None
    for stem, name, city_oblast, lat, lon in _PLACE_INDEX:
        if not stem:
            continue
        if " " in stem:
            ok = norm.startswith(stem)
        else:
            suffix = first_word[len(stem):] if first_word.startswith(stem) else None
            # хвост — только падежное окончание: «буч|а», но не «буч|ач» (Бучач)
            ok = suffix is not None and (suffix == "" or suffix in _CASE_ENDINGS)
        if not ok:
            continue
        if oblast and city_oblast != oblast:
            # Каналы относят город к соседней области (Славутич: «Чернігівщина»),
            # но короткая основа («буч» ⊂ «Бучач») при чужой области — совпадение случайное.
            if fallback is None and len(stem) >= 6:
                fallback = Geo(name, city_oblast, lat, lon, "place")
            continue
        return Geo(name, city_oblast, lat, lon, "place")
    if fallback is not None:
        return fallback
    return _locate_gazetteer(place, oblast)


_ALTERN = (("ці", "к"), ("зі", "г"), ("сі", "х"), ("чі", "к"))
_SUMY_FORMS = {"сум", "сумі", "сумах", "сумам", "сумами", "сумів"}


def locate(place: str, oblast: str = "") -> Geo | None:
    """Координати місця з урахуванням відмінкових форм і чергування приголосних («Кременчуці» → Кременчук)."""
    geo = _locate_core(place, oblast)
    if geo is not None:
        return geo
    words = place.strip().split()
    if not words:
        return None
    w = words[0].lower().strip(".,;:!?()«»\"")
    if w in _SUMY_FORMS:
        return _locate_core("Суми", oblast)
    for end, rep in _ALTERN:
        if w.endswith(end) and len(w) > len(end) + 2:
            geo = _locate_core(" ".join([w[: -len(end)] + rep] + words[1:]), oblast)
            if geo is not None:
                return geo
    return None


def _locate_gazetteer(place: str, oblast: str) -> Geo | None:
    cands = gazetteer.find(place, oblast)
    if not cands and oblast:
        # Пункт мог быть отнесён каналом к соседней области: берём, только если он один в стране.
        everywhere = gazetteer.find(place, "")
        distinct = {(round(c.lat, 2), round(c.lon, 2)) for c in everywhere}
        if len(distinct) == 1 and everywhere:
            cands = everywhere
    if not cands:
        return None
    # Дубли одного объекта (укр./рус. названия) схлопываем по координатам.
    uniq: dict[tuple[float, float], gazetteer.Candidate] = {}
    for c in cands:
        uniq.setdefault((round(c.lat, 2), round(c.lon, 2)), c)
    cands = sorted(uniq.values(), key=lambda c: -c.population)
    top = cands[0]
    if len(cands) == 1 or (top.population > 0 and top.population >= 3 * max(cands[1].population, 1)):
        return Geo(top.name, top.oblast, top.lat, top.lon, "place")
    return Geo(top.name, top.oblast, top.lat, top.lon, "approx",
               tuple((c.lat, c.lon) for c in cands[:30]))


def oblast_geo(oblast: str) -> Geo | None:
    centroid = REGION_CENTROIDS.get(oblast)
    if not centroid:
        return None
    return Geo(oblast, oblast, centroid[0], centroid[1], "oblast")


# --- оружие ---------------------------------------------------------------

_REACTIVE_RE = re.compile(r"реактив|реактыв|реактивыч|геран[ьи]?-?3|jet")
_SHAHED_RE = re.compile(r"(?<!\w)(?:шахед|шахід|шахид|шахэд|шах|шахадов?|шахад|геран|герань)\w*")
_UAV_RE = re.compile(r"(?<!\w)(?:бпла|бпл|бп-ла|дрон|розвіддрон|розвідувальн\w*\s+дрон|безпілотн|мопед|мопєд|бандерол|гербер|молні[яй]|італмас|італмаси|ланцет|fpv)\w*")
_WEAPON_WORDS = re.compile(
    _SHAHED_RE.pattern + "|" + _UAV_RE.pattern + "|" + _REACTIVE_RE.pattern
    + r"|(?<!\w)(?:ракет|балісти|крилат|каб(?!\w)|калібр|кинджал|циркон|іскандер|х-\d{2,3}|kh-\d{2,3})\w*"
)


def weapon_of(text: str) -> tuple[str, str]:
    """(weapon_class, speed_profile) по тексту; ('', '') если оружия нет."""
    lowered = text.lower()
    if _REACTIVE_RE.search(lowered):
        return "uav", "reactive"
    if _SHAHED_RE.search(lowered):
        return "shahed", ""
    if _UAV_RE.search(lowered):
        return "uav", ""
    if _WEAPON_WORDS.search(lowered):
        cls = classify_weapon(text)
        return ("" if cls in ("unknown", "alert", "stand_down") else cls), ""
    return "", ""


# --- структура ------------------------------------------------------------

@dataclass(frozen=True)
class Observation:
    weapon: str            # shahed | uav | ballistic | cruise_missile | kab | …
    profile: str           # '' | 'reactive'
    count: int | None      # число именно у этого места; None — не названо
    group_count: int | None  # число по области из заголовка «4 на Сумщині»
    oblast: str            # slug области или ''
    place: str             # населённый пункт как в тексте ('' — только область)
    relation: str          # at | toward | via | from | near
    lat: float | None
    lon: float | None
    precision: str         # place | oblast | none
    heading_from: str = ""  # сторона света, откуда летит: N NE E SE S SW W NW
    alts: tuple[tuple[float, float], ...] = ()  # кандидаты координат (если precision == 'approx')


@dataclass
class ParsedPost:
    kind: str  # track | alert | all_clear | stats | news | foreign | impact | unknown
    observations: list[Observation] = field(default_factory=list)
    as_of: str = ""        # «13:45» из «станом на 13:45»
    oblasts: list[str] = field(default_factory=list)   # где цель сейчас / куда летит (без областей «звідки»)
    origins: list[str] = field(default_factory=list)   # откуда летит («з Донеччини»)
    weapon: str = ""
    profile: str = ""
    reasons: list[str] = field(default_factory=list)

    @property
    def mappable(self) -> bool:
        """Можно ли рисовать на карте как живую угрозу."""
        return self.kind == "track" and any(o.precision != "none" for o in self.observations)


_CARDINAL = {
    "півночі": "N", "півдня": "S", "заходу": "W", "сходу": "E",
    "північного сходу": "NE", "північного заходу": "NW",
    "південного сходу": "SE", "південного заходу": "SW",
    "північно-східного": "NE", "північно-західного": "NW",
    "південно-східного": "SE", "південно-західного": "SW",
    "севера": "N", "юга": "S", "запада": "W", "востока": "E",
}
_CARDINAL_RE = re.compile(
    r"(?:з|із|зі|из)\s+(північно-східн\w+|північно-західн\w+|південно-східн\w+|південно-західн\w+|"
    r"північного\s+сходу|північного\s+заходу|південного\s+сходу|південного\s+заходу|"
    r"півночі|півдня|заходу|сходу|севера|юга|запада|востока)",
    re.IGNORECASE,
)

_NUMBER_WORDS = {"один": 1, "одна": 1, "два": 2, "дві": 2, "три": 3, "чотири": 4,
                 "п'ять": 5, "пʼять": 5, "шість": 6, "сім": 7, "вісім": 8, "десять": 10}

_BULLET = r"[\s•·▪▫●○◦‣⁃\-–—*🔸🔹🔺🔻▶►➡️→⚠️❗️🔴🟠🟡🛵]*"
_OBL_ALT = "|".join(sorted({s for stems in OBLAST_STEMS.values() for s in stems if " " not in s}, key=len, reverse=True))

# «• 4 на Сумщині — Суми, Низи», «• Черкащина (1) – Чигирин (1)», «Київщина: …»
_OBLAST_LINE_RE = re.compile(
    rf"^{_BULLET}(?:(?P<n>\d{{1,3}})\s*[хx]?\s+(?:на|в|у|над)\s+)?"
    rf"(?P<obl>(?:{_OBL_ALT})\w*)\s*(?:\((?P<n2>\d{{1,3}})\))?\s*(?:[—–:\-]\s*(?P<rest>.*))?$",
    re.IGNORECASE,
)

# «• 3 – Бортничі» (детальная сводка по городу)
_COUNT_PLACE_RE = re.compile(rf"^{_BULLET}(?P<n>\d{{1,3}})\s*[—–-]\s*(?P<place>[^.!?]{{2,60}})$")

# «Початок відбою тривог», «Сигнал по балістиці скасовано» — classify_weapon знал
# только «відбій» и принимал генитив «відбою» за обычную тривогу.
_ALL_CLEAR_RE = re.compile(
    r"(?<!\w)(?:початок\s+відбою|відбою\s+(?:тривог|повітрян|небезпек)|відбій|отбой|"
    r"скасован\w*|небезпека\s+минула|загроза\s+минула|тривог\w*\s+(?:скасован|закінчил)|"
    r"ракети\s+більше\s+не\s+фіксуються|припинила\s+своє\s+існування|мінус)(?!\w)"
)
_NOT_CLEAR_RE = re.compile(r"до\s+відбою|до\s+відбій|до\s+скасування|не\s+скасован")
# Розповідь про людей і візити («зірка відвідав Київ і спустився в укриття») — це новина, а не жива загроза.
_NARRATIVE_RE = re.compile(
    r"\b(?:зірк\w*|акто(?:р|рк)\w*|співа(?:к|чк)\w*|футболіст\w*|блогер\w*|ведуч\w*|знаменит\w*|відом(?:ий|а|ого)\s+\w+)\b"
    r"|\b(?:відвідав|відвідала|відвідали|побував|побувала|приїхав|приїхала|спустив(?:ся|лася|лись)|довелося\s+спускатися)\b")

_STRONG_PROMO_RE = re.compile(r"monobank|приват\s*банк|картка\s+банки|реквізит|iban|paypal")
_PROMO_RE = re.compile(
    r"monobank|send\.monobank|приват\s*банк|картка\s+банки|донат|підписуйтесь|приєднуйтесь|"
    r"підтримайте|збір\s+на|реквізит|сповіщення\s+про\s+тривог|підключили\s+сповіщення"
)

_TIME_RE = re.compile(r"станом\s+на\s+(\d{1,2})[:.](\d{2})", re.IGNORECASE)

_REL = (
    r"курсом\s+на|курсом\s+через|курсом\s+в\s+бік|курсом\s+у\s+бік|"
    r"в\s+напрямку|у\s+напрямку|в\s+напрямі|у\s+напрямі|у\s+бік|в\s+бік|в\s+сторону|у\s+сторону|"
    r"в\s+район|у\s+район|в\s+районі|у\s+районі|в\s+передмісті|у\s+передмісті|на\s+околиці|на\s+околицях|поблизу|біля|над|"
    r"через|повз|"
    r"із|зі|з|из|"
    r"на|до|к"
)
_REL_RE = re.compile(rf"(?<!\w)(?P<rel>{_REL})(?!\w)\s+", re.IGNORECASE)

_REL_KIND = [
    ("from", re.compile(r"^(?:із|зі|з|из)$")),
    ("via", re.compile(r"^(?:через|повз|курсом через)$")),
    ("at", re.compile(r"^(?:над|в район\w*|у район\w*|в передмісті|у передмісті|на околиц\w+|поблизу|біля)$")),
]

_STOP_TOKENS = re.compile(
    r"(?:\s+(?:та|і|й|та ще|а також|потім|далі|ще)\s+|[;()!?🚨⚠️❗️🔴]|\.\s|\s[—–-]\s|$)", re.IGNORECASE
)

_NOT_PLACE = re.compile(
    r"^(?:цю|цей|ця|це|водосховищ\w*|море|моря|морем|чорного\s+моря|акваторі\w+|нас|нашому|наш|вас|вам|нам|тебе|мене|ночі|ранку|вечір|вечора|півдня|півночі|заходу|сходу|"
    r"північ|південь|захід|схід|північний\w*|південний\w*|східний\w*|західний\w*|"
    r"тривогу|тривога|укриття|небо|небі|місто|області|регіон|район|території|"
    r"півд\w*|півн\w*|цілі|ціль|курс|обл\w*|область|"
    r"станом|годину|хвилин\w*|секунд\w*|\d[\d:.\-]*)$",
    re.IGNORECASE,
)


def _clean_place(raw: str) -> str:
    text = re.sub(r"\([^)]*\)", "", raw)
    text = re.sub(r"[*_`\[\]#]|️|[\U0001F300-\U0001FAFF☀-➿]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .,:;!?-–—")
    text = re.sub(r"^(?:ще|також|потім|далі)\s+", "", text, flags=re.IGNORECASE)
    # «маневрує в районі Павлограда» → «Павлограда»
    text = re.sub(
        r"^(?:(?:маневру\w+|кружля\w+|барражу\w+|літа\w+|летить|летять|йде|йдуть|заходить|заходять)\s+)?"
        r"(?:(?:в|у)\s+(?:районі|околицях|район)|над|біля|поблизу|через|повз|курсом\s+на|в\s+бік|у\s+бік|на)\s+",
        "", text, flags=re.IGNORECASE)
    return text


_PAREN_HINT: dict[str, str] = {}


def _split_places(chunk: str) -> list[tuple[str, int | None]]:
    """«Суми, Низи (2), Боромля / Лебедин + Кролевець» → [(Суми,None), (Низи,2), …].

    Уточнение в скобках («ТЕЦ-5 (Київ)») запоминается в _PAREN_HINT[name]."""
    out: list[tuple[str, int | None]] = []
    for part in re.split(r"\s*(?:,|/|\+|\sта\s|\sі\s|\sй\s|\sі\s)\s*", chunk):
        count = None
        m = re.search(r"\((\d{1,3})\)", part)
        if m:
            count = int(m.group(1))
        hint = re.search(r"\(([^)\d][^)]*)\)", part)
        name = _clean_place(part)
        if not name or _NOT_PLACE.match(name) or len(name.split()) > 4 or len(name) < 2:
            continue
        if re.fullmatch(r"[\d\W]+", name) or (re.search(r"\d", name) and not re.search(r"[A-Za-zА-Яа-яІіЇїЄє]{2,}", name)):
            continue
        if hint:
            _PAREN_HINT[name] = hint.group(1).strip()
        out.append((name, count))
    return out


def _count_in(line: str) -> int | None:
    m = re.search(r"(?<![\w:])(\d{1,3})\s*[хx](?!\w)", line, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(
        r"(?<![\w:.])(\d{1,3})\s+(?:реактивн\w+\s+|ударн\w+\s+|ворожих?\s+)?(?:бпла|шахед|дрон|шах)", line, re.IGNORECASE
    )
    if m:
        return int(m.group(1))
    low = line.lower()
    for word, value in _NUMBER_WORDS.items():
        if re.search(rf"(?<!\w){re.escape(word)}(?:\s+\w+)?\s+(?:бпла|шахед|дрон|шах)", low):
            return value
    return None


def _heading_from(line: str) -> str:
    m = _CARDINAL_RE.search(line)
    if not m:
        return ""
    key = re.sub(r"\s+", " ", m.group(1).lower())
    for stem, code in _CARDINAL.items():
        if key.startswith(stem[:-1]) or key == stem:
            return code
    return ""


def _make_obs(
    place: str, rel: str, count: int | None, group: int | None, oblast: str,
    weapon: str, profile: str, heading: str,
) -> Observation:
    geo = locate(place, oblast) if place else None
    if geo is None and place and place in _PAREN_HINT:
        geo = locate(_PAREN_HINT[place], oblast) or locate(_PAREN_HINT[place], "")
    if geo is None and oblast:
        geo = oblast_geo(oblast) if not place else None
    if geo is None and place and oblast:
        # Название не в справочнике: оставляем область, но точку не выдумываем.
        og = oblast_geo(oblast)
        return Observation(weapon, profile, count, group, oblast, place, rel,
                           og.lat if og else None, og.lon if og else None,
                           "oblast" if og else "none", heading)
    if geo is None:
        return Observation(weapon, profile, count, group, oblast, place, rel, None, None, "none", heading)
    return Observation(weapon, profile, count, group, oblast or geo.oblast, place, rel,
                       geo.lat, geo.lon, geo.precision, heading, geo.alts)


_YOU_RE = re.compile(
    rf"^{_BULLET}(?P<place>[^,.!?:()]{{2,60}}?)\s*(?:\((?P<paren>[^)]{{2,40}})\))?\s*,\s*"
    r"(?P<rest>.*(?:до\s+вас|на\s+місто|у\s+ваш\w+\s+напрям\w*|в\s+ваш\w+\s+напрям\w*|на\s+вас)\b.*)$",
    re.IGNORECASE,
)
_SHORT_RE = re.compile(
    rf"^{_BULLET}(?P<place>[^,.!?:()]{{2,50}}?)\s*(?:\((?P<paren>[^)]{{2,40}})\))?\s*,?\s+"
    r"(?P<rest>(?:ще\s+)?(?:\d{1,3}\s*[хx]?\s+|один\s+|одна\s+|дві\s+|два\s+|три\s+)?(?:реактивн\w*\s+)?"
    r"(?:бпла|шахед\w*|шах\w*|дрон\w*|мопед\w*|бандерол\w*|балісти\w*|ракет\w*|каб\w*|крилат\w*|кинджал\w*|циркон\w*|х-\d+)\b.*)$",
    re.IGNORECASE,
)
_ORDINAL_RE = re.compile(r"(?<!\w)(?:наступн\w*|перш\w+|друг\w+|трет\w+|четверт\w+|останн\w+|ще\s+(?:один|два|три|\d+))(?!\w)")
_MOVE_RE = re.compile(r"(?<!\w)(?:летять|летить|летят|йдуть|йде|заходять|заходить|рухаєтьс\w+|рухаються|повертає\w*)")


def _parse_you_form(line: str, oblast: str, group: int | None, weapon: str, profile: str) -> list[Observation]:
    """«Чернігів, шахед на місто!», «Одеса, дві ракети до вас!», «ТЕЦ-5 (Київ), мопед до вас»."""
    m = _YOU_RE.match(line.strip())
    if not m:
        m = _SHORT_RE.match(line.strip())
        if not m:
            return []
        first = _split_places(m.group("place"))
        if not first or not all(locate(n, oblast) or locate(n, "") for n, _ in first):
            return []  # короткую форму принимаем только если места опознаны
    w, p = weapon_of(m.group("rest"))
    weapon, profile = w or weapon, p or profile
    count = _count_in(m.group("rest"))
    if count is None:
        for word, value in _NUMBER_WORDS.items():
            if re.match(rf"\W*{word}(?!\w)", m.group("rest").strip().lower()):
                count = value
                break
    paren = (m.group("paren") or "").strip()
    paren_obl = oblast_in(paren) if paren else ""
    out: list[Observation] = []
    for name, pcount in _split_places(m.group("place")):
        target_obl = paren_obl or oblast
        obs = _make_obs(name, "toward", pcount if pcount is not None else count, group, target_obl,
                        weapon or "unknown", profile, "")
        if obs.precision != "place" and paren and not paren_obl:
            geo = locate(paren, "")
            if geo:  # ориентир внутри города: «ТЕЦ-5 (Київ)»
                obs = Observation(obs.weapon, obs.profile, obs.count, obs.group_count, geo.oblast, name,
                                  "toward", geo.lat, geo.lon, "place", "")
        out.append(obs)
    return out


def _parse_line(line: str, oblast: str, group: int | None, weapon: str, profile: str) -> list[Observation]:
    """Одна строка → наблюдения по её «маршрутным» маркерам."""
    you = _parse_you_form(line, oblast, group, weapon, profile)
    if you:
        return you
    w, p = weapon_of(line)
    weapon, profile = w or weapon, p or profile
    if not weapon and (_MOVE_RE.search(line.lower()) or _ORDINAL_RE.search(line.lower())):
        weapon = "unknown"  # «Летять на Славутич» — тип не назван, но движение есть
    count = _count_in(line)
    heading = _heading_from(line)
    matches = list(_REL_RE.finditer(line))
    obs: list[Observation] = []
    for m in matches:
        rel_raw = re.sub(r"\s+", " ", m.group("rel").lower())
        tail = line[m.end():]
        stop = _STOP_TOKENS.search(tail)
        chunk = tail[: stop.start()] if stop else tail
        # Следующий маркер отрезает хвост («на Київ повз Вишгород»).
        nxt = _REL_RE.search(chunk)
        if nxt:
            chunk = chunk[: nxt.start()]
        if re.match(r"^\W*\d", chunk) or _CARDINAL_RE.match(rel_raw + " " + chunk.strip().lower()):
            continue  # «станом на 13:45», «з півдня»
        rel = "toward"
        for kind, rx in _REL_KIND:
            if rx.match(rel_raw):
                rel = kind
                break
        places = _split_places(chunk)
        # Местный ключ области в самом названии («на Сумщину») задаёт oblast без точки.
        for name, pcount in places:
            local_obl = oblast_in(name) if len(name.split()) <= 2 else ""
            if local_obl and not locate(name, local_obl):
                obs.append(_make_obs("", rel, pcount if pcount is not None else count, group,
                                     local_obl, weapon, profile, heading))
                continue
            obs.append(_make_obs(name, rel, pcount if pcount is not None else (count if len(places) == 1 else None),
                                 group, oblast, weapon, profile, heading))
    return obs


def parse_post(text: str, source: str = "") -> ParsedPost:
    """Главная функция: сырой текст поста → ParsedPost."""
    raw = text or ""
    low = raw.lower()
    post = ParsedPost(kind="unknown")
    if not raw.strip():
        return post

    time_m = _TIME_RE.search(raw)
    if time_m:
        post.as_of = f"{int(time_m.group(1)):02d}:{time_m.group(2)}"

    weapon, profile = weapon_of(raw)
    post.weapon, post.profile = weapon, profile
    has_ua = bool(oblast_in(raw))
    foreign = bool(_FOREIGN_RE.search(low))

    # --- шум: статистика, новости, цитаты -------------------------------
    if re.search(r"(?:збито|збит[іо]|подавлен\w*|знищен\w*|збито/подавлено)[^.\n]{0,30}\d+\s*(?:із|з|из|/)\s*\d+", low) or \
            "силами ппо" in low and re.search(r"\d+\s*(?:із|з|/)\s*\d+", low):
        post.kind, post.reasons = "stats", ["shootdown_stats"]
        return post
    if _STRONG_PROMO_RE.search(low):
        post.kind, post.reasons = "promo", ["donation_requisites"]
        return post
    # Цитата/пересказ: «…, – нардеп Нагорняк», «заявив», «за його словами».
    reported = re.search(
        r"(?:нардеп|речник|заявив|заявила|повідомив|повідомила|зазначив|зазначила|розповів|розповіла|"
        r"за його словами|за її словами|за словами|каже|пояснив|пояснила|, – [а-яіїє'ʼ]+ [а-яіїє'ʼ]+$|"
        r"голова\s+\w+\s+(?:ова|ськ)|міністр|генерал|командувач|експерт|аналітик|показали|презентувал|представил)",
        low,
    )
    # --- структура сводки ----------------------------------------------
    obs: list[Observation] = []
    cur_obl, cur_group = "", None
    cur_weapon, cur_profile = weapon, ""  # профиль («реактивний») — только из своей строки/заголовка
    lines = [ln for ln in re.split(r"\n+", raw) if ln.strip()]
    for line in lines:
        stripped = line.strip()
        lw, lp = weapon_of(stripped)
        header_like = re.search(r"(?:рух|курс|напрямк)", stripped.lower()) and re.search(
            r"(?:бпла|шахед|дрон|ракет|реактив)", stripped.lower()
        ) and not _OBLAST_LINE_RE.match(stripped)
        if header_like and (stripped.endswith(":") or _TIME_RE.search(stripped)):
            cur_weapon, cur_profile = lw or cur_weapon, lp or cur_profile
            cur_obl, cur_group = "", None
            continue
        m = _OBLAST_LINE_RE.match(stripped)
        if m:
            cur_obl = oblast_in(m.group("obl")) or cur_obl
            grp = m.group("n") or m.group("n2")
            cur_group = int(grp) if grp else None
            rest = (m.group("rest") or "").strip()
            if rest:
                inline = _parse_line_rest(rest, cur_obl, cur_group, lw or cur_weapon, lp or cur_profile)
                obs.extend(inline)
            continue
        cnt = _COUNT_PLACE_RE.match(stripped)
        if cnt and (lw or cur_weapon):
            for name, pcount in _split_places(cnt.group("place")):
                obs.append(_make_obs(name, "at", pcount if pcount is not None else int(cnt.group("n")), None,
                                     cur_obl or "", lw or cur_weapon, lp or cur_profile, ""))
            continue
        obs.extend(_parse_line(stripped, cur_obl or "", cur_group, lw or cur_weapon, lp or cur_profile))

    if not obs and weapon and has_ua and not foreign:
        # «Сумщина під балістичним обстрілом», «Загроза КАБ для Запоріжжя/області»
        for slug in dict.fromkeys(oblast_in(ln) for ln in lines if oblast_in(ln)):
            obs.append(_make_obs("", "threat", None, None, slug, weapon, profile, ""))

    # Наблюдения без оружия у нас не берём: «Київ на Бровари» в новостях.
    obs = [o for o in obs if o.weapon]
    # Область из текста, если место не уточнило её.
    if obs and not any(o.oblast for o in obs):
        text_obl = oblast_in(raw)
        if text_obl:
            obs = [o if o.oblast else _with_oblast(o, text_obl) for o in obs]

    obs = _disambiguate(obs)
    post.observations = obs
    all_obl = list(dict.fromkeys(o.oblast for o in obs if o.oblast))
    here = list(dict.fromkeys(o.oblast for o in obs if o.oblast and o.relation != "from"))
    # Область «звідки» — не регион тревоги: иначе пост «Барвінкове з Донеччини» уходит подписчикам Донеччины.
    post.oblasts = here or all_obl
    post.origins = [x for x in all_obl if x not in here]

    if obs and not (foreign and not has_ua):
        if reported and len(lines) <= 6 and not _OBLAST_LINE_RE.search(raw):
            post.kind, post.reasons = "news", ["reported_speech"]
            post.observations = []
            return post
        post.kind = "track"
        return post
    if foreign and not has_ua and (weapon or "бпла" in low or "дрон" in low):
        post.observations = []
        # «Загроза балістики з Брянська» — предупреждение о пуске, а не событие
        # на чужой территории: в канал оно идёт, на карту точкой — нет.
        if re.search(r"загроз|пуск|виліт|злет|зліт|застосуван", low) and re.search(
                r"(?:з|із|зі|від)\s+(?:\w+\s+){0,2}(?:брянськ|курськ|воронезьк|ростовськ|орловськ|москов|белгород|бєлгород|кримськ|крим)", low):
            post.kind, post.reasons = "alert", ["foreign_origin"]
        else:
            post.kind, post.reasons = "foreign", ["foreign_territory"]
        return post
    if reported and (weapon or has_ua):
        post.kind, post.reasons = "news", ["reported_speech"]
        return post
    if _NARRATIVE_RE.search(low) and not obs:
        post.kind, post.reasons = "news", ["narrative_about_people"]
        return post
    cls = classify_weapon(raw)
    if _PROMO_RE.search(low):
        post.kind, post.reasons = "promo", ["promo_or_donation"]
    elif _ALL_CLEAR_RE.search(low) and not _NOT_CLEAR_RE.search(low) or cls == "stand_down":
        post.kind = "all_clear"
    elif re.search(r"(?:влучан|впав|впал|упал|прил[іе]т|вибух|загинул|поранен|руйнуван|пошкодж|уламк)", low):
        post.kind = "impact"
    elif cls == "alert" or re.search(r"тривог|тревог", low):
        post.kind = "alert"
    elif weapon:
        post.kind = "unknown"
        post.reasons = ["weapon_without_place"]
    return post


def _disambiguate(obs: list[Observation]) -> list[Observation]:
    """«Іванівка» (13 штук в области): выбираем кандидата ближе всего к
    однозначным соседям по сводке — цели одной волны летят кучно."""
    anchors = [(o.lat, o.lon) for o in obs if o.precision == "place" and o.lat is not None]
    out = []
    for o in obs:
        if o.precision != "approx" or not o.alts:
            out.append(o)
            continue
        same = [(a.lat, a.lon) for a in obs if a.precision == "place" and a.oblast == o.oblast and a.lat is not None]
        pool = same or anchors
        if not pool:
            out.append(o)
            continue
        ax = sum(p[0] for p in pool) / len(pool)
        ay = sum(p[1] for p in pool) / len(pool)
        lat, lon = min(o.alts, key=lambda c: (c[0] - ax) ** 2 + ((c[1] - ay) * 0.64) ** 2)
        km = (((lat - ax) * 111) ** 2 + ((lon - ay) * 71) ** 2) ** 0.5
        # Ближе 80 км к соседям — считаем найденным; иначе честно «примерно».
        out.append(Observation(o.weapon, o.profile, o.count, o.group_count, o.oblast, o.place, o.relation,
                               lat, lon, "place" if km <= 80 else "approx", o.heading_from, ()))
    return out


def _with_oblast(o: Observation, oblast: str) -> Observation:
    geo = locate(o.place, oblast) if o.place else None
    if geo:
        return Observation(o.weapon, o.profile, o.count, o.group_count, oblast, o.place, o.relation,
                           geo.lat, geo.lon, "place", o.heading_from)
    og = oblast_geo(oblast)
    return Observation(o.weapon, o.profile, o.count, o.group_count, oblast, o.place, o.relation,
                       og.lat if og else None, og.lon if og else None,
                       "oblast" if og else "none", o.heading_from)


def _parse_line_rest(rest: str, oblast: str, group: int | None, weapon: str, profile: str) -> list[Observation]:
    """Хвост после «Область –»: либо перечень пунктов, либо фраза «БпЛА на X»."""
    if _REL_RE.search(rest) and weapon_of(rest)[0]:
        return _parse_line(rest, oblast, group, weapon, profile)
    w, p = weapon_of(rest)
    weapon, profile = w or weapon, p or profile
    heading = _heading_from(rest)
    chunk = re.sub(r"\((?:реактивн\w*|ударн\w*|шахед\w*)\)", "", rest, flags=re.IGNORECASE)
    places = _split_places(chunk)
    out = []
    for name, pcount in places:
        out.append(_make_obs(name, "at", pcount, group if len(places) == 1 else group, oblast,
                             weapon, profile, heading))
    if not places and oblast:
        out.append(_make_obs("", "at", None, group, oblast, weapon, profile, heading))
    return out
