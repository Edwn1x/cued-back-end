"""
The one place an Anthropic client is constructed.

2026-09-11 incident: seventeen `Anthropic(api_key=…)` constructions across the repo,
none with a timeout. The SDK default is 10 minutes with 2 retries, so one hung model
call can pin a thread for ~30 minutes — and any DB session held open across it sits
"idle in transaction" holding locks. During the PR #37 deploy exactly that connection
blocked migrate.py's ALTER TABLE, and every users-table read queued behind the ALTER.

Bounded here for everyone: ANTHROPIC_TIMEOUT_S / ANTHROPIC_MAX_RETRIES (config).
Tests patch `anthropic.resources.messages.Messages.create`, so this stays transparent.
"""

from __future__ import annotations

import anthropic

import config


def make_client(**overrides) -> anthropic.Anthropic:
    kwargs = dict(api_key=config.ANTHROPIC_API_KEY,
                  timeout=config.ANTHROPIC_TIMEOUT_S,
                  max_retries=config.ANTHROPIC_MAX_RETRIES)
    kwargs.update(overrides)
    return anthropic.Anthropic(**kwargs)
