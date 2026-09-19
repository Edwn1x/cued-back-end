"""Import side-effect: register every wired provider with base.PROVIDERS.

Part 0 registers nothing (the framework stands alone; routes 404 for any
provider). Part 1 adds `from integrations import gcal  # noqa` here, Part 2 adds
`from integrations import strava  # noqa`. Keeping the imports in one place means
app.py's single `import integrations.providers_boot` picks up each provider as it
lands, and a provider file that fails to import can't take down the whole app.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("cued.integrations.boot")

# Providers register on import. Wrapped so a broken provider module degrades to
# "that connection is off" rather than crashing boot.
_PROVIDER_MODULES: list[str] = [
    "integrations.gcal",       # Part 1 — Google Calendar
    # "integrations.strava",   # Part 2
    # "integrations.bcourses", # Part 1.4 (no OAuth, but registers for status)
]

for _mod in _PROVIDER_MODULES:
    try:
        __import__(_mod)
    except Exception:  # pragma: no cover
        logger.exception("PROVIDER_IMPORT_FAILED module=%s", _mod)
