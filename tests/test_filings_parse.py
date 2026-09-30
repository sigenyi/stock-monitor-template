"""Model-output parsing and type normalization."""

import json

import pytest

from filings import extraction as ex


@pytest.mark.parametrize("raw, expected", [
    (3.9, 3.9),
    (-2, -2),
    (0, 0),
    ("3.9%", 3.9),
    ("-2%", -2.0),
    ("+1.5%", 1.5),
    ("$786.1", 786.1),
    ("-$12.5", -12.5),
    ("1,234.5", 1234.5),
    ("$1,234", 1234.0),
    ("2.4x", 2.4),
    ("2.4 X", 2.4),
    ("(3.1)", -3.1),
    ("(3.1%)", -3.1),
    ("($786.1)", -786.1),
    # Accounting style: unit / dollar sign outside the parentheses.
    ("(3.1)%", -3.1),
    ("(1.5) %", -1.5),
    ("$(786.1)", -786.1),
    ("$ (1,234.5)", -1234.5),
    ("(2.4)x", -2.4),
    (" 7 ", 7.0),
    (".5", 0.5),
    ("−1.2%", -1.2),  # unicode minus sign
])
def test_num_coerces_numbers(raw, expected):
    assert ex._num(raw) == expected


@pytest.mark.parametrize("raw", [
    True, False, None, "", "n/a", "about 4%", "3.9% organic", "1,23", "4-5%",
    [3.9], {"value": 3.9}, float("nan"), float("inf"),
    "()", "(3.1%)%", "(3.1)%%", "(n/a)", "3.1)", "(3.1",
])
def test_num_rejects_non_numbers(raw):
    assert ex._num(raw) is None


def test_num_handles_integers_beyond_float_range():
    # json.loads keeps a huge integer literal as an int; math.isfinite would
    # raise OverflowError on it. Treated like 1e400 (inf): rejected, no crash.
    assert ex._num(10**308) == 10**308  # still representable -> kept
    for raw in (10**309, 10**400, -(10**400), "1" + "0" * 400, "-" + "9" * 400):
        assert ex._num(raw) is None


@pytest.mark.parametrize("raw, expected", [
    (True, True), (False, False),
    ("true", True), ("TRUE", True), (" Yes ", True),
    ("false", False), ("No", False),
    ("maybe", None), ("", None), (1, None), (0, None), (None, None),
])
def test_bool_coercion(raw, expected):
    assert ex._bool(raw) is expected


def _raw(**entries) -> str:
    return json.dumps(entries)


def test_parse_normalizes_every_key_and_types():
    raw = "```json\n" + _raw(
        organic_growth_core_pct={"found": True, "value": "3.9%", "quote": "grew 3.9%",
                                 "location": "p1"},
        total_reported_revenue_change_pct={"found": True, "value": "(1.2)"},
        perimeter_changed={"found": True, "value": "no"},
        adj_margin_pct={"found": "true", "value": 15.2},
        buyback_suspension_language={"found": True, "value": "maybe"},
        shares_outstanding_m={"found": True, "value": "about 280"},
        leverage_sentence={"found": True, "value": "  Leverage was 2.5x.  "},
        leverage_ratio_x={"found": True, "value": "2.5x"},
        goodwill_impairment_charge={"found": True,
                                    "value": {"impaired": "yes", "amount_m": "$1,200"}},
        guidance_language={"found": True, "value": {"softened": "nope",
                                                    "assessment": "Reaffirmed."}},
        book_equity_b={"found": True, "value": True},
        group_organic_pct={"found": True, "value": None},
        extra_key={"found": True, "value": 1},
    ) + "\n```"
    data = ex.parse_extraction_json(raw, ex.SCHEMA_KEYS)
    assert list(data) == ex.SCHEMA_KEYS  # only schema keys, in order

    for entry in data.values():
        assert {"found", "value", "quote", "location"} <= set(entry)

    assert data["organic_growth_core_pct"] == {
        "found": True, "value": 3.9, "quote": "grew 3.9%", "location": "p1"}
    assert data["total_reported_revenue_change_pct"]["value"] == -1.2
    assert data["perimeter_changed"]["value"] is False
    assert data["perimeter_changed"]["found"] is True
    assert data["adj_margin_pct"] == {"found": True, "value": 15.2,
                                      "quote": None, "location": None}

    # Uncoercible values: found=False, value=None, original kept.
    assert data["buyback_suspension_language"]["found"] is False
    assert data["buyback_suspension_language"]["value"] is None
    assert data["buyback_suspension_language"]["raw_value"] == "maybe"
    assert data["shares_outstanding_m"]["found"] is False
    assert data["shares_outstanding_m"]["raw_value"] == "about 280"
    assert data["book_equity_b"]["found"] is False  # bool is not numeric
    assert data["book_equity_b"]["raw_value"] is True

    assert data["leverage_sentence"]["value"] == "Leverage was 2.5x."
    assert data["leverage_ratio_x"]["value"] == 2.5
    assert data["goodwill_impairment_charge"]["value"] == {"impaired": True,
                                                           "amount_m": 1200.0}
    assert data["guidance_language"]["value"] == {"softened": False,
                                                  "assessment": "Reaffirmed."}

    # found=True with a null value becomes found=False.
    assert data["group_organic_pct"]["found"] is False
    # Keys the model omitted are filled in.
    assert data["book_equity_b"]["quote"] is None
    assert data["segment_organic_pct"] == {"found": False, "value": None,
                                           "quote": None, "location": None}


