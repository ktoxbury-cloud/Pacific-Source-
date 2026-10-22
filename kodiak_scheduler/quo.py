"""Pull new calls, voicemails and texts from Quo (formerly OpenPhone) and turn them into jobs.

Uses Quo's public REST API (https://api.openphone.com/v1). Create a key in
Quo -> Settings -> API, then either paste it in the app's Settings box or set
the QUO_API_KEY environment variable.

Every sync only looks at activity since the previous sync, and remembers which
calls/messages it already read, so you can hit "Sync" as often as you like.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .extract import extract_address
from .intake import normalize_phone

API = "https://api.openphone.com/v1"


class QuoError(Exception):
    pass


class QuoClient:
    def __init__(self, api_key, transport=None, pause=0.12):
        if not api_key:
            raise QuoError("No Quo API key. Add it in Settings or set QUO_API_KEY.")
        self.api_key = api_key
        self.pause = pause
        self._transport = transport or self._http

    def _http(self, url):
        req = urllib.request.Request(url, headers={
            "Authorization": self.api_key, "Accept": "application/json",
            "User-Agent": "KodiakRoofingScheduler/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(body)
            except ValueError:
                return exc.code, {"message": body[:300]}

    def get(self, path, params=None, allow_404=False):
        url = API + path
        if params:
            clean = {k: v for k, v in params.items() if v not in (None, "", [])}
            url += "?" + urllib.parse.urlencode(clean, doseq=True)
        for attempt in range(4):
            if self.pause:
                time.sleep(self.pause)
            status, data = self._transport(url)
            if status == 429:  # rate limited: back off and retry
                time.sleep(2 ** attempt)
                continue
            break
        if status == 404 and allow_404:
            return None
        if status in (401, 403):
            raise QuoError("Quo rejected the API key (check it in Quo -> Settings -> API).")
        if status >= 400:
            raise QuoError(f"Quo API error {status} on {path}: {data.get('message', data)}")
        return data

    def paged(self, path, params, limit=2000):
        params = dict(params)
        out = []
        while True:
            data = self.get(path, params) or {}
            out.extend(data.get("data") or [])
            token = data.get("nextPageToken")
            if not token or len(out) >= limit:
                return out
            params["pageToken"] = token

    # Endpoints ------------------------------------------------------------

    def phone_numbers(self):
        return (self.get("/phone-numbers") or {}).get("data") or []

    def conversations(self, phone_number_id, updated_after):
        return self.paged("/conversations", {
            "phoneNumbers": [phone_number_id], "updatedAfter": updated_after, "maxResults": 100,
        })

    def messages(self, phone_number_id, participant, created_after):
        return self.paged("/messages", {
            "phoneNumberId": phone_number_id, "participants": [participant],
            "createdAfter": created_after, "maxResults": 100,
        }, limit=300)

    def calls(self, phone_number_id, participant, created_after):
        return self.paged("/calls", {
            "phoneNumberId": phone_number_id, "participants": [participant],
            "createdAfter": created_after, "maxResults": 100,
        }, limit=100)

    def voicemail_text(self, call_id):
        data = (self.get(f"/call-voicemails/{call_id}", allow_404=True) or {}).get("data") or {}
        return data.get("transcript") or ""

    def summary_text(self, call_id):
        data = (self.get(f"/call-summaries/{call_id}", allow_404=True) or {}).get("data") or {}
        parts = list(data.get("summary") or []) + list(data.get("nextSteps") or [])
        return " ".join(p for p in parts if isinstance(p, str))

    def transcript_text(self, call_id):
        data = (self.get(f"/call-transcripts/{call_id}", allow_404=True) or {}).get("data") or {}
        return " ".join(d.get("content", "") for d in data.get("dialogue") or [])

    def contacts(self, limit=3000):
        return self.paged("/contacts", {"maxResults": 50}, limit=limit)


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _earliest(a, b):
    return min(x for x in (a, b) if x) if (a or b) else None


def _contact_index(raw_contacts):
    """phone -> (name, address) from Quo contacts (address from any 'address' custom field)."""
    index = {}
    for c in raw_contacts:
        d = c.get("defaultFields") or {}
        name = " ".join(x for x in (d.get("firstName"), d.get("lastName")) if x).strip()
        address = ""
        for f in c.get("customFields") or []:
            label = f"{f.get('name', '')} {f.get('key', '')}".lower()
            if "address" in label and isinstance(f.get("value"), str) and f["value"].strip():
                address = f["value"].strip()
                break
        for p in d.get("phoneNumbers") or []:
            num = normalize_phone(p.get("value", ""))
            if num:
                index[num] = (name, address)
    return index


def resolve_inbox(client, wanted):
    numbers = client.phone_numbers()
    if not numbers:
        raise QuoError("No phone numbers found in this Quo workspace.")
    wanted_l = (wanted or "").strip().lower()
    for n in numbers:
        if wanted_l and wanted_l in (n.get("id", "").lower(), (n.get("number") or "").lower(),
                                     (n.get("name") or "").lower()):
            return n
    for n in numbers:
        if wanted_l and wanted_l in (n.get("name") or "").lower():
            return n
    if len(numbers) == 1 or not wanted_l:
        return numbers[0]
    names = ", ".join(f"{n.get('name')} ({n.get('number')})" for n in numbers)
    raise QuoError(f"Couldn't find inbox '{wanted}'. Available: {names}")


def sync(store, intake, cfg, client=None, hours=None, log=print, use_contacts=True):
    """Read new Quo activity into the job list. Returns a summary dict."""
    client = client or QuoClient(cfg.get("quo_api_key"))
    inbox = resolve_inbox(client, cfg.get("quo_inbox"))
    pn_id = inbox["id"]
    own_numbers = {normalize_phone(inbox.get("number", ""))}
    ignore = {normalize_phone(n) for n in cfg.get("ignore_numbers") or []} | own_numbers

    started = datetime.now(timezone.utc)
    last_key = f"quo_last_sync:{pn_id}"
    if hours:
        since = started - timedelta(hours=float(hours))
    else:
        last = store.meta_get(last_key)
        since = (datetime.fromisoformat(last.replace("Z", "+00:00")) - timedelta(minutes=10)) if last else (
            started - timedelta(hours=float(cfg.get("sync_lookback_hours") or 48)))
    since_s = _iso(since)
    log(f"Quo inbox: {inbox.get('name')} {inbox.get('number')} — reading activity since {since_s}")

    contacts = {}
    if use_contacts:
        try:
            contacts = _contact_index(client.contacts())
        except QuoError as exc:
            log(f"  (skipping contact names: {exc})")

    summary = {"conversations": 0, "created": 0, "updated": 0, "skipped": 0,
               "with_address": 0, "needs_address": 0, "inbox": inbox.get("name")}
    for conv in client.conversations(pn_id, since_s):
        participants = [normalize_phone(p) for p in conv.get("participants") or []]
        participants = [p for p in participants if p and p not in ignore]
        if len(participants) != 1:  # group threads / internal chats
            continue
        phone = participants[0]
        summary["conversations"] += 1

        new_ids, incoming, outgoing, first_at = [], [], [], None
        for m in client.messages(pn_id, phone, since_s):
            if store.seen(m["id"]):
                continue
            new_ids.append(m["id"])
            text = (m.get("text") or m.get("body") or "").strip()
            if not text:
                continue
            (incoming if m.get("direction") == "incoming" else outgoing).append(text)
            first_at = _earliest(first_at, m.get("createdAt"))

        call_texts = []
        for call in client.calls(pn_id, phone, since_s):
            if store.seen(call["id"]):
                continue
            new_ids.append(call["id"])
            first_at = _earliest(first_at, call.get("createdAt"))
            for getter in (client.voicemail_text, client.summary_text):
                try:
                    t = getter(call["id"])
                except QuoError:
                    t = ""
                if t:
                    call_texts.append(t)
            have_address = any(extract_address(t) for t in incoming + call_texts)
            if not have_address and (call.get("duration") or 0) > 20:
                try:
                    t = client.transcript_text(call["id"])
                except QuoError:
                    t = ""
                if t:
                    call_texts.append(t)
            if not call_texts:
                call_texts.append(f"{call.get('direction', '')} call, {call.get('status', '')}")

        if not new_ids:
            continue
        name, contact_address = contacts.get(phone, ("", ""))
        # Customer's own words first, then call notes, then what we texted them.
        texts = list(reversed(incoming)) + call_texts + list(reversed(outgoing))
        action, job_id = intake.from_conversation(
            phone, texts, received_at=first_at, source_ref=conv.get("id", ""),
            contact_name=name, contact_address=contact_address)
        summary[action] += 1
        if job_id:
            job = store.get_job(job_id)
            summary["with_address" if job["address"] else "needs_address"] += 1
        store.mark_seen(new_ids)

    store.meta_set(last_key, _iso(started))
    log(f"Done: {summary['created']} new jobs, {summary['updated']} updated, "
        f"{summary['needs_address']} still need an address.")
    return summary
