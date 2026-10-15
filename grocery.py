"""Build the grocery list from the plan: scale by headcount, merge duplicates, subtract the pantry,
group by store and aisle. Also the shared name/unit/quantity helpers the rest of the app uses."""
import json
import re
import unicodedata
from fractions import Fraction

import db

STORE_LABEL = {"costco": "Costco", "costco_business": "Costco Business Center",
               "king_soopers": "King Soopers", "other": "Other"}
STORE_ORDER = list(STORE_LABEL)
# unit -> (family, factor to base)
UNITS = {"g": ("mass", 1), "kg": ("mass", 1000), "oz": ("mass", 28.35), "lb": ("mass", 453.6),
         "ml": ("vol", 1), "l": ("vol", 1000), "tsp": ("vol", 4.93), "tbsp": ("vol", 14.8), "cup": ("vol", 236.6),
         "floz": ("vol", 29.57), "pint": ("vol", 473.2), "quart": ("vol", 946.4), "gallon": ("vol", 3785),
         "": ("count", 1)}
_UNIT_ALIAS = {"cups": "cup", "c": "cup", "tbs": "tbsp", "tbl": "tbsp", "tbls": "tbsp", "tblsp": "tbsp",
               "tablespoon": "tbsp", "tablespoons": "tbsp", "tbsps": "tbsp", "teaspoon": "tsp", "teaspoons": "tsp",
               "tsps": "tsp", "pound": "lb", "pounds": "lb", "lbs": "lb", "ounce": "oz", "ounces": "oz",
               "gram": "g", "grams": "g", "kilogram": "kg", "kilograms": "kg", "kgs": "kg", "milliliter": "ml",
               "milliliters": "ml", "millilitre": "ml", "liter": "l", "liters": "l", "litre": "l", "litres": "l",
               "fl oz": "floz", "fl. oz": "floz", "fluid ounce": "floz", "fluid ounces": "floz", "fluid_ounce": "floz",
               "pints": "pint", "quarts": "quart", "qt": "quart", "gallons": "gallon", "gal": "gallon"}

# Words that turn a food into a different product when added: "milk" is not "coconut milk",
# "chicken" is not "chicken broth", "tomato" is not "tomato paste".
_DISTINCT = {"peanut", "almond", "cashew", "coconut", "oat", "soy", "rice", "sesame", "garlic", "onion", "chili",
             "corn", "sweet", "sour", "ice", "cream", "cocoa", "apple", "maple", "condensed", "evaporated",
             "noodle", "vinegar", "flour", "paste", "powder", "sauce", "oil", "milk", "butter", "cheese", "cake",
             "cracker", "bread", "chip", "syrup", "juice", "extract", "stock", "broth", "seasoning", "flake",
             "jam", "jelly", "spread", "dressing", "soup", "mix", "candy", "bar", "cereal", "pie", "wine",
             "water", "salt", "pepper", "jack", "pork", "chestnut", "bun", "roll", "cookie", "puff"}
_STAPLE_WORDS = {"salt", "pepper", "oil", "water", "spray"}
_STAPLE_OK = _STAPLE_WORDS | {"black", "white", "olive", "vegetable", "canola", "cooking", "neutral", "and",
                              "kosher", "sea", "fine", "coarse", "flaky", "table", "freshly", "ground", "cracked",
                              "extra", "virgin", "light", "pure", "ice", "cold", "warm", "hot", "boiling",
                              "lukewarm", "cool", "nonstick", "avocado"}
_IRREGULAR = {"leaves": "leaf", "loaves": "loaf", "halves": "half", "knives": "knife", "potatoes": "potato",
              "tomatoes": "tomato", "mangoes": "mango"}


def _singular(w):
    if w in _IRREGULAR:
        return _IRREGULAR[w]
    if len(w) <= 3 or w.endswith(("ss", "us", "is")):
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("ie") and len(w) > 4:          # cookie/cookies and brownie/brownies meet at 'cooky', 'browny'
        return w[:-2] + "y"
    if w.endswith(("ches", "shes", "xes", "oes")):
        return w[:-2]
    return w[:-1] if w.endswith("s") else w


def norm(name):
    """'Fresh Blueberries (optional)' -> 'blueberry'; 'Jalapeño' -> 'jalapeno'."""
    n = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    n = re.sub(r"\(.*?\)|,.*", "", n)
    n = re.sub(r"\b(fresh|large|small|medium|chopped|diced|sliced|minced|organic|boneless|skinless)\b", "", n)
    n = re.sub(r"[^a-z ]", " ", n).strip()
    return " ".join(_singular(w) for w in n.split())


def same(a, b):
    """Whole-word match on normalised names: 'chicken' ~ 'chicken breast', but not 'egg' ~ 'eggplant',
    'oat' ~ 'goat cheese' or 'milk' ~ 'coconut milk'."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return False
    s, l = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return s <= l and not ((l - s) & _DISTINCT)


def is_staple(name):
    """Salt, pepper, cooking oil and water however they are written ('Kosher salt', 'Salt & pepper')."""
    words = set(norm(name).split()) - {"to", "taste", "for", "frying"}
    return bool(words & _STAPLE_WORDS) and words <= _STAPLE_OK


def covers(pantry_key, ingredient_key):
    """Does a pantry item cover an ingredient? A pantry 'Salt' only covers salt, not 'unsalted butter'."""
    if is_staple(pantry_key):
        return is_staple(ingredient_key) and bool(set(pantry_key.split()) & set(ingredient_key.split()) & _STAPLE_WORDS)
    return same(pantry_key, ingredient_key)


def canon_unit(u):
    u = str(u or "").lower().strip().rstrip(".")
    return _UNIT_ALIAS.get(u, u)


def family(unit):
    return UNITS.get(canon_unit(unit), ("count", 1))


def to_qty(v):
    """2, '2', '1/2', '1 1/2', '1.5' -> float; anything else -> 0."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v) if v == v and abs(v) < 1e7 else 0.0
    try:
        return float(sum(Fraction(p) for p in str(v).strip().split()))
    except (ValueError, ZeroDivisionError, TypeError):
        return 0.0