def test_parse_keeps_accounting_style_negative_organic_growth():
    raw = _raw(organic_growth_core_pct={"found": True, "value": "(1.5)%"},
               buyback_dollars_qtr_m={"found": True, "value": "$(786.1)"})
    data = ex.parse_extraction_json(raw, ["organic_growth_core_pct",
                                          "buyback_dollars_qtr_m"])
    assert data["organic_growth_core_pct"]["found"] is True
    assert data["organic_growth_core_pct"]["value"] == -1.5
    assert "raw_value" not in data["organic_growth_core_pct"]
    assert data["buyback_dollars_qtr_m"]["value"] == -786.1


def test_parse_goodwill_and_guidance_shapes():
    raw = _raw(
        goodwill_impairment_charge={"found": True, "value": {"amount_m": "n/a"}},
        guidance_language={"found": True, "value": {"softened": True}},
    )
    data = ex.parse_extraction_json(raw, ["goodwill_impairment_charge", "guidance_language"])
    assert data["goodwill_impairment_charge"]["value"] == {"impaired": False,
                                                           "amount_m": None}
    assert data["guidance_language"]["value"] == {"softened": True, "assessment": ""}

    raw = _raw(goodwill_impairment_charge={"found": True, "value": [1, 2]},
               guidance_language={"found": True, "value": 7})
    data = ex.parse_extraction_json(raw, ["goodwill_impairment_charge", "guidance_language"])
    assert data["goodwill_impairment_charge"]["found"] is False
    assert data["goodwill_impairment_charge"]["raw_value"] == [1, 2]
    assert data["guidance_language"]["found"] is False
    assert data["guidance_language"]["raw_value"] == 7


def test_parse_leverage_sentence_rejects_structures():
    data = ex.parse_extraction_json(
        _raw(leverage_sentence={"found": True, "value": {"x": 1}}), ["leverage_sentence"])
    assert data["leverage_sentence"]["found"] is False
    assert data["leverage_sentence"]["raw_value"] == {"x": 1}


@pytest.mark.parametrize("literal", ["1" + "0" * 400, "1e400", "-1" + "0" * 400],
                         ids=["huge-int", "float-overflow", "huge-negative-int"])
def test_parse_out_of_range_number_becomes_not_found(literal):
    raw = ('{"leverage_ratio_x": {"found": true, "value": %s}, '
           '"goodwill_impairment_charge": {"found": true, '
           '"value": {"impaired": true, "amount_m": %s}}}' % (literal, literal))
    data = ex.parse_extraction_json(raw, ["leverage_ratio_x",
                                          "goodwill_impairment_charge"])
    assert data["leverage_ratio_x"]["found"] is False
    assert data["leverage_ratio_x"]["value"] is None
    assert data["leverage_ratio_x"]["raw_value"] == json.loads(literal)
    assert data["goodwill_impairment_charge"]["value"] == {"impaired": True,
                                                           "amount_m": None}


def test_parse_isolates_outermost_object_from_chatter():
    raw = 'Here you go:\n{"leverage_ratio_x": {"found": true, "value": 2.1}}\nDone.'
    data = ex.parse_extraction_json(raw, ["leverage_ratio_x"])
    assert data["leverage_ratio_x"]["value"] == 2.1


def test_parse_defaults_to_full_schema():
    data = ex.parse_extraction_json("{}")
    assert list(data) == ex.SCHEMA_KEYS


@pytest.mark.parametrize("raw", ["no json here", "[1, 2]", "}{"])
def test_parse_raises_on_non_object(raw):
    with pytest.raises(ValueError):
        ex.parse_extraction_json(raw, ex.SCHEMA_KEYS)
