"""Політика помилок: `except Exception: pass` без логування заборонено (етап 12). Подавлення можливе лише з logger.*(..., exc_info=True)."""
import ast
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
SKIP = {".venv", "node_modules", "__pycache__", "android", "ios", "tests_data"}


def _silent_handlers():
    found = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for f in files:
            if not f.endswith(".py") or f.startswith("test_") or f == "conftest.py":
                continue
            path = os.path.join(root, f)
            tree = ast.parse(open(path, encoding="utf-8").read())
            for n in ast.walk(tree):
                if isinstance(n, ast.ExceptHandler) and isinstance(n.type, ast.Name) and n.type.id in ("Exception", "BaseException"):
                    if all(isinstance(b, ast.Pass) or (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant)) for b in n.body):
                        found.append(f"{os.path.relpath(path, ROOT)}:{n.lineno}")
    return found


def test_no_silent_broad_except():
    assert _silent_handlers() == [], "Широкий except без логування: додайте logger.*(..., exc_info=True) або звузьте тип"


def test_bare_except_forbidden():
    bad = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for f in files:
            if f.endswith(".py") and not f.startswith("test_"):
                p = os.path.join(root, f)
                for n in ast.walk(ast.parse(open(p, encoding="utf-8").read())):
                    if isinstance(n, ast.ExceptHandler) and n.type is None:
                        bad.append(f"{os.path.relpath(p, ROOT)}:{n.lineno}")
    assert bad == []
