"""Group jobs into crew routes, day by day.

For each day, each crew gets a route:
  * It starts from a "seed" job — the oldest/most urgent call still waiting
    (or, in "area" mode, the job farthest from the shop, so outlying areas
    don't get left for last).
  * Nearby jobs are added one at a time, always the one that adds the least
    extra driving, until the crew's day is full (hours or stop limit).
  * The stop order is then tidied up (2-opt) so the crew isn't zig-zagging.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .geo import haversine_km

ROAD_FACTOR = 1.35          # roads aren't straight lines
PER_STOP_OVERHEAD_MIN = 5   # parking, ladder, talking to the homeowner


@dataclass
class Stop:
    job: dict
    order: int = 0
    arrive: str = ""
    leave: str = ""
    drive_km: float = 0.0


@dataclass
class Route:
    crew: str
    stops: list = field(default_factory=list)
    drive_km: float = 0.0
    work_minutes: float = 0.0
    total_minutes: float = 0.0
    overloaded: bool = False

    @property
    def area(self):
        towns = {}
        for s in self.stops:
            parts = [p.strip() for p in (s.job.get("address") or "").split(",")]
            town = parts[-2] if len(parts) >= 3 else "Edmonton"
            towns[town] = towns.get(town, 0) + 1
        return ", ".join(t for t, _ in sorted(towns.items(), key=lambda kv: -kv[1])) if towns else ""


@dataclass
class DayPlan:
    date: str
    routes: list = field(default_factory=list)


@dataclass
class Plan:
    days: list = field(default_factory=list)
    unplaced: list = field(default_factory=list)   # no map location — fix the address

    @property
    def scheduled_count(self):
        return sum(len(r.stops) for d in self.days for r in d.routes)


class Settings:
    def __init__(self, cfg, **overrides):
        merged = dict(cfg)
        merged.update({k: v for k, v in overrides.items() if v is not None})
        self.depot = (float(merged["depot_lat"]), float(merged["depot_lng"]))
        crews = merged["crews"]
        if isinstance(crews, str):
            crews = [c.strip() for c in crews.split(",") if c.strip()]
        if isinstance(crews, int):
            crews = [f"Crew {i + 1}" for i in range(crews)]
        self.crews = crews or ["Crew 1"]
        self.start_time = merged["start_time"]
        self.day_minutes = float(merged["day_hours"]) * 60
        self.minutes_per_job = float(merged["minutes_per_job"])
        self.max_stops = int(merged["max_stops_per_crew"])
        self.speed = float(merged["avg_speed_kmh"])
        self.mode = merged.get("mode", "priority")
        self.skip_weekends = bool(merged.get("skip_weekends", False))


def _pt(job):
    return (job["lat"], job["lng"])


def _drive_min(km, speed):
    return km / speed * 60


class _Dist:
    def __init__(self):
        self.cache = {}

    def __call__(self, a, b):
        key = (a, b) if a <= b else (b, a)
        d = self.cache.get(key)
        if d is None:
            d = haversine_km(a, b) * ROAD_FACTOR
            self.cache[key] = d
        return d


def _path_km(points, dist):
    return sum(dist(a, b) for a, b in zip(points, points[1:]))


def _service(job, s):
    return float(job.get("service_minutes") or s.minutes_per_job)


def _route_minutes(depot, jobs, s, dist):
    pts = [depot] + [_pt(j) for j in jobs]
    km = _path_km(pts, dist)
    work = sum(_service(j, s) + PER_STOP_OVERHEAD_MIN for j in jobs)
    return _drive_min(km, s.speed) + work, km, work


def _two_opt(depot, jobs, dist):
    """Untangle crossings in an open route that starts at the depot."""
    if len(jobs) < 3:
        return jobs
    best = list(jobs)
    improved = True
    while improved:
        improved = False
        for i in range(len(best) - 1):
            for k in range(i + 1, len(best)):
                cand = best[:i] + best[i:k + 1][::-1] + best[k + 1:]
                if _path_km([depot] + [_pt(j) for j in cand], dist) + 1e-9 < \
                        _path_km([depot] + [_pt(j) for j in best], dist):
                    best = cand
                    improved = True
    return best


def _priority_key(job):
    return (0 if job.get("urgent") else 1, job.get("received_at") or "", job["id"])


def _build_route(seed, pool, depot, s, dist):
    route = [seed]
    pool.remove(seed)
    while pool and len(route) < s.max_stops:
        best = None
        for job in pool:
            p = _pt(job)
            pts = [depot] + [_pt(j) for j in route]
            # cheapest place to slot it in (including at the end)
            for pos in range(1, len(pts) + 1):
                prev = pts[pos - 1]
                if pos < len(pts):
                    extra = dist(prev, p) + dist(p, pts[pos]) - dist(prev, pts[pos])
                else:
                    extra = dist(prev, p)
                if best is None or extra < best[0]:
                    best = (extra, job, pos - 1)
        _, job, pos = best
        trial = route[:pos] + [job] + route[pos:]
        minutes, _, _ = _route_minutes(depot, trial, s, dist)
        if minutes > s.day_minutes:
            break
        route = trial
        pool.remove(job)
    route = _two_opt(depot, route, dist)
    # Leaks and other urgent jobs go first thing in the morning, the rest follow.
    urgent = [j for j in route if j.get("urgent")]
    if urgent and len(urgent) < len(route):
        urgent = _two_opt(depot, urgent, dist)
        rest = _two_opt(_pt(urgent[-1]), [j for j in route if not j.get("urgent")], dist)
        route = urgent + rest
        # Doing those first can add driving; hand back stops that no longer fit.
        while len(route) > len(urgent) and _route_minutes(depot, route, s, dist)[0] > s.day_minutes:
            pool.append(route.pop())
        pool.sort(key=_priority_key)
    return route


def _clock(start, minutes):
    h, m = (int(x) for x in start.split(":"))
    t = datetime(2000, 1, 1, h, m) + timedelta(minutes=round(minutes))
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def _finalize(crew, jobs, depot, s, dist):
    route = Route(crew=crew)
    clock = 0.0
    prev = depot
    for i, job in enumerate(jobs, 1):
        km = dist(prev, _pt(job))
        clock += _drive_min(km, s.speed)
        arrive = clock
        clock += _service(job, s) + PER_STOP_OVERHEAD_MIN
        route.stops.append(Stop(job=job, order=i, arrive=_clock(s.start_time, arrive),
                                leave=_clock(s.start_time, clock), drive_km=round(km, 1)))
        route.drive_km += km
        route.work_minutes += _service(job, s) + PER_STOP_OVERHEAD_MIN
        prev = _pt(job)
    route.drive_km = round(route.drive_km, 1)
    route.total_minutes = round(clock)
    route.overloaded = clock > s.day_minutes
    return route


def _next_workday(d, skip_weekends):
    d += timedelta(days=1)
    while skip_weekends and d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def build_plan(jobs, cfg, start_date=None, days=1, **overrides):
    """jobs: list of job dicts (open jobs). Returns a Plan covering up to `days` days."""
    s = Settings(cfg, **overrides)
    dist = _Dist()
    placed = [j for j in jobs if j.get("lat") is not None and j.get("lng") is not None]
    unplaced = [j for j in jobs if j not in placed]
    pool = sorted(placed, key=_priority_key)

    if isinstance(start_date, str) and start_date:
        day = date.fromisoformat(start_date)
    else:
        day = start_date or _next_workday(date.today(), s.skip_weekends)

    plan = Plan(unplaced=unplaced)
    for _ in range(max(1, int(days))):
        if not pool:
            break
        dp = DayPlan(date=day.isoformat())
        for crew in s.crews:
            if not pool:
                break
            if s.mode == "area":
                seed = max(pool, key=lambda j: dist(s.depot, _pt(j)))
            else:
                seed = pool[0]
            route_jobs = _build_route(seed, pool, s.depot, s, dist)
            dp.routes.append(_finalize(crew, route_jobs, s.depot, s, dist))
        plan.days.append(dp)
        day = _next_workday(day, s.skip_weekends)
    plan.leftover = pool
    return plan


def plan_to_dict(plan):
    def job_out(j):
        keys = ("id", "name", "phone", "address", "issue", "urgent", "notes", "last_message",
                "lat", "lng", "geo_quality", "received_at", "status")
        return {k: j.get(k) for k in keys}

    return {
        "days": [{
            "date": d.date,
            "routes": [{
                "crew": r.crew, "area": r.area, "drive_km": r.drive_km,
                "total_minutes": r.total_minutes, "overloaded": r.overloaded,
                "stops": [{"order": st.order, "arrive": st.arrive, "leave": st.leave,
                           "drive_km": st.drive_km, "job": job_out(st.job)} for st in r.stops],
            } for r in d.routes],
        } for d in plan.days],
        "unplaced": [job_out(j) for j in plan.unplaced],
        "leftover": len(getattr(plan, "leftover", [])),
        "scheduled": plan.scheduled_count,
    }
