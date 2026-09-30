"""Profile-driven prompt: anchors, schema keys, schema spec, Claude call."""

import json
import sys
import types
from pathlib import Path

import pytest
import requests

from filings import extraction as ex

CONFIG_FILE = Path(__file__).parent / "fixtures" / "omc_filings_config.json"

# ANCHOR_PHRASES as shipped before the profile refactor (OMC-specific list).
OLD_OMC_ANCHORS = [
    "Issuer Purchases of Equity Securities",
    "Unregistered Sales of Equity Securities",
    "repurchase",
    "leverage ratio",
    "Goodwill",
    "Impairment",
    "Components of Revenue Change",
    "Definitions",
    "Integrated Media",
    "Organic",
    "Total leverage",
]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network access attempted in a test")
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)


@pytest.fixture
def omc_cfg():
    return json.loads(CONFIG_FILE.read_text())


@pytest.fixture
def omc_profile(omc_cfg):
    return omc_cfg["profile"]


# ---------------------------------------------------------------------------
# Anchors & keys
# ---------------------------------------------------------------------------

def test_anchor_set_parity_for_shipped_omc_config(omc_profile):
    phrases = ex.anchor_phrases(omc_profile)
    assert set(phrases) == set(OLD_OMC_ANCHORS)
    assert len(phrases) == 11


def test_anchor_phrases_dedupe_case_insensitively_preserving_order():
    profile = {"extra_anchor_phrases": ["goodwill", "Segment Results", "SEGMENT results"],
               "segment": {"name": "Cloud"}}
    phrases = ex.anchor_phrases(profile)
    assert phrases == ex.BASE_ANCHOR_PHRASES + ["Segment Results", "Cloud"]


def test_anchor_phrases_without_profile_are_the_base_list():
    assert ex.anchor_phrases({}) == ex.BASE_ANCHOR_PHRASES
    assert ex.anchor_phrases(None) == ex.BASE_ANCHOR_PHRASES


def test_build_excerpt_uses_the_given_phrases():
    text = "x" * 20_000 + "Widget backlog grew" + "y" * 20_000
    with_anchor = ex.build_excerpt(text, ["widget backlog"])
    without = ex.build_excerpt(text, [])
    assert "Widget backlog grew" in with_anchor
    assert "Widget backlog grew" not in without


def test_schema_keys_include_segment_only_when_configured(omc_profile):
    assert ex.schema_keys(omc_profile) == ex.SCHEMA_KEYS
    assert len(ex.SCHEMA_KEYS) == 16
    generic = ex.schema_keys({})
    assert "segment_organic_pct" not in generic
    assert len(generic) == 15
    # A segment without a name counts as absent.
    assert "segment_organic_pct" not in ex.schema_keys({"segment": {"plain_name": "x"}})


# ---------------------------------------------------------------------------
# Schema spec
# ---------------------------------------------------------------------------

def test_schema_spec_renders_omc_strings(omc_profile):
    spec = ex.build_schema_spec(omc_profile, ex.schema_keys(omc_profile))
    expected_lines = [
        '1.  "organic_growth_core_pct" — organic revenue growth for Core Operations '
        'for this quarter, in percent (the headline figure, usually on page 1). '
        'value: number.',
        '3.  "perimeter_changed" — judgment: does the "Definitions — Components of '
        'Revenue Change" section (or equivalent) indicate that the Core Operations '
        'perimeter / disposal scope CHANGED versus the prior presentation? value: '
        'true|false. Quote the definition language you relied on.',
        '4.  "adj_margin_pct" — adjusted EBITA margin, in percent. value: number.',
        '5.  "segment_organic_pct" — organic growth for the Integrated Media '
        'segment/discipline, in percent. value: number.',
        '6.  "group_organic_pct" — group/total organic growth, in percent (may '
        'equal key 1). value: number.',
        '16. "guidance_language" — current guidance phrasing. quote: the guidance '
        'sentence(s). value: {"softened": true|false, "assessment": "one short '
        'sentence"} judged RELATIVE TO these reference phrases: "4% organic" and '
        '"$900M synergies in 2026" (softened / dropped counts as true; intact '
        'counts as false).',
    ]
    lines = spec.splitlines()
    for line in expected_lines:
        assert line in lines
    assert spec.startswith("Return ONE JSON object with EXACTLY these keys.")
    assert "NEVER guess, infer, or compute values that are not stated in the text." in spec
    assert spec.endswith(
        'Fields not applicable to this form type (e.g. key 14 outside a 10-K, or '
        'key 8 in a press release) should simply be {"found": false, ...}.')
    # Every key is numbered once, in order.
    for i, key in enumerate(ex.SCHEMA_KEYS, 1):
        assert f'{f"{i}.":<4}"{key}" — ' in spec
    # Old OMC-only key names are gone.
    assert "adj_ebita_margin_pct" not in spec
    assert "integrated_media_organic_pct" not in spec


