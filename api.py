"""Add-on web server: phone-first UI in static/ plus the JSON API. Served through HA ingress,
so the page only uses relative URLs (fetch("api/plan"))."""
import datetime
import ipaddress
import json
import os
import re
import threading
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import ai
import claude_cli
import config
import db
import grocery
import ha
import images
import kitchen
import pantry
import recipes as rx
import sources

HERE = os.path.dirname(os.path.abspath(__file__))
SLOTS = ["breakfast", "lunch", "dinner"]
SLOT_ORDER = "CASE pm.slot WHEN 'breakfast' THEN 0 WHEN 'lunch' THEN 1 ELSE 2 END"


def _version():
    try:
        with open(os.path.join(HERE, "config.yaml")) as f:
            return re.search(r'^version:\s*"?([^"\n]+)"?', f.read(), re.M).group(1)
    except Exception:
        return "dev"


VERSION = _version()

db.init_db()
if not db.get_setting("autosave_migrated"):        # 0.1.2: meals that were planned before now count as saved
    db.run("UPDATE recipes SET saved=1 WHERE id IN (SELECT recipe_id FROM plan_meals) OR origin='claude'")
    db.set_setting("autosave_migrated", True)
app = FastAPI(title="Meal & Grocery Planner", docs_url=None, redoc_url=None, openapi_url=None)

# Only Home Assistant may talk to the add-on: the ingress proxy (172.30.32.2, already logged in to HA) for the
# UI, and other add-ons on the internal network (e.g. Claude Terminal) for the /meals/* endpoints. Outside
# Home Assistant (local development) everything is allowed.
INGRESS = ipaddress.ip_address("172.30.32.2")
HASSIO_NET = ipaddress.ip_network("172.30.32.0/23")


@app.middleware("http")
async def only_home_assistant(request: Request, call_next):
    if os.getenv("SUPERVISOR_TOKEN"):
        try:
            ip = ipaddress.ip_address(request.client.host)
        except Exception:
            ip = None
        ok = ip == INGRESS or (ip is not None and ip in HASSIO_NET and request.url.path.startswith("/meals/"))
        if not ok:
            return JSONResponse({"detail": "Open the planner from Home Assistant."}, status_code=403)
    if int(request.headers.get("content-length") or 0) > 12_000_000:
        return JSONResponse({"detail": "That upload is too large."}, status_code=413)
    return await call_next(request)


class NoCacheStatic(StaticFiles):
    """ETag revalidation (a cheap 304) so phones pick up new JS right after an add-on update."""
    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers["Cache-Control"] = "no-cache"
        return r


app.mount("/static", NoCacheStatic(directory=os.path.join(HERE, "static")), name="static")


@app.get("/")
def index():
    with open(os.path.join(HERE, "static", "index.html"), encoding="utf-8") as f:
        html = f.read().replace("__V__", VERSION)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


