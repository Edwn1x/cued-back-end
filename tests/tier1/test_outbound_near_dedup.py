"""Paraphrased near-duplicate outbound dedup (buffer-race backstop, extended).

PR #119's guard only suppressed NORMALIZED-IDENTICAL consecutive replies. Live, the
buffer race produced PARAPHRASED near-duplicates moments apart ("got it — so pullups,
then u alternate bis and back / what are the actual bi and back moves" then "got it —
pullups, then bi/back alternating / what are the actual back and bicep moves u run").
These are similar-not-identical, so the old guard missed them. The extension adds a
HIGH-similarity check (max of Jaccard / char-ratio / token-sort-ratio) within the same
window + same user, above OUTBOUND_NEAR_DEDUP_THRESHOLD, while leaving genuinely
distinct replies alone. Conservative + fail-open.
"""
from __future__ import annotations

import pytest

import config
from sms import _is_duplicate_send, _similarity, reset_outbound_dedup

# The actual reported live pair.
PARA_A = ("got it — so pullups, then u alternate bis and back\n"
          "what are the actual bi and back moves")
PARA_B = ("got it — pullups, then bi/back alternating\n"
          "what are the actual back and bicep moves u run")


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "OUTBOUND_DEDUP_ENABLED", True)
    monkeypatch.setattr(config, "OUTBOUND_NEAR_DEDUP_ENABLED", True)
    monkeypatch.setattr(config, "OUTBOUND_DEDUP_WINDOW_S", 90)
    monkeypatch.setattr(config, "OUTBOUND_NEAR_DEDUP_THRESHOLD", 0.85)
    reset_outbound_dedup()
    yield
    reset_outbound_dedup()


def test_identical_still_suppressed():
    uid = 1001
    assert _is_duplicate_send(uid, "logged 3 eggs and toast") is False   # first send records
    assert _is_duplicate_send(uid, "logged 3 eggs and toast") is True    # exact repeat


def test_paraphrased_near_duplicate_suppressed():
    uid = 1002
    assert _is_duplicate_send(uid, PARA_A) is False   # first send records
    assert _is_duplicate_send(uid, PARA_B) is True    # paraphrase caught


def test_genuinely_distinct_messages_not_suppressed():
    uid = 1003
    assert _is_duplicate_send(uid, "how many sets are you doing today?") is False
    assert _is_duplicate_send(uid, "nice — that's a solid pull day, keep the rest short") is False
    # a third, unrelated message is also free to send
    assert _is_duplicate_send(uid, "want me to pull up your card?") is False


def test_near_dedup_flag_off_lets_paraphrase_through_but_identical_still_blocked(monkeypatch):
    monkeypatch.setattr(config, "OUTBOUND_NEAR_DEDUP_ENABLED", False)
    uid = 1004
    assert _is_duplicate_send(uid, PARA_A) is False
    assert _is_duplicate_send(uid, PARA_B) is False   # near-dup allowed through with flag off
    # but a byte-identical repeat is still caught by the base guard
    assert _is_duplicate_send(uid, PARA_B) is True


def test_master_flag_off_disables_everything(monkeypatch):
    monkeypatch.setattr(config, "OUTBOUND_DEDUP_ENABLED", False)
    uid = 1005
    assert _is_duplicate_send(uid, "same message") is False
    assert _is_duplicate_send(uid, "same message") is False   # not recorded / not suppressed


def test_dedup_is_per_user():
    assert _is_duplicate_send(2001, PARA_A) is False
    # a different user sending the paraphrase is NOT suppressed by user 2001's send
    assert _is_duplicate_send(2002, PARA_B) is False


def test_similarity_margin_is_wide():
    """The reported paraphrase clears the high threshold; distinct pairs sit far below."""
    assert _similarity("got it — so pullups, then u alternate bis and back",
                       "got it — pullups, then bi/back alternating") >= 0.0  # sanity
    near = _similarity(" ".join(PARA_A.split()).lower(), " ".join(PARA_B.split()).lower())
    distinct = _similarity("how many sets are you doing today?",
                           "want me to pull up your pull-day card?")
    assert near >= 0.85
    assert distinct < 0.5
