"""Pantry tracking and barcode lookup (local cache, then Open Food Facts; no API key needed)."""
import json
import re
import urllib.request

import config
import db
import grocery
import kitchen

OFF_URL = ("https://world.openfoodfacts.org/api/v2/product/{code}.json"
           "?fields=product_name,brands,quantity,categories_tags")


def clean_code(code):
    code = re.sub(r"\D", "", str(code or ""))
    if not 6 <= len(code) <= 14:                            # not a retail barcode
        return ""
    return code.zfill(13) if len(code) == 12 else code     # UPC-A -> EAN-13 form


def _variants(code):
    return {code, code.lstrip("0"), code.zfill(12), code.zfill(13)}


def lookup(code):
    """Return {barcode,name,brand,pack_size,category,source} or None."""
    code = clean_code(code)
    if not code:
        return None
    for v in _variants(code):
        hit = db.rows("SELECT * FROM barcodes WHERE barcode=?", (v,))
        if hit:
            return hit[0]
    # Provider order borrowed from Barcode Buddy: free/open first, keyed/limited last.
    for source in (_off, _usda, _upcitemdb, _upcdatabase):
        try:
            prod = source(code)
        except Exception:
            prod = None
        if prod and prod.get("name"):
            prod["barcode"] = code
            save_product(prod)
            return prod
    return None


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "HA-MealPlanner/0.1"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


def _off(code):
    data = _get(OFF_URL.format(code=code))
    p = data.get("product") if data.get("status") == 1 else None
    if not p or not p.get("product_name"):
        return None
    cats = [c.split(":")[-1].replace("-", " ") for c in p.get("categories_tags", [])]
    return {"name": p["product_name"].strip(), "brand": (p.get("brands") or "").split(",")[0].strip(),
            "pack_size": p.get("quantity") or "", "category": cats[-1] if cats else "", "source": "openfoodfacts"}


def _usda(code):
    """USDA FoodData Central branded foods (US). DEMO_KEY works without signing up, at a low rate limit."""
    key = config.options()["usda_api_key"] or "DEMO_KEY"
    data = _get(f"https://api.nal.usda.gov/fdc/v1/foods/search?dataType=Branded&pageSize=5"
                f"&query={code.lstrip('0')}&api_key={key}")
    for f in data.get("foods", []):
        if (f.get("gtinUpc") or "").lstrip("0") == code.lstrip("0"):
            size = f"{f['packageWeight']}" if f.get("packageWeight") else ""
            return {"name": (f.get("description") or "").title(), "brand": f.get("brandName") or f.get("brandOwner") or "",
                    "pack_size": size, "category": f.get("brandedFoodCategory") or "", "source": "usda"}
    return None


def _upcitemdb(code):
    """UPCitemdb free trial endpoint (about 100 lookups a day, no key)."""
    data = _get(f"https://api.upcitemdb.com/prod/trial/lookup?upc={code.lstrip('0').zfill(12)}")
    items = data.get("items") or []
    if not items:
        return None
    i = items[0]
    return {"name": i.get("title", ""), "brand": i.get("brand", ""), "pack_size": i.get("size", ""),
            "category": (i.get("category") or "").split(">")[-1].strip(), "source": "upcitemdb"}


def _upcdatabase(code):
    """UPCDatabase.org (free key; skipped without one)."""
    key = config.options()["upcdatabase_api_key"]
    if not key:
        return None
    p = _get(f"https://api.upcdatabase.org/product/{code}?apikey={key}")
    if not p.get("success"):
        return None
    return {"name": p.get("title") or p.get("description") or "", "brand": p.get("brand") or "",
            "pack_size": p.get("size") or "", "category": p.get("category") or "", "source": "upcdatabase"}


# Restocking a row that ran out drops its old best-before date (the new pack has its own).
_RESTOCK = ("UPDATE pantry SET expires=CASE WHEN COALESCE(qty,0)<=0 THEN NULL ELSE expires END, "
            "qty=COALESCE(qty,0)+?, updated=CURRENT_TIMESTAMP WHERE id=?")


