"""media_ocr.py — распознавание текста с фото в постах (ТЗ 5.1).

Многие посты в новостных/военных каналах — это скриншоты или фото документов
с текстом. Раньше бот брал только подпись под фото; текст с самого
изображения терялся. Этот модуль скачивает фото из поста и распознаёт текст
через Groq vision-модель (Llama 4 Scout).

Используется в обоих конвейерах (military + interests): если в посте есть
фото, OCR-текст добавляется к подписи, и дальше пост идёт как обычный текст.
"""

from __future__ import annotations

import logging

import aiohttp

from ai_summarizer import ocr_image

logger = logging.getLogger(__name__)

# Лимит размера фото для OCR (Groq ограничивает). Слишком большие пропускаем.
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 МБ


async def extract_text_with_ocr(
    event,
    http_session: aiohttp.ClientSession,
    groq_api_key: str,
    vision_model: str,
    timeout: int = 30,
) -> str:
    """Если в посте есть фото — распознать с него текст и дописать к подписи.

    Возвращает полный текст (подпись + OCR) или просто подпись, если фото нет
    или OCR не сработал. Не падает при ошибках.
    """
    message = getattr(event, "message", None)
    if message is None:
        return ""

    caption = (message.text or message.message or "").strip()
    # Empty means OCR is intentionally disabled in configuration.
    if not vision_model.strip():
        return caption

    # Есть ли фото в сообщении?
    photo = getattr(message, "photo", None)
    if photo is None:
        return caption  # фото нет — отдаём подпись как есть

    try:
        # Скачиваем самое большое доступное фото.
        client = getattr(event, "_client", None) or getattr(event, "client", None)
        if client is None:
            return caption
        buf = await client.download_media(photo, bytes)
        if not buf or len(buf) > MAX_IMAGE_BYTES:
            logger.debug("OCR: фото слишком большое или пустое, пропускаем")
            return caption

        # MIME-тип: Telegram фото — всегда jpeg.
        mime = "image/jpeg"
        ocr_text = await ocr_image(
            image_bytes=buf,
            mime_type=mime,
            api_key=groq_api_key,
            model=vision_model,
            session=http_session,
            timeout=timeout,
        )
        if ocr_text:
            logger.info("OCR: распознано %d символов с фото", len(ocr_text))
            # Объединяем: подпись + распознанный текст.
            if caption:
                return f"{caption}\n{ocr_text}".strip()
            return ocr_text.strip()
        return caption
    except Exception as exc:  # noqa: BLE001
        logger.warning("OCR: сбой распознавания: %s", exc)
        return caption
