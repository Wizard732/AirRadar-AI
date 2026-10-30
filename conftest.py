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
