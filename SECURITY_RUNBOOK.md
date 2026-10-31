# Серверна безпека (P0) — кроки для власника

Ці кроки потребують доступу до хоста й виконуються вами (пароль у чат не вставляти).

1. **Змінити пароль root** (він світився в чаті): `ssh root@<host>` → `passwd`.
2. **Перейти на SSH-ключі**: на Mac `ssh-keygen -t ed25519`, `ssh-copy-id root@<host>`, перевірити вхід без пароля;
   потім у `/etc/ssh/sshd_config`: `PasswordAuthentication no`, `PermitRootLogin prohibit-password`, `systemctl reload ssh`.
3. **Приватний git-репозиторій**: на GitHub створити Private repo; перед першим пушем `python secrets_check.py`
   (ключі підпису `*.jks`, `keystore.properties`, `.env`, `*.session` вже в `.gitignore`).
4. **Секрети**: `.env`, Telethon-сесія та Android keystore — зберігати окремо (менеджер паролів / зашифрований бекап);
   втрата keystore = неможливість оновити APK.
5. **Проміжна перевірка**: `bash deploy_prod.sh` (тести → архів без секретів → CT 100 → перезапуск).
6. **Реальний телефон**: встановити APK, `/pair КОД`, у боті `/selftest` — прийде тест у Telegram і в Android (канал «Тривога»).
7. На проді в `.env`: `ADMIN_ID=<ваш Telegram ID>`, за потреби `TRACKS_PUBLIC_DELAY_MIN` (затримка публічної карти).
