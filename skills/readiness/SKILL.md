---
name: readiness
description: Wearable data interpretation, sleep analysis, HRV processing, and recovery-based adjustments. Loaded when wearable data is present or for morning_briefing.
triggers: morning_briefing, readiness_check
---

# Readiness Skill

## Sleep-Based Adjustments

- Sleep < 5 hours: suggest rest day or very light session. Express concern.
- Sleep 5-6 hours: reduce volume by 1 set per exercise, reduce weight 5-10%. Explain why.
- Sleep 6-7 hours: normal session, mention sleep briefly.
- Sleep 7+ hours: no adjustment needed.
- Always state the sleep number: "6h20m sleep" not "you didn't sleep great."

## HRV-Based Adjustments

- HRV significantly below personal baseline (>15% drop): suggest active recovery or light session
- HRV trending up over 3+ days: signal readiness for higher intensity. "HRV has been climbing all week — your body is ready for a push."
- HRV trending down over 3+ days: flag potential overtraining or stress. Consider early deload.
- Always explain the adjustment: "HRV is 20% below your average — pulling back to 3 sets on compounds."

## Strain and Activity

- If wearable shows high strain from previous day: adjust volume down
- If user had a rest day with low strain: can push harder today
- Consider cumulative weekly strain, not just daily

## Readiness Score Logic

When wearable data is available, mentally calculate a readiness score:
- Good sleep (7+h) + normal/high HRV + low previous strain = full session
- Moderate sleep (6-7h) + normal HRV = normal session, monitor energy
- Poor sleep (<6h) OR low HRV OR high previous strain = reduced session
- Multiple red flags = suggest rest day or active recovery only

## Communication

- Be specific about data: "5h40m sleep, HRV at 32 vs your 45 average"
- Always explain WHY you're adjusting: "Short night — dropping to 3 sets instead of 4 on compounds. Same intensity, less total volume."
- Don't alarm the user. Low readiness days are normal. Frame adjustments as smart, not concerning.
- If no wearable connected: rely on user-reported sleep quality and subjective energy levels.
