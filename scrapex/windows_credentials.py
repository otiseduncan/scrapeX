"""Read X Omni's ALLDATA credential from Windows Credential Manager.

ScrapeX never accepts this secret through HTTP, model arguments, environment
variables, logs, or persisted Navigator state. The only consumer is the
provider-side ALLDATA login helper immediately before a Navigator observation.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Optional

ALLDATA_CREDENTIAL_TARGET = "XOmni/ResearchProvider/ALLDATA"
_CRED_TYPE_GENERIC = 1


class CredentialReadError(RuntimeError):
    pass


class _CREDENTIAL_ATTRIBUTEW(ctypes.Structure):
    _fields_ = [
        ("Keyword", wintypes.LPWSTR),
        ("Flags", wintypes.DWORD),
        ("ValueSize", wintypes.DWORD),
        ("Value", ctypes.POINTER(ctypes.c_ubyte)),
    ]


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
        ("Attributes", ctypes.POINTER(_CREDENTIAL_ATTRIBUTEW)),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


_PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)


def read_alldata_credential() -> Optional[tuple[str, str]]:
    """Return the existing X Omni ALLDATA credential on Windows, if configured.

    The tuple must remain provider-internal. Callers must never serialize,
    persist, log, or return either value.
    """
    if not hasattr(ctypes, "WinDLL"):
        return None

    try:
        advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    except (OSError, AttributeError) as exc:
        raise CredentialReadError(
            "Windows Credential Manager is unavailable on this host."
        ) from exc

    advapi.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_PCREDENTIALW),
    ]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    advapi.CredFree.restype = None

    pointer = _PCREDENTIALW()
    if not advapi.CredReadW(
        ALLDATA_CREDENTIAL_TARGET,
        _CRED_TYPE_GENERIC,
        0,
        ctypes.byref(pointer),
    ):
        error = ctypes.get_last_error()
        if error == 1168:  # ERROR_NOT_FOUND
            return None
        raise CredentialReadError(
            f"Windows Credential Manager read failed (error {error})."
        )

    try:
        credential = pointer.contents
        username = str(credential.UserName or "").strip()
        raw = ctypes.string_at(
            credential.CredentialBlob,
            credential.CredentialBlobSize,
        )
        password = raw.decode("utf-8")
        if not username or not password:
            return None
        return username, password
    except (UnicodeDecodeError, ValueError) as exc:
        raise CredentialReadError(
            "The stored ALLDATA credential could not be decoded."
        ) from exc
    finally:
        advapi.CredFree(pointer)
