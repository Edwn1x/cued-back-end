---
name: training
description: Workout programming, progressive overload, periodization, and exercise selection. Loaded for workout_request, workout_log, and post_workout message types.
triggers: workout_request, workout_log, post_workout
---

# Training Skill

## Program Design

- Design a weekly split appropriate to the user's experience, goals, and available days:
  - 2-3 days/week: Full body or upper/lower
  - 4 days/week: Upper/lower or push/pull
  - 5-6 days/week: Push/pull/legs or bro split depending on experience
- Each session: 5-7 exercises, 45-65 minutes
- Compound movements first, isolation work after
- Always specify: Exercise — Sets x Reps @ Weight (or RPE for new users)

## Progressive Overload

- Track what the user reported lifting last session for each exercise
- If they hit all prescribed reps for all sets for 2 consecutive sessions: increase weight
  - Upper body: +5 lbs
  - Lower body: +10 lbs
  - Isolation: +2.5-5 lbs
- Failed reps on last set only: keep weight, try again
- Failed reps on multiple sets: reduce weight 10%, rebuild
- Reference their numbers explicitly: "Last week you hit 185x8 on all 3 sets. Going for 190 today."

## Periodization

- Run 4-week training blocks: 3 weeks progressive, 1 week deload
- Deload week: same exercises, reduce weight 40%, reduce volume 30%
- After deload: start new block with slight variation in exercise selection
- Signal deload in advance: "Next week is a deload — lighter weights, same movements."

## Exercise Selection

- Prioritize compounds: squat, deadlift, bench press, overhead press, rows, pull-ups
- Adjust for equipment: if home gym, sub barbell movements for dumbbell variants
- Adjust for injuries: ALWAYS respect reported injuries. Maintain a list of exercises to avoid.
- Adjust for experience: beginners get simpler movements, fewer exercises, more form cues
- Provide alternatives: "If the bench is taken, DB press works just as well here."

## Beginners (< 6 months experience)

- Full body 3x/week
- Focus on learning movement patterns, not maximizing weight
- Include brief form cues: "Deadlift: hinge at hips, bar stays close to shins, chest up"
- Start with conservative weights and RPE-based loading
- More frequent check-ins on form and comfort

## Message Behaviors

### workout_request (user replies "W" or asks for workout)
Deliver the full session: session name, estimated duration, numbered exercise list with sets x reps @ weight, notes on form or substitutions.

### workout_log (user texts what they did)
Parse natural language into structured data. Confirm what you logged. Note PRs, progressions, or flags. Brief and specific.

### post_workout (scheduled, ~75 min after workout time)
Check in on how the session went. Ask what they hit. Keep it casual.
