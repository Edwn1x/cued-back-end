# Estimating meal macros — which source, when

When a meal needs macros, escalate by what **kind of information** is missing — not
by a vague confidence feeling, and never by forcing every source on every meal. Stop
climbing the moment the remaining uncertainty is something no data source can
resolve.

The ladder, cheapest-and-truest first:

1. **Printed numbers in view** — a nutrition label, menu board, or package weight in
   the photo or conversation is ground truth. Read it, scale by how much they ate,
   done. A clear label needs no database.
2. **Their own history** (match_meal_history) — a meal they plausibly log regularly
   has a personal ground truth: their portions, their prep. Check it before
   estimating a repeat from scratch.
3. **Dining-hall food** (match_dining_item) — campus food gets looked up in today's
   menu, not eyeballed.
4. **Identifiable generic food** (usda_food_lookup) — no label, no history, not
   dining: get per-100g reference macros and scale by your portion estimate.
5. **Branded / restaurant long tail** (web search) — only when nothing above covers
   it. Treat found calorie pages skeptically; prefer official nutrition info.
6. **Ask the user** — LAST, because it costs them friction. Only for what only they
   know: how much they actually ate, hidden ingredients, an irreducibly ambiguous
   portion. One short question, not an interrogation.

Rules across the ladder:

- Easy cases stop early; hard cases may genuinely climb several rungs before asking.
  Skip any rung whose tool isn't available this turn.
- Every rung fails safe: a tool that errors or returns nothing just means you
  estimate from the next rung — the meal still gets logged (unless asking first is
  the honest move).
- Confidence is for **communication, not control**: when the estimate is rough, say
  so and make the correction cheap — "logged ~650, rough on the portion — one cup or
  two?". Don't dress a guess up as precision.
- Never claim a source you didn't use this turn: "using your usual" / "looked up the
  menu" / "per USDA" only when that tool returned the numbers you used.
