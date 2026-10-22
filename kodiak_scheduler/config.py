"""Settings, kept in the database so the web page and command line share them."""

import os

DEFAULTS = {
    # Where crews start each morning (shop / yard). Any address works.
    "depot_address": "Edmonton, AB",
    "depot_lat": 53.5461,
    "depot_lng": -113.4938,
    # Crew labels. Names are optional — "Crew 1, Crew 2, Crew 3" works fine.
    "crews": ["Crew 1", "Crew 2", "Crew 3"],
    "start_time": "08:00",
    "day_hours": 9.0,
    "minutes_per_job": 45,
    "max_stops_per_crew": 10,
    "avg_speed_kmh": 40,
    # "priority": oldest / urgent calls get booked first, area fills in around them.
    # "area": pack the tightest geographic groups regardless of call order.
    "mode": "priority",
    # Quo (OpenPhone) settings. The API key can also come from QUO_API_KEY.
    "quo_api_key": "",
    "quo_inbox": "Kodiak Roofing",      # inbox name, number (+1...) or PN... id
    "ignore_numbers": [],               # crew / staff phones — never turned into jobs
    "sync_lookback_hours": 48,          # first sync goes back this far
    "online_geocoding": True,
}


def load(store):
    cfg = dict(DEFAULTS)
    cfg.update(store.meta_get("config", {}) or {})
    if not cfg.get("quo_api_key"):
        cfg["quo_api_key"] = os.environ.get("QUO_API_KEY") or os.environ.get("OPENPHONE_API_KEY") or ""
    return cfg


def save(store, updates):
    current = dict(DEFAULTS)
    current.update(store.meta_get("config", {}) or {})
    for key, value in updates.items():
        if key in DEFAULTS:
            current[key] = value
    store.meta_set("config", current)
    return load(store)


def public(cfg):
    """Config safe to send to the browser (API key masked)."""
    out = dict(cfg)
    key = out.get("quo_api_key") or ""
    out["quo_api_key"] = ("•" * 8 + key[-4:]) if key else ""
    out["quo_connected"] = bool(key)
    return out
