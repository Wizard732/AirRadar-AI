"""Скидаємо глобальний дедуп публікацій між тестами, щоб один тест не «з'їдав» пост іншого."""
import sys

import pytest


@pytest.fixture(autouse=True)
def _fresh_publish_dedup():
    mod = sys.modules.get("main")
    if mod is not None and hasattr(mod, "_DEDUP"):
        from publish_dedup import PublishDedup
        mod._DEDUP = PublishDedup()
    yield


# Полный журнал падений: имя теста, длительность, вывод и итоговый exit code — для редких нестабильных сбоев.
_FAIL_LOG = "pytest-failures.log"


def pytest_runtest_logreport(report):
    if report.when in ("setup", "call", "teardown") and report.failed:
        import time as _t
        with open(_FAIL_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{_t.strftime('%Y-%m-%d %H:%M:%S')}] {report.nodeid} ({report.when}, {report.duration:.2f}s)\n{report.longreprtext}\n{'-' * 60}\n")


def pytest_sessionfinish(session, exitstatus):
    if exitstatus != 0:
        import time as _t
        with open(_FAIL_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{_t.strftime('%Y-%m-%d %H:%M:%S')}] сесія завершилась з exit code {int(exitstatus)}\n")
