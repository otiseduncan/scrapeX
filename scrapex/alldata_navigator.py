"""ALLDATA provider adapter for the Navigator.

Ports the existing, dormant vehicle-identity heuristics in ``alldata.py``
(``verify_selected_vehicle``, ``vehicle_matches``) rather than re-implementing
a fourth version of vehicle matching -- ScrapeX already has one, it was just
never wired into a live path.
"""

from __future__ import annotations

import re
from typing import Any

from . import alldata as alldata_heuristics
from .models import VehicleSpec
from .navigator_observation import build_observation
from .provider_credentials import ALLDATA_CREDENTIALS, CredentialUnavailable

PICKER_URL = "https://my.alldata.com/repair/#/select-vehicle"
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_TITLE_SUFFIXES = (" - ALLDATA Collision", " - ALLDATA Repair", " - ALLDATA")
_TITLE_PREFIXES = ("Vehicle Information - ",)
# Where the picker's own search box may live. Selectors describe the field
# by its type and role, not by ALLDATA's markup, so they survive a restyle.
_SEARCH_BOX_SELECTORS = (
    "input[type='search']",
    "[role='searchbox']",
    "input[placeholder*='VIN' i]",
    "input[placeholder*='Search' i]",
    "input[type='text']",
)
_VIN_RESOLVE_POLLS = 24
_VIN_RESOLVE_POLL_MS = 500
_VIN_CONFIRM_POLLS = 8
_VIN_CONFIRM_POLL_MS = 250

async def _aria_signal_candidates(page: Any) -> list[str]:
    """Fallback candidate source using the same aria-snapshot mechanism the
    Navigator's own observation already uses reliably.

    Confirmed live: alldata.py's selected_vehicle_signal (its CSS-selector
    scan plus "Change/Selected/Current Vehicle" text patterns) was written
    against an older ALLDATA layout and finds zero candidates on the
    current live UI even when a vehicle is plainly selected in the header
    -- it silently falls back to just the page <title>, which never
    contains vehicle text. The aria-snapshot element list does contain it.
    """
    observation = await build_observation(page)
    return [element.name for element in observation.elements if element.name]


