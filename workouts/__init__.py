"""Workout logger — sessions, set logs, plan generation, PRs, summaries.
Phase 1 of the iMessage mini-app card (brief 2026-09-14). The legacy `workouts`
table (one flat row per session, exercises as JSON) stays; WorkoutSession +
SetLog live alongside it and are the source of truth for the card."""
