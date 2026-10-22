# Kodiak Roofing — Storm Repair Scheduler

For the days after a wind storm when 100+ people call about blown-off shingles.

1. **Collect.** Pull new calls, voicemails and texts from **Quo**. The tool finds the address in what
   the customer said ("11233 35 ave NW", "150 McDowell Wynd, Leduc", "unit 26, 6802 50 th avenue Camrose"...).
   You can also type or paste addresses yourself.
2. **Group.** It puts every job on a map and groups nearby houses into one crew's day, so nobody
   drives from Mill Woods to St. Albert and back. Leaks go first in the morning, and the oldest calls
   are booked first.
3. **Export.** At the end of the day, click **Export**. You get a printable sheet for each crew
   (stop order, arrival times, phone numbers, Google Maps route links) plus a spreadsheet (CSV).
   Those jobs are marked *scheduled*, so they won't be booked twice.

Run it as often as you like. Each Quo sync only reads what came in since the last one, and jobs pile
up in the **Waiting** list until you export them.

---

## Setup (one time, about 5 minutes)

1. Install **Python 3.10 or newer** from <https://www.python.org/downloads/>. On Windows, tick
   *"Add Python to PATH"* during the install. Nothing else needs to be installed.
2. Download this folder.
3. **Windows:** double-click `start-scheduler.bat`. **Mac:** double-click `start-scheduler.command`.
   Or from a terminal: `python -m kodiak_scheduler serve`.
4. Your browser opens to <http://localhost:8765>. Click **Settings** and fill in:
   - **Shop / start address**: where crews leave from in the morning.
   - **Quo API key**: in Quo go to *Settings → API → Generate key*, then paste it here.
   - **Quo inbox**: `Kodiak Roofing`.
   - **Numbers to ignore**: the crew's and office's own cell numbers, so texts with the guys
     ("you almost at that house?") don't turn into jobs.

All data stays on that computer, in the `scheduler-data` folder next to the program: the database
plus an `exports` folder with every day's sheets.

## Daily use during a storm

| When | What to do |
|---|---|
| Any time | Click **⟳ Sync from Quo**. New callers show up under **Waiting**. Anyone who called or texted without giving an address shows up under **Need address**. Call them back, then click the address line to type it in. |
| Someone calls the office directly | Type the address into **Add a job**, then press Enter. To add a lot at once, use **Paste a bunch at once**: one address or text message per line. |
| End of day | Set the crews (e.g. `Sloane, Leo, Kyle` or just `Crew 1, Crew 2`), then click **Preview**. Check the map and click **Export day & mark scheduled**. Print the sheet that opens, or text each crew their Google Maps route link. |
| Next morning | Mark finished jobs **✔ Done**. Before building the next schedule, click **Return unfinished to queue**. Anything not marked Done goes back to Waiting and keeps its original call date, so it gets booked first. |

**Preview days:** set this to 5 to see how far out you're booked at the current crew size. That's
useful for telling callers "we'll be there Thursday."

**Group by:**
- *Oldest calls first* (the default) books first-come-first-served and fills each crew's day with
  houses near those calls.
- *Tightest areas* packs the densest clusters first, starting from the far-out towns.

**Pins:** each job is tagged with how sure the tool is of its map location. Untagged means the exact
house. "approx" means within a block or two. "postal area only" and "town only" mean you should
check the address. A pin in the wrong spot can be dragged to the right one.

## How it finds locations

1. Looks the address up online on OpenStreetMap (free). If you set a `GOOGLE_MAPS_API_KEY`
   environment variable, it uses Google first, which is better for brand-new subdivisions.
2. With no internet, it uses Edmonton's numbered grid (e.g. `10234 82 Ave NW` is at 102 St & 82 Ave),
   then the postal code area, then the town centre.

Every lookup is saved, so the same address is never looked up twice.

## Quo connection details

The sync uses Quo's public API (`api.openphone.com/v1`). It reads the following from the inbox you
name in Settings:

- conversations updated since the last sync;
- incoming and outgoing texts (the customer's own words are checked first for an address);
- calls, voicemail transcripts and Quo's AI call summaries, plus call transcripts on the Business plan
  when no address has been found yet;
- contacts, for the caller's name and any *Address* custom field.

The tool only **reads** from Quo. It never sends texts or changes anything there. One phone number
equals one job: if the same person texts again, their existing job is updated instead of being
duplicated.

## Command line (optional)

```bash
python -m kodiak_scheduler sync                 # pull from Quo
python -m kodiak_scheduler add "11233 35 Ave NW" --name "Pat" --phone 7805550101 --urgent
python -m kodiak_scheduler paste addresses.txt  # one per line
python -m kodiak_scheduler import-csv leads.csv # columns like Address, Name, Phone, Notes
python -m kodiak_scheduler plan --days 5 --crews "Sloane,Leo,Kyle"
python -m kodiak_scheduler export --crews 3     # tomorrow's sheets -> scheduler-data/exports/
python -m kodiak_scheduler done 12 15 18
python -m kodiak_scheduler rollover
python -m kodiak_scheduler config depot_address="123 Example St NW, Edmonton" max_stops_per_crew=8
```

To let other computers in the office open the app, run
`python -m kodiak_scheduler serve --host 0.0.0.0` and visit `http://<that-computer's-IP>:8765`.

## Tests

```bash
python -m unittest discover tests
```
