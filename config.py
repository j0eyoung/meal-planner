"""Add-on options from /data/options.json (env vars override for local dev)."""
import json
import os

_DEFAULTS = {
    "anthropic_api_key": "", "model": "claude-sonnet-5-5", "usda_api_key": "", "upcdatabase_api_key": "",
    "recipeapi_key": "", "recipeapi_monthly_budget": 450, "pexels_api_key": "",
    "s3_endpoint": "", "s3_bucket": "", "s3_access_key": "", "s3_secret_key": "", "s3_region": "auto",
    "s3_prefix": "meals/images",
    "todo_entity": "todo.shopping_list", "notify_service": "", "prep_reminder_time": "20:00",
}


def options():
    opts = dict(_DEFAULTS)
    try:
        with open("/data/options.json") as f:
            # strip: a key pasted with a trailing newline would break the HTTP headers it goes into
            opts.update({k: v.strip() if isinstance(v, str) else v for k, v in json.load(f).items()
                         if v not in (None, "") and not (isinstance(v, str) and not v.strip())})
    except (OSError, ValueError):
        pass
    if os.getenv("ANTHROPIC_API_KEY"):
        opts["anthropic_api_key"] = os.environ["ANTHROPIC_API_KEY"]
    return opts
