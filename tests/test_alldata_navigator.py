import pytest

from scrapex.alldata_navigator import AlldataNavigatorProvider


class _EmptyLocs:
    first = None

    def __init__(self):
        self.first = _InvisibleLoc()

    async def count(self):
        return 0

    def nth(self, i):
        raise AssertionError("count() was 0; nth() must not be called")


class _InvisibleLoc:
    first = None

    def __init__(self):
        self.first = self

    async def is_visible(self, timeout=None):
        return False

    def locator(self, selector):
        return self


class _VisibleLoc:
    def __init__(self):
        self.first = self

    async def is_visible(self, timeout=None):
        return True

    def locator(self, selector):
        return self


class _AriaSnapshotLoc:
    async def aria_snapshot(self, mode=None):
        return ""


class _FakePage:
    def __init__(self, title: str, *, password_visible: bool = False, login_text_visible: bool = False):
        self._title = title
        self._password_visible = password_visible
        self._login_text_visible = login_text_visible
        self.url = "https://my.alldata.com/current"
        self.frames = [self]

    def locator(self, selector):
        if selector == "input[type='password']" and self._password_visible:
            return _VisibleLoc()
        if selector == "body":
            return _AriaSnapshotLoc()
        return _EmptyLocs()

    def get_by_text(self, pattern, exact=None):
        if self._login_text_visible:
            return _VisibleLoc()
        return _InvisibleLoc()

    async def inner_text(self, selector):
        return ""

    async def title(self):
        return self._title


@pytest.mark.asyncio
async def test_authenticated_is_false_when_a_password_field_is_visible():
    # Confirmed live: a fresh ALLDATA login page's own <title> is just
    # "ALLDATA" -- a title-substring check alone would report this page as
    # authenticated even though a password field is plainly on screen.
    provider = AlldataNavigatorProvider("https://my.alldata.com/")
    page = _FakePage("ALLDATA", password_visible=True)
    assert await provider.authenticated(page) is False


@pytest.mark.asyncio
async def test_authenticated_is_false_when_a_log_in_control_is_visible():
    provider = AlldataNavigatorProvider("https://my.alldata.com/")
    page = _FakePage("ALLDATA", login_text_visible=True)
    assert await provider.authenticated(page) is False


@pytest.mark.asyncio
async def test_authenticated_is_true_past_the_login_page():
    provider = AlldataNavigatorProvider("https://my.alldata.com/")
    page = _FakePage("ALLDATA Collision - Home")
    assert await provider.authenticated(page) is True


@pytest.mark.asyncio
async def test_authenticated_is_false_on_login_titled_page_with_no_dom_markers():
    provider = AlldataNavigatorProvider("https://my.alldata.com/")
    page = _FakePage("Sign In - ALLDATA")
    assert await provider.authenticated(page) is False


@pytest.mark.asyncio
async def test_target_signal_falls_back_to_aria_snapshot_when_dom_selectors_find_nothing(monkeypatch):
    # Confirmed live: alldata.py's CSS-selector heuristic finds zero
    # candidates on the current ALLDATA UI even with a vehicle plainly
    # selected in the header -- the aria-snapshot fallback must catch it.
    import scrapex.alldata_navigator as mod

    async def fake_verify(page, vehicle):
        return {"verified": False, "label": None, "candidates": []}

    async def fake_aria_candidates(page):
        return ["2020 Mercedes Benz E 350 4MATIC Sedan (213.084)"]

    monkeypatch.setattr(mod.alldata_heuristics, "verify_selected_vehicle", fake_verify)
    monkeypatch.setattr(mod, "_aria_signal_candidates", fake_aria_candidates)

    provider = mod.AlldataNavigatorProvider("https://my.alldata.com/")
    result = await provider.target_signal(object(), {"year": 2020, "make": "BENZ", "model": "E 350"})
    assert result["selected"] is True
    assert result["reason"] is None
    assert "E 350" in result["label"]


@pytest.mark.asyncio
async def test_target_signal_is_false_when_neither_source_matches(monkeypatch):
    import scrapex.alldata_navigator as mod

    async def fake_verify(page, vehicle):
        return {"verified": False, "label": None}

    async def fake_aria_candidates(page):
        return ["Procedures - ALLDATA Collision"]

    monkeypatch.setattr(mod.alldata_heuristics, "verify_selected_vehicle", fake_verify)
    monkeypatch.setattr(mod, "_aria_signal_candidates", fake_aria_candidates)

    provider = mod.AlldataNavigatorProvider("https://my.alldata.com/")
    result = await provider.target_signal(object(), {"year": 2020, "make": "BENZ", "model": "E 350"})
    assert result["selected"] is False
    assert result["reason"]


