"""Pull street addresses, towns, names and the type of job out of free text.

Customers text or say their address in every format imaginable:
"11233 35 ave NW", "7535 - 152 B Avenue", "unit 26, 6802, 50 th avenue Camrose",
"5724 Sunview Point Sherwood Park T8H 0J9", "Lot 36, 53305 Range Road 273".
This module turns those into one clean, consistent address string.
"""

import re
from dataclasses import dataclass, field

# Towns around Edmonton we get calls from, with an approximate centre point.
# Keys are the canonical name; values are (lat, lng, [aliases]).
TOWNS = {
    "Edmonton": (53.5461, -113.4938, ["edmonton", "edm", "yeg"]),
    "Sherwood Park": (53.5413, -113.2958, ["sherwood park", "sherwood pk", "strathcona county"]),
    "St. Albert": (53.6305, -113.6256, ["st. albert", "st albert", "saint albert", "st.albert"]),
    "Spruce Grove": (53.5450, -113.9008, ["spruce grove"]),
    "Stony Plain": (53.5264, -114.0026, ["stony plain"]),
    "Leduc": (53.2594, -113.5492, ["leduc"]),
    "Nisku": (53.3370, -113.5310, ["nisku"]),
    "Beaumont": (53.3572, -113.4147, ["beaumont"]),
    "Fort Saskatchewan": (53.7128, -113.2134, ["fort saskatchewan", "fort sask", "ft sask", "ft. sask", "fort sk"]),
    "Devon": (53.3636, -113.7322, ["devon"]),
    "Morinville": (53.8022, -113.6497, ["morinville"]),
    "Gibbons": (53.8283, -113.3264, ["gibbons"]),
    "Bon Accord": (53.8364, -113.4171, ["bon accord"]),
    "Legal": (53.9500, -113.5833, ["legal, ab", "legal alberta", "town of legal"]),
    "Calmar": (53.2650, -113.8186, ["calmar"]),
    "Ardrossan": (53.5560, -113.1440, ["ardrossan"]),
    "Acheson": (53.5500, -113.7700, ["acheson"]),
    "Enoch": (53.5000, -113.8000, ["enoch"]),
    "Villeneuve": (53.6600, -113.8200, ["villeneuve"]),
    "Cardiff": (53.7600, -113.6100, ["cardiff"]),
    "Namao": (53.7100, -113.5000, ["namao"]),
    "Onoway": (53.7032, -114.1980, ["onoway"]),
    "Val Quentin": (53.6900, -114.2900, ["val quentin"]),
    "Wabamun": (53.5600, -114.4700, ["wabamun"]),
    "Thorsby": (53.2300, -114.0500, ["thorsby"]),
    "Millet": (53.0967, -113.4744, ["millet"]),
    "Wetaskiwin": (52.9694, -113.3769, ["wetaskiwin"]),
    "Camrose": (53.0167, -112.8350, ["camrose"]),
    "Tofield": (53.3700, -112.6700, ["tofield"]),
    "Lamont": (53.7600, -112.7800, ["lamont"]),
    "Bruderheim": (53.8000, -112.9300, ["bruderheim"]),
    "Redwater": (53.9500, -113.1100, ["redwater"]),
    "Westlock": (54.1500, -113.8600, ["westlock"]),
    "Drayton Valley": (53.2200, -114.9800, ["drayton valley"]),
}

STREET_TYPES = (
    "street|st|avenue|ave|av|road|rd|drive|dr|crescent|cres|cr|close|cl|way|"
    "boulevard|blvd|court|crt|ct|place|pl|lane|ln|link|wynd|green|grn|point|pt|"
    "cove|trail|trl|tr|park|gate|bay|landing|terrace|ter|heights|hts|view|vista|"
    "circle|cir|square|sq|row|common|commons|mews|manor|grove|ridge|hill|bend|"
    "crossing|run|rise|plaza|walk|loop|vale|glen|gardens|garden|meadow|meadows|"
    "key|harbour|parade|path|wood|woods|estates|haven|hollow|knoll|lookout|bluff|"
    "chase|wharf|place|promenade|pointe"
)

# Words that can't be part of a street name ("at 10 am on the way" isn't an address).
_STOP = (
    "the|a|an|on|in|at|to|for|of|and|or|is|are|am|pm|my|your|our|it|its|this|that|"
    "we|i|you|me|be|so|if|no|yes|with|from|all|more|out|was|will|can|just|get|got|"
    "hours?|mins?|minutes?|days?|weeks?|bundles?|shingles?|squares?|feet|ft|sq|"
    "dollars?|bucks|years?|times?|people|guys|pieces?|boxes|rolls?|tabs?"
)

