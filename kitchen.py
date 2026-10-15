"""Kitchen smarts borrowed from CookTrace, Grocy and KitchenOwl (ideas only, no code):
pantry-match per recipe, use-soon expiry list, "I cooked this" consumption, and grocery suggestions."""
import datetime

import db
from grocery import clean_ingredients, clean_store, covers, family, is_staple, norm, pack_count, same, to_qty


def pantry_keys():
    return [k for k in (norm(p["name"]) for p in db.rows("SELECT name FROM pantry WHERE qty>0")) if k]


def _optional(ing):
    return "optional" in f"{ing.get('name')} {ing.get('note')}".lower()


# --- Pantry match (CookTrace) ---------------------------------------------------------------------

def match(ingredients, keys=None):
    """{"have": n, "total": n, "missing": [names]} ignoring staples like salt and oil, and optional items."""
    keys = pantry_keys() if keys is None else keys
    have, missing, total = 0, [], 0
    for ing in clean_ingredients(ingredients):
        n = norm(ing["name"])
        if not n or is_staple(ing["name"]) or _optional(ing):
            continue
        total += 1
        if any(covers(k, n) for k in keys):
            have += 1
        else:
            missing.append(ing["name"])
    return {"have": have, "total": total, "missing": missing[:8]}


# --- Expiry (Grocy) -------------------------------------------------------------------------------

def expiring(days=5):
    """Pantry items expiring within `days` (or already expired), soonest first."""
    days = max(-3650, min(int(days), 3650))
    until = (datetime.date.today() + datetime.timedelta(days=days)).isoformat()
    return db.rows("SELECT id, name, qty, unit, expires FROM pantry WHERE qty>0 AND expires IS NOT NULL "
                   "AND expires<>'' AND expires<=? ORDER BY expires", (until,))


# --- "I cooked this" (Grocy consume) --------------------------------------------------------------

def _rank(n, p):
    """Lower is better: exact name, then fewer extra words."""
    return (0 if n == p else 1, abs(len(p.split()) - len(n.split())))


def consume(date, slot):
    """Take a planned meal's ingredients (scaled to headcount) out of the pantry.
    Counts subtract directly (a '24 ct' carton counts as 24 items); weights/volumes subtract when the pantry row
    uses the same kind of unit. Package-tracked rows used by weight are left alone: we cannot know how much of
    the pack is left. Returns what changed, or None when nothing is planned there."""
    row = db.rows("SELECT pm.headcount, pm.cooked, r.servings, r.ingredients FROM plan_meals pm "
                  "JOIN recipes r ON r.id=pm.recipe_id WHERE pm.date=? AND pm.slot=?", (date, slot))
    if not row:
        return None
    m = row[0]
    if m["cooked"]:
        return {"changed": [], "already": True}
    scale = (m["headcount"] or 1) / max(int(to_qty(m["servings"]) or 1), 1)
    pantry = db.rows("SELECT id, name, qty, unit, pack_size FROM pantry WHERE qty>0 ORDER BY expires IS NULL, expires, id")
    for p in pantry:
        p["key"] = norm(p["name"])
    changed = {}
    conn = db.connect()
    for ing in clean_ingredients(m["ingredients"]):
        n = norm(ing["name"])
        need = ing["qty"] * scale
        if not n or is_staple(ing["name"]) or need <= 0:
            continue
        need_fam, need_f = family(ing["unit"])
        cands = sorted((p for p in pantry if p["qty"] > 0 and p["key"] and covers(p["key"], n)),
                       key=lambda p: _rank(n, p["key"]))
        for p in cands:                     # draw down across matching rows, soonest-expiring first
            have_fam, have_f = family(p["unit"])
            if need_fam == "count" and have_fam == "count":
                per = pack_count(p["pack_size"]) if not p["unit"] else 1
                used = min(p["qty"], need / per)
                need -= used * per
            elif need_fam == have_fam and p["unit"]:
                used = min(p["qty"], need * need_f / have_f)
                need -= used * have_f / need_f
            else:
                continue
            p["qty"] = round(p["qty"] - used, 2)
            conn.execute("UPDATE pantry SET qty=?, updated=CURRENT_TIMESTAMP WHERE id=?", (p["qty"], p["id"]))
            changed[p["id"]] = {"name": p["name"], "left": p["qty"], "unit": p["unit"]}
            if need <= 1e-6:
                break
    conn.execute("UPDATE plan_meals SET cooked=1 WHERE date=? AND slot=?", (date, slot))
    conn.commit()
    conn.close()
    return {"changed": list(changed.values())}


# --- Purchase history + suggestions (KitchenOwl) --------------------------------------------------

def log_purchase(name, store=""):
    db.run("INSERT INTO purchases(name,key,date,store) VALUES(?,?,?,?)",
           (name, norm(name), datetime.date.today().isoformat(), clean_store(store) if store else ""))


def suggestions(limit=12):
    """Items bought on 2+ separate trips whose usual gap since the last purchase has passed, and that are not
    on the list or in the pantry. Purchases within 3 days count as one trip (ticked in the store, then put in
    the pantry at home). Items not bought for 4+ usual gaps are treated as lapsed. Score 1.0 = due today."""
    today = datetime.date.today()
    on_list = [norm(g["name"]) for g in db.rows("SELECT name FROM grocery WHERE checked=0")]
    stocked = pantry_keys()
    hist = {}
    for p in db.rows("SELECT name, key, date, store FROM purchases WHERE key<>'' ORDER BY date, id"):
        hist.setdefault(p["key"], []).append(p)
    out = []
    for key, buys in hist.items():
        trips = []
        for d in sorted({datetime.date.fromisoformat(b["date"]) for b in buys}):
            if not trips or (d - trips[-1]).days > 3:
                trips.append(d)
        if len(trips) < 2 or any(same(key, o) for o in on_list if o) or any(covers(s, key) for s in stocked):
            continue
        gap = (trips[-1] - trips[0]).days / (len(trips) - 1)
        score = (today - trips[-1]).days / max(gap, 1)
        if 0.8 <= score <= 4:
            out.append({"name": buys[-1]["name"],
                        "store": next((b["store"] for b in reversed(buys) if b["store"]), "king_soopers"),
                        "every_days": round(gap), "last": trips[-1].isoformat(), "score": round(score, 2)})
    return sorted(out, key=lambda s: -s["score"])[:limit]
