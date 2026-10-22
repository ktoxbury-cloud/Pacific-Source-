"""Small local web app: open http://localhost:8765 in a browser on the office computer."""

import json
import os
import re
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as config_mod
from .export import mark_scheduled, roll_over, write_day
from .geo import Geocoder
from .intake import Intake
from .planner import build_plan, plan_to_dict
from .quo import QuoError, sync
from .store import OPEN_STATUSES, STATUSES

STATIC = os.path.join(os.path.dirname(__file__), "static")


class App:
    def __init__(self, store, export_dir):
        self.store = store
        self.export_dir = os.path.abspath(export_dir)
        self.sync_lock = threading.Lock()

    def cfg(self):
        return config_mod.load(self.store)

    def geocoder(self):
        return Geocoder(self.store, online=bool(self.cfg().get("online_geocoding", True)))

    def intake(self):
        return Intake(self.store, self.geocoder())

    def plannable(self):
        return self.store.jobs(["new"])

    def plan(self, body):
        cfg = self.cfg()
        return build_plan(
            self.plannable(), cfg, start_date=body.get("date") or None, days=int(body.get("days") or 1),
            crews=body.get("crews"), max_stops_per_crew=body.get("max_stops_per_crew"),
            minutes_per_job=body.get("minutes_per_job"), day_hours=body.get("day_hours"),
            mode=body.get("mode"), start_time=body.get("start_time"),
            skip_weekends=body.get("skip_weekends"),
        )


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "KodiakScheduler/1.0"

        def log_message(self, fmt, *args):  # quieter console
            if "/api/" in (args[0] if args else ""):
                return
            super().log_message(fmt, *args)

        # helpers --------------------------------------------------------
        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else (
                json.dumps(body, default=str).encode() if ctype == "application/json" else body.encode())
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            raw = self.rfile.read(n).decode("utf-8")
            return json.loads(raw) if raw.strip() else {}

        def _route(self, method):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            try:
                if method == "GET":
                    return self._get(path, urllib.parse.parse_qs(parsed.query))
                body = self._body()
                return self._write(method, path, body)
            except QuoError as exc:
                return self._send(400, {"error": str(exc)})
            except Exception as exc:
                traceback.print_exc()
                return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PATCH(self):
            self._route("PATCH")

        def do_DELETE(self):
            self._route("DELETE")

        # GET -----------------------------------------------------------
        def _get(self, path, query):
            if path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html")
            if path == "/api/jobs":
                statuses = query.get("status", [None])[0]
                wanted = statuses.split(",") if statuses else list(OPEN_STATUSES)
                if statuses == "all":
                    wanted = list(STATUSES)
                return self._send(200, {"jobs": app.store.jobs(wanted), "counts": app.store.counts()})
            if path == "/api/config":
                return self._send(200, config_mod.public(app.cfg()))
            if path.startswith("/exports/"):
                rel = urllib.parse.unquote(path[len("/exports/"):])
                full = os.path.abspath(os.path.join(app.export_dir, rel))
                if not full.startswith(app.export_dir + os.sep) or not os.path.isfile(full):
                    return self._send(404, {"error": "not found"})
                ctype = "text/html" if full.endswith(".html") else "text/csv"
                with open(full, "rb") as fh:
                    return self._send(200, fh.read(), ctype)
            return self._send(404, {"error": "not found"})

        # POST / PATCH / DELETE ------------------------------------------
        def _write(self, method, path, body):
            m = re.fullmatch(r"/api/jobs/(\d+)", path)
            if m:
                job_id = int(m.group(1))
                if method == "DELETE":
                    app.store.delete_job(job_id)
                    return self._send(200, {"ok": True})
                if "address" in body:
                    app.intake().edit_address(job_id, body.pop("address"))
                if "lat" in body and "lng" in body:  # pin dragged on the map
                    body["geo_quality"] = "manual"
                if body.get("status") == "new":
                    body.setdefault("scheduled_date", "")
                    body.setdefault("crew", "")
                if "urgent" in body:
                    body["urgent"] = int(bool(body["urgent"]))
                app.store.update_job(job_id, **body)
                return self._send(200, {"job": app.store.get_job(job_id)})

            if path == "/api/jobs" and method == "POST":
                job_id = app.intake().add_manual(
                    address=body.get("address", ""), name=body.get("name", ""),
                    phone=body.get("phone", ""), notes=body.get("notes", ""),
                    urgent=body.get("urgent"), text=body.get("text", ""),
                    service_minutes=body.get("service_minutes") or None)
                return self._send(200, {"job": app.store.get_job(job_id)})
            if path == "/api/jobs/bulk":
                ids = app.intake().add_lines(body.get("text", ""))
                return self._send(200, {"added": len(ids)})
            if path == "/api/import-csv":
                ids = app.intake().import_csv(body.get("csv", ""))
                return self._send(200, {"added": len(ids)})
            if path == "/api/regeocode":
                n = 0
                geo = app.geocoder()
                for j in app.store.jobs(["new", "needs_address", "scheduled"]):
                    if j["address"] and j["geo_quality"] not in ("exact", "manual"):
                        loc = geo.locate(j["address"])
                        if loc:
                            app.store.update_job(j["id"], lat=loc["lat"], lng=loc["lng"],
                                                 geo_quality=loc["quality"])
                            n += 1
                return self._send(200, {"updated": n, "error": geo.last_error})
            if path == "/api/sync":
                if not app.sync_lock.acquire(blocking=False):
                    return self._send(409, {"error": "A sync is already running."})
                try:
                    lines = []
                    result = sync(app.store, app.intake(), app.cfg(), hours=body.get("hours") or None,
                                  log=lines.append)
                    result["log"] = lines
                    return self._send(200, result)
                finally:
                    app.sync_lock.release()
            if path == "/api/plan":
                return self._send(200, plan_to_dict(app.plan(body)))
            if path == "/api/export":
                cfg = app.cfg()
                plan = app.plan({**body, "days": 1})
                if not plan.days:
                    return self._send(400, {"error": "Nothing to schedule."})
                day = plan.days[0]
                files = write_day(day, cfg["depot_address"], app.export_dir)
                mark_scheduled(app.store, day)
                rel = [os.path.relpath(f, app.export_dir).replace(os.sep, "/") for f in files]
                return self._send(200, {"date": day.date, "files": ["/exports/" + r for r in rel],
                                        "paths": files, "scheduled": sum(len(r.stops) for r in day.routes)})
            if path == "/api/rollover":
                return self._send(200, {"returned": roll_over(app.store, body.get("date") or None)})
            if path == "/api/config":
                updates = dict(body)
                if updates.get("quo_api_key", "").startswith("•"):
                    updates.pop("quo_api_key")
                if "depot_address" in updates and updates["depot_address"]:
                    loc = app.geocoder().locate(updates["depot_address"])
                    if loc:
                        updates["depot_lat"], updates["depot_lng"] = loc["lat"], loc["lng"]
                if isinstance(updates.get("ignore_numbers"), str):
                    updates["ignore_numbers"] = [x.strip() for x in re.split(r"[,\n]", updates["ignore_numbers"]) if x.strip()]
                if isinstance(updates.get("crews"), str):
                    updates["crews"] = [x.strip() for x in updates["crews"].split(",") if x.strip()]
                return self._send(200, config_mod.public(config_mod.save(app.store, updates)))
            return self._send(404, {"error": "not found"})

    return Handler


def serve(store, export_dir, host="127.0.0.1", port=8765, open_browser=True):
    app = App(store, export_dir)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
    print(f"Kodiak repair scheduler running at {url}  (Ctrl+C to stop)")
    if open_browser:
        try:
            import webbrowser
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
