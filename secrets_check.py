"""secrets_check.py — пошук секретів у проєкті перед комітом/публікацією. Значення НЕ друкуються (лише файл і тип).

Запуск: python secrets_check.py [DIR]   (код 1 — знайдено підозріле)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SKIP_DIRS = {".venv", "node_modules", "__pycache__", ".git", "build", ".gradle"}
SKIP_SUFFIX = {".gz", ".png", ".jpg", ".apk", ".jar", ".ico", ".webp", ".pyc", ".db", ".lock"}
PATTERNS = {
    "telegram_bot_token": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "groq_key": re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "assigned_secret": re.compile(r"(?i)\b(?:password|passwd|api_hash|secret|token)\s*[=:]\s*['\"]?[A-Za-z0-9_\-/+]{16,}"),
}
SENSITIVE_FILES = (".env", ".session", ".jks", "keystore.properties", "service-account")


def scan(root: Path) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for p in root.rglob("*"):
        if not p.is_file() or SKIP_DIRS & set(p.parts) or p.suffix in SKIP_SUFFIX:
            continue
        name = p.name
        if name != ".env.example" and any(name == s or name.endswith(s) for s in SENSITIVE_FILES):
            hits.append((str(p.relative_to(root)), "sensitive_file_present"))
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if name.startswith("test_") or name == "secrets_check.py" or name.endswith(".md"):
            continue
        for kind, rx in PATTERNS.items():
            if rx.search(text):
                hits.append((str(p.relative_to(root)), kind))
    return hits


def gitignore_gaps(root: Path) -> list[str]:
    gi = (root / ".gitignore").read_text(encoding="utf-8") if (root / ".gitignore").exists() else ""
    need = [".env", "*.session", "*.jks", "keystore.properties", "*.db"]
    return [n for n in need if n not in gi]


if __name__ == "__main__":
    base = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    found = scan(base)
    for f, k in found:
        print(f"{k}: {f}")
    gaps = gitignore_gaps(base)
    if gaps:
        print("gitignore не покриває:", ", ".join(gaps))
    sys.exit(1 if found or gaps else 0)