def add_items(items, store="", log=True):
    """Bulk add (receipt import, grocery 'bought'). Merges into a row with the same name and unit."""
    added = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()[:200]
        if not name:
            continue
        qty = grocery.to_qty(it.get("qty")) or 1
        unit = grocery.canon_unit(it.get("unit"))
        hit = db.rows("SELECT id FROM pantry WHERE lower(name)=lower(?) AND COALESCE(unit,'')=?", (name, unit))
        if hit:
            db.run(_RESTOCK, (qty, hit[0]["id"]))
        else:
            db.run("INSERT INTO pantry(name,qty,unit,pack_size,category) VALUES(?,?,?,?,?)",
                   (name, qty, unit, str(it.get("pack_size") or ""), str(it.get("category") or "")))
        if log:
            kitchen.log_purchase(name, store)
        price = grocery.to_qty(it.get("price"))
        if 0 < price < 2000:
            db.run("INSERT INTO prices(name,key,store,price,qty,pack_size,date) VALUES(?,?,?,?,?,?,date('now'))",
                   (name, grocery.norm(name), grocery.clean_store(store) if store else "", price, qty,
                    str(it.get("pack_size") or "")))
        added.append(name)
    return added


def save_product(p):
    db.run("INSERT INTO barcodes(barcode,name,brand,pack_size,category,source) VALUES(?,?,?,?,?,?) "
           "ON CONFLICT(barcode) DO UPDATE SET name=excluded.name, brand=excluded.brand, "
           "pack_size=excluded.pack_size, category=excluded.category",
           (p["barcode"], p["name"], p.get("brand", ""), p.get("pack_size", ""), p.get("category", ""),
            p.get("source", "user")))


def scan(code, qty=1):
    """Add one scan to the pantry. Same barcode again bumps the count.
    Returns {status: added|bumped|unknown, item|barcode}."""
    code = clean_code(code)
    if not code:
        return {"status": "unknown", "barcode": ""}
    existing = db.rows("SELECT * FROM pantry WHERE barcode=?", (code,))
    if existing:
        db.run(_RESTOCK, (qty, existing[0]["id"]))
        kitchen.log_purchase(existing[0]["name"])
        return {"status": "bumped", "item": db.rows("SELECT * FROM pantry WHERE id=?", (existing[0]["id"],))[0]}
    prod = lookup(code)
    if not prod:
        return {"status": "unknown", "barcode": code}
    pid = db.run("INSERT INTO pantry(barcode,name,brand,qty,pack_size,category) VALUES(?,?,?,?,?,?)",
                 (code, prod["name"], prod["brand"], qty, prod["pack_size"], prod["category"]))
    kitchen.log_purchase(prod["name"])
    return {"status": "added", "item": db.rows("SELECT * FROM pantry WHERE id=?", (pid,))[0]}


def teach(code, name, brand="", pack_size="", qty=1):
    """Unknown barcode: user names it once; remembered for next time."""
    code = clean_code(code)
    name = str(name or "").strip()[:200]
    if not code or not name:
        return {"status": "unknown", "barcode": code}
    save_product({"barcode": code, "name": name, "brand": brand, "pack_size": pack_size, "source": "user"})
    if db.rows("SELECT id FROM pantry WHERE barcode=?", (code,)):
        return scan(code, qty)
    pid = db.run("INSERT INTO pantry(barcode,name,brand,qty,pack_size) VALUES(?,?,?,?,?)",
                 (code, name, brand, qty, pack_size))
    kitchen.log_purchase(name)
    return {"status": "added", "item": db.rows("SELECT * FROM pantry WHERE id=?", (pid,))[0]}


def names():
    return [r["name"] for r in db.rows("SELECT name FROM pantry WHERE qty>0")]