@pytest.mark.asyncio
async def test_current_page_signals_falls_back_when_dom_selectors_find_nothing(monkeypatch):
    import scrapex.alldata_navigator as mod

    async def fake_signal(page):
        return {"candidates": [], "label": None}

    async def fake_aria_candidates(page):
        return ["2020 Mercedes Benz E 350 4MATIC Sedan (213.084)"]

    monkeypatch.setattr(mod.alldata_heuristics, "selected_vehicle_signal", fake_signal)
    monkeypatch.setattr(mod, "_aria_signal_candidates", fake_aria_candidates)

    provider = mod.AlldataNavigatorProvider("https://my.alldata.com/")
    signals = await provider.current_page_signals(object())
    assert signals == ["2020 Mercedes Benz E 350 4MATIC Sedan (213.084)"]


def test_is_search_action_matches_fill_and_enter_press():
    provider = AlldataNavigatorProvider("https://my.alldata.com/")
    assert provider.is_search_action({"action": "fill", "ref": "e1"}) is True
    assert provider.is_search_action({"action": "press", "key": "Enter"}) is True
    assert provider.is_search_action({"action": "press", "key": "Tab"}) is False
    assert provider.is_search_action({"action": "click", "ref": "e1"}) is False


def test_match_terms_ignores_stopwords_and_short_tokens():
    provider = AlldataNavigatorProvider("https://my.alldata.com/")
    matched, score = provider.match_terms(
        "Blind Spot Monitor Beam Axis Calibration Procedure for a 2023 Toyota Camry",
        "blind spot monitor calibration",
    )
    assert "blind" in matched
    assert "spot" in matched
    assert "monitor" in matched
    # "calibration" and "the"/"and"-style stopwords are filtered out.
    assert "calibration" not in matched
    assert score == len(matched)


def test_match_terms_accepts_semantic_oem_aliases():
    provider = AlldataNavigatorProvider("https://my.alldata.com/")
    matched, score = provider.match_terms(
        "Lane Change Assist Rear Side Radar Beam Axis Adjustment for 2023 Toyota Camry",
        "blind spot monitor calibration",
    )
    assert "system:blind_spot" in matched
    assert "operation:calibration" in matched
    assert score >= 2


def test_match_terms_accepts_front_radar_adjustment_for_calibration_request():
    provider = AlldataNavigatorProvider("https://my.alldata.com/")
    matched, score = provider.match_terms(
        "Adaptive Cruise Control Cruise Control Module Aiming Adjustment",
        "front radar calibration",
    )
    assert "system:front_radar" in matched
    assert "operation:calibration" in matched
    assert score >= 2


def test_match_terms_finds_nothing_on_an_unrelated_page():
    provider = AlldataNavigatorProvider("https://my.alldata.com/")
    matched, score = provider.match_terms(
        "General inspection notes: tire pressure and fluid levels.",
        "blind spot monitor calibration",
    )
    assert matched == []
    assert score == 0


@pytest.mark.asyncio
async def test_current_page_signals_falls_back_to_the_page_title():
    provider = AlldataNavigatorProvider("https://my.alldata.com/")
    page = _FakePage("2023 Toyota Camry - ALLDATA")
    signals = await provider.current_page_signals(page)
    assert signals == ["2023 Toyota Camry - ALLDATA"]


@pytest.mark.asyncio
async def test_current_page_signals_is_empty_when_nothing_is_found():
    provider = AlldataNavigatorProvider("https://my.alldata.com/")
    page = _FakePage("")
    signals = await provider.current_page_signals(page)
    assert signals == []


class _LoginElement:
    def __init__(self, page, kind: str):
        self.page = page
        self.kind = kind
        self.first = self

    async def is_visible(self, timeout=None):
        if self.kind == "username":
            return self.page.stage == "login"
        if self.kind == "password":
            return self.page.stage == "login"
        if self.kind == "submit":
            return self.page.stage == "login"
        if self.kind == "challenge":
            return self.page.stage == "challenge"
        return False

    async def count(self):
        return 1 if await self.is_visible() else 0

    async def fill(self, value):
        if self.kind == "username":
            self.page.username = value
        elif self.kind == "password":
            self.page.password = value
        else:
            raise AssertionError(f"cannot fill {self.kind}")

    async def click(self):
        if self.kind != "submit":
            raise AssertionError(f"cannot click {self.kind}")
        self.page.submit()

    async def press(self, key):
        if key != "Enter":
            raise AssertionError(key)
        self.page.submit()