def guarded(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except HTTPException:
        raise
    except ai.Busy as e:
        raise HTTPException(429, str(e))
    except Exception as e:
        raise HTTPException(502, str(e)[:300] or type(e).__name__)


def parse_date(s):
    try:
        return datetime.date.fromisoformat(str(s)).isoformat()
    except ValueError:
        raise HTTPException(422, "Dates look like 2026-10-14.")


def check_slot(slot):
    if slot not in SLOTS:
        raise HTTPException(422, "Slot is breakfast, lunch or dinner.")
    return slot


@app.get("/api/state")
def state():
    return {"version": VERSION, "today": datetime.date.today().isoformat()}


# --- Claude sign-in (Claude Code CLI, subscription) ----------------------------------------------

class LoginCode(BaseModel):
    code: str = Field(max_length=500)


@app.get("/api/claude")
def claude_state():
    return claude_cli.login_state()


@app.post("/api/claude/login")
def claude_login():
    return claude_cli.start_login()


@app.post("/api/claude/code")
def claude_code(c: LoginCode):
    return claude_cli.send_code(c.code)


@app.post("/api/claude/cancel")
def claude_cancel():
    return claude_cli.cancel_login()


@app.post("/api/claude/logout")
def claude_logout():
    claude_cli.logout()
    return claude_cli.login_state()


# --- Recipes ------------------------------------------------------------------------------------

class Recipe(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    meal_type: Optional[str] = "dinner"
    servings: float = 1
    ingredients: list = []
    steps: list = []
    calories: Optional[float] = None
    protein: Optional[float] = None
    carbs: Optional[float] = None
    fat: Optional[float] = None
    source_url: Optional[str] = None
    favorite: int = 0


def save_recipe(r, rid=None):
    """Every recipe (typed in, imported, from Claude) goes through rx.clean_recipe before it is stored."""
    c = rx.clean_recipe(r)
    if not c["name"]:
        raise HTTPException(422, "The recipe has no name.")
    vals = (c["name"], c["meal_type"], c["servings"], json.dumps(c["ingredients"]), json.dumps(c["steps"]),
            c["calories"], c["protein"], c["carbs"], c["fat"], c["source_url"], int(bool(r.get("favorite"))),
            c["cost"])
    if rid:
        db.run("UPDATE recipes SET name=?,meal_type=?,servings=?,ingredients=?,steps=?,calories=?,protein=?,"
               "carbs=?,fat=?,source_url=?,favorite=?,cost=COALESCE(?,cost) WHERE id=?", vals + (rid,))
        return rid
    return db.run("INSERT INTO recipes(name,meal_type,servings,ingredients,steps,calories,protein,carbs,fat,"
                  "source_url,favorite,cost,saved,origin,image_src) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  vals + (int(r.get("saved", 1)), r.get("origin", "mine"), c["image_src"]))


def save_or_reuse(r):
    """Auto-save a planned recipe to My recipes, reusing one with the same name instead of piling up copies."""
    name = str((r or {}).get("name") or "").strip()
    hit = db.rows("SELECT id FROM recipes WHERE saved=1 AND lower(name)=lower(?) ORDER BY favorite DESC, id LIMIT 1", (name,))
    return hit[0]["id"] if hit else save_recipe({**r, "saved": 1})


def inflate(r, keys=None):
    r["ingredients"] = grocery.clean_ingredients(r.get("ingredients"))
    r["steps"] = grocery.clean_steps(r.get("steps"))
    r["servings"] = int(grocery.to_qty(r.get("servings")) or 1)
    r["match"] = kitchen.match(r["ingredients"], keys)
    r["image_url"] = images.url_for(r)
    return r


def price_hints():
    """Latest price per product from receipt photos, for Claude's cost estimates."""
    rows = db.rows("SELECT name, store, price, qty, pack_size FROM prices WHERE id IN "
                   "(SELECT max(id) FROM prices GROUP BY key) ORDER BY id DESC LIMIT 40")
    return [f"{r['name']}{' ' + r['pack_size'] if r['pack_size'] else ''}: ${r['price'] / max(r['qty'] or 1, 1):.2f}"
            f"{' at ' + grocery.STORE_LABEL.get(r['store'], '') if r['store'] else ''}" for r in rows]


@app.get("/api/img/{rid}")
def recipe_image(rid: int):
    r = db.rows("SELECT image FROM recipes WHERE id=?", (rid,))
    if not r or not (r[0]["image"] or "").startswith("b2:"):
        raise HTTPException(404, "No picture.")
    try:
        data = images.fetch(r[0]["image"])
    except Exception:
        raise HTTPException(502, "Could not load the picture.")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=2592000"})


@app.get("/api/recipes")
def recipes(q: str = "", live: bool = False, sort: str = "", fav: bool = False):
    """My recipes by default; with q, search the local library (plus TheMealDB/Recipe API when live).
    sort=match puts the recipes you can make from the pantry first."""
    q = q.strip()[:100]
    keys = kitchen.pantry_keys()
    rows = sources.search(q, live) if q else \
        db.rows("SELECT * FROM recipes WHERE saved=1" + (" AND favorite=1" if fav else "") +
                " ORDER BY favorite DESC, name")
    out = [inflate(r, keys) for r in rows]
    if sort == "match" or q:
        out.sort(key=lambda r: (-(r.get("saved") or 0), -(r["match"]["have"] / max(r["match"]["total"], 1))))
    return out


@app.post("/api/recipes/{rid}/save")
def recipe_save(rid: int, saved: bool = True):
    db.run("UPDATE recipes SET saved=? WHERE id=?", (int(saved), rid))
    return {"ok": True}


@app.post("/api/recipes/{rid}/favorite")
def recipe_favorite(rid: int, on: bool = True):
    """Favorites stay at the top of My recipes and the weekly planner reuses them."""
    db.run("UPDATE recipes SET favorite=?, saved=CASE WHEN ? THEN 1 ELSE saved END WHERE id=?", (int(on), int(on), rid))
    return {"ok": True}


@app.get("/api/library")
def library_status():
    return {"recipeapi": sources.recipeapi_status(),
            "total": db.rows("SELECT count(*) n FROM recipes")[0]["n"]}


@app.post("/api/recipes")
def recipe_add(r: Recipe):
    return {"id": save_recipe(r.model_dump())}


@app.put("/api/recipes/{rid}")
def recipe_edit(rid: int, r: Recipe):
    save_recipe(r.model_dump(), rid)
    return {"id": rid}


@app.delete("/api/recipes/{rid}")
def recipe_del(rid: int):
    today = datetime.date.today().isoformat()
    if db.rows("SELECT 1 FROM plan_meals WHERE recipe_id=? AND date>=?", (rid, today)):
        raise HTTPException(409, "That recipe is on this week's plan. Swap or clear the meal first.")
    db.run("DELETE FROM recipes WHERE id=?", (rid,))
    return {"ok": True}


class ImportReq(BaseModel):
    url: Optional[str] = Field(None, max_length=2000)
    text: Optional[str] = Field(None, max_length=60000)


@app.post("/api/recipes/import")
def recipe_import(req: ImportReq):
    if not (req.url or req.text):
        raise HTTPException(400, "Give a URL or paste the recipe text.")
    r = guarded(ai.import_recipe, text=req.text, url=req.url)
    r["source_url"] = req.url
    return {"id": save_recipe(r), "recipe": rx.clean_recipe(r)}


# --- Weekly plan --------------------------------------------------------------------------------

def week_dates(start=None):
    d = datetime.date.fromisoformat(parse_date(start)) if start else datetime.date.today()
    return [(d + datetime.timedelta(days=i)).isoformat() for i in range(7)]


def headcounts(dates):
    """{date: {slot: people}}: the day's count, overridden per meal (Friday dinner = 4)."""
    day = {r["date"]: r["headcount"] for r in db.rows(
        "SELECT * FROM day_headcount WHERE date BETWEEN ? AND ?", (min(dates), max(dates)))}
    meal = {(r["date"], r["slot"]): r["headcount"] for r in db.rows(
        "SELECT * FROM meal_headcount WHERE date BETWEEN ? AND ?", (min(dates), max(dates)))}
    return {d: {s: meal.get((d, s), day.get(d, 1)) for s in SLOTS} for d in dates}


def rebuild_list():
    """Keep the grocery list in step with the plan (ticks and HA-sync state survive a rebuild)."""
    dates = week_dates()
    try:
        grocery.rebuild(dates[0], dates[-1])
    except Exception as e:
        print(f"Grocery rebuild failed: {e}", flush=True)


def _scaled(ingredients, factor):
    out = []
    for i in grocery.clean_ingredients(ingredients):
        q = i["qty"] * factor
        out.append({**i, "qty": round(q, 2) if q else 0})
    return out


@app.get("/api/plan")
def plan(start: Optional[str] = None):
    dates = week_dates(start)
    hc = headcounts(dates)
    keys = kitchen.pantry_keys()
    meals = {}
    for m in db.rows("SELECT pm.date, pm.slot, pm.headcount, pm.recipe_id, pm.cooked, r.name, r.servings, r.calories, "
                     "r.protein, r.carbs, r.fat, r.ingredients, r.steps, r.saved, r.favorite, r.cost, r.image, r.image_src "
                     "FROM plan_meals pm "
                     "JOIN recipes r ON r.id=pm.recipe_id WHERE pm.date BETWEEN ? AND ?", (dates[0], dates[-1])):
        ings = grocery.clean_ingredients(m.pop("ingredients"))
        servings = int(grocery.to_qty(m["servings"]) or 1)
        m["match"] = kitchen.match(ings, keys)
        m["ingredients"] = _scaled(ings, (m["headcount"] or 1) / servings)   # amounts for the people eating
        m["steps"] = grocery.clean_steps(m["steps"])
        m["meal_cost"] = round(m["cost"] * (m["headcount"] or 1), 2) if m["cost"] else None
        m["image_url"] = images.url_for({**m, "id": m["recipe_id"]})
        meals[(m["date"], m["slot"])] = m
    out = []
    for d in dates:
        day_meals = {s: meals.get((d, s)) for s in SLOTS}
        costs = [m["meal_cost"] for m in day_meals.values() if m and m["meal_cost"]]
        out.append({"date": d, "people": hc[d], "meals": day_meals, "cost": round(sum(costs), 2) if costs else None})
    return out


class CookReq(BaseModel):
    date: str
    slot: str


@app.post("/api/plan/cooked")
def cooked(req: CookReq):
    """'I cooked this': take the meal's ingredients out of the pantry."""
    res = kitchen.consume(parse_date(req.date), check_slot(req.slot))
    if res is None:
        raise HTTPException(404, "No meal is planned there.")
    if not res.get("already"):
        rebuild_list()
    return res


def use_soon():
    """For the planner prompt: items expiring this week first, then things that expired in the last 2 weeks,
    clearly labelled (older ones are left to the pantry's warning card)."""
    today = datetime.date.today()
    items = kitchen.expiring(7)
    fresh = [f"{p['name']} (expires {p['expires']})" for p in items if p["expires"] >= today.isoformat()]
    stale = [f"{p['name']} (EXPIRED {p['expires']}, only if still safe)" for p in items
             if (today - datetime.timedelta(days=14)).isoformat() <= p["expires"] < today.isoformat()]
    return fresh + stale


class HeadReq(BaseModel):
    date: str
    headcount: int
    slot: Optional[str] = None         # set for one meal (Friday dinner = 4); unset for the whole day


@app.post("/api/plan/headcount")
def set_headcount(h: HeadReq):
    d, n = parse_date(h.date), max(1, min(h.headcount, 30))
    if h.slot:
        check_slot(h.slot)
        db.run("INSERT INTO meal_headcount(date,slot,headcount) VALUES(?,?,?) "
               "ON CONFLICT(date,slot) DO UPDATE SET headcount=excluded.headcount", (d, h.slot, n))
        db.run("UPDATE plan_meals SET headcount=? WHERE date=? AND slot=?", (n, d, h.slot))
    else:
        db.run("INSERT INTO day_headcount(date,headcount) VALUES(?,?) "
               "ON CONFLICT(date) DO UPDATE SET headcount=excluded.headcount", (d, n))
        db.run("DELETE FROM meal_headcount WHERE date=?", (d,))
        db.run("UPDATE plan_meals SET headcount=? WHERE date=?", (n, d))
    rebuild_list()
    return {"ok": True}


class GenReq(BaseModel):
    start: Optional[str] = None
    notes: str = Field("", max_length=1000)


# Generating a week takes minutes, longer than a phone keeps one request open, so it runs as a background
# job and the page polls GET /api/plan/generate.
_gen = {"status": "idle", "message": "", "skipped": 0, "finished": None}
_gen_lock = threading.Lock()


def _favorite_ids():
    return {r["name"].lower(): r["id"] for r in db.rows(
        "SELECT id, name FROM recipes WHERE saved=1 ORDER BY favorite DESC, id DESC LIMIT 200")}


def _run_generation(start, notes):
    try:
        dates = week_dates(start)
        hc = headcounts(dates)
        recent = [r["name"] for r in db.rows(
            "SELECT r.name FROM plan_meals pm JOIN recipes r ON r.id=pm.recipe_id WHERE pm.date >= ? "
            "GROUP BY r.name ORDER BY max(pm.date) DESC LIMIT 40",
            ((datetime.date.today() - datetime.timedelta(days=21)).isoformat(),))]
        saved = _favorite_ids()
        favs = [r["name"] for r in db.rows("SELECT name FROM recipes WHERE favorite=1 ORDER BY id DESC LIMIT 40")]
        items, bad = ai.generate_week(dates, hc, pantry.names(), recent, notes, use_soon(), favs, price_hints())
        for b in bad:                   # Claude slipped in ocean fish: replace just that meal
            try:
                r = ai.regenerate_meal(b["date"], b["slot"], hc[b["date"]][b["slot"]], pantry.names(),
                                       b["recipe"].get("name", ""), "no fish at all", use_soon(), price_hints())
                items.append({"date": b["date"], "slot": b["slot"], "recipe": r})
            except Exception:
                _gen["skipped"] += 1
        for it in items:
            people = hc[it["date"]][it["slot"]]
            if "saved_recipe" in it:
                rid = saved.get(it["saved_recipe"].lower())
                if not rid:
                    _gen["skipped"] += 1
                    continue
            else:
                rid = save_or_reuse({**it["recipe"], "origin": "claude"})
            db.run("INSERT OR REPLACE INTO plan_meals(date,slot,recipe_id,headcount,cooked) VALUES(?,?,?,?,0)",
                   (it["date"], it["slot"], rid, people))
        grocery.rebuild(dates[0], dates[-1])
        _gen.update(status="done", message="Week ready, grocery list updated.")
    except Exception as e:
        _gen.update(status="error", message=str(e)[:300] or type(e).__name__)
    finally:
        _gen["finished"] = time.time()


@app.post("/api/plan/generate")
def generate(req: GenReq):
    if req.start:
        parse_date(req.start)
    with _gen_lock:
        if _gen["status"] == "running":
            raise HTTPException(409, "A plan is already being generated. Give it a few minutes.")
        if not ai.available():
            raise HTTPException(400, "Connect Claude first (Plan tab).")
        _gen.update(status="running", message="", skipped=0, finished=None)
    threading.Thread(target=_run_generation, args=(req.start, req.notes), daemon=True).start()
    return _gen


@app.get("/api/plan/generate")
def generate_status():
    return _gen


class MealReq(BaseModel):
    date: str
    slot: str
    hint: str = Field("", max_length=300)
    recipe_id: Optional[int] = None      # set to put a saved recipe on the plan instead of generating


@app.post("/api/plan/meal")
def regen_meal(req: MealReq):
    d, slot = parse_date(req.date), check_slot(req.slot)
    people = headcounts([d])[d][slot]
    if req.recipe_id:
        if not db.rows("SELECT 1 FROM recipes WHERE id=?", (req.recipe_id,)):
            raise HTTPException(404, "No such recipe.")
        rid = req.recipe_id
        db.run("UPDATE recipes SET saved=1 WHERE id=?", (rid,))     # planned from the library -> My recipes
    else:
        cur = db.rows("SELECT r.name FROM plan_meals pm JOIN recipes r ON r.id=pm.recipe_id "
                      "WHERE pm.date=? AND pm.slot=?", (d, slot))
        r = guarded(ai.regenerate_meal, d, slot, people, pantry.names(),
                    cur[0]["name"] if cur else "", req.hint, use_soon(), price_hints())
        rid = save_or_reuse({**r, "origin": "claude"})
    db.run("INSERT OR REPLACE INTO plan_meals(date,slot,recipe_id,headcount,cooked) VALUES(?,?,?,?,0)",
           (d, slot, rid, people))
    rebuild_list()
    return {"ok": True}


@app.delete("/api/plan/meal")
def clear_meal(date: str, slot: str):
    db.run("DELETE FROM plan_meals WHERE date=? AND slot=?", (parse_date(date), check_slot(slot)))
    rebuild_list()
    return {"ok": True}


# --- Grocery list -------------------------------------------------------------------------------

@app.get("/api/grocery")
def grocery_get():
    return grocery.listing()


@app.get("/api/grocery/estimate")
def grocery_estimate():
    """Rough total of the unticked list from what you paid last time (receipt photos)."""
    known = {}
    for r in db.rows("SELECT key, price, qty FROM prices WHERE id IN (SELECT max(id) FROM prices GROUP BY key)"):
        known[r["key"]] = r["price"] / max(r["qty"] or 1, 1)
    total, priced, items = 0.0, 0, db.rows("SELECT name FROM grocery WHERE checked=0")
    for g in items:
        n = grocery.norm(g["name"])
        hit = next((v for k, v in known.items() if k and grocery.same(k, n)), None)
        if hit:
            total += hit
            priced += 1
    return {"total": round(total, 2), "priced": priced, "items": len(items)}


@app.post("/api/grocery/rebuild")
def grocery_rebuild(start: Optional[str] = None):
    dates = week_dates(start)
    return grocery.rebuild(dates[0], dates[-1])


class GItem(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    qty: float = 1
    unit: str = Field("", max_length=20)
    store: str = Field("king_soopers", max_length=40)
    aisle: str = Field("Other", max_length=40)


@app.post("/api/grocery")
def grocery_add(g: GItem):
    db.run("INSERT INTO grocery(name,qty,unit,store,aisle,note) VALUES(?,?,?,?,?,'manual')",
           (g.name.strip(), g.qty, grocery.canon_unit(g.unit), grocery.clean_store(g.store), g.aisle))
    return grocery.listing()


@app.post("/api/grocery/{gid}/check")
def grocery_check(gid: int, checked: bool = True, to_pantry: bool = False):
    g = db.rows("SELECT * FROM grocery WHERE id=?", (gid,))
    if not g:
        raise HTTPException(404, "That item is no longer on the list.")
    g = g[0]
    db.run("UPDATE grocery SET checked=? WHERE id=?", (int(checked), gid))
    if checked:
        if to_pantry and not g["stocked"]:
            # One purchase per trip: if it was already ticked in the store, that tick was the purchase.
            pantry.add_items([{"name": g["name"], "qty": g["qty"] or 1, "unit": g["unit"]}], g["store"],
                             log=not g["checked"])
            db.run("UPDATE grocery SET stocked=1 WHERE id=?", (gid,))
        elif not to_pantry and not g["checked"]:
            kitchen.log_purchase(g["name"], g["store"])
    return {"ok": True}


@app.get("/api/grocery/suggestions")
def grocery_suggestions():
    """Things you buy regularly that are probably due again (KitchenOwl-style)."""
    return kitchen.suggestions()


@app.delete("/api/grocery/{gid}")
def grocery_del(gid: int):
    db.run("DELETE FROM grocery WHERE id=?", (gid,))
    return {"ok": True}


@app.post("/api/grocery/sync")
def grocery_sync():
    """Push unsynced, unchecked items to the HA to-do list so Google speakers can read them."""
    pending = db.rows("SELECT * FROM grocery WHERE checked=0 AND synced=0 ORDER BY store, aisle, name")
    for g in pending:
        label = grocery.STORE_LABEL.get(g["store"], "Other")
        qty = f"{g['qty']:g} {g['unit'] or ''}".strip() if g["qty"] else ""
        guarded(ha.add_todo, f"{g['name']}{f' ({qty})' if qty else ''} [{label}]")
        db.run("UPDATE grocery SET synced=1 WHERE id=?", (g["id"],))
    return {"synced": len(pending)}


# --- Pantry + barcodes --------------------------------------------------------------------------

@app.get("/api/pantry")
def pantry_list():
    return db.rows("SELECT * FROM pantry ORDER BY category, name")


@app.get("/api/pantry/expiring")
def pantry_expiring(days: int = 5):
    return kitchen.expiring(days)


class ScanReq(BaseModel):
    barcode: str = Field(max_length=40)
    qty: float = Field(1, gt=0, le=1000)


@app.post("/api/pantry/scan")
def pantry_scan(s: ScanReq):
    return guarded(pantry.scan, s.barcode, s.qty)


class TeachReq(BaseModel):
    barcode: str = Field(max_length=40)
    name: str = Field(min_length=1, max_length=200)
    brand: str = Field("", max_length=100)
    pack_size: str = Field("", max_length=50)
    qty: float = Field(1, gt=0, le=1000)


@app.post("/api/pantry/teach")
def pantry_teach(t: TeachReq):
    res = pantry.teach(t.barcode, t.name, t.brand, t.pack_size, t.qty)
    if res.get("status") == "unknown":
        raise HTTPException(422, "That does not look like a barcode.")
    return res


class PhotoReq(BaseModel):
    image: str = Field(max_length=8_000_000)    # base64 JPEG (the page downsizes it first)
    barcode: str = Field("", max_length=40)


@app.post("/api/pantry/photo")
def pantry_photo(p: PhotoReq):
    """Claude identifies the product from a photo; with a barcode it is remembered for next scan."""
    prod = guarded(ai.identify_product, p.image)
    if p.barcode and pantry.clean_code(p.barcode):
        return pantry.teach(p.barcode, prod["name"], str(prod.get("brand") or ""), str(prod.get("pack_size") or ""))
    pantry.add_items([prod])
    return {"status": "added", "item": prod}


@app.post("/api/pantry/receipt")
def pantry_receipt(p: PhotoReq):
    """Read a receipt photo; returns the items for the user to confirm (nothing is saved yet)."""
    return guarded(ai.read_receipt, p.image)


class BulkReq(BaseModel):
    items: list = Field(max_length=300)
    store: str = Field("", max_length=40)


@app.post("/api/pantry/bulk")
def pantry_bulk(b: BulkReq):
    return {"added": pantry.add_items(b.items, b.store)}


class PantryItem(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    qty: float = Field(1, ge=0, le=100000)
    unit: str = Field("", max_length=20)
    expires: Optional[str] = None

    @field_validator("expires", mode="before")
    @classmethod
    def _iso_date(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        if not v:
            return None
        try:
            return datetime.date.fromisoformat(v[:10]).isoformat()
        except ValueError:
            raise ValueError("Best-before dates look like 2026-10-14.")


@app.post("/api/pantry")
def pantry_add(p: PantryItem):
    return {"id": db.run("INSERT INTO pantry(name,qty,unit,expires) VALUES(?,?,?,?)",
                         (p.name.strip(), p.qty, grocery.canon_unit(p.unit), p.expires))}


@app.put("/api/pantry/{pid}")
def pantry_edit(pid: int, p: PantryItem):
    db.run("UPDATE pantry SET name=?,qty=?,unit=?,expires=?,updated=CURRENT_TIMESTAMP WHERE id=?",
           (p.name.strip(), p.qty, grocery.canon_unit(p.unit), p.expires, pid))
    return {"ok": True}


@app.delete("/api/pantry/{pid}")
def pantry_del(pid: int):
    db.run("DELETE FROM pantry WHERE id=?", (pid,))
    return {"ok": True}


# --- Claude Terminal endpoints ---------------------------------------------------------------------
# Plain JSON for the Claude Terminal add-on (curl http://<add-on hostname>:8502/meals/today), like /workout/today.

@app.get("/meals/today")
def meals_today(date: Optional[str] = None):
    d = parse_date(date) if date else datetime.date.today().isoformat()
    meals = db.rows(f"SELECT pm.slot, pm.headcount, pm.cooked, r.* FROM plan_meals pm "
                    f"JOIN recipes r ON r.id=pm.recipe_id WHERE pm.date=? ORDER BY {SLOT_ORDER}", (d,))
    out = []
    for m in meals:
        servings = int(grocery.to_qty(m["servings"]) or 1)
        out.append({"slot": m["slot"], "people": m["headcount"], "name": m["name"], "cooked": bool(m["cooked"]),
                    "calories_per_serving": m["calories"], "protein_per_serving": m["protein"],
                    "ingredients_for_people": _scaled(m["ingredients"], (m["headcount"] or 1) / servings),
                    "steps": grocery.clean_steps(m["steps"])})
    return {"date": d, "meals": out}


@app.get("/meals/week")
def meals_week(start: Optional[str] = None):
    return plan(start)


@app.get("/meals/pantry")
def meals_pantry():
    return {"items": [{"name": p["name"], "qty": p["qty"], "unit": p["unit"], "expires": p["expires"]}
                      for p in pantry_list() if p["qty"] > 0],
            "use_soon": kitchen.expiring(5)}


@app.post("/meals/cooked")
def meals_cooked(req: CookReq):
    return cooked(req)


@app.get("/meals/grocery")
def meals_grocery():
    return grocery.listing()


@app.post("/meals/grocery")
def meals_grocery_add(g: GItem):
    return grocery_add(g)


@app.post("/meals/swap")
def meals_swap(req: MealReq):
    """Claude Terminal: regenerate one meal, optionally with a hint ("something with chicken")."""
    regen_meal(req)
    return meals_today(req.date)


# --- Background jobs: night-before reminder and the daily Recipe API sync -------------------------

def _reminder_time():
    try:
        h, m = str(config.options()["prep_reminder_time"]).strip().split(":")[:2]
        return datetime.time(int(h), int(m))
    except Exception:
        return datetime.time(20, 0)


def reminder_message(day):
    meals = db.rows(f"SELECT pm.slot, r.name FROM plan_meals pm JOIN recipes r ON r.id=pm.recipe_id "
                    f"WHERE pm.date=? ORDER BY {SLOT_ORDER}", (day,))
    soon = [p["name"] for p in kitchen.expiring(1)][:10]
    msg = "; ".join(f"{m['slot']}: {m['name']}" for m in meals)
    if soon:
        msg += (". " if msg else "") + "Use soon: " + ", ".join(soon)
    return (("Tomorrow: " if meals else "") + msg) if msg else ""


def reminder_loop():
    """Sends once a day in the 3 hours after the set time, so a restart or a busy minute does not skip it.
    The sent date is stored, so a restart does not send it twice."""
    while True:
        try:
            now = datetime.datetime.now()
            t = _reminder_time()
            today = now.date().isoformat()
            in_window = t <= now.time() < (datetime.datetime.combine(now.date(), t) + datetime.timedelta(hours=3)).time() \
                if t.hour < 21 else now.time() >= t
            if config.options()["notify_service"] and in_window and db.get_setting("reminder_sent") != today:
                msg = reminder_message((now.date() + datetime.timedelta(days=1)).isoformat())
                if msg:
                    ha.notify(msg)
                db.set_setting("reminder_sent", today)
        except Exception as e:
            print(f"Prep reminder failed: {e}", flush=True)
        time.sleep(60)


def sync_loop():
    """Recipe API background sync, once a day (budget spread over the month)."""
    time.sleep(60)
    while True:
        try:
            today = datetime.date.today().isoformat()
            if config.options()["recipeapi_key"] and db.get_setting("recipeapi_synced") != today:
                db.set_setting("recipeapi_synced", today)
                sources.recipeapi_sync(sources.daily_calls())
        except Exception as e:
            print(f"Recipe API sync failed: {e}", flush=True)
        time.sleep(1800)


threading.Thread(target=reminder_loop, daemon=True).start()
threading.Thread(target=sync_loop, daemon=True).start()
threading.Thread(target=images.worker, daemon=True).start()
