from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest

FIXTURE_SITE_ROOT = Path(__file__).parent / "fixtures" / "navigator_site"


@pytest.fixture(scope="session")
def navigator_fixture_server():
    """Serve the static Navigator test site on an ephemeral loopback port.

    Session-scoped: the site is static, so one server for the whole test
    run is fine and avoids port-churn across many tests.
    """
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(FIXTURE_SITE_ROOT)
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
