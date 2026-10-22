"""Run with:  python -m unittest discover tests"""

import json
import os
import tempfile
import unittest
import urllib.parse

from kodiak_scheduler import config as config_mod
from kodiak_scheduler.export import route_links, write_day
from kodiak_scheduler.extract import classify_issue, extract_address, find_name
from kodiak_scheduler.geo import Geocoder, grid_point, haversine_km
from kodiak_scheduler.intake import Intake, normalize_phone
from kodiak_scheduler.planner import build_plan
from kodiak_scheduler.quo import QuoClient, sync
from kodiak_scheduler.store import Store


def addr(text):
    a = extract_address(text)
    return a.text if a else None


class ExtractTests(unittest.TestCase):
    def test_numbered_grid_formats(self):
        self.assertEqual(addr("11250 35 ave NW. I will send a photo"), "11250 35 Ave NW, Edmonton, AB")
        self.assertEqual(addr("7520 - 152 B Avenue"), "7520 152B Ave NW, Edmonton, AB")
        self.assertEqual(addr("so 14211 24A st"), "14211 24A St NW, Edmonton, AB")
        self.assertEqual(addr("My address is 17710 10th Ave. SW. I just called"), "17710 10 Ave SW, Edmonton, AB")
        self.assertEqual(addr("10420 63A Street NW - hey"), "10420 63A St NW, Edmonton, AB")

    def test_named_streets_and_towns(self):
        self.assertEqual(addr("188 McDowell Wynd. Leduc."), "188 McDowell Wynd, Leduc, AB")
        self.assertEqual(addr("22 Chambery Crescent, St. albert"), "22 Chambery Cres, St. Albert, AB")
        self.assertEqual(addr("Address is 5730 Sunview Point Sherwood Park T8H 0J9."),
                         "5730 Sunview Point, Sherwood Park, AB T8H 0J9")
        self.assertEqual(addr("1044 West Mount Crescent Northwest. Edmonton, Alberta"),
                         "1044 West Mount Cres NW, Edmonton, AB")
        self.assertEqual(addr("Address - unit 12 , 6810, 50 th avenue Camrose T4V5C7"),
                         "Unit 12, 6810 50 Ave, Camrose, AB T4V 5C7")

    def test_rural(self):
        self.assertEqual(addr("Lot 12, 53210 Range Road 263, Spruce Grove."), "53210 Range Rd 263, Spruce Grove, AB")

    def test_not_addresses(self):
        for text in ("Yes, 442.31 for tidan heights", "I will be there at 10 am on the way",
                     "Need 3 bundles of shingles and 2 rolls", "fit in #18 at some point today",
                     "IKO Mystique 42 Brownstone"):
            self.assertIsNone(addr(text), text)

    def test_name_and_issue(self):
        text = "Hi. My name is Sophia from 3120 Montrose Blvd. shingles blew off and now it's leaking"
        self.assertEqual(find_name(text), "Sophia")
        self.assertEqual(classify_issue(text), ("Leak", True))
        self.assertEqual(classify_issue("wind took a bunch of shingles off the back"), ("Shingle blow-off", False))

    def test_phone(self):
        self.assertEqual(normalize_phone("(780) 555-1524"), "+17805551524")
        self.assertEqual(normalize_phone("+1 587 555 0000"), "+15875550000")


class GeoTests(unittest.TestCase):
    def test_grid_is_in_the_right_place(self):
        # 10234 82 Ave NW (Whyte Ave near 102 St) is ~1.5 km south-west of downtown.
        pt = grid_point(extract_address("10234 82 Ave NW"))
        self.assertLess(haversine_km(pt, (53.5185, -113.4975)), 0.6)
        # 17504 87 Ave NW is near West Edmonton Mall.
        pt = grid_point(extract_address("17504 87 Ave NW"))
        self.assertLess(haversine_km(pt, (53.5225, -113.6270)), 1.0)
        # Mill Woods: 2804 66 St NW
        pt = grid_point(extract_address("2804 66 St NW"))
        self.assertLess(haversine_km(pt, (53.4590, -113.4380)), 1.5)

    def test_offline_fallbacks(self):
        with tempfile.TemporaryDirectory() as d:
            geo = Geocoder(Store(os.path.join(d, "t.db")), online=False)
            self.assertEqual(geo.locate("11250 35 Ave NW")["quality"], "grid")
            self.assertEqual(geo.locate("88 Somewhere Close, Leduc")["quality"], "town")
            self.assertEqual(geo.locate("12 Fake Way NW T6R 1A1")["quality"], "postal")


def _store(d):
    store = Store(os.path.join(d, "t.db"))
    config_mod.save(store, {"online_geocoding": False})
    return store, Intake(store, Geocoder(store, online=False))


