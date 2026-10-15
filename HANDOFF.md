# Meal and Grocery Planner: handoff

Home Assistant add-on for an HA Green (ARM, 4GB RAM, eMMC: keep it light, limit disk writes). Build in this folder (`C:\AntiGravity\MealPlanner`).

## Match the packaging of the workout app
Copy the structure of `C:\AntiGravity\WorkoutTracker` (read these first): `config.yaml`, `Dockerfile`, `run.sh`, `repository.json`, `requirements.txt`, `make_addon.ps1`, `translations/en.yaml`, `static/` (index.html, app.js, app.css), Python backend (`api.py`, `db.py`, `planner.py`, ...), `icon.png`/`logo.png`. Ingress web UI.

## Decisions from the user
- Goals: none (no cutting/bulking, no protein target). The workout app does not store nutrition goals. Show macros per meal as information only; optional goals can be added later.
- Diet: eats everything except fish. Shrimp is OK. River/freshwater fish is OK. No ocean fish.
- Plan generation: fully generated each week with Claude (Anthropic API), with no approval step required. Allow tweaking or regenerating a single meal.
- Stores: Costco, Costco Business Center, King Soopers (Kroger family). Others occasionally. Group the grocery list by store and aisle. Costco-sized quantities should be noted.
- Servings: usually 1 person. Must support per-day guest counts (e.g. Friday dinner = 4 people) that scale the recipe and grocery list.

## Core features (v1)
1. Weekly plan generator (breakfast/lunch/dinner, per-day headcount).
2. Recipe library: saved meals, import from URL or paste, ingredients plus macros.
3. Grocery list: deduplicated, grouped by store/aisle, synced to the HA todo list (so Google speakers can add and read items).
4. Optional pantry tracking so the list only includes what is missing.
5. HA notifications, e.g. a prep reminder the night before.

## Later (not v1)
- Cart handoff to King Soopers/Kroger (API, may need approval).
- Pull training-day load from the workout app to adjust portions.
- Feed the Daily Briefing add-on (today's meals) and the Claude assistant add-on.

## Notes
- Anthropic API key goes in the add-on options, never in the repo.
- Other planned HA add-ons (context only): Daily Briefing, Work tracker 24/7, budgeting app (SimpleFIN), trading alert engine.
