"""Outside recipe sources: Recipe API (recipeapi.io, keyed, monthly budget) and TheMealDB (free).
Recipes land in the recipes table with saved=0 (library) until the user saves them."""
import datetime
import json
import re
import threading
import urllib.parse
import urllib.request

import ai
import config
import db
import recipes as rx

RECIPEAPI = "https://recipeapi.io/api/v1/recipes"
MEALDB = "https://www.themealdb.com/api/json/v1/1/search.php?s="
CATEGORY_AISLE = {"spice": "Spices", "herb": "Produce", "vegetable": "Produce", "fruit": "Produce",
                  "dairy": "Dairy", "meat": "Meat", "poultry": "Meat", "seafood": "Meat", "grain": "Pantry",
                  "bakery": "Bakery", "condiment": "Pantry", "oil": "Pantry", "legume": "Pantry", "nut": "Pantry",
                  "beverage": "Beverages", "sweetener": "Pantry", "baking": "Pantry"}
MEAL_SLOT = {"breakfast": "breakfast", "brunch": "breakfast", "main": "dinner", "soup": "lunch",
             "starter": "lunch", "appetizer": "lunch", "side_dish": "lunch", "snack": "lunch"}


def _get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "HA-MealPlanner/0.1", **(headers or {})})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _store(r, origin, ext_id):
    """Insert unless already there or it breaks the diet. Returns the row id or None."""
    r = rx.clean_recipe(r)
    if not r["name"] or ai.violates_diet(r):
        return None
    db.run("INSERT OR IGNORE INTO recipes(name,meal_type,servings,ingredients,steps,calories,protein,carbs,fat,"
           "source_url,saved,origin,ext_id,cost,image_src) VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)",
           (r["name"], r["meal_type"], r["servings"], json.dumps(r["ingredients"]), json.dumps(r["steps"]),
            r["calories"], r["protein"], r["carbs"], r["fat"], r.get("source_url"), origin, str(ext_id),
            r["cost"], r["image_src"]))
    hit = db.rows("SELECT id FROM recipes WHERE origin=? AND ext_id=?", (origin, str(ext_id)))
    return hit[0]["id"] if hit else None


# --- Recipe API -----------------------------------------------------------------------------------

def _budget():
    """Calls used this month vs. the configured monthly cap (the free plan allows 500)."""
    month = datetime.date.today().strftime("%Y-%m")
    used = db.get_setting("recipeapi_used", {})
    return month, used.get(month, 0), int(config.options()["recipeapi_monthly_budget"])


_budget_lock = threading.Lock()


def _reserve_call(limit):
    """Count a call before making it (the sync thread and searches share the budget). False if over `limit`."""
    with _budget_lock:
        month, n, _ = _budget()
        if n >= limit:
            return False
        db.set_setting("recipeapi_used", {month: n + 1})
        return True


def recipeapi_status():
    month, used, cap = _budget()
    return {"enabled": bool(config.options()["recipeapi_key"]), "used": used, "cap": cap,
            "next_page": db.get_setting("recipeapi_page", 1), "last_page": db.get_setting("recipeapi_last_page"),
            "stored": db.rows("SELECT count(*) n FROM recipes WHERE origin='recipeapi'")[0]["n"]}


def _from_recipeapi(d):
    ings = [i for i in (d.get("ingredients") or []) if isinstance(i, dict) and i.get("name")]
    return {"name": d.get("name") or "", "meal_type": MEAL_SLOT.get(d.get("meal_type"), "dinner"),
            "servings": d.get("servings") or 1, "calories": d.get("calories_per_serving"),
            "protein": d.get("protein"), "steps": d.get("instructions") or [],
            "source_url": f"https://recipeapi.io/recipes/{d.get('id')}", "image": d.get("image"),
            "ingredients": [{"name": i["name"], "qty": i.get("quantity") or 0,
                             "unit": rx._unit(i.get("unit") or "") or
                             ("" if i.get("unit") in (None, "piece", "clove", "slice") else str(i["unit"])[:20]),
                             "aisle": CATEGORY_AISLE.get(i.get("category"), rx.aisle_for(i["name"])),
                             "store": "king_soopers", "note": "optional" if i.get("optional") else ""}
                            for i in ings]}