class PlannerTests(unittest.TestCase):
    def test_groups_by_area_and_respects_limits(self):
        with tempfile.TemporaryDirectory() as d:
            store, intake = _store(d)
            north = ["13410 127 St NW", "13520 128 St NW", "13305 125 St NW", "13650 130 St NW"]
            south = ["2510 66 St NW", "2630 67 St NW", "2440 64 St NW", "2715 68 St NW"]
            west = ["8710 178 St NW", "8805 180 St NW", "8620 176 St NW", "8930 182 St NW"]
            for a in north + south + west:
                intake.add_manual(address=a)
            cfg = config_mod.load(store)
            plan = build_plan(store.jobs(["new"]), cfg, start_date="2026-11-02", crews=3,
                              max_stops_per_crew=4)
            self.assertEqual(plan.scheduled_count, 12)
            groups = [{s.job["address"].split(",")[0] for s in r.stops} for r in plan.days[0].routes]
            for g in groups:
                self.assertIn(g, [set(north), set(south), set(west)])

    def test_urgent_first_and_overflow_days(self):
        with tempfile.TemporaryDirectory() as d:
            store, intake = _store(d)
            for i in range(10):
                intake.add_manual(address=f"{100 + i}20 {80 + i} Ave NW")
            intake.add_manual(address="10950 88 Ave NW", notes="water leaking into the bedroom")
            cfg = config_mod.load(store)
            plan = build_plan(store.jobs(["new"]), cfg, start_date="2026-11-02", days=3,
                              crews=["Sloane"], max_stops_per_crew=4)
            self.assertEqual(len(plan.days), 3)
            self.assertTrue(plan.days[0].routes[0].stops[0].job["urgent"])
            self.assertEqual(plan.days[1].date, "2026-11-03")

    def test_export_files(self):
        with tempfile.TemporaryDirectory() as d:
            store, intake = _store(d)
            for a in ("11250 35 Ave NW", "11330 36 Ave NW"):
                intake.add_manual(address=a, name="Test", phone="7805550000")
            plan = build_plan(store.jobs(["new"]), config_mod.load(store), start_date="2026-11-02")
            files = write_day(plan.days[0], "Edmonton, AB", os.path.join(d, "exports"))
            with open(files[0], encoding="utf-8-sig") as fh:
                rows = fh.read().splitlines()
            self.assertEqual(len(rows), 3)
            self.assertIn("11250 35 Ave NW", "\n".join(rows))
            with open(files[1], encoding="utf-8") as fh:
                self.assertIn("google.com/maps/dir", fh.read())
        self.assertEqual(len(route_links("A", [str(i) for i in range(12)])), 2)


class FakeQuo:
    """Mimics the Quo REST API responses the sync uses."""

    def __init__(self):
        self.calls_made = []

    def __call__(self, url):
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.replace("/v1", "", 1)
        q = urllib.parse.parse_qs(parsed.query)
        self.calls_made.append(path)
        if path == "/phone-numbers":
            return 200, {"data": [{"id": "PNpaint", "number": "+15870000001", "name": "Kodiak Painting"},
                                  {"id": "PNroof", "number": "+15870000002", "name": "Kodiak Roofing"}]}
        if path == "/contacts":
            return 200, {"data": [{"defaultFields": {"firstName": "Dana", "lastName": "Example",
                                                     "phoneNumbers": [{"value": "+17805550102"}]},
                                   "customFields": []}]}
        if path == "/conversations":
            assert q["phoneNumbers"] == ["PNroof"]
            return 200, {"data": [
                {"id": "CN1", "participants": ["+17805550101"]},
                {"id": "CN2", "participants": ["+17805550102"]},
                {"id": "CN3", "participants": ["+17805550199"]},   # crew member — ignored
                {"id": "CN4", "participants": ["+17805550103"]},
            ]}
        if path == "/messages":
            who = q["participants"][0]
            msgs = {
                "+17805550101": [{"id": "AC1", "direction": "incoming", "createdAt": "2026-10-22T15:00:00Z",
                                  "text": "Hi my name is Robin, wind blew shingles off. 11250 35 ave NW"}],
                "+17805550199": [{"id": "AC9", "direction": "incoming", "createdAt": "2026-10-22T15:00:00Z",
                                  "text": "at 10420 63A Street NW now"}],
                "+17805550103": [{"id": "AC5", "direction": "incoming", "createdAt": "2026-10-22T18:00:00Z",
                                  "text": "Can someone look at my roof?"}],
            }
            return 200, {"data": msgs.get(who, [])}
        if path == "/calls":
            if q["participants"][0] == "+17805550102":
                return 200, {"data": [{"id": "ACcall", "direction": "incoming", "status": "missed",
                                       "createdAt": "2026-10-22T16:00:00Z", "duration": 0}]}
            return 200, {"data": []}
        if path == "/call-voicemails/ACcall":
            return 200, {"data": {"transcript": "Hi, water is leaking through my ceiling at 22 Chambery Crescent St. Albert"}}
        return 404, {"message": "not found"}


class QuoSyncTests(unittest.TestCase):
    def test_sync_creates_jobs_and_is_incremental(self):
        with tempfile.TemporaryDirectory() as d:
            store, intake = _store(d)
            cfg = config_mod.save(store, {"quo_api_key": "test", "ignore_numbers": ["780-555-0199"]})
            fake = FakeQuo()
            client = QuoClient("test", transport=fake, pause=0)
            result = sync(store, intake, cfg, client=client, log=lambda *_: None)
            self.assertEqual(result["created"], 3)
            jobs = {j["phone"]: j for j in store.jobs(["new", "needs_address"])}
            self.assertEqual(jobs["+17805550101"]["address"], "11250 35 Ave NW, Edmonton, AB")
            self.assertEqual(jobs["+17805550101"]["name"], "Robin")
            self.assertEqual(jobs["+17805550101"]["issue"], "Shingle blow-off")
            self.assertEqual(jobs["+17805550102"]["address"], "22 Chambery Cres, St. Albert, AB")
            self.assertEqual(jobs["+17805550102"]["name"], "Dana Example")
            self.assertTrue(jobs["+17805550102"]["urgent"])
            self.assertEqual(jobs["+17805550103"]["status"], "needs_address")
            self.assertNotIn("+17805550199", jobs)

            again = sync(store, intake, cfg, client=client, log=lambda *_: None)
            self.assertEqual(again["created"] + again["updated"], 0)
            self.assertEqual(len(store.jobs(["new", "needs_address"])), 3)


if __name__ == "__main__":
    unittest.main()
