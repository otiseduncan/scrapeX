from __future__ import annotations

import uvicorn

from .config import Settings
from .navigator_capture_integrity import install as install_capture_integrity
from .navigator_worker import NavigatorTaskRunner


def main() -> None:
    # The supported ``python -m scrapex`` runtime installs the mechanical
    # capture-integrity repair before uvicorn imports the application module.
    # No browser is opened here; the runner class is only patched in memory.
    install_capture_integrity(NavigatorTaskRunner)
    settings = Settings.load()
    uvicorn.run(
        "scrapex.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
