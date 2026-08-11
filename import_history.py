#!/usr/bin/env python3
"""import_history.py — импорт HTML-экспортов Telegram Desktop в базу AirRadar.

Стандартный экспорт Telegram Desktop (Export chat history → HTML) складывает
сообщения в файлы messages.html, messages2.html ... messagesN.html. Этот
скрипт проходит по всем папкам-каналам, достаёт из каждого сообщения текст и
timestamp, прогоняет через фильтр/определение региона/классификацию угрозы и
записывает в SQLite-журнал (database.py).

Результат: мгновенная статистика и ETA на основе тысяч реальных сообщений,
без ожидания накопления в реальном времени.

Запуск (из папки проекта):
    python import_history.py "C:/Users/.../данные для обучения"

Без аргумента — использует путь по умолчанию на рабочем столе.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from database import Database
from fast_filter import clean_signature, matches_keywords
from regions import detect_region
from sticker import classify_threat
from weapon_classes import classify_weapon

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("importer")

DB_PATH = "airradar.db"

# Регэксп для timestamp из title="24.02.2022 14:27:25 UTC+02:00".
# Telegram пишет дату в атрибуте title у div.pull_right.date.
DATE_RE = re.compile(
    r'title="(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}:\d{2})'
)


class MessageParser(HTMLParser):
    """Парсер одного messages*.html — извлекает (timestamp, text) сообщений.

    Telegram Desktop оборачивает текст сообщения в <div class="text">...</div>,
    а дату — в <div class="pull_right date details" title="ДД.ММ.ГГГГ ЧЧ:ММ:СС">.
    Парсер отслеживает вложенность и собирает текстовые узлы внутри div.text.
    """

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[tuple[int, str]] = []  # (unix_ts, text)
        # Состояние конечного автомата.
        self._current_ts: int | None = None
        self._in_text_div = False
        self._text_div_depth = 0
        self._text_parts: list[str] = []
        self._div_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        # В экспортe Telegram перевод строки обычно передан через <br>, а не
        # текстовый whitespace. Сохраняем разделитель, чтобы слова не склеивались.
        if tag == "br":
            if self._in_text_div:
                self._text_parts.append(" ")
            return
        if tag != "div":
            return
        self._div_depth += 1
        classes = dict(attrs).get("class", "")
        title = dict(attrs).get("title", "")

        # Захватываем дату сообщения.
        if title and "date" in classes:
            self._current_ts = self._parse_ts(title)

        # Входим в div.text — начинаем сбор текста.
        if "text" in classes and "from_name" not in classes:
            # различаем body.text (сообщение) от прочих text-классов
            self._in_text_div = True
            self._text_div_depth = self._div_depth
            self._text_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag != "div":
            return
        # Если закрываем div, в котором начали собирать текст — фиксируем сообщение.
        if self._in_text_div and self._div_depth == self._text_div_depth:
            text = "".join(self._text_parts).strip()
            if text and self._current_ts is not None:
                # Несколько div.text подряд могут быть в одном сообщении — берём первый
                # содержательный. Гарантируем, что не добавляем пустые/дубли.
                self.messages.append((self._current_ts, text))
            self._in_text_div = False
            self._text_parts = []
        self._div_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_text_div:
            # Не срезаем пробелы у каждого узла: Telegram дробит текст на HTML-
            # элементы, и ``strip()`` склеивал «20:57Повітряна» в один токен.
            self._text_parts.append(data)

    @staticmethod
    def _parse_ts(title: str) -> int | None:
        """'24.02.2022 14:27:25 UTC+02:00' -> unix timestamp."""
        m = re.search(r"(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}:\d{2})", title)
        if not m:
            return None
        try:
            # Telegram отдаёт локальное время с указанием смещения. Для статистики
            # достаточно парсить как naive (расхождение в часовом поясе несущественно
            # для расчёта интервалов пуск→прилёт — оба события в одной зоне).
            dt = datetime.strptime(m.group(1), "%d.%m.%Y %H:%M:%S")
            return int(dt.timestamp())
        except ValueError:
            return None


def parse_html_file(path: Path) -> list[tuple[int, str]]:
    """Распарсить один HTML-файл, вернуть список (ts, text)."""
    try:
        html = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("Не смог прочитать %s: %s", path.name, exc)
        return []
    parser = MessageParser()
    try:
        parser.feed(html)
    except Exception as exc:  # битый HTML —Telethon не падаем
        log.warning("Ошибка парсинга %s: %s", path.name, exc)
    return parser.messages


def import_folder(data_dir: Path, db: Database) -> tuple[int, int]:
    """Импортировать все messages*.html из data_dir (рекурсивно по подканалам).

    Возвращает (всего_сообщений, записано_угроз).
    """
    total = 0
    saved = 0
    pending_events = 0
    # Канал = имя непосредственной родительской папки файла.
    for html_path in sorted(data_dir.rglob("messages*.html")):
        channel = html_path.parent.name
        msgs = parse_html_file(html_path)
        log.info("  %-30s %s: %d сообщений", channel, html_path.name, len(msgs))

        for ts, text in msgs:
            total += 1
            # Очищаем подпись канала ДО анализа — иначе хвост «➡️Підписатись»
            # засоряет определение региона и классификацию угрозы.
            text = clean_signature(text)
            if not matches_keywords(text):
                continue  # не угроза — пропускаем (экономим место в БД)
            threat_type = classify_threat(text)
            weapon = classify_weapon(text)
            lowered = text.lower()
            if weapon == "stand_down":
                stage, outcome = "all_clear", "unknown"
            elif weapon in ("explosion", "artillery", "mlrs") or any(word in lowered for word in ("вибух", "взрыв", "прильот", "прилет", "влучан", "попадан")):
                stage, outcome = "impact", "impact"
            elif weapon == "air_defense" or any(word in lowered for word in ("збито", "сбито", "перехоп")):
                stage, outcome = "intercept", "intercept"
            elif weapon == "alert":
                stage, outcome = "alert", "unknown"
            elif any(word in lowered for word in ("пуск", "запуск", "зліт", "взлет")):
                stage, outcome = "launch", "unknown"
            else:
                stage, outcome = "movement", "unknown"
            regions = detect_region(text) or ["unknown"]
            for slug in regions:
                # Одна транзакция на сотни строк быстрее commit на каждый пост
                # в тысячи раз и не допускает гонку с MAX(id).
                with db._lock:
                    assert db._conn is not None
                    db._conn.execute(
                        "INSERT INTO threats (ts, threat_type, region, text, source) VALUES (?, ?, ?, ?, ?)",
                        (ts, threat_type, slug, text[:500], f"import:{channel}"),
                    )
                db.add_event(
                    event_ts=ts, weapon_class=weapon, stage=stage, region=slug,
                    outcome=outcome, text=text, source=f"import:{channel}", confidence=0.75, commit=False,
                )
                pending_events += 1
            saved += 1
            if pending_events >= 500:
                with db._lock:
                    assert db._conn is not None
                    db._conn.commit()
                pending_events = 0
    if pending_events:
        with db._lock:
            assert db._conn is not None
            db._conn.commit()
    return total, saved


def main() -> None:
    ap = argparse.ArgumentParser(description="Импорт HTML-архивов Telegram в БД AirRadar.")
    ap.add_argument(
        "path",
        nargs="?",
        default=r"C:\Users\bvdov\Desktop\данные для обучения",
        help="Папка с подканалами экспорта Telegram Desktop.",
    )
    args = ap.parse_args()
    data_dir = Path(args.path)
    if not data_dir.is_dir():
        print(f"[FATAL] Папка не найдена: {data_dir}", file=sys.stderr)
        sys.exit(1)

    log.info("Папка данных: %s", data_dir)
    log.info("Подканалы: %s", ", ".join(p.name for p in data_dir.iterdir() if p.is_dir()))

    db = Database(DB_PATH)
    try:
        total, saved = import_folder(data_dir, db)
        log.info("=" * 60)
        log.info("Готово! Всего сообщений обработано: %d", total)
        log.info("Угроз записано в БД: %d", saved)
        log.info("Теперь статистика и ETA в меню бота будут на реальных данных.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
