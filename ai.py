"""Claude calls: weekly plan, single-meal regeneration, recipe import, product and receipt photos."""
import ipaddress
import json
import re
import socket
import threading
import urllib.parse
import urllib.request

import claude_cli
import config
import recipes

# Ocean fish is out; shrimp and river/freshwater fish are fine. Each name may take a plural ending.
_OCEAN = (r"salmon|tuna|cod|tilapia|halibut|mahi(?:[ -]mahi)?|sea ?bass|(?:striped|chilean|black) bass|snapper|"
          r"haddock|sardine|anchov(?:y|ie)|mackerel|swordfish|flounder|sole|pollock|herring|kipper|grouper|hake|"
          r"whiting|rockfish|orange roughy|barramundi|wahoo|marlin|bluefish|dorado|turbot|plaice|pompano|mullet|"
          r"sea ?bream|eel|unagi|bacalao|gravlax|lox|ahi|yellowtail|monkfish|branzino|bonito|dashi|surimi|"
          r"smoked fish|ocean perch|sea trout|(?:white|firm|flaky|mild) ?fish|"
          r"fish (?:fillet|stick|finger|taco|cake|pie|stock|broth|sauce|curry|and chips)")
BANNED = re.compile(r"\b(?:" + _OCEAN + r")(?:e?s)?\b", re.I)
FRESHWATER_OK = re.compile(r"(?<!ocean )(?<!sea )\b(?:rainbow |brook |lake )?(?:trout|catfish|perch|walleye|pike|"
                           r"crappie|bluegill|carp)\b", re.I)

SYSTEM = (
    "You are a meal planner for one home cook. Rules: eats everything EXCEPT ocean fish "
    "(no salmon, tuna, cod, tilapia, anchovies, sardines, fish sauce, etc). Shrimp and freshwater/river fish "
    "(trout, catfish) are fine. No nutrition goals; give macros per serving as information only. Shops at "
    "Costco, Costco Business Center and King Soopers (Kroger); use `store` = costco | costco_business | "
    "king_soopers | other and a short grocery `aisle` (Produce, Meat, Dairy, Frozen, Pantry, Bakery, Spices, "
    "Beverages, Other). For Costco items add a short `note` about the bulk pack size when relevant. Prefer "
    "reusing ingredients across the week and prefer ingredients from the provided pantry. Quantities are for "
    "the recipe's stated `servings`. Use units: g, kg, ml, l, tsp, tbsp, cup, oz, lb, or '' for counts, and "
    "numbers (not fractions in strings) for qty. `cost_per_serving` is a rough USD estimate at Colorado "
    "Costco/King Soopers prices (use the cook's recent prices when given). Reply with JSON only, no prose."
)

RECIPE_SHAPE = (
    '{"name":str,"meal_type":"breakfast|lunch|dinner","servings":int,"ingredients":[{"name":str,"qty":number,'
    '"unit":str,"aisle":str,"store":str,"note":str}],"steps":[str],"calories":number,"protein":number,'
    '"carbs":number,"fat":number,"cost_per_serving":number}'
)

# One Claude job at a time: each CLI run is a few hundred MB of Node on a 4 GB Green, and every call
# spends subscription quota. A second request gets a clear "busy" message instead of queueing.
_slot = threading.BoundedSemaphore(1)


class Busy(RuntimeError):
    pass


def available():
    return claude_cli.available() or bool(config.options()["anthropic_api_key"])


def _extract_json(text):
    """The longest valid JSON object/array in the reply, ignoring prose, code fences and stray brackets."""
    dec, best = json.JSONDecoder(), None
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    for chunk in ([fence.group(1)] if fence else []) + [text]:
        for m in re.finditer(r"[\[{]", chunk):
            try:
                obj, end = dec.raw_decode(chunk[m.start():])
            except ValueError:
                continue
            if isinstance(obj, (dict, list)) and (best is None or end > best[0]):
                best = (end, obj)
        if best:
            return best[1]
    raise RuntimeError("Claude did not return valid JSON: " + text[:200])


def _ask(prompt, max_tokens=8000, image_b64=None, timeout=600):
    """Claude Code CLI (subscription) first; the Anthropic API key is a fallback, also when the CLI fails."""
    if not _slot.acquire(blocking=False):
        raise Busy("Claude is already working on another request. Try again in a minute.")
    try:
        opts = config.options()
        text, cli_error = None, None
        if claude_cli.available():
            try:
                text = claude_cli.run(SYSTEM + "\n\n" + prompt, model=opts["model"], image_b64=image_b64,
                                      timeout=timeout)
            except RuntimeError as e:
                cli_error = e
        if text is None and opts["anthropic_api_key"]:
            import anthropic
            content = prompt
            if image_b64:
                content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                         "data": image_b64}},
                           {"type": "text", "text": prompt}]
            msg = anthropic.Anthropic(api_key=opts["anthropic_api_key"]).messages.create(
                model=opts["model"], max_tokens=max_tokens, system=SYSTEM,
                messages=[{"role": "user", "content": content}])
            text = "".join(b.text for b in msg.content if b.type == "text")
        if text is None:
            raise cli_error or RuntimeError("Connect Claude on the Plan tab (or add an API key in the Configuration tab).")
        return _extract_json(text)
    finally:
        _slot.release()