class AlldataNavigatorProvider:
    slug = "alldata"

    def __init__(self, home_url: str):
        self.home_url = home_url
        self.allowed_domain_suffixes = ("alldata.com",)
        self.picker_url = PICKER_URL

    @staticmethod
    def display_title(title: str) -> str:
        """The page's own title without the provider's suffix or prefix."""
        text = " ".join(str(title or "").split())
        for prefix in _TITLE_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):]
        for suffix in _TITLE_SUFFIXES:
            if text.endswith(suffix):
                text = text[: -len(suffix)]
        return text.strip()

    async def select_vehicle(self, page: Any, action: dict[str, Any]) -> dict[str, Any]:
        """Select the exact vehicle a VIN names, mechanically.

        A VIN identifies one vehicle including trim and engine, which neither
        a year/make/model cascade nor ALLDATA's own "<Make> Truck" shelving
        can do. ALLDATA's picker ignores a value set programmatically (one
        synthetic input event, no reaction) and its submit control carries no
        accessible name, so the VIN is typed as keystrokes and the page is
        given time to resolve on its own. Nothing here chooses a vehicle: the
        VIN was supplied by the caller and the outcome is reported as it is.
        """
        vin = "".join(str(action.get("vin") or "").split()).upper()
        if not _VIN_RE.match(vin):
            return {"selected": False, "reason": f"{vin!r} is not a 17-character VIN."}
        try:
            await page.goto(self.picker_url, wait_until="load")
            await page.wait_for_timeout(1500)
        except Exception as exc:  # noqa: BLE001 - reported, never hidden
            return {"selected": False, "reason": f"The vehicle picker did not open: {type(exc).__name__}."}
        box = None
        for selector in _SEARCH_BOX_SELECTORS:
            candidate = page.locator(selector).first
            try:
                if await candidate.is_visible(timeout=1500):
                    box = candidate
                    break
            except Exception:
                continue
        if box is None:
            return {"selected": False, "reason": "The vehicle search box was not on the picker."}
        try:
            await box.click(timeout=5_000)
            await page.keyboard.type(vin, delay=25)
        except Exception as exc:  # noqa: BLE001
            return {"selected": False, "reason": f"Typing the VIN failed: {type(exc).__name__}."}
        url = ""
        for _ in range(_VIN_RESOLVE_POLLS):
            await page.wait_for_timeout(_VIN_RESOLVE_POLL_MS)
            url = str(page.url or "")
            if "/vehicle/" in url:
                break
        if "/vehicle/" not in url:
            return {
                "selected": False,
                "vin": vin,
                "url": url,
                "reason": f"ALLDATA did not resolve VIN {vin} to a vehicle page.",
            }
        # A /vehicle/ route alone is not proof that the VIN search landed. The
        # provider can restore the previously open vehicle while its picker is
        # settling, which used to make this fast path report selected=True for
        # a stale, different VIN. ALLDATA renders the resolved VIN in the
        # vehicle header, so require that exact value before returning success.
        title = ""
        observed_text = ""
        for _ in range(_VIN_CONFIRM_POLLS):
            try:
                title = await page.title()
            except Exception:
                title = ""
            try:
                observed_text = await page.locator("body").inner_text(timeout=2_500)
            except Exception:
                observed_text = ""
            if vin in "".join(str(observed_text or "").split()).upper():
                return {
                    "selected": True,
                    "vin": vin,
                    "label": self.display_title(title),
                    "url": str(page.url or url),
                }
            await page.wait_for_timeout(_VIN_CONFIRM_POLL_MS)
        return {
            "selected": False,
            "vin": vin,
            "label": self.display_title(title),
            "url": str(page.url or url),
            "reason": (
                f"ALLDATA opened a vehicle page, but it did not show the requested VIN {vin}."
            ),
        }

    async def authenticated(self, page: Any) -> bool:
        """Fail closed: a title-only check is not proof.

        Confirmed live against a fresh, never-signed-in profile: ALLDATA's
        login page's own <title> is just "ALLDATA" -- it contains neither
        "login" nor "sign in" -- so a title-substring check alone reports
        "authenticated" while a password field is plainly on screen. A
        visible password input or a "Log In" control is the actual signal.
        """
        try:
            password_field = page.locator("input[type='password']").first
            if await password_field.is_visible(timeout=500):
                return False
        except Exception:
            pass
        try:
            login_control = page.get_by_text(re.compile(r"\bLog\s*In\b", re.I)).first
            if await login_control.is_visible(timeout=400):
                return False
        except Exception:
            pass
        try:
            title = (await page.title() or "").casefold()
        except Exception:
            return False
        return "login" not in title and "sign in" not in title

    async def ensure_authenticated(self, page: Any) -> dict[str, Any]:
        """Bring the profile to a signed-in state, or report a human blocker.

        The persistent profile is usually already authenticated, in which case
        this is a cheap check. When it is not, the Navigator used to simply
        hand the model whatever was on screen -- which is the ALLDATA *login
        page*, semantically indistinguishable to a reasoning model from "this
        procedure does not exist here". Signing in with the credential Otis
        already saved keeps that from becoming a dead end.

        Never bypasses a human gate: MFA, CAPTCHA, and any provider challenge
        that survives the fill leave the browser open and return a blocker for
        a person to finish. The returned dict is API/model-facing and so
        carries booleans and reasons only, never the secret.
        """
        if await self.authenticated(page):
            return {"authenticated": True, "method": "existing_session"}

        try:
            credential = ALLDATA_CREDENTIALS.read()
        except CredentialUnavailable as exc:
            return {
                "authenticated": False,
                "method": "vault_unavailable",
                "requires_human": True,
                "reason": str(exc),
            }
        if credential is None:
            return {
                "authenticated": False,
                "method": "credential_missing",
                "requires_human": True,
                "reason": (
                    "No saved ALLDATA credential. Save one in X Omni's ALLDATA "
                    "setup card, then retry."
                ),
            }

        filled = await self._submit_saved_login(page, credential)
        if not filled:
            return {
                "authenticated": False,
                "method": "login_form_not_found",
                "requires_human": True,
                "reason": (
                    "ALLDATA is not signed in and no login form was present to "
                    "complete automatically."
                ),
            }

        # Re-check rather than trusting the submit. A wrong password, an MFA
        # step, or a CAPTCHA all look like a successful click.
        if await self.authenticated(page):
            return {"authenticated": True, "method": "saved_credential"}
        return {
            "authenticated": False,
            "method": "challenge_pending",
            "requires_human": True,
            "reason": (
                "ALLDATA did not complete sign-in with the saved credential. A "
                "provider challenge (MFA, CAPTCHA, or a rejected credential) "
                "must be completed by a person in the visible Navigator browser."
            ),
        }

    async def _submit_saved_login(
        self, page: Any, credential: tuple[str, str]
    ) -> bool:
        """Fill and submit the login form. Returns whether it was attempted.

        Selectors are deliberately generic. ALLDATA has changed its sign-in UI
        before, and role/type survive that better than a CSS class does.
        """
        username, password = credential
        try:
            password_box = page.locator("input[type='password']").first
            try:
                if not await password_box.is_visible(timeout=3_000):
                    return False
            except Exception:
                return False

            username_box = None
            for selector in (
                "input[type='email']",
                "input[name*='user' i]",
                "input[id*='user' i]",
                "input[name*='email' i]",
                "input[id*='email' i]",
                "input[type='text']",
            ):
                candidate = page.locator(selector).first
                try:
                    if await candidate.is_visible(timeout=500):
                        username_box = candidate
                        break
                except Exception:
                    continue

            if username_box is not None:
                await username_box.fill(username)
            await password_box.fill(password)

            submit = page.locator("button[type='submit'], input[type='submit']").first
            try:
                if await submit.count():
                    await submit.click()
                else:
                    await password_box.press("Enter")
            except Exception:
                await password_box.press("Enter")

            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass
            return True
        finally:
            # Drop local references promptly; nothing here is returned or stored.
            username = ""
            password = ""
            credential = ("", "")

    async def target_signal(self, page: Any, target: dict[str, Any]) -> dict[str, Any]:
        vehicle = VehicleSpec(
            year=target.get("year"),
            make=target.get("make") or "",
            model=target.get("model") or "",
            trim=target.get("trim"),
            vin=target.get("vin"),
        )
        result = await alldata_heuristics.verify_selected_vehicle(page, vehicle)
        if result.get("verified"):
            return {"selected": True, "reason": None, "label": result.get("label")}
        for candidate in await _aria_signal_candidates(page):
            if alldata_heuristics.vehicle_matches(candidate, vehicle):
                return {"selected": True, "reason": None, "label": candidate}
        return {
            "selected": False,
            "reason": "ALLDATA vehicle selection was not confirmed.",
            "label": result.get("label"),
        }

    async def current_page_signals(self, page: Any) -> list[str]:
        """Bounded, generic "what vehicle is on screen" text signals.

        Not bound to any specific candidate vehicle -- callers (e.g.
        Calibration IQ work-prep matching) check many candidate rows against
        this same bounded signal list, mirroring the synchronous read this
        replaced.
        """
        signal = await alldata_heuristics.selected_vehicle_signal(page)
        candidates = list(signal.get("candidates") or [])
        if not candidates:
            candidates = await _aria_signal_candidates(page)
        return candidates

    def is_search_action(self, action: dict[str, Any]) -> bool:
        kind = action.get("action")
        if kind in {"fill", "type", "select_vehicle"}:
            return True
        if kind == "press" and str(action.get("key") or "").casefold() == "enter":
            return True
        return False
