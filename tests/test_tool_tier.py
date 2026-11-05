"""The on-demand tier: tools that exist without paying for every turn."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ["LEO_TTS_ENABLED"] = "false"

import httpx  # noqa: E402

import config  # noqa: E402
from leo.brain import Brain  # noqa: E402
from tools import _ALLOWED_ARGS  # noqa: E402
from tools.schemas import (  # noqa: E402
    CORE_TOOLS,
    ON_DEMAND_MAX,
    ON_DEMAND_TOOLS,
    TOOL_SPECS,
    base_specs,
    full_schema_names,
    select_tools,
    to_gemini_tools,
    to_openai_tools,
)


def _tokens(specs: list[dict]) -> int:
    return len(json.dumps([
        {"type": "function", "function": {
            "name": s["name"], "description": s["description"], "parameters": s["parameters"],
        }} for s in specs
    ])) // 4


def _brain() -> Brain:
    return Brain(httpx.AsyncClient())


def test_the_flag_never_reaches_a_provider():
    """Gemini rejects a whole request over one key it does not know."""
    names = sorted(ON_DEMAND_TOOLS)
    assert "on_demand" not in json.dumps(to_openai_tools(names))
    assert "on_demand" not in json.dumps(to_gemini_tools(names))


def test_a_flagged_tool_keeps_its_arguments():
    """This is why it is a flag: a second list would lose them silently."""
    assert _ALLOWED_ARGS["media_control"]
    assert "action" in _ALLOWED_ARGS["media_control"]


def test_the_full_schema_leaves_on_demand_tools_out():
    sent = {tool["function"]["name"] for tool in to_openai_tools()}
    assert sent == {spec["name"] for spec in base_specs()}
    assert not sent & ON_DEMAND_TOOLS


def test_asking_for_one_still_offers_it():
    assert "media_control" in select_tools("turn the volume up")
    assert "media_control" not in select_tools("open notepad")


def test_a_worst_case_turn_still_fits_two_turn_budget():
    """The base schema, plus the two largest on-demand tools, twice over."""
    largest = sorted(
        (spec for spec in TOOL_SPECS if spec.get("on_demand")), key=lambda s: _tokens([s])
    )[-ON_DEMAND_MAX:]
    floor = _tokens(base_specs() + largest) + len(config.SYSTEM_PROMPT) // 4
    assert 2 * floor < 8000, f"two worst-case turns would be {2 * floor}"


def test_an_utterance_matching_every_list_is_capped():
    every = "volume backlog vscode"
    picked = [name for name in select_tools(every) if name in ON_DEMAND_TOOLS]
    assert len(picked) == ON_DEMAND_MAX


def test_what_was_said_first_wins_the_cap():
    picked = select_tools("backlog then vscode then volume")
    assert "backlog" in picked and "dev_workflow" in picked
    assert "media_control" not in picked


def test_what_was_said_beats_what_was_recent():
    picked = select_tools("the backlog and vscode", sticky=["media_control"])
    assert "media_control" not in picked


def test_core_tools_are_never_lost():
    assert set(CORE_TOOLS) <= set(select_tools(""))


def test_a_retry_keeps_the_tool_that_was_asked_for():
    names = full_schema_names(select_tools("turn the volume up"))
    assert "media_control" in names
    assert "dev_workflow" not in names
    assert {spec["name"] for spec in base_specs()} <= set(names)


def test_a_follow_up_with_no_keyword_can_still_reach_the_tool():
    brain = _brain()
    assert "media_control" in brain._selected_tools("turn the volume up", "")
    brain.remember("turn the volume up", "Done.")
    assert "media_control" in brain._selected_tools("a bit more", "")


def test_a_tool_stops_being_offered_after_a_few_turns():
    brain = _brain()
    brain._selected_tools("turn the volume up", "")
    for _turn in range(config.TOOL_ON_DEMAND_STICKY_TURNS + 1):
        brain.remember("something else", "Sure.")
        selected = brain._selected_tools("something else", "")
    assert "media_control" not in selected


def test_being_re_offered_does_not_renew_it():
    """Or one mention would keep a tool on every payload for ever."""
    brain = _brain()
    brain._selected_tools("turn the volume up", "")
    seen_at = brain._on_demand_seen["media_control"]
    brain.remember("x", "y")
    brain._selected_tools("no keyword here", "")
    assert brain._on_demand_seen["media_control"] == seen_at


def test_with_subsets_off_the_requested_tool_still_rides(monkeypatch):
    monkeypatch.setattr(config, "TOOL_SUBSET_ENABLED", False)
    names = _brain()._selected_tools("turn the volume up", "")
    assert "media_control" in names
    assert "dev_workflow" not in names
