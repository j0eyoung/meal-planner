import json
import os
import sqlite3

DB_PATH = os.getenv("DB_PATH", "meal_planner.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    meal_type TEXT,            -- breakfast / lunch / dinner / snack
    servings INTEGER DEFAULT 1,
    ingredients TEXT,          -- JSON [{name, qty, unit, aisle, store}]
    steps TEXT,                -- JSON [str]
    calories REAL, protein REAL, carbs REAL, fat REAL,   -- per serving, information only
    source_url TEXT,
    favorite INTEGER DEFAULT 0,
    saved INTEGER DEFAULT 1,   -- 1 = in "My recipes"; 0 = library/generated only
    origin TEXT DEFAULT 'mine', -- mine / claude / recipeapi / mealdb
    ext_id TEXT,               -- id at the origin, for de-duplication
    created TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS plan_meals (
    date TEXT, slot TEXT,      -- slot: breakfast / lunch / dinner
    recipe_id INTEGER,
    headcount INTEGER DEFAULT 1,
    PRIMARY KEY (date, slot)
);
CREATE TABLE IF NOT EXISTS day_headcount (date TEXT PRIMARY KEY, headcount INTEGER);
CREATE TABLE IF NOT EXISTS meal_headcount (  -- per-meal override, e.g. Friday dinner = 4
    date TEXT, slot TEXT, headcount INTEGER, PRIMARY KEY (date, slot)
);
CREATE TABLE IF NOT EXISTS pantry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    barcode TEXT,
    name TEXT NOT NULL,
    brand TEXT,
    qty REAL DEFAULT 1,
    unit TEXT DEFAULT '',      -- '' = count (packages/items)
    pack_size TEXT,            -- e.g. "16 oz" from the label
    category TEXT,
    expires TEXT,
    updated TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS barcodes (        -- local cache + user-taught products
    barcode TEXT PRIMARY KEY,
    name TEXT, brand TEXT, pack_size TEXT, category TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS grocery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, qty REAL, unit TEXT,
    store TEXT, aisle TEXT, note TEXT,
    checked INTEGER DEFAULT 0, synced INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS purchases (       -- what was bought when, for grocery suggestions
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, key TEXT, date TEXT, store TEXT
);
"""

# Columns that may be missing from an older database: (table, column, definition).
# ALTER TABLE cannot add DEFAULT CURRENT_TIMESTAMP, hence plain TEXT for `updated`.
MIGRATIONS = [
    ("recipes", "favorite", "INTEGER DEFAULT 0"), ("recipes", "saved", "INTEGER DEFAULT 1"),
    ("recipes", "origin", "TEXT DEFAULT 'mine'"), ("recipes", "ext_id", "TEXT"),
    ("plan_meals", "cooked", "INTEGER DEFAULT 0"),
    ("pantry", "barcode", "TEXT"), ("pantry", "brand", "TEXT"), ("pantry", "pack_size", "TEXT"),
    ("pantry", "category", "TEXT"), ("pantry", "expires", "TEXT"), ("pantry", "updated", "TEXT"),
    ("grocery", "note", "TEXT"), ("grocery", "checked", "INTEGER DEFAULT 0"),
    ("grocery", "synced", "INTEGER DEFAULT 0"), ("grocery", "stocked", "INTEGER DEFAULT 0"),
]

# Indexes come after the migrations: an index on a column an old table lacks would stop start-up.
INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS recipes_ext ON recipes(origin, ext_id) WHERE ext_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS pantry_barcode ON pantry(barcode);
CREATE INDEX IF NOT EXISTS purchases_key ON purchases(key);
"""

_keeper = None   # one connection held open so the WAL files are not created and deleted on every query


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    global _keeper
    conn = connect()
    conn.execute("PRAGMA journal_mode=WAL")      # persistent; fewer eMMC writes than a rollback journal
    conn.executescript(SCHEMA)
    for table, col, ddl in MIGRATIONS:
        if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    conn.executescript(INDEXES)
    conn.commit()
    conn.close()
    if _keeper is None:
        _keeper = sqlite3.connect(DB_PATH, check_same_thread=False)


def rows(sql, args=()):
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def run(sql, args=()):
    conn = connect()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_setting(key, default=None):
    r = rows("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(r[0]["value"]) if r else default


def set_setting(key, value):
    run("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)))


if __name__ == "__main__":
    init_db()
    print("Database ready.")
