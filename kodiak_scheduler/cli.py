"""Command line: `python -m kodiak_scheduler <command>`.

  serve                 open the web app (what most people will use)
  sync [--hours 24]     pull new calls/texts from Quo into the job list
  add "address" ...     add one job
  paste FILE            add one job per line of a text file
  import-csv FILE       add jobs from a spreadsheet export
  list                  show waiting jobs
  plan [--days 3]       preview the schedule in the terminal
  export [--date ...]   write tomorrow's crew sheets and mark those jobs scheduled
  done ID [ID ...]      mark jobs finished
  rollover              put scheduled-but-unfinished jobs back in the queue
  config key=value ...  change a setting (e.g. crews="Crew 1,Crew 2" depot_address="...")
"""

import argparse
import json
import os
import sys

from . import config as config_mod
from .export import mark_scheduled, roll_over, write_day
from .geo import Geocoder
from .intake import Intake
from .planner import build_plan
from .quo import QuoError, sync
from .store import Store

DEFAULT_HOME = os.environ.get("KODIAK_SCHEDULER_HOME") or os.path.join(os.getcwd(), "scheduler-data")


def _setup(args):
    os.makedirs(args.home, exist_ok=True)
    store = Store(os.path.join(args.home, "jobs.db"))
    cfg = config_mod.load(store)
    if getattr(args, "offline", False):
        cfg["online_geocoding"] = False
    geo = Geocoder(store, online=bool(cfg.get("online_geocoding", True)))
    return store, cfg, Intake(store, geo)


def _print_plan(plan):
    for d in plan.days:
        print(f"\n=== {d.date} ===")
        for r in d.routes:
            flag = "  (LONG DAY)" if r.overloaded else ""
            print(f"\n  {r.crew}: {len(r.stops)} stops, {r.area}, ~{r.drive_km:.0f} km, "
                  f"~{r.total_minutes / 60:.1f} h{flag}")
            for st in r.stops:
                j = st.job
                urgent = " [URGENT]" if j.get("urgent") else ""
                print(f"    {st.order:>2}. {st.arrive:>8}  {j['address']}{urgent}  "
                      f"{j.get('name') or ''} {j.get('phone') or ''}")
    left = len(getattr(plan, "leftover", []))
    if left:
        print(f"\n{left} more waiting jobs didn't fit.")
    if plan.unplaced:
        print(f"{len(plan.unplaced)} jobs have no map location (fix their address):")
        for j in plan.unplaced:
            print(f"   #{j['id']} {j['address'] or '(no address)'}")


def _plan_kwargs(args):
    return dict(crews=args.crews, max_stops_per_crew=args.max_stops, minutes_per_job=args.minutes,
                day_hours=args.hours_per_day, mode=args.mode)


def main(argv=None):
    p = argparse.ArgumentParser(prog="kodiak_scheduler", description="Storm repair scheduler")
    p.add_argument("--home", default=DEFAULT_HOME, help="folder for the database and exports")
    p.add_argument("--offline", action="store_true", help="don't look addresses up online")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to open it to other office computers")
    s.add_argument("--no-browser", action="store_true")

    s = sub.add_parser("sync")
    s.add_argument("--hours", type=float, help="look back this many hours instead of since the last sync")

    s = sub.add_parser("add")
    s.add_argument("address")
    s.add_argument("--name", default="")
    s.add_argument("--phone", default="")
    s.add_argument("--notes", default="")
    s.add_argument("--urgent", action="store_true", default=None)

    s = sub.add_parser("paste")
    s.add_argument("file")
    s = sub.add_parser("import-csv")
    s.add_argument("file")
    s = sub.add_parser("list")
    s.add_argument("--status", default="new,needs_address,scheduled")

    for name in ("plan", "export"):
        s = sub.add_parser(name)
        s.add_argument("--date", help="YYYY-MM-DD (default: tomorrow)")
        s.add_argument("--crews", help='e.g. "Crew 1,Crew 2,Crew 3" or 3')
        s.add_argument("--max-stops", type=int)
        s.add_argument("--minutes", type=int, help="minutes per job")
        s.add_argument("--hours-per-day", type=float)
        s.add_argument("--mode", choices=["priority", "area"])
        if name == "plan":
            s.add_argument("--days", type=int, default=1)

    s = sub.add_parser("done")
    s.add_argument("ids", nargs="+", type=int)
    sub.add_parser("rollover")
    s = sub.add_parser("config")
    s.add_argument("pairs", nargs="*", help="key=value")

    args = p.parse_args(argv)
    if getattr(args, "crews", None) and args.crews.isdigit():
        args.crews = int(args.crews)
    store, cfg, intake = _setup(args)

    if args.cmd == "serve":
        from .web import serve
        serve(store, os.path.join(args.home, "exports"), host=args.host, port=args.port,
              open_browser=not args.no_browser)
    elif args.cmd == "sync":
        try:
            sync(store, intake, cfg, hours=args.hours)
        except QuoError as exc:
            sys.exit(f"Quo sync failed: {exc}")
    elif args.cmd == "add":
        job_id = intake.add_manual(address=args.address, name=args.name, phone=args.phone,
                                   notes=args.notes, urgent=args.urgent)
        j = store.get_job(job_id)
        print(f"#{job_id} {j['address']}  (map: {j['geo_quality'] or 'not found'})")
    elif args.cmd == "paste":
        with open(args.file, encoding="utf-8") as fh:
            print(f"Added {len(intake.add_lines(fh.read()))} jobs.")
    elif args.cmd == "import-csv":
        with open(args.file, encoding="utf-8-sig") as fh:
            print(f"Imported {len(intake.import_csv(fh.read()))} jobs.")
    elif args.cmd == "list":
        for j in store.jobs(args.status.split(",")):
            print(f"#{j['id']:<4} {j['status']:<13} {'!' if j['urgent'] else ' '} {j['address'] or '(no address)':<45} "
                  f"{j['name'] or '':<18} {j['phone'] or '':<13} {j['issue']}")
        print(store.counts())
    elif args.cmd == "plan":
        _print_plan(build_plan(store.jobs(["new"]), cfg, start_date=args.date, days=args.days,
                               **_plan_kwargs(args)))
    elif args.cmd == "export":
        plan = build_plan(store.jobs(["new"]), cfg, start_date=args.date, days=1, **_plan_kwargs(args))
        if not plan.days:
            sys.exit("Nothing waiting to schedule.")
        files = write_day(plan.days[0], cfg["depot_address"], os.path.join(args.home, "exports"))
        mark_scheduled(store, plan.days[0])
        _print_plan(plan)
        print("\nWrote:\n  " + "\n  ".join(files))
    elif args.cmd == "done":
        for i in args.ids:
            store.update_job(i, status="done")
        print(f"Marked {len(args.ids)} done.")
    elif args.cmd == "rollover":
        print(f"{roll_over(store)} jobs back in the queue.")
    elif args.cmd == "config":
        updates = {}
        for pair in args.pairs:
            key, _, value = pair.partition("=")
            try:
                updates[key] = json.loads(value)
            except ValueError:
                updates[key] = [v.strip() for v in value.split(",")] if key in ("crews", "ignore_numbers") else value
        if "depot_address" in updates:
            loc = intake.geocoder.locate(updates["depot_address"])
            if loc:
                updates["depot_lat"], updates["depot_lng"] = loc["lat"], loc["lng"]
        cfg = config_mod.save(store, updates) if updates else cfg
        print(json.dumps(config_mod.public(cfg), indent=2))


if __name__ == "__main__":
    main()
