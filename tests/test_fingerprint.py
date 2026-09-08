import pytest

from smartops_desktop import fingerprint as fp


def test_every_layer_is_present_even_when_nothing_was_captured():
    result = fp.normalize({})
    assert list(result["layers"]) == list(fp.LAYER_KEYS)
    assert all(v["status"] == fp.UNAVAILABLE for v in result["layers"].values())
    assert fp.score(result)["identified"] == 0


def test_an_unknown_layer_or_status_never_enters_the_fingerprint():
    result = fp.normalize({"layers": {"web": {"status": "brilliant"}, "made_up": {"status": fp.FOUND}}})
    assert "made_up" not in result["layers"]
    assert result["layers"]["web"]["status"] == fp.UNAVAILABLE


def test_page_supplied_data_is_bounded():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": "too deep"}}}}}}}
    result = fp.normalize({"source": "x" * 5000, "layers": {"web": {"status": fp.FOUND, "detail": "y" * 5000,
                                                                   "data": {"text": "z" * 5000, "items": list(range(500)), "nested": deep}}}})
    web = result["layers"]["web"]
    assert len(result["source"]) <= fp.MAX_TEXT
    assert len(web["detail"]) <= fp.MAX_TEXT
    assert len(web["data"]["text"]) <= fp.MAX_TEXT
    assert len(web["data"]["items"]) <= fp.MAX_ITEMS


def test_radar_reports_one_row_per_layer_in_capture_order():
    rows = fp.radar({"layers": {"web": {"status": fp.FOUND, "detail": "button · id save"},
                                "keyboard": {"status": fp.MISSING, "detail": "Not reachable by Tab."}}})
    assert [row["key"] for row in rows] == list(fp.LAYER_KEYS)
    assert rows[0]["icon"] == "✅" and rows[0]["detail"] == "button · id save"
    assert next(r for r in rows if r["key"] == "keyboard")["icon"] == "❌"
    assert next(r for r in rows if r["key"] == "windows")["icon"] == "⚪"


@pytest.mark.parametrize("found,confident", [([], False), (["web"], False), (["web", "anchor"], True)])
def test_confidence_needs_more_than_one_independent_layer(found, confident):
    result = fp.score({"layers": {key: {"status": fp.FOUND, "detail": key} for key in found}})
    assert result["confident"] is confident
    assert result["identified"] == len(found)
    assert result["headline"].startswith(f"{len(found)} of {len(fp.LAYER_KEYS)}")