def _first(obj):
    """Claude sometimes wraps one object in a list or a {"recipe": ...} envelope."""
    if isinstance(obj, list):
        obj = obj[0] if obj else {}
    if isinstance(obj, dict) and isinstance(obj.get("recipe"), dict):
        obj = obj["recipe"]
    return obj if isinstance(obj, dict) else {}


def violates_diet(recipe):
    if not isinstance(recipe, dict):
        return False
    ings = recipe.get("ingredients") or []
    names = [i.get("name") if isinstance(i, dict) else i for i in ings] if isinstance(ings, list) else []
    steps = recipe.get("steps") if isinstance(recipe.get("steps"), list) else []
    blob = " ".join(str(x or "") for x in [recipe.get("name")] + names + steps)
    return bool(BANNED.search(FRESHWATER_OK.sub(" ", blob)))


def _use_soon_line(use_soon):
    return (f"USE THESE FIRST, they expire soon (plan them into the earliest days): {json.dumps(use_soon[:30])}.\n"
            if use_soon else "")


def _prices_line(prices):
    return f"Recent prices the cook paid: {json.dumps(list(prices)[:40])}.\n" if prices else ""


def generate_week(dates, headcounts, pantry_names, avoid=(), notes="", use_soon=(), favorites=(), prices=()):
    """headcounts: {date: {slot: people}}. Returns (items, skipped) where items are
    {"date","slot","recipe"} or {"date","slot","saved_recipe": name}."""
    days = [{"date": d, "people": headcounts.get(d, {})} for d in dates]
    fav = (f"The cook's FAVOURITE recipes: reuse 2-4 of them this week where they fit (to reuse one, return "
           f"\"saved_recipe\": \"<exact name>\" instead of \"recipe\"): {json.dumps(list(favorites)[:40])}.\n"
           if favorites else "")
    prompt = (
        f"Plan breakfast, lunch and dinner for these days. `people` gives the number of people eating each meal; "
        f"write each recipe for that many servings: {json.dumps(days)}.\n"
        f"Pantry on hand: {json.dumps(sorted(pantry_names)[:150])}.\n"
        f"{_use_soon_line(list(use_soon))}{fav}{_prices_line(prices)}"
        f"Avoid repeating recent meals: {json.dumps(list(avoid)[:40])}.\n"
        f"Extra notes: {notes or 'none'}.\n"
        f"Return JSON: a list of {{\"date\":\"YYYY-MM-DD\",\"slot\":\"breakfast|lunch|dinner\",\"recipe\":{RECIPE_SHAPE}}}."
    )
    raw = _ask(prompt, max_tokens=16000)
    if isinstance(raw, dict):           # {"plan": [...]} or {"meals": [...]}
        raw = next((v for v in raw.values() if isinstance(v, list)), [])
    items, bad = [], []
    for i in raw if isinstance(raw, list) else []:
        if not isinstance(i, dict) or i.get("date") not in headcounts or i.get("slot") not in ("breakfast", "lunch", "dinner"):
            continue
        if isinstance(i.get("saved_recipe"), str) and i["saved_recipe"].strip():
            items.append({"date": i["date"], "slot": i["slot"], "saved_recipe": i["saved_recipe"].strip()})
        elif isinstance(i.get("recipe"), dict) and i["recipe"].get("name"):
            (bad if violates_diet(i["recipe"]) else items).append(i)
    return items, bad


def regenerate_meal(date, slot, headcount, pantry_names, current_name="", hint="", use_soon=(), prices=()):
    for _ in range(2):
        prompt = (
            f"Suggest ONE {slot} for {date} for {headcount} people, different from '{current_name}'. "
            f"Pantry: {json.dumps(sorted(pantry_names)[:150])}. Hint: {hint or 'none'}.\n"
            f"{_use_soon_line(list(use_soon))}{_prices_line(prices)}"
            f"Return JSON: {RECIPE_SHAPE}"
        )
        r = _first(_ask(prompt))
        if r.get("name") and not violates_diet(r):
            return r
    raise RuntimeError("Claude kept suggesting fish; try again.")


