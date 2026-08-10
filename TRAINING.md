# Датасет и дообучение AirRadar

Цель модели: преобразовать сырой Telegram-пост в одно короткое украинское
оперативное сообщение. Модель не должна добавлять тип оружия, город, маршрут,
количество или последствия, которых нет в источнике.

## 1. Пилотный набор

Сначала создай небольшой набор кандидатов — он не меняет исходный HTML-архив:

```bash
python prepare_training_dataset.py candidates "C:/Users/bvdov/Desktop/дані для навчання" --limit 1000
```

Если папка называется `данные для обучения`, укажи её точное имя или запусти без
пути: это путь по умолчанию. Результаты появятся в игнорируемой Git-папке
`training_data/`:

- `candidates.jsonl` — кандидаты для ИИ-разметки;
- `rejected.jsonl` — исключённые сообщения с причиной;
- `candidate_report.json` — статистика.

## 2. ИИ-разметка

Для локальной Ollama нужна модель, способная строго возвращать JSON:

```bash
ollama pull qwen2.5:7b
python label_training_candidates.py training_data/candidates.jsonl --backend ollama
```

Или используй Groq (ключ `GROQ_API_KEY` должен быть в окружении / `.env`):

```bash
python label_training_candidates.py training_data/candidates.jsonl --backend groq --delay 0.2
```

Скрипт пишет каждую строку сразу в `llm_labels.jsonl`; после остановки его можно
безопасно запустить повторно. Перед полной разметкой открой первые 100–200 строк
и проверь, что `target` не выдумывает факты.

## 3. Валидация и разбиение

```bash
python prepare_training_dataset.py finalize training_data/llm_labels.jsonl
python -m unittest test_training_pipeline.py
```

Получатся `train.jsonl`, `validation.jsonl`, `test.jsonl`, а также очереди
`needs_review.jsonl` и `rejected.jsonl`. Дубли не пересекают разбиения.

## 4. Дообучение на сервере с P100 16 GB

Для 7B-модели используйте Linux + NVIDIA driver/CUDA и QLoRA. Рекомендуемая
база: `Qwen/Qwen2.5-7B-Instruct`; P100 требует `fp16`, не `bf16`.

```bash
pip install "torch" transformers datasets peft trl accelerate bitsandbytes
```

Преобразуйте каждую строку в формат диалога:

```json
{"messages":[
  {"role":"system","content":"Ти військовий редактор. Передавай лише факти з повідомлення."},
  {"role":"user","content":"<input>"},
  {"role":"assistant","content":"<target>"}
]}
```

Начальные параметры QLoRA: 4-bit NF4, `r=16`, `alpha=32`, batch size 1–2,
gradient accumulation 16–32, max sequence length 512, 2 эпохи,
learning rate `1e-4`. На P100 ориентир для десятков тысяч коротких примеров —
примерно 4–12 часов. Сначала обучите на пилоте и оцените exact/semantic качество
на `test.jsonl`; не используйте test-набор для подбора параметров.

RX 6600 под Windows для такого 7B QLoRA не рекомендуется: CUDA-стек недоступен,
а 8 GB VRAM и 16 GB RAM недостаточны для комфортного процесса. Для разметки на
ПК можно использовать Ollama, а само обучение — P100-сервер.
