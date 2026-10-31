import gzip

import pytest

from airradar.product import geo_quality as g
import shelters


@pytest.fixture(scope="module")
def bounds():
    return g.Boundaries()


def test_locate_known_points(bounds):
    assert bounds.locate(50.45, 30.52)[0] in ("Київ", "Київська")       # Київ
    assert bounds.locate(49.84, 24.03)[0] == "Львівська"                # Львів
    assert bounds.locate(47.02, 28.86) is None                          # Кишинів (Молдова)
    assert bounds.locate(45.68, 23.79) is None                          # Румунія
    assert bounds.locate(52.3, 21.0) is None                            # Варшава


def test_bbox_and_nan():
    assert g.in_bbox(50, 30) and not g.in_bbox(60, 30) and not g.in_bbox(float("nan"), 30)


def test_shelter_check_flags_foreign_bad_and_duplicates(tmp_path, bounds):
    f = tmp_path / "s.tsv.gz"
    rows = ["50.4501\t30.5234\tукриття\tA\tвул. 1", "50.4501\t30.5234\tукриття\tA\tвул. 1",   # дубль
            "47.0105\t28.8638\tукриття\tChisinau\t", "abc\t30\tукриття\tX\t", "50.0\t30.0"]
    with gzip.open(f, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(rows) + "\n")
    r = g.check_shelters(f, bounds)
    assert (r["valid"], r["duplicates"], r["outside_ukraine"], r["bad_format"]) == (1, 1, 1, 2)


def test_gazetteer_check_counts(tmp_path):
    f = tmp_path / "g.tsv.gz"
    with gzip.open(f, "wt", encoding="utf-8") as fh:
        fh.write("Березівка\tkyivska\t50.1\t30.1\t100\nБерезівка\tkyivska\t50.1001\t30.1001\t100\n"
                 "Березівка\todeska\t47.0\t30.0\t50\nХ\todeska\t10\t10\t1\nбитий\tрядок\n")
    r = g.check_gazetteer(f)
    assert r["duplicates_near"] == 1 and r["homonyms_multi_oblast"] == 1 and r["out_of_bbox"] == 1 and r["bad_format"] == 1


def test_shipped_clean_shelters_have_no_foreign_points(bounds):
    clean = g.ROOT / "data" / "shelters_ua.clean.tsv.gz"
    assert clean.exists()
    assert not shelters._load_local() or shelters._load_local()[0][0] > 0
    foreign = [(la, lo) for la, lo, *_ in shelters._load_local() if bounds.locate(la, lo) is None and not bounds.near_boundary(la, lo)]
    assert foreign == []
    assert shelters.local_nearest(50.4501, 30.5234, limit=3, max_m=3000)   # Київ покритий


def test_real_gazetteer_is_sane():
    r = g.check_gazetteer()
    assert r["bad_format"] == 0 and r["out_of_bbox"] == 0