_QUAD = (
    r"(?:north\s?west|south\s?west|north\s?east|south\s?east|"
    r"n\.?\s?w\.?|s\.?\s?w\.?|n\.?\s?e\.?|s\.?\s?e\.?)(?![a-z])"
)

_ADDRESS_RE = re.compile(
    rf"""
    (?P<unit>(?:(?:unit|suite|apt|apartment|\#)\s*\#?\s*[0-9a-z]{{1,5}}\s*[,\-]?\s*)?)
    (?<![\d.$/:])\b
    (?P<num>\d{{1,6}}[a-z]?)\b
    \s*(?:-\s*)?,?\s*
    (?P<street>
        (?P<numbered>\d{{1,3}})\s?(?:st|nd|rd|th)?\s?(?P<letter>[a-z])?\s+
            (?P<ntype>street|st|avenue|ave|av)\b\.?
      | (?P<rural>range\s+road|rge\.?\s+rd|rng\s+rd|rr|township\s+road|twp\.?\s+rd|twp|highway|hwy)
            \s*(?P<ruralnum>\d{{1,4}}[a-z]?)\b
      | (?P<named>(?:(?!(?:{_STOP})\b)[a-z][a-z'.]*\s+){{1,3}}?)
            (?P<stype>{STREET_TYPES})\b\.?
    )
    (?P<quad>\s*,?\s*{_QUAD})?
    """,
    re.IGNORECASE | re.VERBOSE,
)

POSTAL_RE = re.compile(r"\b([abceghj-nprstvxy]\d[abceghj-nprstv-z])\s?(\d[abceghj-nprstv-z]\d)\b", re.I)

_NAME_RE = re.compile(
    r"(?:[Mm]y name is|[Tt]his is|[Ii]t'?s|[Ii] am|[Ii]'?m|[Nn]ame'?s|[Nn]ame:)\s+"
    r"([A-Z][a-z]+(?:\s+(?:[A-Z][a-z]+|Mac[A-Z][a-z]+|Mc[A-Z][a-z]+))?)"
)
_NAME_BLOCK = {"Kodiak", "Hi", "Hello", "Just", "Wondering", "Calling", "Here", "Sorry", "Not", "Going"}

_TYPE_CANON = {
    "st": "St", "street": "St", "ave": "Ave", "av": "Ave", "avenue": "Ave",
    "rd": "Rd", "road": "Rd", "dr": "Dr", "drive": "Dr", "cres": "Cres", "crescent": "Cres",
    "cr": "Cres", "cl": "Close", "close": "Close", "blvd": "Blvd", "boulevard": "Blvd",
    "ct": "Crt", "crt": "Crt", "court": "Crt", "pl": "Pl", "place": "Pl", "ln": "Lane",
    "lane": "Lane", "pt": "Point", "point": "Point", "grn": "Green", "tr": "Trail",
    "trl": "Trail", "trail": "Trail", "ter": "Terrace", "hts": "Heights", "cir": "Circle",
    "sq": "Square",
}

_QUAD_CANON = {
    "northwest": "NW", "southwest": "SW", "northeast": "NE", "southeast": "SE",
}


@dataclass
class Address:
    text: str                 # clean one-line address, e.g. "11233 35 Ave NW, Edmonton, AB"
    house_number: str
    street: str
    quadrant: str = ""
    unit: str = ""
    town: str = ""
    postal: str = ""
    numbered_street: int | None = None   # 35 in "35 Ave"
    numbered_kind: str = ""              # "Ave" or "St"
    rural: bool = False
    span: tuple = field(default=(0, 0), repr=False)


def find_town(text):
    """Return the canonical town named in text, or ''."""
    low = " " + re.sub(r"\s+", " ", text.lower()) + " "
    best, best_pos = "", None
    for town, (_, _, aliases) in TOWNS.items():
        for alias in aliases:
            m = re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", low)
            if m and (best_pos is None or m.start() < best_pos):
                best, best_pos = town, m.start()
    return best


def find_postal(text):
    m = POSTAL_RE.search(text)
    return f"{m.group(1).upper()} {m.group(2).upper()}" if m else ""


def _canon_quad(raw):
    q = re.sub(r"[\s.,]", "", raw or "").lower()
    return _QUAD_CANON.get(q, q.upper())


def _title(word):
    if word.lower().startswith("mc") and len(word) > 2:
        return "Mc" + word[2:].capitalize()
    return word.capitalize()