def _recipeapi_page(params, limit=None):
    """One Recipe API call, or None when there is no key or the budget is spent. The background sync passes
    limit=90% of the cap so 10% stays free for searches in the app."""
    key = config.options()["recipeapi_key"]
    if not key or not _reserve_call(limit if limit is not None else _budget()[2]):
        return None
    return _get(RECIPEAPI + "?" + urllib.parse.urlencode(params), {"Authorization": f"Bearer {key}"})


def recipeapi_sync(calls):
    """Walk the catalog page by page (sorted by name, so pages are stable). Remembers where it stopped;
    after the last page it starts over to pick up new recipes."""
    before = recipeapi_status()["stored"]
    for _ in range(calls):
        page = db.get_setting("recipeapi_page", 1)
        data = _recipeapi_page({"page": page, "per_page": 10, "sort": "name"}, limit=int(_budget()[2] * 0.9))
        if data is None:
            break
        for d in data.get("data") or []:
            try:                            # one bad record must not wedge the sync on this page forever
                if isinstance(d, dict) and d.get("id") is not None:
                    _store(_from_recipeapi(d), "recipeapi", d["id"])
            except Exception:
                continue
        last = (data.get("meta") or {}).get("last_page") or page
        db.set_setting("recipeapi_last_page", last)
        db.set_setting("recipeapi_page", 1 if page >= last else page + 1)
    return recipeapi_status()["stored"] - before


def daily_calls():
    """Spread the month's budget evenly, keeping 10% back for searches made in the app."""
    month, used, cap = _budget()
    today = datetime.date.today()
    days_in_month = ((today.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - today).days
    left = int(cap * 0.9) - used
    return max(0, left // max(days_in_month, 1))


# --- TheMealDB ------------------------------------------------------------------------------------

def _from_mealdb(m):
    ings = []
    for n in range(1, 21):
        name, measure = (m.get(f"strIngredient{n}") or "").strip(), (m.get(f"strMeasure{n}") or "").strip()
        if name:
            p = rx.parse_ingredient(f"{measure} {name}".strip())
            p["name"] = name                # TheMealDB names are already clean
            ings.append(p)
    steps = [s.strip() for s in re.split(r"\r?\n+", m.get("strInstructions") or "") if len(s.strip()) > 3]
    cat = (m.get("strCategory") or "").lower()
    return {"name": m["strMeal"], "meal_type": "breakfast" if cat == "breakfast" else "dinner", "servings": 4,
            "image": m.get("strMealThumb"),
            "ingredients": ings, "steps": steps, "source_url": m.get("strSource") or
            f"https://www.themealdb.com/meal/{m['idMeal']}"}


# --- Search (local library first, then live sources) ----------------------------------------------

def search(q, live=True):
    like = f"%{q}%"
    found = {r["id"] for r in db.rows("SELECT id FROM recipes WHERE name LIKE ? LIMIT 40", (like,))}
    if live:                                # TheMealDB is free: always ask it when Online is pressed
        try:
            for m in (_get(MEALDB + urllib.parse.quote(q)).get("meals") or [])[:15]:
                try:
                    rid = _store(_from_mealdb(m), "mealdb", m["idMeal"])
                    rid and found.add(rid)
                except Exception:
                    continue
        except Exception:
            pass
    if live and len(found) < 10:            # Recipe API costs budget: only when the rest came up short
        try:
            data = _recipeapi_page({"search": q, "per_page": 10})
            for d in (data or {}).get("data") or []:
                try:
                    rid = _store(_from_recipeapi(d), "recipeapi", d["id"])
                    rid and found.add(rid)
                except Exception:
                    continue
        except Exception:
            pass
    if not found:
        return []
    marks = ",".join("?" * len(found))
    return db.rows(f"SELECT * FROM recipes WHERE id IN ({marks}) ORDER BY saved DESC, name LIMIT 60", tuple(found))