def identify_product(image_b64):
    """Front-of-package photo -> product, for barcodes no database knows (Kirkland etc.)."""
    r = _first(_ask('Identify this grocery product from the photo. Return JSON: {"name":str,"brand":str,'
                    '"pack_size":str,"category":str}. Use the plain product name a shopper would say.',
                    max_tokens=500, image_b64=image_b64, timeout=180))
    if not r.get("name"):
        raise RuntimeError("Claude could not tell what that is. Type the name instead.")
    return r


def read_receipt(image_b64):
    """Store receipt photo -> food items bought. Expands Costco/Kroger abbreviations."""
    r = _ask('This is a grocery receipt (often Costco or King Soopers, with abbreviations like '
             '"KS ORG EGGS 24CT"). List the FOOD and drink items only (skip fees, tax, bags, deposits, '
             'non-food). Return JSON: {"store":"costco|costco_business|king_soopers|other","items":[{"name":str,'
             '"qty":number,"unit":str,"pack_size":str,"category":str,"price":number}]}. name = plain product name '
             '("Kirkland organic eggs"); qty = packages bought; pack_size from the abbreviation if shown; '
             'price = what that line cost in USD after any instant savings on the receipt.',
             max_tokens=4000, image_b64=image_b64, timeout=240)
    if isinstance(r, list):
        r = {"store": "other", "items": r}
    items = [i for i in (r.get("items") or []) if isinstance(i, dict) and i.get("name")]
    return {"store": r.get("store") if isinstance(r.get("store"), str) else "other", "items": items}


# --- Recipe import: only public http(s) pages (no file://, no LAN or Supervisor addresses) -----------------

def _check_url(url):
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise RuntimeError("Only http(s) recipe links can be imported.")
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except OSError:
        raise RuntimeError("Could not find that website.")
    for *_, sa in infos:
        ip = ipaddress.ip_address(sa[0].split("%")[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not ip.is_global:
            raise RuntimeError("That address is not allowed.")


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _page_text(html, cap=30000):
    """Linear-time text extraction (regex .*? on broken pages can freeze the add-on for minutes).
    JSON-LD recipe blocks win when present."""
    low, out, ld, i = html.lower(), [], [], 0
    while True:
        j = min([p for p in (low.find("<script", i), low.find("<style", i)) if p >= 0], default=-1)
        if j < 0:
            out.append(html[i:])
            break
        out.append(html[i:j])
        tag = "script" if low.startswith("<script", j) else "style"
        head_end = low.find(">", j)
        close = low.find(f"</{tag}", head_end if head_end >= 0 else j)
        if head_end < 0 or close < 0:
            break
        if "ld+json" in low[j:head_end]:
            ld.append(html[head_end + 1:close])
        i = close + len(tag) + 3
    if any("Recipe" in b for b in ld):
        return "\n".join(ld)[:cap]
    text, depth, buf = "".join(out), 0, []
    for ch in text:                       # drop tags without a regex
        if ch == "<":
            depth = 1
        elif ch == ">" and depth:
            depth = 0
            buf.append(" ")
        elif not depth:
            buf.append(ch)
    return re.sub(r"\s+", " ", "".join(buf))[:cap]


def import_recipe(text=None, url=None):
    if url:
        _check_url(url)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.build_opener(_CheckedRedirect).open(req, timeout=20) as r:
                html = r.read(1_500_000).decode("utf-8", "ignore")
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError("Could not fetch that page.")
        parsed = recipes.scrape(html, url)          # recipe-scrapers: free and exact for 500+ sites
        if parsed:
            if (parsed.get("calories") is None or parsed.get("cost_per_serving") is None) and available():
                try:                                # one small call to fill in macros the page did not publish
                    lines = [" ".join(str(i.get(k) or "") for k in ("qty", "unit", "name")).strip()
                             for i in parsed["ingredients"]]
                    est = _first(_ask(
                        f"Estimate macros and cost per serving for this recipe ({parsed['servings']} servings). Return JSON "
                        '{"calories":number,"protein":number,"carbs":number,"fat":number,"cost_per_serving":number}.\n'
                        + json.dumps(lines),
                        max_tokens=300, timeout=120))
                    parsed.update({k: est.get(k) for k in ("calories", "protein", "carbs", "fat", "cost_per_serving")
                                   if isinstance(est.get(k), (int, float))})
                except Exception:
                    pass
            return parsed
        text = _page_text(html[:600_000])
    text = (text or "")[:30000].replace("\x00", "")
    prompt = f"Extract this recipe into JSON ({RECIPE_SHAPE}). Estimate macros per serving if missing.\n\n{text}"
    r = _first(_ask(prompt, max_tokens=4000, timeout=240))
    if not r.get("name"):
        raise RuntimeError("Claude could not find a recipe in that.")
    return r
