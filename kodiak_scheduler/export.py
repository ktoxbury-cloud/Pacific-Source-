"""Write crew sheets: a spreadsheet (CSV) and a printable page with one crew per sheet."""

import csv
import html
import os
import urllib.parse

from .store import now_iso


def pretty_phone(phone):
    p = phone or ""
    if len(p) == 12 and p.startswith("+1"):
        return f"({p[2:5]}) {p[5:8]}-{p[8:]}"
    return p


def maps_link(address):
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(address)


def route_links(depot_address, addresses, chunk=9):
    """Google Maps turn-by-turn links (Maps allows ~9 stops per link, so long days get split)."""
    links = []
    origin = depot_address
    for i in range(0, len(addresses), chunk):
        part = addresses[i:i + chunk]
        params = {"api": "1", "origin": origin, "destination": part[-1], "travelmode": "driving"}
        if len(part) > 1:
            params["waypoints"] = "|".join(part[:-1])
        links.append("https://www.google.com/maps/dir/?" + urllib.parse.urlencode(params))
        origin = part[-1]
    return links


CSV_COLUMNS = ["Date", "Crew", "Stop", "Arrive", "Customer", "Phone", "Address", "Job",
               "Urgent", "Notes", "Map", "Job ID"]


def _notes(job):
    return (job.get("notes") or job.get("last_message") or "").replace("\n", " ").strip()


def write_day(plan_day, depot_address, out_dir):
    """Write schedule.csv and schedule.html for one day. Returns the file paths."""
    folder = os.path.join(out_dir, plan_day.date)
    os.makedirs(folder, exist_ok=True)
    csv_path = os.path.join(folder, f"schedule-{plan_day.date}.csv")
    html_path = os.path.join(folder, f"schedule-{plan_day.date}.html")

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for r in plan_day.routes:
            for st in r.stops:
                j = st.job
                w.writerow([plan_day.date, r.crew, st.order, st.arrive, j.get("name") or "",
                            pretty_phone(j.get("phone")), j.get("address") or "", j.get("issue") or "",
                            "YES" if j.get("urgent") else "", _notes(j), maps_link(j["address"]),
                            j["id"]])

    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(render_html(plan_day, depot_address))
    return [csv_path, html_path]


def render_html(plan_day, depot_address):
    e = html.escape
    pages = []
    for r in plan_day.routes:
        links = route_links(depot_address, [st.job["address"] for st in r.stops])
        link_html = " ".join(
            f'<a href="{e(u)}">Route{(" part " + str(i + 1)) if len(links) > 1 else ""}</a>'
            for i, u in enumerate(links))
        rows = []
        for st in r.stops:
            j = st.job
            phone = j.get("phone") or ""
            rows.append(f"""
            <tr>
              <td class="n">{st.order}</td>
              <td class="t">{e(st.arrive)}</td>
              <td><b>{e(j.get('address') or '')}</b><br>
                  <a href="{e(maps_link(j['address']))}">map</a>
                  {'<span class="urgent">URGENT</span>' if j.get('urgent') else ''}</td>
              <td>{e(j.get('name') or '')}<br>
                  {f'<a href="tel:{e(phone)}">{e(pretty_phone(phone))}</a>' if phone else ''}</td>
              <td>{e(j.get('issue') or '')}<div class="note">{e(_notes(j)[:220])}</div></td>
              <td class="box">&#9744;</td>
            </tr>""")
        hours = r.total_minutes / 60
        pages.append(f"""
        <section class="crew">
          <header>
            <h1>{e(r.crew)} &middot; {e(plan_day.date)}</h1>
            <p>{len(r.stops)} stops &middot; {e(r.area)} &middot; ~{r.drive_km:.0f} km driving &middot;
               ~{hours:.1f} h day{' &middot; <b>LONG DAY</b>' if r.overloaded else ''}
               &middot; {link_html}</p>
          </header>
          <table>
            <thead><tr><th>#</th><th>ETA</th><th>Address</th><th>Customer</th><th>Job / notes</th><th>Done</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
          </table>
        </section>""")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Repair Schedule {e(plan_day.date)}</title>
<style>
  body {{ font: 14px/1.35 -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 16px; color: #111; background: #fff; }}
  .crew {{ page-break-after: always; margin-bottom: 32px; }}
  .crew:last-child {{ page-break-after: auto; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  header p {{ margin: 0 0 10px; color: #444; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ border: 1px solid #bbb; padding: 6px 8px; vertical-align: top; text-align: left; }}
  th {{ background: #eee; }}
  td.n {{ width: 24px; text-align: center; font-weight: bold; }}
  td.t {{ width: 70px; white-space: nowrap; }}
  td.box {{ width: 40px; text-align: center; font-size: 22px; }}
  .note {{ color: #555; font-size: 12px; margin-top: 3px; }}
  .urgent {{ background: #c00; color: #fff; font-size: 11px; padding: 1px 5px; border-radius: 3px; margin-left: 6px; }}
  a {{ color: #0645ad; }}
  @media print {{ a {{ color: #111; text-decoration: none; }} body {{ margin: 0; }} }}
</style></head>
<body>{''.join(pages)}</body></html>"""


def mark_scheduled(store, plan_day):
    for r in plan_day.routes:
        for st in r.stops:
            store.update_job(st.job["id"], status="scheduled", scheduled_date=plan_day.date,
                             crew=r.crew, stop_order=st.order)
    store.meta_set("last_export", {"date": plan_day.date, "at": now_iso()})


def roll_over(store, before_date=None):
    """Put scheduled-but-not-done jobs back in the queue (keeps their original call date)."""
    n = 0
    for j in store.jobs(["scheduled"]):
        if before_date is None or (j["scheduled_date"] or "") <= before_date:
            store.update_job(j["id"], status="new", scheduled_date="", crew="", stop_order=None)
            n += 1
    return n
