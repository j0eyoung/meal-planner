"""Home Assistant bridge (Supervisor proxy): to-do list sync and notifications."""
import json
import os
import urllib.request

import config

BASE = "http://supervisor/core/api"


def _call(path, body):
    token = os.getenv("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("Not running inside Home Assistant.")
    req = urllib.request.Request(f"{BASE}{path}", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read()


def add_todo(text):
    _call("/services/todo/add_item", {"entity_id": config.options()["todo_entity"], "item": text})


def notify(message, title="Meal prep"):
    svc = config.options()["notify_service"]
    if svc:
        _call("/services/" + svc.replace(".", "/", 1), {"title": title, "message": message})
