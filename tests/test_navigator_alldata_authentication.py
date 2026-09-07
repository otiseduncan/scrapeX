"""The ALLDATA Navigator must sign itself in, or fail closed to a human.

Two failures motivated this. First, the Navigator assumed its persistent
profile was already logged in, so a fresh or expired profile had no way to
proceed even though X Omni already holds a saved ALLDATA credential. Second --
and worse -- `observe()` returned whatever was on screen with no authentication
check at all, so the ALLDATA *login page* reached the model as an ordinary
observation. A reasoning model cannot tell that apart from "this procedure is
not in this source", which turns a licensed source into a silent false
negative.

So: sign in from the saved credential when possible, never bypass a provider
challenge, and never hand back an observation taken from an unauthenticated
page. The secret must not appear in any returned shape, task state, or error.
"""

from __future__ import annotations

import json

import pytest

from scrapex import provider_credentials
from scrapex.alldata_navigator import AlldataNavigatorProvider
from scrapex.navigator_worker import NavigatorTaskError, NavigatorTaskRunner

USERNAME = "otis@example.test"
PASSWORD = "correct-horse-battery-staple"


# --------------------------------------------------------------------------
# page doubles
# --------------------------------------------------------------------------


class _Loc:
    def __init__(self, visible: bool = False, count: int = 0, page=None):
        self._visible = visible
        self._count = count
        self._page = page
        self.first = self

    async def is_visible(self, timeout=None):
        return self._visible

    async def count(self):
        return self._count

    async def fill(self, value):
        if self._page is not None:
            self._page.filled.append(value)

    async def click(self):
        if self._page is not None:
            self._page.submitted = True
            self._page.on_submit()

    async def press(self, _key):
        if self._page is not None:
            self._page.submitted = True
            self._page.on_submit()


class FakePage:
    """Minimal Playwright page stand-in driven by a login-visible flag."""

    def __init__(self, *, logged_in: bool = False, succeeds_on_submit: bool = True):
        self.logged_in = logged_in
        self.succeeds_on_submit = succeeds_on_submit
        self.filled: list[str] = []
        self.submitted = False
        self.url = "https://my.alldata.com/"

    def on_submit(self):
        if self.succeeds_on_submit:
            self.logged_in = True

    def locator(self, selector: str):
        if "password" in selector:
            return _Loc(visible=not self.logged_in, page=self)
        if "submit" in selector:
            return _Loc(visible=not self.logged_in, count=1, page=self)
        return _Loc(visible=not self.logged_in, page=self)

    def get_by_text(self, _pattern):
        # The "Log In" control is present exactly while signed out.
        return _Loc(visible=not self.logged_in, page=self)

    async def title(self):
        return "ALLDATA"


@pytest.fixture
def provider():
    return AlldataNavigatorProvider("https://my.alldata.com/")


@pytest.fixture
def saved_credential(monkeypatch):
    monkeypatch.setattr(
        provider_credentials.ALLDATA_CREDENTIALS,
        "read",
        lambda: (USERNAME, PASSWORD),
    )


@pytest.fixture
def no_credential(monkeypatch):
    monkeypatch.setattr(
        provider_credentials.ALLDATA_CREDENTIALS, "read", lambda: None
    )


# --------------------------------------------------------------------------
# sign-in behavior
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_already_signed_in_profile_is_left_alone(provider, monkeypatch):
    def fail(*_a, **_k):
        raise AssertionError("an authenticated session must not touch the vault")

    monkeypatch.setattr(provider_credentials.ALLDATA_CREDENTIALS, "read", fail)
    page = FakePage(logged_in=True)

    result = await provider.ensure_authenticated(page)

    assert result["authenticated"] is True
    assert result["method"] == "existing_session"
    assert page.submitted is False


@pytest.mark.asyncio
async def test_a_signed_out_profile_signs_in_from_the_saved_credential(
    provider, saved_credential
):
    page = FakePage(logged_in=False)

    result = await provider.ensure_authenticated(page)

    assert result["authenticated"] is True
    assert result["method"] == "saved_credential"
    assert page.submitted is True
    assert USERNAME in page.filled and PASSWORD in page.filled


@pytest.mark.asyncio
async def test_a_missing_saved_credential_is_an_actionable_blocker(
    provider, no_credential
):
    page = FakePage(logged_in=False)

    result = await provider.ensure_authenticated(page)

    assert result["authenticated"] is False
    assert result["method"] == "credential_missing"
    assert result["requires_human"] is True
    assert page.submitted is False


@pytest.mark.asyncio
async def test_an_unreadable_vault_is_not_reported_as_unconfigured(
    provider, monkeypatch
):
    """A broken vault and an empty vault need different answers."""

    def unavailable():
        raise provider_credentials.CredentialUnavailable("vault is unavailable")

    monkeypatch.setattr(
        provider_credentials.ALLDATA_CREDENTIALS, "read", unavailable
    )
    result = await provider.ensure_authenticated(FakePage(logged_in=False))

    assert result["authenticated"] is False
    assert result["method"] == "vault_unavailable"
    assert result["requires_human"] is True


