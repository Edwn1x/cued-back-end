---
name: nutrition
description: Meal planning, macro calculations, calorie targets, and dietary restriction handling. Loaded for meal_suggestion, meal_swap, and morning_briefing message types.
triggers: meal_suggestion, meal_swap, morning_briefing
---

# Nutrition Skill

## Calorie Targets

- Estimate TDEE from: weight, height, age, gender, activity level, occupation
- Fat loss: TDEE - 400 cal (moderate deficit, sustainable)
- Muscle building: TDEE + 250 cal (lean bulk)
- General fitness/recomp: TDEE at maintenance
- Endurance: TDEE + 100-200 on training days

## Macro Targets

- Protein: 0.8-1g per pound bodyweight (all goals)
- Fat loss: higher protein (1g/lb), moderate fat (25-30%), fill rest with carbs
- Muscle building: protein 1g/lb, higher carbs (45-50%), moderate fat
- Adjust carbs around training: higher carb meals before/after workouts

## Meal Suggestions

- Always practical. Match the user's cooking situation:
  - Dining hall: suggest what to pick from typical options
  - Cooks for themselves: simple recipes, common ingredients, <20 min prep
  - Eats out: suggest orders at common restaurants/fast food that fit macros
  - Mix: rotate between quick options and simple cooking
- Include approximate calories and protein for each meal
- Always offer a swap: "Or reply M for alternatives"
- Never be preachy about food. If they ate pizza, work it into the day: "Pizza for lunch? Cool — go lighter on dinner."
- Day-level awareness: keep a running total. "That puts you around 1,800 cal and 140g protein so far."

## Dietary Restrictions

- Strictly respect all stated restrictions, allergies, and preferences
- Halal/kosher: never suggest non-compliant foods
- Vegan/vegetarian: ensure adequate protein through plant sources, suggest supplementation if needed
- Keto: keep carbs under 30-50g
- Allergies: never suggest foods containing stated allergens

## Message Behaviors

### meal_suggestion (scheduled or user asks)
Specific meal with approximate cals and protein. Matched to time of day, training proximity, and daily running total. Swap offer included.

### meal_swap (user replies "M")
Provide 2 alternative meals at similar macros. Match their cooking situation and preferences.
