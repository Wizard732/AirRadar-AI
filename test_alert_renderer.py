import unittest

from alert_renderer import render_evidence_alert
from incident_fusion import extract_incident_fact


class EvidenceAlertTests(unittest.TestCase):
    def test_single_source_alert_only_contains_evidence(self):
        fact = extract_incident_fact("БпЛА на Одесу", "uav", "imminent")
        result = render_evidence_alert(
            text="БпЛА на Одесу", source="raketa_trevoga", event_ts=1_700_000_000,
            fact=fact, confirmation={"status": "reported", "sources": 1, "count_kind": "unspecified"},
        )
        # Заголовок капсом — визуальный якорь.
        self.assertIn("🔴 ОДЕСЬКА ОБЛ. | БПЛА", result)
        # Одиночное сообщение статуса не получает вовсе (шум в каждом посте).
        self.assertNotIn("повідомлення одного джерела", result)
        self.assertNotIn("Статус:", result)
        self.assertIn("🕒 ", result)
        self.assertIn("БпЛА на Одесу", result)
        # Читабельность: блоки разделены пустыми строками.
        self.assertIn("\n\n", result)
        # Строка источника — всегда наш канал (пассивный брендинг).
        self.assertIn("📡 Джерело: AirRadar AI", result)
        # Хендлы исходных каналов в пост не попадают.
        self.assertNotIn("@raketa_trevoga", result)
        # Старые блоки удалены.
        self.assertNotIn("Кількість", result)
        self.assertNotIn("Не підтверджено або невідомо", result)
        # Правило 2: без ETA-прогнозов и вероятностей.
        self.assertNotIn("ETA", result)
        self.assertNotIn("Аналіз", result)
        self.assertNotIn("Ризик", result)
        self.assertNotIn("укритті", result)

    def test_conflicting_count_is_not_aggregated(self):
        fact = extract_incident_fact("3 БпЛА на Одесу", "uav", "imminent")
        result = render_evidence_alert(
            text="3 БпЛА на Одесу", source="source", event_ts=1_700_000_000,
            fact=fact, confirmation={"status": "corroborated", "sources": 2, "count_kind": "conflicting", "count_value": 2},
        )
        self.assertIn("підтверджено 2 незалежними джерелами", result)
        # Спорное число не публикуется как факт.
        self.assertNotIn("щонайменше", result)
        self.assertNotIn("5", result)

    def test_motion_line_requires_origin_and_destination(self):
        fact = extract_incident_fact("БПЛА з Сум у напрямку Києва", "uav", "imminent")
        result = render_evidence_alert(
            text="БПЛА з Сум у напрямку Києва", source="a", event_ts=1_700_000_000,
            fact=fact, confirmation={"status": "reported", "sources": 1},
        )
        self.assertIn("Рух: цілі прямують з Сумська обл. на Київ та область", result)

        fact2 = extract_incident_fact("БпЛА на Одесу", "uav", "imminent")
        result2 = render_evidence_alert(
            text="БпЛА на Одесу", source="a", event_ts=1_700_000_000,
            fact=fact2, confirmation={"status": "reported", "sources": 1},
        )
        self.assertNotIn("Рух:", result2)

    def test_aggregated_sources_line(self):
        fact = extract_incident_fact("БпЛА на Одесу", "uav", "imminent")
        result = render_evidence_alert(
            text="БпЛА на Одесу", source="b", event_ts=1_700_000_000, fact=fact,
            confirmation={"status": "corroborated", "sources": 2},
            sources=["a", "b"],
        )
        # Агрегация видна в статусе; хендлы источников не публикуются.
        self.assertIn("підтверджено 2 незалежними джерелами", result)
        self.assertIn("📡 Джерело: AirRadar AI", result)
        self.assertNotIn("@a", result)
        self.assertNotIn("@b", result)

    def test_numeric_source_is_monitoring(self):
        fact = extract_incident_fact("БпЛА на Одесу", "uav", "imminent")
        result = render_evidence_alert(
            text="БпЛА на Одесу", source="-1003979438669", event_ts=1_700_000_000,
            fact=fact, confirmation={"status": "reported", "sources": 1},
        )
        self.assertIn("📡 Джерело: AirRadar AI", result)
        self.assertNotIn("-1003979438669", result)

    def test_stand_down_branch(self):
        fact = extract_incident_fact("Відбій повітряної тривоги у Києві", "stand_down", "unknown")
        result = render_evidence_alert(
            text="Відбій повітряної тривоги у Києві", source="a", event_ts=1_700_000_000,
            fact=fact, confirmation={"status": "reported", "sources": 1},
        )
        self.assertTrue(result.startswith("🟢 ВІДБІЙ — КИЇВ ТА ОБЛАСТЬ — "))
        self.assertIn("📡 Джерело: AirRadar AI", result)

    def test_multi_region_roundup_header(self):
        # Сборный пост: заголовок «Кілька областей», без ложного региона.
        roundup = (
            "Київщина\n2 реактивні БпЛА на Васильків\n"
            "Полтавщина\n1 реактивний БпЛА на Глобине\n"
            "Одещина\n1 реактивний БпЛА на Сергіївку"
        )
        fact = extract_incident_fact(roundup, "uav", "imminent")
        result = render_evidence_alert(
            text=roundup, source="a", event_ts=1_700_000_000,
            fact=fact, confirmation={"status": "reported", "sources": 1},
        )
        self.assertIn("🔴 КІЛЬКА ОБЛАСТЕЙ | БПЛА", result)
        # Ни один отдельный регион не вынесен в заголовок.
        self.assertFalse(result.splitlines()[0].startswith("🔴 ПОЛТАВСЬКА"))
        self.assertFalse(result.splitlines()[0].startswith("🔴 ОДЕСЬКА"))

    def test_track_line_displaces_typical_eta(self):
        """Каскад ETA в шапке: трек по засечкам вытесняет «типовий підліт»."""
        fact = extract_incident_fact("БпЛА на Київ", "uav", "imminent")
        kwargs = dict(
            text="БпЛА на Київ", source="a", event_ts=1_700_000_000,
            fact=fact, confirmation={"status": "reported", "sources": 1},
        )
        # Без трека каскад падает до типового подлёта класса (орієнтовно).
        plain = render_evidence_alert(**kwargs)
        self.assertIn("Типовий підліт", plain)
        self.assertIn("орієнтовно", plain)
        # Измеренный трек: строка «За треком», типовой диапазон вытеснен.
        track = {
            "available": True, "points": 2,
            "from_name": "Конотоп", "to_name": "Бровари",
            "from": [51.243, 33.207], "to": [50.511, 30.790],
            "km": 188.2, "speed_kmh": 188.2, "course_deg": 244,
            "minutes": 8, "region": fact.destination_region,
        }
        with_track = render_evidence_alert(**kwargs, track=track)
        self.assertIn("За треком: Конотоп → Бровари", with_track)
        self.assertIn("≈8 хв", with_track)
        self.assertIn("188 км/год", with_track)
        self.assertIn("орієнтовно", with_track)
        self.assertNotIn("Типовий підліт", with_track)
        # Недоступный трек ведёт себя как отсутствие трека.
        dead = dict(track, available=False, minutes=None)
        fallback = render_evidence_alert(**kwargs, track=dead)
        self.assertNotIn("За треком", fallback)
        self.assertIn("Типовий підліт", fallback)

    def test_long_roundup_body_not_truncated(self):
        """Сводка мониторинга >300 знаков не режется: все направления на месте.

        Регрессия: лимит тела 300 обрезал сводки на полуслове — хвост
        («…ще 2 на Київ») терялся, и направления пропадали из поста.
        """
        lines = [f"Область {i}: 2 реактивні БпЛА на напрямку міст і сіл номер {i}" for i in range(15)]
        roundup = "\n".join(lines)  # ~1000+ знаков
        fact = extract_incident_fact(roundup, "uav", "imminent")
        result = render_evidence_alert(
            text=roundup, source="a", event_ts=1_700_000_000,
            fact=fact, confirmation={"status": "reported", "sources": 1},
        )
        self.assertIn(lines[-1], result, "хвост сводки обязан сохраниться")
        self.assertIn("напрямку міст і сіл номер 14", result)


if __name__ == "__main__":
    unittest.main()
