# SDK bump — anthropic 0.42.0 → 0.116.0 (isolated commit)

Done as its own commit now that the Phase 0 harness exists as a regression net
(roadmap B5). Exact pin, like the rest of `requirements.txt`.

## Verification (ground truth = the installed 0.116.0, not the changelog)

| Risk flagged | Finding | Verdict |
|---|---|---|
| Stub patch point moved | `anthropic.resources.messages.Messages.create` still exists (`.stream()` is additive) | patch unchanged ✅ |
| Exception hierarchy changed | codebase references **zero** `anthropic.<Exception>` classes (retry/fallback use bare `except Exception`); all classes still exist anyway | non-issue ✅ |
| Response content typing | `TextBlock.text` intact; call sites use `response.content[i].text` + code-fence stripping | safe ✅ |
| Usage/token fields | `cost_tracking` reads via `getattr(usage, field, 0)`; base input/output/cache fields intact (0.105 only *added* `output_tokens_details`) | safe ✅ |
| Other SDK surface | no streaming / raw-response / count_tokens / beta / `with_options` usage anywhere | minimal surface ✅ |
| Full tier-1 + tier-2(collected) suite | `10 passed, 7 skipped, 5 xfailed` — identical to pre-bump | no regression ✅ |

## Behavior deltas to record

- **Automatic retries:** 0.116 defaults to `max_retries=2` (retries 408/409/429/5xx with
  backoff). The changelog shows no change to this default across 0.42→0.116, so it is a
  long-standing SDK property, not a bump-introduced delta. Interaction notes:
  - *Cost tracking is unaffected:* only a **successful** response carries `usage`, and
    Anthropic bills per successful request; failed attempts that trigger a retry are not
    metered and never reach `cost_tracking.track`.
  - *Distinct from the webhook-retry story:* SDK retries are HTTP-level and return once to
    the caller; the Twilio-retry duplicate (Phase 1 `MessageSid` dedup) is a separate,
    inbound concern.
- **Default timeout:** `Timeout(connect=5, read=600, write=600, pool=600)`. Unchanged concern
  for this rewrite; noted only for completeness.

## Residual risk (honest)

Tier-1 mocks `Messages.create`, so this pass proves import + stub + covered behavior, **not**
real-response parsing on 0.116. That is covered indirectly (stable `TextBlock.text`, minimal
surface) but should get a **tier-2 live smoke** once the funded `ANTHROPIC_API_KEY` lands —
tracked, non-blocking.

## Post-rewrite chore (do NOT drive-by fix)

- **`twilio==9.4.0` is yanked on PyPI** (installs with a warning). Leave the pin as-is for the
  rewrite; bump it deliberately as a post-rewrite dependency chore so it doesn't ride in on an
  unrelated diff.
