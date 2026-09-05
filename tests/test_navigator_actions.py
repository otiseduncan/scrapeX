import pytest

from scrapex.navigator_actions import ActionError, NavigatorActionExecutor
from scrapex.navigator_observation import Observation, ObservationNode


class FakeLocator:
    def __init__(self, ref, *, exists=True):
        self.ref = ref
        self._exists = exists
        self.clicked = False
        self.filled = None
        self.pressed = None

    async def count(self):
        return 1 if self._exists else 0

    @property
    def first(self):
        return self

    async def click(self, timeout=None):
        self.clicked = True

    async def fill(self, text, timeout=None):
        self.filled = text

    async def press(self, key, timeout=None):
        self.pressed = key


class FakePage:
    def __init__(self, locators=None, url="https://example.test/start"):
        self._locators = locators or {}
        self.url = url
        self.goto_calls = []
        self.went_back = False

    def locator(self, selector):
        assert selector.startswith("aria-ref=")
        ref = selector.split("=", 1)[1]
        return self._locators.get(ref, FakeLocator(ref, exists=False))

    async def goto(self, url, wait_until=None):
        self.goto_calls.append(url)
        self.url = url

    async def go_back(self, wait_until=None):
        self.went_back = True

    async def wait_for_timeout(self, ms):
        pass


class FakeProvider:
    allowed_domain_suffixes = ("example.test",)

    def is_search_action(self, action):
        return action.get("action") == "fill" or (
            action.get("action") == "press" and action.get("key") == "Enter"
        )


def _observation(*refs):
    return Observation(
        url="https://example.test/start",
        title="Start",
        elements=[ObservationNode(ref=r, role="button", name=r) for r in refs],
    )


@pytest.mark.asyncio
async def test_invalid_action_is_rejected():
    executor = NavigatorActionExecutor(FakeProvider())
    with pytest.raises(ActionError) as exc:
        await executor.execute(FakePage(), _observation("e1"), {"action": "teleport"})
    assert exc.value.code == "invalid_action"


@pytest.mark.asyncio
async def test_done_and_extract_and_back_need_no_ref():
    executor = NavigatorActionExecutor(FakeProvider())
    page = FakePage()

    done = await executor.execute(page, None, {"action": "done"})
    assert done.executed is True

    extract = await executor.execute(page, None, {"action": "extract"})
    assert extract.executed is True

    back = await executor.execute(page, None, {"action": "back"})
    assert back.executed is True
    assert page.went_back is True


@pytest.mark.asyncio
async def test_open_allowed_domain_navigates():
    executor = NavigatorActionExecutor(FakeProvider())
    page = FakePage()
    result = await executor.execute(page, None, {"action": "open", "url": "https://sub.example.test/page"})
    assert result.executed is True
    assert page.goto_calls == ["https://sub.example.test/page"]


@pytest.mark.asyncio
async def test_open_disallowed_domain_is_rejected():
    executor = NavigatorActionExecutor(FakeProvider())
    page = FakePage()
    with pytest.raises(ActionError) as exc:
        await executor.execute(page, None, {"action": "open", "url": "https://evil.test/"})
    assert exc.value.code == "domain_not_allowed"
    assert page.goto_calls == []


@pytest.mark.asyncio
async def test_open_missing_url_is_rejected():
    executor = NavigatorActionExecutor(FakeProvider())
    with pytest.raises(ActionError) as exc:
        await executor.execute(FakePage(), None, {"action": "open"})
    assert exc.value.code == "invalid_arguments"


@pytest.mark.asyncio
async def test_click_with_no_prior_observation_is_rejected():
    executor = NavigatorActionExecutor(FakeProvider())
    with pytest.raises(ActionError) as exc:
        await executor.execute(FakePage(), None, {"action": "click", "ref": "e1"})
    assert exc.value.code == "no_prior_observation"


@pytest.mark.asyncio
async def test_click_ref_not_in_observation_is_rejected():
    executor = NavigatorActionExecutor(FakeProvider())
    observation = _observation("e1")
    with pytest.raises(ActionError) as exc:
        await executor.execute(FakePage(), observation, {"action": "click", "ref": "e99"})
    assert exc.value.code == "unknown_ref"


@pytest.mark.asyncio
async def test_click_ref_that_no_longer_resolves_is_a_stale_ref():
    observation = _observation("e1")
    page = FakePage(locators={"e1": FakeLocator("e1", exists=False)})
    executor = NavigatorActionExecutor(FakeProvider())
    with pytest.raises(ActionError) as exc:
        await executor.execute(page, observation, {"action": "click", "ref": "e1"})
    assert exc.value.code == "stale_ref"


@pytest.mark.asyncio
async def test_click_resolves_and_clicks_the_live_element():
    locator = FakeLocator("e1", exists=True)
    observation = _observation("e1")
    page = FakePage(locators={"e1": locator})
    executor = NavigatorActionExecutor(FakeProvider())
    result = await executor.execute(page, observation, {"action": "click", "ref": "e1"})
    assert result.executed is True
    assert locator.clicked is True


@pytest.mark.asyncio
async def test_fill_requires_text_and_marks_search_action():
    locator = FakeLocator("e2", exists=True)
    observation = _observation("e2")
    page = FakePage(locators={"e2": locator})
    executor = NavigatorActionExecutor(FakeProvider())

    with pytest.raises(ActionError) as exc:
        await executor.execute(page, observation, {"action": "fill", "ref": "e2"})
    assert exc.value.code == "invalid_arguments"

    result = await executor.execute(page, observation, {"action": "fill", "ref": "e2", "text": "Camry"})
    assert locator.filled == "Camry"
    assert result.is_search_action is True


@pytest.mark.asyncio
async def test_press_requires_key_and_marks_search_action_for_enter():
    locator = FakeLocator("e3", exists=True)
    observation = _observation("e3")
    page = FakePage(locators={"e3": locator})
    executor = NavigatorActionExecutor(FakeProvider())

    with pytest.raises(ActionError) as exc:
        await executor.execute(page, observation, {"action": "press", "ref": "e3"})
    assert exc.value.code == "invalid_arguments"

    result = await executor.execute(page, observation, {"action": "press", "ref": "e3", "key": "Enter"})
    assert locator.pressed == "Enter"
    assert result.is_search_action is True