def extract_addresses(text):
    """Return every address found in text, in order of appearance."""
    if not text:
        return []
    results = []
    for m in _ADDRESS_RE.finditer(text):
        num = m.group("num").upper()
        unit = re.sub(r"(?i)^(unit|suite|apt|apartment|#)\s*#?\s*", "", m.group("unit").strip(" ,-")).upper()
        quad = _canon_quad(m.group("quad"))
        numbered = None
        kind = ""
        rural = False
        if m.group("numbered"):
            numbered = int(m.group("numbered"))
            kind = _TYPE_CANON[m.group("ntype").lower()]
            letter = (m.group("letter") or "").upper()
            street = f"{numbered}{letter} {kind}"
            if len(num) > 6:
                continue
        elif m.group("rural"):
            rural = True
            r = m.group("rural").lower()
            label = "Range Rd" if r.startswith(("range", "rge", "rng", "rr")) else (
                "Township Rd" if r.startswith(("town", "twp")) else "Hwy")
            street = f"{label} {m.group('ruralnum').upper()}"
        else:
            words = m.group("named").split()
            stype = m.group("stype")
            street = " ".join(_title(w.strip(".")) for w in words) + " " + _TYPE_CANON.get(
                stype.lower(), stype.capitalize())
        # Look a little past the street for the town and postal code.
        tail = text[m.end(): m.end() + 70]
        tail = re.split(r"[\n!?]", tail)[0]
        town = find_town(tail)
        postal = find_postal(tail)
        if not town and postal:
            town = _town_from_postal(postal)
        # Edmonton's numbered streets are almost all NW; default it when unsaid.
        if numbered is not None and not quad and town in ("", "Edmonton"):
            quad = "NW"
        parts = []
        line = f"{num} {street}"
        if quad:
            line += f" {quad}"
        if unit:
            line = f"Unit {unit}, {line}"
        parts.append(line)
        parts.append(town or "Edmonton")
        parts.append("AB" + (f" {postal}" if postal else ""))
        results.append(Address(
            text=", ".join(parts), house_number=num, street=street, quadrant=quad,
            unit=unit, town=town or "", postal=postal, numbered_street=numbered,
            numbered_kind=kind, rural=rural, span=m.span(),
        ))
    return results


def extract_address(text):
    """Best single address in text (the first one), or None."""
    found = extract_addresses(text)
    return found[0] if found else None


def _town_from_postal(postal):
    fsa = postal[:3].upper()
    if fsa.startswith(("T5", "T6")):
        return "Edmonton"
    return {
        "T8A": "Sherwood Park", "T8B": "Sherwood Park", "T8C": "Sherwood Park",
        "T8E": "Sherwood Park", "T8G": "Sherwood Park", "T8H": "Sherwood Park",
        "T8N": "St. Albert", "T8T": "St. Albert", "T7X": "Spruce Grove", "T7Y": "Spruce Grove",
        "T7Z": "Stony Plain", "T9E": "Leduc", "T4X": "Beaumont", "T8L": "Fort Saskatchewan",
        "T9G": "Devon", "T8R": "Morinville", "T4V": "Camrose", "T9A": "Wetaskiwin",
    }.get(fsa, "")


def find_name(text):
    """A caller's name if they introduce themselves ("My name is Dave"), else ''."""
    for m in _NAME_RE.finditer(text or ""):
        name = m.group(1)
        if name.split()[0] not in _NAME_BLOCK:
            return name
    return ""


# Job type detection --------------------------------------------------------

_ISSUES = [
    ("Leak", r"leak|leaking|water (?:coming|is coming|dripping|in)|dripping|drips|ceiling stain|water damage"),
    ("Shingle blow-off", r"blow[n\s-]*off|blew off|blown|missing shingles?|shingles? (?:off|missing|gone|flew|came off|ripped)|wind (?:damage|storm)|torn shingles?|lifted"),
    ("Hail damage", r"\bhail"),
    ("Flashing / vents", r"flashing|vent|chimney|skylight|ridge cap"),
    ("Eaves / gutters", r"eaves|eavestrough|gutter|downspout|soffit|fascia"),
    ("Siding", r"siding"),
]
_URGENT = re.compile(
    r"leak|leaking|water (?:coming|is coming|dripping|in)|dripping|tarp|urgent|asap|emergency|"
    r"exposed|open to|rain (?:coming|tonight|tomorrow)|insurance adjuster",
    re.I,
)


def classify_issue(text):
    """Return (issue_label, urgent_bool) for a chunk of customer text."""
    text = text or ""
    label = ""
    for name, pattern in _ISSUES:
        if re.search(pattern, text, re.I):
            label = name
            break
    return label or "Roof repair", bool(_URGENT.search(text))
