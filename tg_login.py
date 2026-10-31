"""tg_login.py — ручной вход в Telegram-аккаунт для читающей сессии.

Запуск на сервере: cd /opt/airradar && .venv/bin/python tg_login.py
Код приходит в приложение Telegram (чат «Telegram», 777000) на другом устройстве.
Не пришёл — на запрос кода введи `resend`: Telegram переключит доставку (SMS/звонок).
"""

import asyncio
import getpass

from dotenv import dotenv_values
from telethon import TelegramClient
from telethon.errors import PhoneCodeInvalidError, SessionPasswordNeededError
from telethon.tl.functions.auth import ResendCodeRequest

env = dotenv_values(".env")


async def qr_login(client: TelegramClient) -> None:
    """Вход без кода: Telegram на телефоне → Настройки → Устройства → Подключить устройство → скан QR."""
    import qrcode

    qr = await client.qr_login()
    while True:
        code = qrcode.QRCode(border=1)
        code.add_data(qr.url)
        code.make()
        print("\nОтсканируй QR в Telegram (Настройки → Устройства → Подключить устройство). Обновляется каждые ~30 с.")
        code.print_ascii(invert=True)
        try:
            await qr.wait(timeout=25)
            break
        except asyncio.TimeoutError:
            await qr.recreate()
        except SessionPasswordNeededError:
            await client.sign_in(password=getpass.getpass("Облачный пароль (2FA): "))
            break
    me = await client.get_me()
    print("Готово, вход выполнен:", me.first_name)
    await client.disconnect()


async def main() -> None:
    client = TelegramClient(env.get("SESSION_NAME", "airradar"), int(env["TG_API_ID"]), env["TG_API_HASH"])
    await client.connect()
    if await client.is_user_authorized():
        print("Уже авторизован.")
        await client.disconnect()
        return
    if input("Способ входа: 1 — код, 2 — QR из приложения Telegram [1/2]: ").strip() == "2":
        await qr_login(client)
        return
    raw = input("Телефон (+380...): ")
    phone = "+" + "".join(ch for ch in raw if ch.isdigit())  # убираем пробелы, скобки, невидимые символы
    print("Использую номер:", phone)
    sent = await client.send_code_request(phone)
    print(f"Код отправлен способом: {type(sent.type).__name__} (следующий: {type(sent.next_type).__name__ if sent.next_type else '—'})")
    while True:
        code = input("Код (или `resend`): ").strip().replace(" ", "")
        if code == "resend":
            sent = await client(ResendCodeRequest(phone, sent.phone_code_hash))
            print(f"Повторная отправка: {type(sent.type).__name__}")
            continue
        try:
            await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
            break
        except PhoneCodeInvalidError:
            print("Неверный код, ещё раз.")
        except SessionPasswordNeededError:
            await client.sign_in(password=getpass.getpass("Облачный пароль (2FA): "))
            break
    me = await client.get_me()
    print("Готово, вход выполнен:", me.first_name)
    await client.disconnect()


asyncio.run(main())
