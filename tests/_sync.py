"""
Determinism shims for tier-1: make the pipeline's concurrency synchronous.

Production fires memory writes as fire-and-forget daemon threads (never joined)
and defers the reply behind a `threading.Timer` in message_buffer. Both are
races against test assertions. These shims replace, *only inside the target
modules' namespaces* (never global threading), the pieces that introduce
nondeterminism:

  - SyncThread     : runs the target inline on .start(), swallowing exceptions
                     exactly like a daemon thread would (a raising background
                     write must not break the foreground turn).
  - FakeTimer      : does NOT run on .start(); it registers itself so the test
                     driver can fire the buffer flush deterministically. (The
                     real Timer callback re-enters message_buffer._lock, which
                     buffer_message still holds at .start() time — running
                     inline there would deadlock. Deferring mirrors reality.)
  - make_threading_shim: an object that proxies attribute access to the real
                     threading module but overrides the chosen names, so a
                     module doing `import threading; threading.Thread(...)`
                     picks up SyncThread without disturbing anyone else.
"""

from __future__ import annotations

import logging
import threading as _real_threading

logger = logging.getLogger("cued.tests.sync")

# phone -> FakeTimer, populated on .start(), fired by the driver's flush().
PENDING_TIMERS: dict = {}


class SyncThread:
    """Drop-in for threading.Thread that runs the target synchronously."""

    def __init__(self, group=None, target=None, name=None, args=(), kwargs=None,
                 daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self.daemon = daemon
        self.name = name

    def start(self):
        if self._target is None:
            return
        try:
            self._target(*self._args, **self._kwargs)
        except Exception:  # a daemon thread's exception never reaches the caller
            logger.exception("SyncThread target raised (swallowed, as a daemon thread would)")

    def join(self, timeout=None):
        return None

    def is_alive(self):
        return False


class FakeTimer:
    """Drop-in for threading.Timer that registers instead of running."""

    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.args = args if args is not None else []
        self.kwargs = kwargs if kwargs is not None else {}
        self.daemon = None
        # message_buffer keys everything by phone, which it passes as args[0].
        self._key = self.args[0] if self.args else None

    def start(self):
        if self._key is not None:
            PENDING_TIMERS[self._key] = self

    def cancel(self):
        if self._key is not None:
            PENDING_TIMERS.pop(self._key, None)

    def fire(self):
        return self.function(*self.args, **self.kwargs)


class _ThreadingShim:
    """Proxies to the real threading module, overriding selected attributes."""

    def __init__(self, overrides: dict):
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(_real_threading, name)


def make_threading_shim(**overrides) -> _ThreadingShim:
    return _ThreadingShim(overrides)


def clear_pending():
    PENDING_TIMERS.clear()