def test_schema_spec_drops_segment_item_and_renumbers_without_segment(omc_profile):
    profile = dict(omc_profile)
    del profile["segment"]
    keys = ex.schema_keys(profile)
    spec = ex.build_schema_spec(profile, keys)
    assert "segment_organic_pct" not in spec
    assert "Integrated Media" not in spec
    assert '5.  "group_organic_pct"' in spec
    assert "(e.g. key 13 outside a 10-K, or key 7 in a press release)" in spec


def test_schema_spec_generic_wording_without_profile():
    spec = ex.build_schema_spec({}, ex.schema_keys({}))
    assert ("the company's headline organic (or constant-currency, excluding "
            "acquisitions/divestitures) revenue growth for this quarter") in spec
    assert ("does the section defining organic growth (or its non-GAAP "
            "reconciliation) indicate that the organic-growth perimeter") in spec
    assert ("adjusted operating margin (the company's headline adjusted/non-GAAP "
            "operating margin), in percent") in spec
    assert "Core Operations" not in spec
    assert "{" in spec and "{core_label" not in spec


def test_guidance_generic_criterion_without_phrases():
    spec = ex.build_schema_spec({"guidance_reference_phrases": []}, ex.schema_keys({}))
    assert ("softened=true ONLY if the document explicitly lowers, withdraws, or "
            "narrows toward the low end previously issued guidance; absence of "
            "guidance is NOT softening") in spec
    assert "RELATIVE TO" not in spec


def test_guidance_phrase_joining():
    one = ex.build_schema_spec({"guidance_reference_phrases": ["5% growth"]}, ["guidance_language"])
    assert 'RELATIVE TO this reference phrase: "5% growth" (softened' in one
    three = ex.build_schema_spec({"guidance_reference_phrases": ["a", "b", "c"]},
                                 ["guidance_language"])
    assert 'RELATIVE TO these reference phrases: "a", "b" and "c" (softened' in three


def test_system_prompt_unchanged():
    assert ex.SYSTEM_PROMPT.startswith(
        "You are a careful financial-filings extraction assistant.")
    assert ex.SYSTEM_PROMPT.endswith(
        "Never guess, estimate, or compute values that are not stated in the text.")


# ---------------------------------------------------------------------------
# call_claude (fake SDK — no network)
# ---------------------------------------------------------------------------

class _Block:
    def __init__(self, type_, text=""):
        self.type = type_
        self.text = text


class _Response:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


@pytest.fixture
def fake_anthropic(monkeypatch):
    calls = []
    state = {"response": None}

    class _Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            return state["response"]

    class Anthropic:
        def __init__(self, api_key=None):
            self.api_key = api_key
            self.messages = _Messages()

    module = types.ModuleType("anthropic")
    module.Anthropic = Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", module)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    good_json = json.dumps({"organic_growth_core_pct": {"found": True, "value": "3.9%",
                                                        "quote": "q", "location": "p1"}})
    state["response"] = _Response([_Block("thinking"), _Block("text", good_json)])
    return calls, state


def test_call_claude_uses_config_and_passes_temperature(fake_anthropic, omc_cfg):
    calls, _ = fake_anthropic
    omc_cfg["extraction"] = {"model": "some-model", "max_tokens": 1234, "temperature": 0}
    keys = ex.schema_keys(omc_cfg["profile"])
    result = ex.call_claude("doc text", "10-Q", omc_cfg, keys)
    (kwargs,) = calls
    assert kwargs["model"] == "some-model"
    assert kwargs["max_tokens"] == 1234
    assert kwargs["temperature"] == 0
    assert kwargs["system"] == ex.SYSTEM_PROMPT
    prompt = kwargs["messages"][0]["content"]
    assert "organic revenue growth for Core Operations" in prompt
    assert "doc text" in prompt
    assert result["organic_growth_core_pct"]["value"] == 3.9
    assert set(result) == set(keys)


def test_call_claude_defaults_and_omits_null_temperature(fake_anthropic):
    calls, _ = fake_anthropic
    ex.call_claude("doc", "8-K", {"company": {"cik": "1"}}, ex.schema_keys({}))
    assert calls[-1]["model"] == "claude-sonnet-4-6"
    assert calls[-1]["max_tokens"] == 4000
    assert calls[-1]["temperature"] == 0
    ex.call_claude("doc", "8-K", {"extraction": {"temperature": None}}, ex.schema_keys({}))
    assert "temperature" not in calls[-1]


@pytest.mark.parametrize("stop_reason, message", [
    ("max_tokens", "extraction.max_tokens"),
    ("refusal", "model declined the request (stop_reason=refusal)"),
])
def test_call_claude_raises_on_bad_stop_reason(fake_anthropic, stop_reason, message):
    _, state = fake_anthropic
    state["response"] = _Response([_Block("text", "{}")], stop_reason=stop_reason)
    with pytest.raises(RuntimeError, match=message.replace("(", r"\(").replace(")", r"\)")):
        ex.call_claude("doc", "10-Q", {}, ex.schema_keys({}))


def test_call_claude_requires_api_key(fake_anthropic, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        ex.call_claude("doc", "10-Q", {}, ex.schema_keys({}))