class _CredentialLoginPage:
    def __init__(self, *, challenge=False, authenticated=False):
        self.stage = "authenticated" if authenticated else "login"
        self.challenge_after_submit = challenge
        self.username = ""
        self.password = ""

    @property
    def url(self):
        if self.stage == "authenticated":
            return "https://my.alldata.com/repair/"
        if self.stage == "challenge":
            return "https://my.alldata.com/challenge"
        return "https://my.alldata.com/login"

    def locator(self, selector):
        lowered = selector.casefold()
        if "one-time-code" in lowered or "otp" in lowered or "verification" in lowered or "captcha" in lowered:
            return _LoginElement(self, "challenge")
        if "password" in lowered:
            return _LoginElement(self, "password")
        if "button[type='submit']" in lowered or "input[type='submit']" in lowered:
            return _LoginElement(self, "submit")
        if (
            "type='email'" in lowered
            or "name*='user'" in lowered
            or "id*='user'" in lowered
            or "name*='email'" in lowered
            or "id*='email'" in lowered
            or "type='text'" in lowered
        ):
            return _LoginElement(self, "username")
        return _EmptyLocs()

    def get_by_text(self, pattern, exact=None):
        text = getattr(pattern, "pattern", str(pattern)).casefold()
        if self.stage == "login" and ("log" in text or "sign" in text):
            return _LoginElement(self, "submit")
        if self.stage == "challenge" and (
            "verification" in text or "factor" in text or "captcha" in text or "identity" in text
        ):
            return _LoginElement(self, "challenge")
        return _InvisibleLoc()

    async def title(self):
        if self.stage == "authenticated":
            return "ALLDATA Collision - Home"
        if self.stage == "challenge":
            return "Verify Identity - ALLDATA"
        return "ALLDATA"

    async def wait_for_timeout(self, _milliseconds):
        return None

    async def wait_for_load_state(self, _state, timeout=None):
        return None

    def submit(self):
        self.stage = "challenge" if self.challenge_after_submit else "authenticated"


@pytest.mark.asyncio
async def test_saved_xomni_credential_logs_navigator_in_before_observation(monkeypatch):
    import scrapex.alldata_navigator as mod

    secret = "never-return-this-password"
    monkeypatch.setattr(
        mod,
        "read_alldata_credential",
        lambda: ("otis@example.com", secret),
    )
    provider = mod.AlldataNavigatorProvider("https://my.alldata.com/")
    page = _CredentialLoginPage()

    result = await provider.ensure_authenticated(page)

    assert result == {
        "authenticated": True,
        "credential_configured": True,
        "saved_login_attempted": True,
        "interactive_auth_required": False,
        "message": None,
    }
    assert page.username == "otis@example.com"
    assert page.password == secret
    assert secret not in repr(result)
    assert "otis@example.com" not in repr(result)


@pytest.mark.asyncio
async def test_saved_credential_does_not_bypass_mfa_or_expose_secret(monkeypatch):
    import scrapex.alldata_navigator as mod

    secret = "never-return-this-password"
    monkeypatch.setattr(
        mod,
        "read_alldata_credential",
        lambda: ("otis@example.com", secret),
    )
    provider = mod.AlldataNavigatorProvider("https://my.alldata.com/")
    page = _CredentialLoginPage(challenge=True)

    result = await provider.ensure_authenticated(page)

    assert result["authenticated"] is False
    assert result["credential_configured"] is True
    assert result["saved_login_attempted"] is True
    assert result["interactive_auth_required"] is True
    assert "interactive authentication" in result["message"].casefold()
    assert secret not in repr(result)
    assert "otis@example.com" not in repr(result)
    assert await provider.authenticated(page) is False


@pytest.mark.asyncio
async def test_already_authenticated_navigator_never_reads_saved_password(monkeypatch):
    import scrapex.alldata_navigator as mod

    def should_not_read():
        raise AssertionError("credential should not be read for an authenticated profile")

    monkeypatch.setattr(mod, "read_alldata_credential", should_not_read)
    provider = mod.AlldataNavigatorProvider("https://my.alldata.com/")
    page = _CredentialLoginPage(authenticated=True)

    result = await provider.ensure_authenticated(page)

    assert result["authenticated"] is True
    assert result["saved_login_attempted"] is False


@pytest.mark.asyncio
async def test_signed_out_navigator_without_saved_credential_requests_human_auth(monkeypatch):
    import scrapex.alldata_navigator as mod

    monkeypatch.setattr(mod, "read_alldata_credential", lambda: None)
    provider = mod.AlldataNavigatorProvider("https://my.alldata.com/")
    page = _CredentialLoginPage()

    result = await provider.ensure_authenticated(page)

    assert result["authenticated"] is False
    assert result["credential_configured"] is False
    assert result["interactive_auth_required"] is True
    assert "windows credential manager" in result["message"].casefold()


def test_scrapex_reads_the_same_alldata_credential_target_as_xomni():
    from scrapex.windows_credentials import ALLDATA_CREDENTIAL_TARGET

    assert ALLDATA_CREDENTIAL_TARGET == "XOmni/ResearchProvider/ALLDATA"
