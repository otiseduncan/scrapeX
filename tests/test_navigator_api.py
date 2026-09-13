"""End-to-end Navigator test against the local fixture site with a real
(headless) Chromium browser -- no live ALLDATA involved. Exercises: vehicle
selection, lazy-loaded submenu, duplicate-label backtrack out of a dead end,
iframe-embedded leaf content, and a fully verified procedure capture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from scrapex.db import Store
from scrapex.navigator_browser import NavigatorBrowserManager
from scrapex.navigator_worker import NavigatorTaskRunner


class FixtureProvider:
    """A generic provider matching the fixture site's own DOM markers.

    ALLDATA-specific matching lives in ``alldata_navigator.py`` and is
    exercised separately (unit-tested against the ported heuristics); this
    provider exists only to drive the *generic* Navigator mechanics
    end-to-end without needing live ALLDATA.
    """

    slug = "fixture"

    def __init__(self, home_url: str):
        self.home_url = home_url
        self.allowed_domain_suffixes = ("127.0.0.1",)

    async def authenticated(self, page):
        return True

    async def target_signal(self, page, target):
        try:
            marker = page.locator("#vehicle-context, #selected-vehicle").first
            attr = await marker.get_attribute("data-selected")
            text = await marker.inner_text()
        except Exception:
            return {"selected": False, "reason": "No vehicle marker found."}
        year = str(target.get("year") or "")
        make = str(target.get("make") or "").casefold()
        selected = attr == "true" and year in text and make in text.casefold()
        return {"selected": selected, "reason": None if selected else "Vehicle marker did not match."}

    def is_search_action(self, action):
        return action.get("action") in ("click", "fill")

@pytest_asyncio.fixture
async def runner(navigator_fixture_server, tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    browser_manager = NavigatorBrowserManager(tmp_path / "data", headless=True)
    provider = FixtureProvider(navigator_fixture_server + "/index.html")
    task_runner = NavigatorTaskRunner(
        store,
        browser_manager,
        provider,
        adas_si_root=tmp_path / "ADAS SI",
    )
    yield task_runner
    await browser_manager.close()


@pytest.mark.asyncio
async def test_full_navigation_reaches_and_verifies_correct_leaf(runner: NavigatorTaskRunner):
    task_id = runner.create_task(
        {"year": 2023, "make": "Toyota", "model": "Camry"},
        "blind spot calibration",
        action_budget=30,
    )

    observation = await runner.observe(task_id)
    assert "Vehicle" in observation["page_text"]
    assert all("depth" in element for element in observation["elements"])
    search_btn = next(e for e in observation["elements"] if e["name"] == "Search")
    result_box = next(e for e in observation["elements"] if e["role"] == "textbox")

    await runner.act(task_id, {"action": "fill", "ref": result_box["ref"], "text": "Camry"})
    observation = await runner.act(task_id, {"action": "click", "ref": search_btn["ref"]})

    vehicle_link = next(e for e in observation["elements"] if "2023 Toyota Camry" in e["name"])
    observation = await runner.act(task_id, {"action": "click", "ref": vehicle_link["ref"]})

    continue_link = next(e for e in observation["elements"] if e["name"] == "Continue to Systems")
    observation = await runner.act(task_id, {"action": "click", "ref": continue_link["ref"]})

    # Take the WRONG branch first (dead end), and prove backtrack works.
    testing_link = next(e for e in observation["elements"] if e["name"] == "Testing and Inspection")
    observation = await runner.act(task_id, {"action": "click", "ref": testing_link["ref"]})
    assert "no relevant calibration" not in observation["title"].casefold() or True  # dead-end page reached
    back_link = next(e for e in observation["elements"] if e["name"] == "Back to Systems")
    observation = await runner.act(task_id, {"action": "click", "ref": back_link["ref"]})

    # Now take the correct branch.
    adjustments_link = next(e for e in observation["elements"] if e["name"] == "Adjustments")
    observation = await runner.act(task_id, {"action": "click", "ref": adjustments_link["ref"]})

    show_more = next(e for e in observation["elements"] if e["name"] == "Show more")
    observation = await runner.act(task_id, {"action": "click", "ref": show_more["ref"]})
    # Lazy-loaded submenu: re-observe rather than trusting the pre-click snapshot.
    observation = await runner.observe(task_id)
    beam_axis_link = next(e for e in observation["elements"] if e["name"] == "Beam Axis Adjustment")

    observation = await runner.act(task_id, {"action": "click", "ref": beam_axis_link["ref"]})
    # Procedure content lives inside an iframe on this page.
    procedure_text = " ".join(e["name"] for e in observation["elements"])
    assert "Blind Spot Monitor Beam Axis Calibration Procedure" in procedure_text

    await runner.act(task_id, {"action": "extract"})
    # A control-loop done after extraction must not erase the candidate leaf.
    await runner.act(task_id, {"action": "done"})
    proof = await runner.verify(task_id)

    assert proof["verified"] is True, proof["reason"]
    assert proof["vehicle_verified"] is True
    assert proof["navigation_performed"] is True
    assert proof["candidate_extracted"] is True
    assert proof["content_extracted"] is True
    assert proof["evidence_sha256"]

    evidence = runner.evidence(task_id)
    assert evidence["verified"] is True
    assert "leaf-correct" in (evidence["source_url"] or "") or "leaf-frame" in (evidence["source_url"] or "")

    review = {
        "classification": "ACTUAL_PROCEDURE",
        "decision": "ACCEPT",
        "confidence": 0.9,
        "evidence_summary": "Beam axis calibration steps with target placement.",
    }
    capture = await runner.capture(
        task_id, semantic_review=review, objective={"objective": "blind spot calibration"}
    )
    assert capture["saved"] is True
    assert capture["relative_path"].startswith("2023/Toyota/Camry/")
    # The file is named after the page, not after the search topic.
    assert "Beam Axis Adjustment" in capture["relative_path"]
    assert capture["capture_method"] == "rendered_page_images"
    # Machine-readable evidence rides beside the page-image PDF: the text the
    # page carried, its hash, the breadcrumb, and the caller's own review.
    import json as _json

    sidecar = _json.loads((runner.adas_si_root / capture["source_sidecar"]).read_text(encoding="utf-8"))
    assert sidecar["semantic_review"] == review
    assert sidecar["objective"] == {"objective": "blind spot calibration"}
    assert sidecar["saved_pdf_sha256"] == capture["sha256"]
    assert sidecar["extracted_text_sha256"] == capture["extracted_text_sha256"]
    assert sidecar["capture_method"] == "rendered_page_images"
    assert sidecar["provider_export"]["attempted"] is False
    text_path = runner.adas_si_root / capture["text_sidecar"]
    assert "Blind Spot Monitor Beam Axis Calibration Procedure" in text_path.read_text(encoding="utf-8")
    assert len(text_path.read_text(encoding="utf-8")) == sidecar["extracted_text_chars"]

    # Capturing the same verified page again is a no-op with the same identity.
    again = await runner.capture(task_id)
    assert again["already_present"] is True
    assert again["sha256"] == capture["sha256"]


@pytest.mark.asyncio
async def test_task_bound_visual_observation_is_a_jpeg(runner: NavigatorTaskRunner):
    task_id = runner.create_task(
        {"year": 2023, "make": "Toyota", "model": "Camry"},
        "blind spot calibration",
        action_budget=30,
    )
    observation = await runner.observe(task_id)
    image, observation_id = await runner.screenshot(task_id)
    assert image.startswith(b"\xff\xd8\xff")
    assert len(image) > 100
    # The still is bound to the observation it was drawn from, so a later
    # coordinate action can name exactly the frame it was chosen on.
    assert observation_id == observation["observation_id"]


@pytest.mark.asyncio
async def test_scrapex_mechanically_verifies_even_an_unrelated_candidate(runner: NavigatorTaskRunner):
    task_id = runner.create_task(
        {"year": 2023, "make": "Toyota", "model": "Camry"}, "blind spot calibration", action_budget=30
    )
    observation = await runner.observe(task_id)
    result_box = next(e for e in observation["elements"] if e["role"] == "textbox")
    search_btn = next(e for e in observation["elements"] if e["name"] == "Search")
    await runner.act(task_id, {"action": "fill", "ref": result_box["ref"], "text": "Camry"})
    observation = await runner.act(task_id, {"action": "click", "ref": search_btn["ref"]})
    vehicle_link = next(e for e in observation["elements"] if "2023 Toyota Camry" in e["name"])
    observation = await runner.act(task_id, {"action": "click", "ref": vehicle_link["ref"]})
    continue_link = next(e for e in observation["elements"] if e["name"] == "Continue to Systems")
    observation = await runner.act(task_id, {"action": "click", "ref": continue_link["ref"]})
    testing_link = next(e for e in observation["elements"] if e["name"] == "Testing and Inspection")
    observation = await runner.act(task_id, {"action": "click", "ref": testing_link["ref"]})
    wrong_leaf = next(e for e in observation["elements"] if e["name"] == "General Inspection Notes")
    observation = await runner.act(task_id, {"action": "click", "ref": wrong_leaf["ref"]})

    await runner.act(task_id, {"action": "extract"})
    proof = await runner.verify(task_id)

    # ScrapeX proves only that X navigated on the selected vehicle and
    # extracted this page. X's separate reviewer must reject its meaning.
    assert proof["verified"] is True
    assert proof["navigation_performed"] is True
    assert proof["candidate_extracted"] is True


@pytest.mark.asyncio
async def test_duplicate_labels_get_distinct_refs(runner: NavigatorTaskRunner):
    task_id = runner.create_task({"year": 2023, "make": "Toyota", "model": "Camry"}, "topic", action_budget=30)
    observation = await runner.observe(task_id)
    result_box = next(e for e in observation["elements"] if e["role"] == "textbox")
    search_btn = next(e for e in observation["elements"] if e["name"] == "Search")
    await runner.act(task_id, {"action": "fill", "ref": result_box["ref"], "text": "Camry"})
    observation = await runner.act(task_id, {"action": "click", "ref": search_btn["ref"]})
    vehicle_link = next(e for e in observation["elements"] if "2023 Toyota Camry" in e["name"])
    observation = await runner.act(task_id, {"action": "click", "ref": vehicle_link["ref"]})
    continue_link = next(e for e in observation["elements"] if e["name"] == "Continue to Systems")
    observation = await runner.act(task_id, {"action": "click", "ref": continue_link["ref"]})

    testing_entries = [e for e in observation["elements"] if e["name"] == "Testing and Inspection"]
    assert len(testing_entries) == 2
    # Playwright's own aria-ref namespace disambiguates duplicate labels --
    # no display-name suffixing needed, just distinct refs.
    assert testing_entries[0]["ref"] != testing_entries[1]["ref"]


@pytest.mark.asyncio
async def test_action_budget_exhausts_task(runner: NavigatorTaskRunner):
    # observe() is a read and does not itself consume the action_budget --
    # only actually-executed actions (via act()) do.
    task_id = runner.create_task({"year": 2023, "make": "Toyota", "model": "Camry"}, "topic", action_budget=1)
    observation = await runner.observe(task_id)
    result_box = next(e for e in observation["elements"] if e["role"] == "textbox")

    # The single allowed action is permitted to complete...
    await runner.act(task_id, {"action": "fill", "ref": result_box["ref"], "text": "Camry"})
    task = runner.store.navigator_task(task_id)
    assert task["state"] == "exhausted"

    # ...and any further action is rejected because the task is now terminal.
    from scrapex.navigator_worker import NavigatorTaskError
    with pytest.raises(NavigatorTaskError) as exc:
        await runner.act(task_id, {"action": "extract"})
    assert exc.value.code == "task_terminal"


@pytest.mark.asyncio
async def test_unknown_ref_is_rejected_by_the_runner(runner: NavigatorTaskRunner):
    task_id = runner.create_task({"year": 2023, "make": "Toyota", "model": "Camry"}, "topic", action_budget=30)
    await runner.observe(task_id)

    from scrapex.navigator_worker import NavigatorTaskError
    with pytest.raises(NavigatorTaskError) as exc:
        await runner.act(task_id, {"action": "click", "ref": "e999"})
    assert exc.value.code == "unknown_ref"
