"""Shared logic for adding jobs, whether they come from Quo, a paste, a CSV or the form."""

import csv
import io
import re
from datetime import datetime, timedelta, timezone

from .extract import classify_issue, extract_address, find_name
from .store import now_iso


def normalize_phone(raw):
    """Any North American phone format -> +1XXXXXXXXXX ('' if it isn't one)."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return ("+" + digits) if raw and raw.strip().startswith("+") and digits else ""


_PHONE_IN_TEXT = re.compile(r"(?:\+?1[\s.-]?)?\(?\b(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})\b")


class Intake:
    def __init__(self, store, geocoder):
        self.store = store
        self.geocoder = geocoder

    def _locate(self, address):
        loc = self.geocoder.locate(address) if address else None
        if not loc:
            return {"lat": None, "lng": None, "geo_quality": ""}
        return {"lat": loc["lat"], "lng": loc["lng"], "geo_quality": loc["quality"]}

    def add_manual(self, address="", name="", phone="", notes="", urgent=None, issue="",
                   text="", service_minutes=None, source="manual"):
        """Add one job from the form/CLI. `text` can be a whole pasted message."""
        blob = " ".join(x for x in (address, notes, text) if x)
        parsed = extract_address(address) or extract_address(text)
        clean = parsed.text if parsed else address.strip()
        guess_issue, guess_urgent = classify_issue(blob)
        if not phone:
            m = _PHONE_IN_TEXT.search(text or "")
            phone = "".join(m.groups()) if m else ""
        fields = {
            "address": clean,
            "address_locked": 1 if address else 0,
            "name": name or find_name(text),
            "phone": normalize_phone(phone),
            "notes": notes or (text if text and text != address else ""),
            "issue": issue or guess_issue,
            "urgent": int(guess_urgent if urgent is None else bool(urgent)),
            "source": source,
            "service_minutes": service_minutes,
        }
        fields.update(self._locate(clean))
        fields["status"] = "new" if clean else "needs_address"
        return self.store.add_job(**fields)

    def add_lines(self, text):
        """Bulk paste: one job per non-empty line."""
        ids = []
        for line in (text or "").splitlines():
            line = line.strip(" \t-•*")
            if line:
                ids.append(self.add_manual(text=line, address=line if extract_address(line) else ""))
        return ids

    def import_csv(self, data):
        """CSV with columns like Address / Name / Phone / Notes (any capitalisation)."""
        reader = csv.DictReader(io.StringIO(data))
        ids = []
        for row in reader:
            low = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}

            def pick(*keys):
                for k in low:
                    if any(key in k for key in keys) and low[k]:
                        return low[k]
                return ""

            address = pick("address", "street", "location")
            city = pick("city", "town")
            if address and city and city.lower() not in address.lower():
                address = f"{address}, {city}"
            first, last = pick("first"), pick("last")
            name = pick("name") if not (first or last) else f"{first} {last}".strip()
            ids.append(self.add_manual(
                address=address, name=name, phone=pick("phone", "mobile", "cell", "number"),
                notes=pick("note", "description", "issue", "message", "comment"),
            ))
        return ids

    def edit_address(self, job_id, address):
        parsed = extract_address(address)
        clean = parsed.text if parsed else address.strip()
        fields = {"address": clean, "address_locked": 1}
        fields.update(self._locate(clean))
        job = self.store.get_job(job_id)
        if job and job["status"] == "needs_address" and clean:
            fields["status"] = "new"
        self.store.update_job(job_id, **fields)

    def from_conversation(self, phone, texts, received_at, source_ref="", contact_name="",
                          contact_address=""):
        """Create or update the job for a caller from their messages / voicemails / call notes.

        texts: list of strings, most important first (customer's own words before ours).
        Returns ('created'|'updated'|'skipped', job_id).
        """
        phone = normalize_phone(phone) or phone
        blob = "\n".join(t for t in texts if t)
        parsed = extract_address(contact_address) if contact_address else None
        if parsed is None:
            for t in texts:
                parsed = extract_address(t)
                if parsed:
                    break
        issue, urgent = classify_issue(blob)
        name = contact_name or find_name(blob)
        last = next((t for t in texts if t), "")[:500]

        job = self.store.open_job_for_phone(phone)
        if job is None:
            # Finished in the last 3 days? That's a thank-you text, not a new job.
            since = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
            if self.store.recent_closed_job_for_phone(phone, since):
                return "skipped", None
            fields = {
                "phone": phone, "name": name, "issue": issue, "urgent": int(urgent),
                "source": "quo", "source_ref": source_ref, "received_at": received_at or now_iso(),
                "last_message": last, "notes": "",
            }
            if parsed:
                fields["address"] = parsed.text
                fields.update(self._locate(parsed.text))
            fields["status"] = "new" if parsed else "needs_address"
            return "created", self.store.add_job(**fields)

        updates = {"last_message": last or job["last_message"]}
        if name and not job["name"]:
            updates["name"] = name
        if urgent and not job["urgent"]:
            updates["urgent"] = 1
        if issue != "Roof repair" and job["issue"] in ("", "Roof repair"):
            updates["issue"] = issue
        if parsed and not job["address_locked"] and parsed.text != job["address"]:
            updates["address"] = parsed.text
            updates.update(self._locate(parsed.text))
            if job["status"] == "needs_address":
                updates["status"] = "new"
        self.store.update_job(job["id"], **updates)
        return "updated", job["id"]
