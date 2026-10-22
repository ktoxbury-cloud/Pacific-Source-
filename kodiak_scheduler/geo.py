"""Turn addresses into map points.

Order of attempts (first that works wins, results are cached in the database):
  1. Google Geocoding API, if GOOGLE_MAPS_API_KEY is set (most accurate, paid).
  2. OpenStreetMap Nominatim (free, ~1 lookup per second).
  3. Edmonton's numbered grid: "10234 82 Ave NW" is between 102 and 103 St on
     82 Ave, so we can place it without the internet (within a block or two).
  4. Postal code area centre (T6R, T5X, ...).
  5. Town centre (Leduc, Spruce Grove, ...).
"""

import json
import math
import os
import time
import urllib.parse
import urllib.request

from .extract import TOWNS, extract_address

USER_AGENT = "KodiakRoofingScheduler/1.0 (repair scheduling)"

# Rough box around the area we serve; anything an online geocoder returns
# outside of it is a wrong match (same street name in Ontario, etc.).
REGION = {"lat_min": 52.6, "lat_max": 54.6, "lng_min": -115.6, "lng_max": -112.2}

# Edmonton grid calibration points: avenue number -> latitude (north of Quadrant Ave),
# street number -> longitude (west of Quadrant St). Interpolated between points.
_AVE_LAT = [(-41, 53.3955), (-9, 53.4205), (23, 53.4553), (51, 53.4848), (82, 53.5185),
            (87, 53.5230), (101, 53.5413), (107, 53.5505), (118, 53.5705), (137, 53.5975),
            (153, 53.6185), (167, 53.6345), (195, 53.6640)]
_ST_LNG = [(0, -113.3380), (17, -113.3640), (34, -113.3905), (50, -113.4140), (66, -113.4380),
           (82, -113.4620), (97, -113.4885), (101, -113.4938), (109, -113.5110), (124, -113.5390),
           (142, -113.5650), (156, -113.5900), (170, -113.6240), (184, -113.6505), (199, -113.6800),
           (215, -113.7120), (231, -113.7410)]
_NE_STREET_DEG = 0.0016  # streets east of Quadrant St (NE / SE)

# Postal code area (first 3 characters) -> approximate centre.
FSA = {
    "T5A": (53.590, -113.410), "T5B": (53.570, -113.450), "T5C": (53.605, -113.470),
    "T5E": (53.590, -113.500), "T5G": (53.565, -113.500), "T5H": (53.550, -113.480),
    "T5J": (53.543, -113.490), "T5K": (53.540, -113.515), "T5L": (53.570, -113.540),
    "T5M": (53.555, -113.555), "T5N": (53.540, -113.560), "T5P": (53.540, -113.600),
    "T5R": (53.520, -113.590), "T5S": (53.545, -113.670), "T5T": (53.510, -113.640),
    "T5V": (53.585, -113.590), "T5W": (53.580, -113.420), "T5X": (53.620, -113.520),
    "T5Y": (53.620, -113.420), "T5Z": (53.630, -113.450), "T6A": (53.540, -113.420),
    "T6B": (53.520, -113.390), "T6C": (53.520, -113.460), "T6E": (53.505, -113.480),
    "T6G": (53.520, -113.520), "T6H": (53.480, -113.520), "T6J": (53.460, -113.520),
    "T6K": (53.460, -113.450), "T6L": (53.450, -113.420), "T6M": (53.490, -113.660),
    "T6N": (53.440, -113.490), "T6P": (53.550, -113.370), "T6R": (53.460, -113.580),
    "T6S": (53.500, -113.360), "T6T": (53.460, -113.380), "T6V": (53.640, -113.560),
    "T6W": (53.420, -113.560), "T6X": (53.420, -113.470),
    "T8A": (53.540, -113.300), "T8B": (53.530, -113.280), "T8C": (53.560, -113.200),
    "T8E": (53.570, -113.150), "T8G": (53.520, -113.210), "T8H": (53.550, -113.310),
    "T8N": (53.635, -113.620), "T8T": (53.650, -113.600), "T7X": (53.545, -113.910),
    "T7Y": (53.540, -113.880), "T7Z": (53.525, -114.000), "T9E": (53.265, -113.550),
    "T4X": (53.357, -113.415), "T8L": (53.712, -113.213), "T9G": (53.364, -113.732),
    "T8R": (53.802, -113.650), "T4V": (53.017, -112.835), "T9A": (52.969, -113.377),
}

# How good a point is, best first. Shown in the app so you know which pins to double-check.
QUALITY_RANK = {"exact": 0, "street": 1, "grid": 2, "manual": 0, "postal": 3, "town": 4, "": 9}


def haversine_km(a, b):
    lat1, lng1 = a
    lat2, lng2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _interp(table, x):
    if x <= table[0][0]:
        (x0, y0), (x1, y1) = table[0], table[1]
    elif x >= table[-1][0]:
        (x0, y0), (x1, y1) = table[-2], table[-1]
    else:
        for (x0, y0), (x1, y1) in zip(table, table[1:]):
            if x0 <= x <= x1:
                break
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def _house_to_cross(num):
    """10234 -> 102.34 (the cross street/avenue the house sits on)."""
    digits = "".join(ch for ch in num if ch.isdigit())
    if len(digits) < 3:
        return None
    return int(digits) / 100.0


