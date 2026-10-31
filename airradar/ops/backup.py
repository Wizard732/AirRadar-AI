"""backup.py — повна резервна копія AirRadar: БД, конфіг без секретів, Telegram-сесія, критичні файли.

Архів: airradar-backup-YYYYmmdd-HHMMSS.tar.gz[.enc] + manifest.json (sha256 кожного файлу).
  * БД копіюється онлайн через sqlite backup API (без зупинки бота, WAL-безпечно);
  * .env зберігається з ВИТЕРТИМИ значеннями секретів (ключі лишаються для відновлення структури);
  * файли Telethon-сесії потрапляють в архів ЛИШЕ якщо задано BACKUP_PASSPHRASE (архів тоді шифрується
    AES-256 через openssl); без пароля сесія пропускається з попередженням — секрети не лежать у відкритому вигляді;
  * права каталогу 0700, файлів 0600; ротація за кількістю та сумарним розміром; перевірка цілісності.
CLI: python -m airradar.ops.backup create | verify <file> | list | restore-test <file> | restore <file> <target_dir>
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

SECRET_KEY = re.compile(r"(TOKEN|KEY|SECRET|PASSWORD|PASS|HASH|API_ID|PRIVATE|PASSPHRASE)", re.I)
STATE_FILE = "last_backup.json"
CRITICAL_PATHS = ["data", "miniapp/vapid.json", "airradar-bot.service", "airradar-backup.service", "airradar-backup.timer",
                  "requirements.txt", "deploy_prod.sh", ".env.example"]
SESSION_GLOBS = ["*.session", "*.session-journal"]


def backup_dir() -> Path:
    return Path(os.getenv("BACKUP_DIR", "/var/backups/airradar"))


def settings() -> dict:
    return {
        "keep": int(os.getenv("BACKUP_KEEP", "7")),
        "max_total_mb": int(os.getenv("BACKUP_MAX_TOTAL_MB", "5000")),
        "max_single_mb": int(os.getenv("BACKUP_MAX_SINGLE_MB", "2000")),
        "passphrase": os.getenv("BACKUP_PASSPHRASE", ""),
    }


def sqlite_online_copy(src: Path, dst: Path) -> None:
    """Консистентна копія через backup API (працює при активному записі)."""
    s = sqlite3.connect(str(src), timeout=60)
    d = sqlite3.connect(str(dst))
    try:
        with d:
            s.backup(d)
    finally:
        d.close(); s.close()


def redact_env(text: str) -> str:
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$", line)
        if m and SECRET_KEY.search(m.group(1)) and m.group(2).strip():
            out.append(f"{m.group(1)}=<REDACTED>")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _openssl(mode: str, src: Path, dst: Path, passphrase: str) -> None:
    flag = "-e" if mode == "enc" else "-d"
    env = dict(os.environ, AR_BACKUP_PW=passphrase)
    subprocess.run(["openssl", "enc", flag, "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-in", str(src), "-out", str(dst),
                    "-pass", "env:AR_BACKUP_PW"], check=True, env=env, capture_output=True)


def create_backup(project_dir: Path, db_path: Path, out_dir: Path | None = None, *, session_name: str = "airradar",
                  keep: int | None = None, passphrase: str | None = None, max_single_mb: int | None = None,
                  now: float | None = None) -> dict:
    cfg = settings()
    out_dir = out_dir or backup_dir()
    keep = keep if keep is not None else cfg["keep"]
    passphrase = cfg["passphrase"] if passphrase is None else passphrase
    max_single_mb = max_single_mb if max_single_mb is not None else cfg["max_single_mb"]
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime((time.time() if now is None else now)))
    warnings: list[str] = []
    with tempfile.TemporaryDirectory(dir=out_dir) as td:
        stage = Path(td) / "stage"
        (stage / "files").mkdir(parents=True)
        # 1) БД (онлайн-копія + перевірка цілісності копії)
        db_copy = stage / "files" / "airradar.db"
        sqlite_online_copy(db_path, db_copy)
        c = sqlite3.connect(str(db_copy))
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
        c.close()
        if integrity != "ok":
            raise RuntimeError(f"integrity_check копії БД: {integrity[:100]}")
        # 2) конфіг без секретів
        env_file = project_dir / ".env"
        if env_file.exists():
            (stage / "files" / "env.redacted").write_text(redact_env(env_file.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
        # 3) критичні файли / локальні дані (укриття, gazetteer тощо)
        for rel in CRITICAL_PATHS:
            p = project_dir / rel
            if p.is_dir():
                shutil.copytree(p, stage / "files" / rel, dirs_exist_ok=True)
            elif p.is_file():
                (stage / "files" / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, stage / "files" / rel)
        # 4) Telegram-сесія — тільки в зашифрованому архіві
        sessions = []
        for g in SESSION_GLOBS:
            sessions += list(project_dir.glob(f"{session_name}{g[1:]}"))
        sessions += [p for p in [project_dir / "vapid_private.pem"] if p.exists()]   # без нього Web Push-підписки перестануть працювати
        if sessions:
            if passphrase:
                for sp in sessions:
                    shutil.copy2(sp, stage / "files" / sp.name)
            else:
                warnings.append("Telegram-сесію пропущено: задайте BACKUP_PASSPHRASE, щоб включати її в зашифрований архів")
        # 5) маніфест
        files = {}
        for p in sorted((stage / "files").rglob("*")):
            if p.is_file():
                files[str(p.relative_to(stage / "files"))] = {"sha256": sha256_file(p), "size": p.stat().st_size}
        manifest = {"created": int((time.time() if now is None else now)), "db_integrity": integrity, "files": files,
                    "encrypted": bool(passphrase), "has_session": any(n.endswith(".session") for n in files), "has_vapid": "vapid_private.pem" in files, "warnings": warnings}
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        tar_path = Path(td) / "a.tar.gz"
        with tarfile.open(tar_path, "w:gz", compresslevel=4) as tf:
            tf.add(stage / "manifest.json", arcname="manifest.json")
            tf.add(stage / "files", arcname="files")
        final = out_dir / f"airradar-backup-{ts}.tar.gz"
        if passphrase:
            final = final.with_name(final.name + ".enc")
            _openssl("enc", tar_path, final, passphrase)
        else:
            shutil.move(str(tar_path), final)
    os.chmod(final, 0o600)
    size_mb = final.stat().st_size / 1_000_000
    if size_mb > max_single_mb:
        warnings.append(f"Розмір копії {size_mb:.0f} МБ перевищує ліміт {max_single_mb} МБ")
    removed = rotate(out_dir, keep, settings()["max_total_mb"])
    result = {"path": str(final), "size_mb": round(size_mb, 1), "sha256": sha256_file(final), "warnings": warnings,
              "rotated": removed, "ts": int((time.time() if now is None else now)), "encrypted": bool(passphrase)}
    (out_dir / STATE_FILE).write_text(json.dumps({"ts": result["ts"], "path": result["path"], "ok": True}), encoding="utf-8")
    return result


def list_backups(out_dir: Path | None = None) -> list[Path]:
    out_dir = out_dir or backup_dir()
    return sorted(set(out_dir.glob("airradar-backup-*.tar.gz")) | set(out_dir.glob("airradar-backup-*.tar.gz.enc")))


def rotate(out_dir: Path, keep: int, max_total_mb: int) -> list[str]:
    items = sorted(set(list_backups(out_dir)), key=lambda p: p.name)
    removed = []
    while len(items) > max(1, keep):
        removed.append(items.pop(0).name); (out_dir / removed[-1]).unlink()
    while len(items) > 1 and sum(p.stat().st_size for p in items) / 1_000_000 > max_total_mb:
        removed.append(items.pop(0).name); (out_dir / removed[-1]).unlink()
    return removed


def last_backup_age_s(now: float | None = None, out_dir: Path | None = None) -> int | None:
    """Вік останнього успішного backup у секундах; None = невідомо (ще не було/каталог недоступний)."""
    try:
        data = json.loads(((out_dir or backup_dir()) / STATE_FILE).read_text(encoding="utf-8"))
        return int(((time.time() if now is None else now)) - data["ts"])
    except (OSError, ValueError, KeyError):
        return None


def _extract(archive: Path, dest: Path, passphrase: str) -> None:
    src = archive
    tmp = None
    if archive.name.endswith(".enc"):
        if not passphrase:
            raise RuntimeError("архів зашифрований: потрібен BACKUP_PASSPHRASE")
        tmp = dest / "_dec.tar.gz"
        _openssl("dec", archive, tmp, passphrase)
        src = tmp
    with tarfile.open(src, "r:gz") as tf:
        for m in tf.getmembers():  # захист від path traversal
            if m.name.startswith("/") or ".." in Path(m.name).parts or m.issym() or m.islnk():
                raise RuntimeError(f"небезпечний шлях в архіві: {m.name}")
        tf.extractall(dest, filter="data")
    if tmp:
        tmp.unlink()


def verify_backup(archive: Path, passphrase: str | None = None) -> dict:
    """Розпакувати у тимчасову теку, звірити sha256 з маніфестом і зробити PRAGMA integrity_check БД."""
    passphrase = settings()["passphrase"] if passphrase is None else passphrase
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _extract(archive, d, passphrase)
        manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        bad = [n for n, meta in manifest["files"].items() if not (d / "files" / n).is_file() or sha256_file(d / "files" / n) != meta["sha256"]]
        con = sqlite3.connect(str(d / "files" / "airradar.db"))
        try:
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            tables = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        finally:
            con.close()
    return {"ok": not bad and integrity == "ok" and tables > 0, "bad_files": bad, "integrity": integrity, "tables": tables,
            "files": len(manifest["files"]), "has_session": manifest.get("has_session", False)}


def restore(archive: Path, target_dir: Path, passphrase: str | None = None, *, with_session: bool = False) -> list[str]:
    """Відновити у target_dir (НЕ перезаписує існуючі файли). БД → airradar.db; сесія лише з with_session=True."""
    passphrase = settings()["passphrase"] if passphrase is None else passphrase
    target_dir.mkdir(parents=True, exist_ok=True)
    restored = []
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _extract(archive, d, passphrase)
        for p in sorted((d / "files").rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(d / "files")
            if rel.name == "env.redacted":
                continue  # секрети відновлюються вручну з менеджера паролів
            if rel.suffix.startswith(".session") or rel.name.endswith(".session-journal") or rel.name == "vapid_private.pem":
                if not with_session:
                    continue
            dst = target_dir / rel
            if dst.exists():
                dst = dst.with_name(dst.name + ".restored")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            restored.append(str(rel))
    return restored


def restore_test(archive: Path, passphrase: str | None = None) -> dict:
    """Відновлення в ізольовану тимчасову теку + відкриття БД штатним Database і перевірка міграцій."""
    with tempfile.TemporaryDirectory() as td:
        files = restore(archive, Path(td), passphrase)
        from airradar.ops import migrations
        from database import Database
        db = Database(str(Path(td) / "airradar.db"))
        try:
            migrations.apply_pending(db)
            with db._lock:  # noqa: SLF001
                ok = db._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"  # noqa: SLF001
        finally:
            db.close()
    return {"ok": ok, "restored": len(files)}


def main(argv: list[str]) -> int:
    from config import load_settings
    cmd = argv[1] if len(argv) > 1 else "create"
    if cmd == "create":
        s = load_settings()
        try:
            res = create_backup(Path.cwd(), Path(s.db_path), session_name=s.session_name)
            v = verify_backup(Path(res["path"]))
            print(f"ok {Path(res['path']).name} {res['size_mb']} MB verify={v['ok']} warnings={res['warnings']}")
            if not v["ok"]:
                _notify(f"🚨 Резервна копія створена, але не пройшла перевірку цілісності: {v['bad_files'][:3]} {v['integrity'][:60]}")
                return 1
            if os.getenv("BACKUP_NOTIFY_SUCCESS", "1") == "1":
                _notify(f"✅ Резервна копія створена і перевірена: {res['size_mb']} МБ" + (f" ⚠️ {'; '.join(res['warnings'])}" if res["warnings"] else ""))
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"BACKUP FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            _notify(f"🚨 Резервна копія не створена: {type(exc).__name__}: {str(exc)[:150]}")
            return 1
    if cmd == "verify":
        print(json.dumps(verify_backup(Path(argv[2])))); return 0
    if cmd == "restore-test":
        print(json.dumps(restore_test(Path(argv[2])))); return 0
    if cmd == "restore":
        print(json.dumps(restore(Path(argv[2]), Path(argv[3]), with_session="--with-session" in argv))); return 0
    if cmd == "list":
        for p in list_backups():
            print(p.name, p.stat().st_size // 1_000_000, "MB")
        return 0
    return 2


def _notify(msg: str) -> None:
    """Повідомлення адміну про результат backup через Bot API (без Telethon-сесії). Токен не логуємо."""
    token, admin = os.getenv("BOT_TOKEN", ""), os.getenv("ADMIN_ID", "")
    if not (token and admin):
        return
    try:
        import requests
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": admin, "text": msg[:500]}, timeout=15)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    sys.exit(main(sys.argv))
