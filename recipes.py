"""Recipe import helpers: recipe-scrapers for URLs, ingredient-parser for ingredient lines, and the one
cleaner every recipe goes through before it is stored. The parsers are optional at runtime; callers fall
back to Claude when they return None."""
import re

from grocery import canon_unit, clean_ingredients, clean_steps, to_qty

AISLES = [  # first keyword match wins
    ("Produce", r"lettuce|spinach|kale|onion|garlic|tomato|bell pepper|carrot|celery|potato|avocado|lemon|lime|apple|"
                r"banana|berr|cilantro|parsley|basil|ginger|cucumber|zucchini|broccoli|mushroom|scallion|herb"),
    ("Meat", r"chicken|beef|pork|turkey|bacon|sausage|steak|lamb|\bham\b|shrimp|trout|catfish|ground"),
    ("Dairy", r"milk|cheese|butter|yogurt|cream|egg"),
    ("Bakery", r"bread|tortilla|\bbun|\broll|pita|bagel"),
    ("Frozen", r"frozen"),
    ("Spices", r"salt|pepper|cumin|paprika|oregano|cinnamon|chili powder|thyme|spice|seasoning"),
    ("Pantry", r"rice|pasta|flour|sugar|oil|vinegar|sauce|broth|stock|bean|lentil|\boat|honey|\bcan\b|noodle|nut"),
]


def aisle_for(name):
    n = str(name or "").lower()
    for aisle, pat in AISLES:
        if re.search(pat, n):
            return aisle
    return "Other"


def parse_ingredient(line):
    """'2 lb boneless chicken thighs, diced' -> {name, qty, unit, aisle, store, note}."""
    name, qty, unit = line, 0, ""
    try:
        from ingredient_parser import parse_ingredient as _parse
        p = _parse(line)
        names = p.name if isinstance(p.name, list) else [p.name]
        if names and names[0]:
            name = names[0].text
        if p.amount:
            a = p.amount[0]
            qty = to_qty(getattr(a, "quantity", 0))
            unit = _unit(str(getattr(a, "unit", "") or ""))
    except Exception:
        m = re.match(r"\s*(\d+(?:\.\d+)?(?:\s+\d+/\d+)?|\d+/\d+)\s*([a-zA-Z]+\.?)?\s+(.*)", line)
        if m:
            qty, unit, name = to_qty(m.group(1)), _unit(m.group(2) or ""), m.group(3)
    return {"name": name.strip(), "qty": qty, "unit": unit, "aisle": aisle_for(name),
            "store": "king_soopers", "note": "optional" if re.search(r"\boptional\b", line, re.I) else ""}


_UNIT_MAP = {"gram": "g", "kilogram": "kg", "ounce": "oz", "pound": "lb", "milliliter": "ml", "liter": "l",
             "litre": "l", "teaspoon": "tsp", "tablespoon": "tbsp", "cup": "cup", "g": "g", "kg": "kg", "oz": "oz",
             "lb": "lb", "lbs": "lb", "ml": "ml", "l": "l", "tsp": "tsp", "tbsp": "tbsp", "cups": "cup",
             "fluid_ounce": "floz", "fluid ounce": "floz", "fl oz": "floz", "floz": "floz", "pint": "pint",
             "quart": "quart", "gallon": "gallon", "tblsp": "tbsp", "tbs": "tbsp", "tbls": "tbsp", "tsps": "tsp",
             "tbsps": "tbsp", "qt": "quart"}


def _unit(u):
    u = str(u or "").lower().strip().rstrip(".")
    if u in _UNIT_MAP:
        return _UNIT_MAP[u]
    u2 = u.rstrip("s") if len(u) > 3 else u
    return _UNIT_MAP.get(u2, "")


def _num(s):
    """'1,200 kcal' -> 1200.0, 'approx. 300' -> 300.0, 'N/A' -> None."""
    s = re.sub(r"(?<=\d),(?=\d{3}\b)", "", str(s or ""))
    m = re.search(r"\d+(?:\.\d+)?|\.\d+", s)
    return float(m.group(0)) if m else None


def clean_recipe(r):
    """Coerce a recipe from Claude, a scraper or an outside API into safe types. Never raises."""
    r = r if isinstance(r, dict) else {}

    def macro(k):
        v = _num(r.get(k)) if r.get(k) is not None else None
        return v if v is None or v < 1e5 else None

    servings = int(_num(r.get("servings")) or 1)
    meal_type = str(r.get("meal_type") or "dinner").lower()
    return {**r,
            "name": str(r.get("name") or "").strip()[:200],
            "meal_type": meal_type if meal_type in ("breakfast", "lunch", "dinner", "snack") else "dinner",
            "servings": max(1, min(servings, 100)),
            "ingredients": clean_ingredients(r.get("ingredients")),
            "steps": clean_steps(r.get("steps")),
            "calories": macro("calories"), "protein": macro("protein"), "carbs": macro("carbs"), "fat": macro("fat"),
            "source_url": str(r["source_url"])[:500] if r.get("source_url") else None}


def scrape(html, url):
    """Parse a recipe page with recipe-scrapers. Returns the app's recipe dict or None."""
    try:
        from recipe_scrapers import scrape_html
        s = scrape_html(html, org_url=url)
        ingredients = s.ingredients()
        if not ingredients:
            return None
        try:
            steps = s.instructions_list()
        except Exception:
            steps = [x for x in (s.instructions() or "").split("\n") if x.strip()]
        try:
            nut = s.nutrients() or {}
        except Exception:
            nut = {}
        try:
            servings = int(_num(s.yields()) or 1)
        except Exception:
            servings = 1
        return {"name": s.title(), "meal_type": "dinner", "servings": servings,
                "ingredients": [parse_ingredient(i) for i in ingredients], "steps": steps,
                "calories": _num(nut.get("calories")), "protein": _num(nut.get("proteinContent")),
                "carbs": _num(nut.get("carbohydrateContent")), "fat": _num(nut.get("fatContent"))}
    except Exception:
        return None