def grid_point(addr):
    """Place an Edmonton numbered-grid address without the internet."""
    if addr is None or addr.numbered_street is None or addr.town not in ("", "Edmonton"):
        return None
    cross = _house_to_cross(addr.house_number)
    if cross is None:
        return None
    quad = addr.quadrant or "NW"
    south = quad in ("SW", "SE")
    east = quad in ("NE", "SE")
    if addr.numbered_kind == "Ave":
        ave, street = addr.numbered_street, cross
    else:
        street, ave = addr.numbered_street, cross
    if south:
        ave = -ave
    lat = _interp(_AVE_LAT, ave)
    if east:
        lng = _ST_LNG[0][1] + street * _NE_STREET_DEG
    else:
        lng = _interp(_ST_LNG, street)
    return lat, lng


def _in_region(lat, lng):
    return REGION["lat_min"] <= lat <= REGION["lat_max"] and REGION["lng_min"] <= lng <= REGION["lng_max"]


def _http_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


_last_nominatim = [0.0]


def nominatim(query):
    """Free OpenStreetMap lookup. Returns (lat, lng, quality, display) or None."""
    wait = 1.1 - (time.time() - _last_nominatim[0])
    if wait > 0:
        time.sleep(wait)
    _last_nominatim[0] = time.time()
    params = {
        "q": query, "format": "jsonv2", "limit": 1, "countrycodes": "ca", "addressdetails": 1,
        "viewbox": "-114.6,54.0,-112.7,53.1",
    }
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
    data = _http_json(url)
    if not data:
        return None
    hit = data[0]
    lat, lng = float(hit["lat"]), float(hit["lon"])
    if not _in_region(lat, lng):
        return None
    quality = "exact" if (hit.get("address") or {}).get("house_number") else "street"
    if hit.get("addresstype") in ("city", "town", "village", "municipality", "county", "state"):
        quality = "town"
    return lat, lng, quality, hit.get("display_name", "")


def google(query, key):
    params = {"address": query, "key": key, "region": "ca",
              "bounds": "53.1,-114.6|54.0,-112.7"}
    url = "https://maps.googleapis.com/maps/api/geocode/json?" + urllib.parse.urlencode(params)
    data = _http_json(url)
    if data.get("status") != "OK" or not data.get("results"):
        return None
    hit = data["results"][0]
    loc = hit["geometry"]["location"]
    lat, lng = float(loc["lat"]), float(loc["lng"])
    if not _in_region(lat, lng):
        return None
    ltype = hit["geometry"].get("location_type", "")
    quality = "exact" if ltype in ("ROOFTOP", "RANGE_INTERPOLATED") else (
        "street" if "route" in hit.get("types", []) or ltype == "GEOMETRIC_CENTER" else "town")
    return lat, lng, quality, hit.get("formatted_address", "")


def _query_for(address_text):
    q = address_text
    if "alberta" not in q.lower() and ", ab" not in q.lower():
        q += ", Alberta"
    return q + ", Canada"


class Geocoder:
    def __init__(self, store, online=True, google_key=None):
        self.store = store
        self.online = online
        self.google_key = google_key if google_key is not None else os.environ.get("GOOGLE_MAPS_API_KEY")
        self.last_error = ""

    def locate(self, address_text):
        """Return dict(lat, lng, quality) or None if nothing could place it."""
        if not address_text or not address_text.strip():
            return None
        key = address_text.strip().lower()
        cached = self.store.geocache_get(key)
        if cached and (cached["quality"] in ("exact", "street") or not self.online):
            return cached

        parsed = extract_address(address_text)
        result = None
        if self.online:
            query = _query_for(parsed.text if parsed else address_text)
            for fn in self._online_sources():
                try:
                    hit = fn(query)
                except Exception as exc:  # network down, rate limited, etc.
                    self.last_error = f"{fn.__name__ if hasattr(fn, '__name__') else 'geocoder'}: {exc}"
                    hit = None
                if hit and hit[2] in ("exact", "street"):
                    result = {"lat": hit[0], "lng": hit[1], "quality": hit[2]}
                    break
        if result is None:
            result = self._offline(address_text, parsed)
        if result is None and cached:
            return cached
        if result:
            self.store.geocache_put(key, result["lat"], result["lng"], result["quality"])
        return result

    def _online_sources(self):
        sources = []
        if self.google_key:
            sources.append(lambda q: google(q, self.google_key))
        sources.append(nominatim)
        return sources

    @staticmethod
    def _offline(address_text, parsed):
        pt = grid_point(parsed)
        if pt:
            return {"lat": pt[0], "lng": pt[1], "quality": "grid"}
        from .extract import find_postal, find_town
        postal = (parsed.postal if parsed else "") or find_postal(address_text)
        if postal and postal[:3] in FSA:
            lat, lng = FSA[postal[:3]]
            return {"lat": lat, "lng": lng, "quality": "postal"}
        town = (parsed.town if parsed else "") or find_town(address_text)
        if town:
            lat, lng, _ = TOWNS[town]
            return {"lat": lat, "lng": lng, "quality": "town"}
        return None