@pytest.mark.asyncio
async def test_a_provider_challenge_is_never_reported_as_signed_in(
    provider, saved_credential
):
    """MFA, CAPTCHA, and a rejected password all survive the submit."""
    page = FakePage(logged_in=False, succeeds_on_submit=False)

    result = await provider.ensure_authenticated(page)

    assert result["authenticated"] is False
    assert result["method"] == "challenge_pending"
    assert result["requires_human"] is True
    assert page.submitted is True  # attempted, not bypassed


# --------------------------------------------------------------------------
# the secret must not escape
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("logged_in,succeeds", [(False, True), (False, False)])
async def test_no_returned_shape_contains_the_secret(
    provider, saved_credential, logged_in, succeeds
):
    page = FakePage(logged_in=logged_in, succeeds_on_submit=succeeds)
    result = await provider.ensure_authenticated(page)

    serialized = json.dumps(result)
    assert PASSWORD not in serialized
    assert USERNAME not in serialized


def test_credential_status_reports_configuration_without_the_secret(monkeypatch):
    monkeypatch.setattr(
        provider_credentials.ALLDATA_CREDENTIALS,
        "read",
        lambda: (USERNAME, PASSWORD),
    )
    status = provider_credentials.ALLDATA_CREDENTIALS.status()

    assert status["configured"] is True
    assert status["secret_exposed"] is False
    assert PASSWORD not in json.dumps(status)


def test_the_reader_owns_no_write_path():
    """X Omni owns creating and revoking the credential; ScrapeX only reads."""
    reader = provider_credentials.WindowsCredentialReader()
    assert not hasattr(reader, "write")
    assert not hasattr(reader, "delete")
    assert (
        provider_credentials.ALLDATA_CREDENTIAL_TARGET
        == "XOmni/ResearchProvider/ALLDATA"
    )


# --------------------------------------------------------------------------
# the gate: no observation from an unauthenticated page
# --------------------------------------------------------------------------


class _StubProvider:
    slug = "alldata"
    home_url = "https://my.alldata.com/"
    allowed_domain_suffixes = ("alldata.com",)

    def __init__(self, ensure_result):
        self._ensure_result = ensure_result
        self.ensure_calls = 0

    async def ensure_authenticated(self, _page):
        self.ensure_calls += 1
        return self._ensure_result

    def is_search_action(self, _action):
        return False

    def match_terms(self, _text, _topic):
        return [], 0


class _StubBrowser:
    def __init__(self):
        self.pages_handed_out = 0

    async def page_for(self, _slug, home_url=None):
        self.pages_handed_out += 1
        return FakePage(logged_in=False)


def _runner(ensure_result):
    provider = _StubProvider(ensure_result)
    return NavigatorTaskRunner(_StubStore(), _StubBrowser(), provider), provider


class _StubStore:
    def navigator_task(self, task_id):
        return {
            "id": task_id,
            "state": "active",
            "graph": {},
            "target": {},
            "step_count": 0,
            "action_budget": 50,
            "last_observation": None,
        }


@pytest.mark.asyncio
async def test_observe_refuses_to_return_an_unauthenticated_page():
    runner, _ = _runner(
        {
            "authenticated": False,
            "method": "challenge_pending",
            "reason": "MFA must be completed by a person.",
        }
    )

    with pytest.raises(NavigatorTaskError) as excinfo:
        await runner.observe("task-1")

    # X Omni distinguishes an ALLDATA authentication blocker by this code plus
    # the provider name; unmapped codes surface as HTTP 409.
    assert excinfo.value.code == "authentication_required"
    lowered = excinfo.value.message.casefold()
    assert "alldata" in lowered
    assert "authentication" in lowered
    assert "MFA must be completed by a person." in excinfo.value.message


@pytest.mark.asyncio
async def test_act_refuses_to_run_against_an_unauthenticated_page():
    runner, _ = _runner({"authenticated": False, "reason": "signed out"})

    with pytest.raises(NavigatorTaskError) as excinfo:
        await runner.act("task-1", {"action": "click", "ref": "e1"})

    assert excinfo.value.code == "authentication_required"


@pytest.mark.asyncio
async def test_the_blocker_never_carries_credential_material():
    runner, _ = _runner(
        {"authenticated": False, "reason": "A provider challenge is pending."}
    )

    with pytest.raises(NavigatorTaskError) as excinfo:
        await runner.observe("task-1")

    assert PASSWORD not in excinfo.value.message
    assert USERNAME not in excinfo.value.message


@pytest.mark.asyncio
async def test_a_provider_without_the_hook_keeps_its_previous_behavior():
    """The gate must not break providers that never declared it."""

    class _NoHook(_StubProvider):
        ensure_authenticated = None

    provider = _NoHook({"authenticated": False})
    provider.ensure_authenticated = None
    runner = NavigatorTaskRunner(_StubStore(), _StubBrowser(), provider)

    page = await runner._authenticated_page()

    assert page is not None
