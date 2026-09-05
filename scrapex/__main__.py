from __future__ import annotations

import uvicorn

from .config import Settings


def main() -> None:
    settings = Settings.load()
    uvicorn.run(
        "scrapex.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