def clean_store(s):
    s = str(s or "").strip().lower().replace(" ", "_")
    return s if s in STORE_LABEL else ("costco_business" if "business" in s else "costco" if "costco" in s
                                       else "king_soopers" if "king" in s or "kroger" in s else "other")


def clean_ingredients(raw):
    """Never raises: strings become {"name": ...}, junk is dropped, qty becomes a number."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    out = []
    for i in raw if isinstance(raw, list) else []:
        if isinstance(i, str):
            i = {"name": i}
        if not isinstance(i, dict) or not str(i.get("name") or "").strip():
            continue
        out.append({"name": str(i["name"]).strip()[:200], "qty": to_qty(i.get("qty")),
                    "unit": canon_unit(i.get("unit")), "aisle": str(i.get("aisle") or "Other")[:40],
                    "store": clean_store(i.get("store") or "king_soopers"), "note": str(i.get("note") or "")[:200]})
    return out


def clean_steps(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = [raw]
    return [str(s) for s in raw if s] if isinstance(raw, list) else []


def pack_count(pack_size):
    """'24 ct' -> 24, '18 pk' -> 18, 'dozen' -> 12, else 1 (one package = one item)."""
    ps = str(pack_size or "")
    m = re.search(r"(\d+)\s*(?:ct|count|pk|pack|pcs|pieces|ea|eggs)\b", ps, re.I)
    return int(m.group(1)) if m else (12 if re.search(r"\bdozen\b", ps, re.I) else 1)


def week_ingredients(start, end):
    meals = db.rows("SELECT pm.date, pm.slot, pm.headcount, r.servings, r.ingredients FROM plan_meals pm "
                    "JOIN recipes r ON r.id=pm.recipe_id WHERE pm.date BETWEEN ? AND ? "
                    "AND COALESCE(pm.cooked,0)=0", (start, end))
    totals = {}
    for m in meals:
        scale = (m["headcount"] or 1) / max(int(to_qty(m["servings"]) or 1), 1)
        for ing in clean_ingredients(m["ingredients"]):
            n = norm(ing["name"])
            if not n or n == "water" or (is_staple(ing["name"]) and not ing["qty"]):
                continue
            fam, factor = family(ing["unit"])
            t = totals.setdefault((n, fam), {"name": ing["name"], "base": 0.0, "fam": fam, "unit": ing["unit"],
                                             "store": ing["store"], "aisle": ing["aisle"], "note": ing["note"],
                                             "unspec": False, "covered": False})
            t["base"] += ing["qty"] * factor * scale
            t["unspec"] = t["unspec"] or not ing["qty"]
            if ing["note"] and not t["note"]:
                t["note"] = ing["note"]
    return totals


def subtract_pantry(totals):
    for item in db.rows("SELECT name, qty, unit, pack_size FROM pantry WHERE qty>0"):
        key = norm(item["name"])
        if not key:
            continue
        pf, pfactor = family(item["unit"])
        for (n, fam), t in totals.items():
            if not covers(key, n):
                continue
            t["covered"] = True
            if fam == "count" and pf == "count":
                t["base"] -= item["qty"] * (pack_count(item["pack_size"]) if not item["unit"] else 1)
            elif fam == pf and item["unit"]:
                t["base"] -= item["qty"] * pfactor
            else:
                t["base"] = 0      # tracked as packages (or a different kind of unit): any stock covers it
    return {k: t for k, t in totals.items() if t["base"] > 0.01 or (t["unspec"] and not t["covered"])}


def _display(t):
    fam, base = t["fam"], max(t["base"], 0)
    if fam == "count":
        return (round(base + 0.49) if base > 0.01 else 0), ""
    unit = t["unit"] if family(t["unit"])[0] == fam else ("g" if fam == "mass" else "ml")
    return round(base / family(unit)[1], 1), unit


def rebuild(start, end):
    """Fresh list from the plan. Keeps manual rows, and keeps the ticked/sent-to-HA state of items that are
    still needed so a rebuild never re-sends the whole list to the Home Assistant to-do list."""
    totals = subtract_pantry(week_ingredients(start, end))
    conn = db.connect()
    old = {norm(r["name"]): (r["checked"], r["synced"]) for r in
           conn.execute("SELECT name, checked, synced FROM grocery WHERE note IS NOT 'manual'")}
    conn.execute("DELETE FROM grocery WHERE note IS NOT 'manual'")
    for (n, _), t in totals.items():
        qty, unit = _display(t)
        if not qty and not t["unspec"]:
            continue
        checked, synced = old.get(n, (0, 0))
        conn.execute("INSERT INTO grocery(name,qty,unit,store,aisle,note,checked,synced) VALUES(?,?,?,?,?,?,?,?)",
                     (t["name"], qty, unit, clean_store(t["store"]), t["aisle"], t["note"] or "", checked, synced))
    conn.commit()
    conn.close()
    return listing()


def listing():
    items = db.rows("SELECT * FROM grocery ORDER BY aisle, name")
    groups = {}
    for i in items:
        groups.setdefault(i["store"] if i["store"] in STORE_LABEL else "other", {}).setdefault(i["aisle"], []).append(i)
    return [{"store": s, "label": STORE_LABEL[s],
             "aisles": [{"aisle": a, "items": groups[s][a]} for a in sorted(groups[s])]}
            for s in STORE_ORDER if s in groups]
