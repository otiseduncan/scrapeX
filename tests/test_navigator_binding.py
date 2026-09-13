"""Observation-bound actions: ref, mark, and coordinate targets.

Every case here is about the runtime proving that the target the caller
chose is the target it is about to act on -- and refusing, never redirecting,
when it cannot.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scrapex.navigator_actions import ActionError, NavigatorActionExecutor
from scrapex.navigator_observation import (
    DomControl,
    Mark,
    Observation,
    ObservationNode,
    boxes_overlap,
    control_signature,
    page_identity_of,
    select_marks,
)
from scrapex.navigator_visual import FrameCache
from tests.test_navigator_visual import make_png


class Locator:
    def __init__(self, *, exists=True, box=None, name="", visible=True, tag="a", role="", classes="", element_id=""):
        self._exists = exists
        self._box = box
        self._name = name
        self._visible = visible
        self._tag = tag
        self._role = role
        self._classes = classes
        self._id = element_id
        self.clicked = 0
        self.filled = None
        self.pressed = None

    async def count(self):
        return 1 if self._exists else 0

    @property
    def first(self):
        return self

    async def is_visible(self):
        return self._visible

    async def bounding_box(self):
        if self._box is None:
            return None
        x, y, w, h = self._box
        return {"x": x, "y": y, "width": w, "height": h}

    async def evaluate(self, _js):
        w = h = 0
        if self._box:
            w, h = self._box[2], self._box[3]
        return {"tag": self._tag, "role": self._role, "name": self._name, "classes": self._classes, "id": self._id, "w": w, "h": h}

    async def click(self, timeout=None):
        self.clicked += 1

    async def fill(self, text, timeout=None):
        self.filled = text

    async def press(self, key, timeout=None):
        self.pressed = key


class Mouse:
    def __init__(self):
        self.clicks = []
        self.wheels = []

    async def click(self, x, y):
        self.clicks.append((x, y))

    async def wheel(self, x, y):
        self.wheels.append((x, y))


class Keyboard:
    def __init__(self):
        self.typed = []

    async def type(self, text, delay=None):
        self.typed.append(text)


class Page:
    def __init__(self, *, url="https://example.test/page", title="Page", ref_locators=None, path_locators=None, png=None):
        self.url = url
        self._title = title
        self._ref_locators = ref_locators or {}
        self._path_locators = path_locators or {}
        self.mouse = Mouse()
        self.keyboard = Keyboard()
        self.frames = [self]
        self._png = png or b""
        self.waits = []

    async def title(self):
        return self._title

    def locator(self, selector):
        if selector.startswith("aria-ref="):
            return self._ref_locators.get(selector.split("=", 1)[1], Locator(exists=False))
        return self._path_locators.get(selector, Locator(exists=False))

    async def wait_for_timeout(self, ms):
        self.waits.append(ms)

    async def screenshot(self, type="png", full_page=False, quality=None):
        return self._png

    async def evaluate(self, js, arg=None):
        if "elementFromPoint" in js:
            return {"tag": "span", "role": "", "name": "print", "classes": "icon-print", "id": "", "is_frame": False, "x": 0, "y": 0, "w": 20, "h": 20}
        return None


class Provider:
    slug = "fixture"
    allowed_domain_suffixes = ("example.test",)

    def is_search_action(self, action):
        return action.get("action") in {"fill", "type"}


def _observation(**overrides):
    base = dict(
        url="https://example.test/page",
        title="Page",
        elements=[
            ObservationNode(ref="e1", role="link", name="ADAS Quick Reference", box={"x": 10, "y": 10, "w": 100, "h": 20}),
            ObservationNode(ref="e2", role="button", name="Search", box={"x": 300, "y": 10, "w": 60, "h": 20}),
        ],
        observation_id="obs_1",
        page_identity=page_identity_of("https://example.test/page", "Page"),
        window_width=400,
        window_height=300,
    )
    base.update(overrides)
    return Observation(**base)


def _control(**overrides):
    values = dict(tag="span", role="", name="print", classes="icon-print", element_id="", path="#toolbar > span:nth-of-type(3)", frame=0, x=370, y=8, w=24, h=24)
    values.update(overrides)
    sig = control_signature(values["tag"], values["role"], values["name"], values["classes"], values["element_id"], values["w"], values["h"])
    return DomControl(sig=sig, **values)


# ------------------------------------------------------------------ refs


@pytest.mark.asyncio
async def test_ref_click_bound_to_the_current_observation_works_and_receipts_the_target():
    locator = Locator(box=(12, 11, 100, 20), name="ADAS Quick Reference")
    page = Page(ref_locators={"e1": locator})
    result = await NavigatorActionExecutor(Provider()).execute(
        page, _observation(), {"action": "click", "ref": "e1", "observation_id": "obs_1"}
    )
    assert locator.clicked == 1
    assert result.target["kind"] == "ref" and result.target["observation_id"] == "obs_1"
    assert result.target["current_box"] == {"x": 12, "y": 11, "w": 100, "h": 20}


@pytest.mark.asyncio
async def test_ref_click_from_a_superseded_observation_is_rejected_not_redirected():
    locator = Locator(box=(10, 10, 100, 20), name="ADAS Quick Reference")
    page = Page(ref_locators={"e1": locator})
    with pytest.raises(ActionError) as exc:
        await NavigatorActionExecutor(Provider()).execute(
            page, _observation(), {"action": "click", "ref": "e1", "observation_id": "obs_0"}
        )
    assert exc.value.code == "stale_observation"
    assert exc.value.detail["current_observation_id"] == "obs_1"
    assert locator.clicked == 0


@pytest.mark.asyncio
async def test_ref_that_moved_across_the_page_is_a_stale_target():
    locator = Locator(box=(10, 250, 100, 20), name="ADAS Quick Reference")
    page = Page(ref_locators={"e1": locator})
    with pytest.raises(ActionError) as exc:
        await NavigatorActionExecutor(Provider()).execute(
            page, _observation(), {"action": "click", "ref": "e1", "observation_id": "obs_1"}
        )
    assert exc.value.code == "stale_target"
    assert exc.value.detail["observed_box"] == {"x": 10, "y": 10, "w": 100, "h": 20}
    assert locator.clicked == 0


@pytest.mark.asyncio
async def test_ref_whose_label_was_replaced_is_a_stale_target():
    locator = Locator(box=(10, 10, 100, 20), name="Removal and Replacement")
    page = Page(ref_locators={"e1": locator})
    with pytest.raises(ActionError) as exc:
        await NavigatorActionExecutor(Provider()).execute(
            page, _observation(), {"action": "click", "ref": "e1", "observation_id": "obs_1"}
        )
    assert exc.value.code == "stale_target"
    assert "now reads" in exc.value.message
    assert locator.clicked == 0


@pytest.mark.asyncio
async def test_ref_action_bound_to_an_observation_refuses_a_navigated_page():
    locator = Locator(box=(10, 10, 100, 20), name="ADAS Quick Reference")
    page = Page(url="https://example.test/other", title="Other", ref_locators={"e1": locator})
    with pytest.raises(ActionError) as exc:
        await NavigatorActionExecutor(Provider()).execute(
            page, _observation(), {"action": "click", "ref": "e1", "observation_id": "obs_1"}
        )
    assert exc.value.code == "stale_observation"
    assert exc.value.detail["current_url"] == "https://example.test/other"
    assert locator.clicked == 0


@pytest.mark.asyncio
async def test_unbound_ref_click_keeps_the_old_contract_but_still_checks_the_target():
    locator = Locator(box=(10, 10, 100, 20), name="ADAS Quick Reference")
    page = Page(url="https://example.test/other", title="Other", ref_locators={"e1": locator})
    # No observation_id: the page identity is not enforced (compatibility),
    # but the ref's own box and label still are.
    result = await NavigatorActionExecutor(Provider()).execute(
        page, _observation(), {"action": "click", "ref": "e1"}
    )
    assert result.executed is True and locator.clicked == 1


@pytest.mark.asyncio
async def test_type_sends_real_keystrokes_and_counts_as_a_search():
    locator = Locator(box=(10, 10, 100, 20), name="")
    page = Page(ref_locators={"e3": locator})
    observation = _observation(elements=[ObservationNode(ref="e3", role="searchbox", name="Search by VIN", box={"x": 10, "y": 10, "w": 100, "h": 20})])
    result = await NavigatorActionExecutor(Provider()).execute(
        page, observation, {"action": "type", "ref": "e3", "text": "1HGCV1F1XPA000000", "observation_id": "obs_1"}
    )
    assert locator.clicked == 1
    assert page.keyboard.typed == ["1HGCV1F1XPA000000"]
    assert result.is_search_action is True


# ----------------------------------------------------------------- marks


def test_marks_are_offered_only_for_controls_no_ref_covers():
    elements = [ObservationNode(ref="e1", role="button", name="Search", box={"x": 300, "y": 10, "w": 60, "h": 20})]
    covered = _control(x=310, y=12, w=20, h=16, name="Search")
    print_icon = _control()
    marks = select_marks(elements, [covered, print_icon], limit=24)
    assert [mark.control.name for mark in marks] == ["print"]
    assert marks[0].mark == 21


def test_mark_budget_is_bounded():
    controls = [_control(x=10 + 30 * index, name=f"c{index}") for index in range(40)]
    assert len(select_marks([], controls, limit=24)) == 24


@pytest.mark.asyncio
async def test_click_mark_requires_the_observation_it_was_offered_on():
    executor = NavigatorActionExecutor(Provider())
    observation = _observation(marks=[Mark(mark=21, control=_control())])
    with pytest.raises(ActionError) as exc:
        await executor.execute(Page(), observation, {"action": "click_mark", "mark": 21})
    assert exc.value.code == "invalid_arguments"
    with pytest.raises(ActionError) as exc:
        await executor.execute(Page(), observation, {"action": "click_mark", "mark": 22, "observation_id": "obs_1"})
    assert exc.value.code == "unknown_mark"


@pytest.mark.asyncio
async def test_click_mark_relocates_the_control_and_clicks_its_current_centre():
    control = _control()
    locator = Locator(box=(372, 9, 24, 24), name="print", tag="span", classes="icon-print")
    page = Page(path_locators={control.path: locator})
    observation = _observation(marks=[Mark(mark=21, control=control)])
    result = await NavigatorActionExecutor(Provider()).execute(
        page, observation, {"action": "click_mark", "mark": 21, "observation_id": "obs_1"}
    )
    assert page.mouse.clicks == [(384.0, 21.0)]
    assert result.target["kind"] == "mark" and result.target["mark"] == 21


@pytest.mark.asyncio
async def test_click_mark_refuses_a_control_that_moved_or_changed():
    control = _control()
    moved = Locator(box=(20, 200, 24, 24), name="print", tag="span", classes="icon-print")
    page = Page(path_locators={control.path: moved})
    observation = _observation(marks=[Mark(mark=21, control=control)])
    with pytest.raises(ActionError) as exc:
        await NavigatorActionExecutor(Provider()).execute(
            page, observation, {"action": "click_mark", "mark": 21, "observation_id": "obs_1"}
        )
    assert exc.value.code == "stale_target"
    assert page.mouse.clicks == []

    replaced = Locator(box=(370, 8, 24, 24), name="close", tag="span", classes="icon-close")
    page = Page(path_locators={control.path: replaced})
    with pytest.raises(ActionError) as exc:
        await NavigatorActionExecutor(Provider()).execute(
            page, observation, {"action": "click_mark", "mark": 21, "observation_id": "obs_1"}
        )
    assert exc.value.code == "stale_target"
    assert "different control" in exc.value.message
    assert page.mouse.clicks == []

    gone = Page(path_locators={})
    with pytest.raises(ActionError) as exc:
        await NavigatorActionExecutor(Provider()).execute(
            gone, observation, {"action": "click_mark", "mark": 21, "observation_id": "obs_1"}
        )
    assert exc.value.code == "stale_target"
    assert gone.mouse.clicks == []


# ---------------------------------------------------------------- visual


def _frame_png(covered: bool = False) -> bytes:
    return make_png(
        400, 300,
        lambda x, y: (10, 10, 10) if covered and 330 <= x <= 390 and 0 <= y <= 40 else (220, 220, 220),
    )


@pytest.mark.asyncio
async def test_click_visual_requires_observation_binding_and_a_held_frame():
    executor = NavigatorActionExecutor(Provider())
    observation = _observation()
    page = Page(png=_frame_png())
    with pytest.raises(ActionError) as exc:
        await executor.execute(page, observation, {"action": "click_visual", "x_norm": 0.9, "y_norm": 0.05})
    assert exc.value.code == "invalid_arguments"
    with pytest.raises(ActionError) as exc:
        await executor.execute(
            page, observation, {"action": "click_visual", "x_norm": 0.9, "y_norm": 0.05, "observation_id": "obs_1"}
        )
    assert exc.value.code == "visual_frame_missing"
    assert page.mouse.clicks == []


@pytest.mark.asyncio
async def test_click_visual_clicks_when_the_region_still_matches_the_frame_shown():
    cache = FrameCache()
    frame = cache.put("task", "obs_1", _frame_png(), 400, 300)
    page = Page(png=_frame_png())
    observation = _observation(controls=[_control()])
    result = await NavigatorActionExecutor(Provider()).execute(
        page,
        observation,
        {"action": "click_visual", "x_norm": 0.955, "y_norm": 0.067, "observation_id": "obs_1"},
        visual_frame=frame,
    )
    assert page.mouse.clicks == [(382, 20)]
    assert result.target["kind"] == "visual"
    assert result.target["observed_control"]["name"] == "print"
    assert result.target["element_under_point"]["name"] == "print"
    assert result.target["region_difference"] == 0


@pytest.mark.asyncio
async def test_click_visual_refuses_a_point_whose_surroundings_changed():
    cache = FrameCache()
    frame = cache.put("task", "obs_1", _frame_png(), 400, 300)
    page = Page(png=_frame_png(covered=True))
    with pytest.raises(ActionError) as exc:
        await NavigatorActionExecutor(Provider()).execute(
            page,
            _observation(),
            {"action": "click_visual", "x_norm": 0.9, "y_norm": 0.05, "observation_id": "obs_1"},
            visual_frame=frame,
        )
    assert exc.value.code == "stale_visual_target"
    assert page.mouse.clicks == []


@pytest.mark.asyncio
async def test_click_visual_refuses_a_frame_from_another_observation():
    cache = FrameCache()
    frame = cache.put("task", "obs_0", _frame_png(), 400, 300)
    page = Page(png=_frame_png())
    with pytest.raises(ActionError) as exc:
        await NavigatorActionExecutor(Provider()).execute(
            page,
            _observation(),
            {"action": "click_visual", "x_norm": 0.9, "y_norm": 0.05, "observation_id": "obs_1"},
            visual_frame=frame,
        )
    assert exc.value.code == "visual_frame_missing"
    assert page.mouse.clicks == []


# ------------------------------------------------------------ fast path


@pytest.mark.asyncio
async def test_select_vehicle_needs_a_provider_hook_and_reports_its_outcome():
    with pytest.raises(ActionError) as exc:
        await NavigatorActionExecutor(Provider()).execute(Page(), None, {"action": "select_vehicle", "vin": "1" * 17})
    assert exc.value.code == "provider_unsupported"

    class VinProvider(Provider):
        async def select_vehicle(self, page, action):
            return {"selected": True, "vin": action["vin"], "label": "2023 Honda Accord"}

    result = await NavigatorActionExecutor(VinProvider()).execute(
        Page(), None, {"action": "select_vehicle", "vin": "1HGCV1F1XPA000000"}
    )
    assert result.executed is True and result.is_search_action is True
    assert result.target == {"kind": "vehicle", "selected": True, "vin": "1HGCV1F1XPA000000", "label": "2023 Honda Accord"}


def test_box_overlap_tolerates_small_shifts_only():
    assert boxes_overlap({"x": 10, "y": 10, "w": 100, "h": 20}, {"x": 14, "y": 12, "w": 100, "h": 20})
    assert not boxes_overlap({"x": 10, "y": 10, "w": 100, "h": 20}, {"x": 10, "y": 200, "w": 100, "h": 20})
    assert not boxes_overlap({"x": 10}, {"x": 10, "y": 10, "w": 5, "h": 5})
