"""Read-only access to X Omni's saved licensed-provider credential.

X Omni collects the ALLDATA credential through its own secure card and stores
it in Windows Credential Manager under ``XOmni/ResearchProvider/ALLDATA``. The
Navigator here needs the same secret to sign a fresh browser profile in, but it
is emphatically not the owner of it: this module reads and never writes, so
there is exactly one place a credential can be created, changed, or revoked.

The secret is handled under three rules, and every public shape in this module
is built to keep them true:

* it is never returned through the ScrapeX HTTP API,
* it is never persisted to ScrapeX's store, logs, or task state,
* it never enters model context.

``status()`` exists so callers can say "a credential is configured" without
touching the secret at all, and ``read()`` -- the only function that yields it
-- is called solely by the login path, which discards it in a ``finally``.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Any, Optional

# The target name is X Omni's, not ours. Changing it here silently decouples
# the Navigator from the credential Otis actually saved.
ALLDATA_CREDENTIAL_TARGET = "XOmni/ResearchProvider/ALLDATA"

CRED_TYPE_GENERIC = 1
ERROR_NOT_FOUND = 1168


class CredentialUnavailable(RuntimeError):
    """The vault could not be consulted -- distinct from 'nothing is saved'.

    A missing credential is a normal, actionable state (ask Otis to save one).
    A vault that cannot be read at all is an environment fault, and conflating
    the two would report a healthy host as unconfigured.
    """


if hasattr(ctypes, "windll"):

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    _PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)
else:  # pragma: no cover - exercised by the portable (non-Windows) suite
    _CREDENTIALW = None  # type: ignore[assignment]
    _PCREDENTIALW = None  # type: ignore[assignment]


class WindowsCredentialReader:
    """Reader for one Windows Credential Manager generic credential."""

    def __init__(self, target: str = ALLDATA_CREDENTIAL_TARGET):
        self.target = target
        self._advapi = None
        if not hasattr(ctypes, "windll"):
            return
        advapi = ctypes.windll.advapi32
        advapi.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_PCREDENTIALW),
        ]
        advapi.CredReadW.restype = wintypes.BOOL
        advapi.CredFree.argtypes = [ctypes.c_void_p]
        advapi.CredFree.restype = None
        self._advapi = advapi

    @property
    def available(self) -> bool:
        return self._advapi is not None

    def read(self) -> Optional[tuple[str, str]]:
        """Return ``(username, password)``, or None when nothing is saved.

        The caller must not retain, log, or return the result.
        """
        if self._advapi is None:
            raise CredentialUnavailable(
                "Windows Credential Manager is unavailable on this host."
            )
        pointer = _PCREDENTIALW()
        if not self._advapi.CredReadW(
            self.target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)
        ):
            error = ctypes.get_last_error()
            if error == ERROR_NOT_FOUND:
                return None
            raise CredentialUnavailable(
                f"Windows Credential Manager read failed (error {error})."
            )
        try:
            credential = pointer.contents
            username = str(credential.UserName or "")
            raw = ctypes.string_at(
                credential.CredentialBlob, credential.CredentialBlobSize
            )
            return username, raw.decode("utf-8")
        finally:
            self._advapi.CredFree(pointer)

    def status(self) -> dict[str, Any]:
        """Secret-free description, safe to return through the API."""
        try:
            found = self.read()
        except CredentialUnavailable as exc:
            return {
                "configured": False,
                "vault": "windows_credential_manager",
                "vault_available": False,
                "error": str(exc),
                "secret_exposed": False,
            }
        return {
            "configured": found is not None,
            "username": found[0] if found else "",
            "vault": "windows_credential_manager",
            "vault_available": True,
            "secret_exposed": False,
        }


ALLDATA_CREDENTIALS = WindowsCredentialReader()
