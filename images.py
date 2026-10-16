"""Recipe pictures. Source: the recipe's own photo (imported page, TheMealDB) or a Pexels search by dish name.
With the B2 bucket set up (same S3-style settings as the workout add-on), each picture is shrunk to ~600px and
kept in the PRIVATE bucket; the page asks this add-on for `api/img/<id>` and the add-on fetches it with the key
(which never reaches the browser). Nothing is written to the Green's disk. Without a bucket, the page shows the
picture straight from its source link.

A background worker fills in pictures one at a time for recipes you saved, planned or favourited, so plan
generation is never slowed down and Pexels' free limit (200 searches an hour) is respected."""
import io
import json
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict

import config
import db

PEXELS = "https://api.pexels.com/v1/search?per_page=1&orientation=landscape&query="
_client = {"c": None, "sig": None}
_mem = OrderedDict()            # small in-memory cache of served pictures (no disk writes)
_mem_lock = threading.Lock()


def settings():
    o = config.options()
    return {"endpoint": o["s3_endpoint"], "bucket": o["s3_bucket"], "key": o["s3_access_key"],
            "secret": o["s3_secret_key"], "region": o["s3_region"] or "auto",
            "prefix": (o["s3_prefix"] or "meals/images").strip("/")}


def bucket_ready():
    s = settings()
    return bool(s["endpoint"] and s["bucket"] and s["key"] and s["secret"])


def _s3():
    s = settings()
    sig = (s["endpoint"], s["key"], s["secret"], s["region"])
    if _client["c"] is None or _client["sig"] != sig:
        import boto3
        from botocore.config import Config
        _client["c"] = boto3.client(
            "s3", endpoint_url=s["endpoint"], aws_access_key_id=s["key"], aws_secret_access_key=s["secret"],
            region_name=s["region"],
            # boto3 checksum headers can be refused by Backblaze B2 (same as the workout add-on)
            config=Config(retries={"max_attempts": 4, "mode": "standard"}, connect_timeout=10, read_timeout=60,
                          request_checksum_calculation="when_required", response_checksum_validation="when_required"))
        _client["sig"] = sig
    return _client["c"]


def _http(url, headers=None, limit=6_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(limit)


def pexels_search(name):
    key = config.options()["pexels_api_key"]
    if not key:
        return None
    data = json.loads(_http(PEXELS + urllib.parse.quote(f"{name} food"), {"Authorization": key}))
    photos = data.get("photos") or []
    return photos[0]["src"].get("large") if photos else None


def _shrink(raw):
    from PIL import Image
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    im.thumbnail((600, 600))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=80, optimize=True)
    return out.getvalue()


def fill_one():
    """Give one recipe that needs a picture its picture. Returns True if there was work to do."""
    row = db.rows("SELECT id, name, image_src FROM recipes WHERE (image IS NULL OR image='') AND image_tries<3 "
                  "AND (saved=1 OR favorite=1 OR id IN (SELECT recipe_id FROM plan_meals)) "
                  "ORDER BY favorite DESC, id DESC LIMIT 1")
    if not row:
        return False
    r = row[0]
    db.run("UPDATE recipes SET image_tries=image_tries+1 WHERE id=?", (r["id"],))
    src = r["image_src"] or pexels_search(r["name"])
    if not src:
        return True
    if not bucket_ready():
        db.run("UPDATE recipes SET image=?, image_src=? WHERE id=?", (src, src, r["id"]))
        return True
    data = _shrink(_http(src))
    key = f"{settings()['prefix']}/{r['id']}.jpg"
    _s3().put_object(Bucket=settings()["bucket"], Key=key, Body=data, ContentType="image/jpeg")
    db.run("UPDATE recipes SET image=?, image_src=? WHERE id=?", ("b2:" + key, src, r["id"]))
    return True


def fetch(image):
    """Bytes of a b2: picture, from memory or the bucket."""
    key = image[3:]
    with _mem_lock:
        if key in _mem:
            _mem.move_to_end(key)
            return _mem[key]
    data = _s3().get_object(Bucket=settings()["bucket"], Key=key)["Body"].read()
    with _mem_lock:
        _mem[key] = data
        while len(_mem) > 60:
            _mem.popitem(last=False)
    return data


def url_for(r):
    """What the page should load for a recipe row (None = no picture yet)."""
    img = r.get("image") or ""
    if img.startswith("b2:"):
        return f"api/img/{r['id']}"
    return img if img.startswith("https://") else (r.get("image_src") if (r.get("image_src") or "").startswith("https://") else None)


def worker():
    time.sleep(30)
    while True:
        busy = False
        try:
            if config.options()["pexels_api_key"] or bucket_ready() or db.rows(
                    "SELECT 1 FROM recipes WHERE image_src IS NOT NULL AND (image IS NULL OR image='') LIMIT 1"):
                busy = fill_one()
        except Exception as e:
            print(f"Recipe picture failed: {e}", flush=True)
        time.sleep(20 if busy else 300)     # ~180 an hour at most, under Pexels' free 200
