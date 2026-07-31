"""
Two-layer replay driver (see rewrite/phase-0/INVESTIGATION.md §2).

Layer A: POST Twilio-shaped form data to the real Flask /webhook via the test
client — exercises the true synchronous ordering, all four early-return
branches, and the synchronous classify_message call (stubbed).

Buffer:  the webhook arms a (faked) Timer instead of processing inline; the
driver fires that timer deterministically, which runs the real
_flush_buffer -> process_buffered_message (Layer B) with the post-reply memory
threads made synchronous.

`MessageSid` is always sent so the webhook-idempotency case (replay same sid
twice -> exactly one set of writes) is expressible. Outbound texts are captured
via the sms._send_single patch installed in conftest.

OBJECT LIFECYCLE, not just call sequence: prod refetches the User row on every
webhook, so no state carries between turns except what's in the database. A test
that reuses one User object across turns is testing a different system — the
object holds the profile as of its last refresh, so a mid-test write (remember,
targets, summaries) is invisible to the next turn and the model looks amnesiac
when it isn't (or a real persistence bug looks fixed when it isn't). Between
turns that involve a user-row write, refetch (`db.expire_all()` or a fresh
`session.get(User, ...)`). Found the hard way in the tier-2 tenders replay.
"""

from __future__ import annotations

import itertools

from tests._sync import PENDING_TIMERS

_sid_counter = itertools.count(1)


class Driver:
    def __init__(self, client, sms_capture: list):
        self.client = client
        self.sms_capture = sms_capture  # list of (phone, body) appended by the _send_single patch

    def send(self, user, body: str, *, message_sid: str = None, num_media: int = 0,
             media_url: str = None):
        """Replay one inbound message end-to-end; return outbound texts it produced."""
        if num_media:
            # The webhook downloads MMS media via a live requests.get; that path
            # is tier-2 / Phase 3. Tier-1 does not exercise it yet.
            raise NotImplementedError("MMS media path is not wired for tier-1 yet (Phase 3).")

        before = len(self.sms_capture)
        form = {
            "From": user.phone,
            "Body": body,
            "NumMedia": str(num_media),
            "MessageSid": message_sid or f"SM{next(_sid_counter):032d}",
        }
        if media_url:
            form["MediaUrl0"] = media_url

        resp = self.client.post("/webhook", data=form)
        assert resp.status_code == 200, f"webhook returned {resp.status_code}"

        # Fire the buffered flush if the webhook armed one (early-return branches don't).
        self.flush(user.phone)

        return [body for (_phone, body) in self.sms_capture[before:]]

    def flush(self, phone: str):
        timer = PENDING_TIMERS.pop(phone, None)
        if timer is not None:
            timer.fire()

    def replies(self):
        return list(self.sms_capture)
