#!/usr/bin/env python3
"""List free "Rezervacija rednog broja" time slots on https://redomat.mup.hr.

The site embeds every location, service and free slot for the whole booking
window in its first HTML response (`var locations = [...]`), so one plain GET
is enough: no browser, no day-by-day clicking. The script is read-only and
never books anything.

Examples:
  python redomat_slots.py --list                              # all PUs
  python redomat_slots.py --list --pu zagreb                  # services of one PU
  python redomat_slots.py --pu zagrebačka --service Prebivalište --persons 1
  python redomat_slots.py --pu 17 --service 524288 --state-file zg.json   # mark NEW slots
  python redomat_slots.py --pu 17 --service 524288 --watch 30             # monitor: check every 30 s,
                                                                          # alert when the earliest slot gets earlier
  python redomat_slots.py --notify-test                                   # check desktop notifications work
"""
import argparse
import base64
import gzip
import json
import os
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

URL = "https://redomat.mup.hr/"
MARKER = "var locations = "
USER_AGENT = "Mozilla/5.0 (compatible; redomat-slot-checker/1.0)"
MIN_WATCH_SECONDS = 10
HIGHLIGHT, RESET = "\x1b[1;30;42m", "\x1b[0m"  # black on green

# Windows toast through the built-in Windows PowerShell 5.1 (no extra packages). Unregistered apps cannot
# show toasts, so it borrows PowerShell's own app id. The toast XML arrives in an environment variable.
TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:REDOMAT_TOAST_XML)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe').Show($toast)
"""


class UserError(Exception):
    """Bad input; reported as a usage error."""


class FetchError(Exception):
    """Site unreachable or its page layout changed."""


def norm(text):
    """Lower-case and strip diacritics so 'zagrebacka' matches 'zagrebačka'."""
    text = unicodedata.normalize("NFD", text.casefold().replace("đ", "d"))
    return "".join(c for c in text if not unicodedata.combining(c))


def fetch_locations(timeout=30, retries=3):
    request = urllib.request.Request(
        URL, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip", "Accept-Language": "hr"}
    )
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            break
        except (urllib.error.URLError, OSError) as exc:
            if attempt == retries:
                raise FetchError(f"cannot download {URL}: {exc}") from exc
            time.sleep(2 ** attempt)

    html = raw.decode("utf-8", errors="replace")
    start = html.find(MARKER)
    if start < 0:
        raise FetchError(f"'{MARKER.strip()}' not found in {URL} - the page layout has probably changed")
    try:
        locations, _ = json.JSONDecoder().raw_decode(html, start + len(MARKER))
    except ValueError as exc:
        raise FetchError(f"cannot parse embedded location data: {exc}") from exc
    return locations


def pick(query, items, what):
    """Find one item (PU or service) by exact id/name, else by unique substring."""
    q = norm(query.strip())
    matches = [i for i in items if q == str(i["id"]) or q == norm(i["name"])]
    if not matches:
        matches = [i for i in items if q in norm(i["name"])]
    if len(matches) == 1:
        return matches[0]
    shown = matches or items
    listing = "\n".join(f"  {i['id']:>10}  {i['name']}" for i in shown)
    problem, header = ("is ambiguous", "Candidates") if matches else ("not found", "Available")
    raise UserError(f"{what} '{query}' {problem}. {header}:\n{listing}")


def reservation_group(loc, service_id):
    """Index into each day's `values` for this service, or None if it is not reservable."""
    config = loc.get("td_Mobile_ReservationConfig") or {}
    for index, group in enumerate(config.get("groups", [])):
        if service_id in group:
            return index
    return None


def reservable_services(loc):
    return [s for s in loc.get("serviceList") or [] if reservation_group(loc, s["id"]) is not None]


def persons_term(loc, service_id):
    terms = loc.get("Ticket_Reservation_TermCount")
    for term in terms if isinstance(terms, list) else []:  # some PUs send {} or null here
        if service_id in term["serviceIds"]:
            return term
    return None


def slots_needed(loc, service_id, persons):
    """Back-to-back slots required, plus a note if the site's persons dropdown differs."""
    term = persons_term(loc, service_id)
    if term is None:
        note = "this service has no 'Broj osoba' choice on the site; persons ignored" if persons != 1 else ""
        return 1, note
    options = term["optionList"]
    top = max(o["value"] for o in options)
    if persons - 1 > top:
        label = next(o["text"] for o in options if o["value"] == top)
        return top + 1, f"site offers at most '{label}'; checking for {top + 1} persons"
    return persons, ""


def find_slots(loc, group, need):
    """Free (date, weekday, time) tuples; N persons need N consecutive slot indexes `i`."""
    slots = []
    for day in loc.get("reservationList") or []:
        values = day["values"]
        times = values[group] if group < len(values) else []
        for pos, slot in enumerate(times):
            if all(pos + k < len(times) and times[pos + k]["i"] == slot["i"] + k for k in range(need)):
                slots.append((day["value"], day["text"].split(",")[0], slot["value"]))
    return sorted(slots)


def load_state(path, signature):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))  # tolerate a BOM from Notepad/PowerShell
    except (OSError, ValueError):
        return None
    return set(data["slots"]) if data.get("query") == signature else None


def save_state(path, signature, keys):
    Path(path).write_text(json.dumps({"query": signature, "slots": sorted(keys)}, indent=1), encoding="utf-8")


def list_locations(locations):
    for loc in sorted(locations, key=lambda l: norm(l["name"])):
        where = f" - {loc['address']}" if loc.get("address") else ""
        reservations = f"reservations, {loc.get('td_Mobile_ReservationDays')} days" if loc.get("isReservation") else "NO reservations"
        print(f"{loc['id']:>5}  {loc['name']}{where}  [{reservations}]")


def list_services(loc):
    print(f"{loc['name']} (id {loc['id']}) - services that can be reserved:")
    for service in reservable_services(loc):
        term = persons_term(loc, service["id"])
        persons = f"persons: {term['optionList'][0]['text']} - {term['optionList'][-1]['text']}" if term else "no persons choice"
        print(f"  {service['id']:>10}  {service['name']}  ({persons})")


def enable_color():
    """ANSI colours only on a real terminal; switches on VT processing in the Windows console."""
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.GetStdHandle.restype = ctypes.c_void_p
            kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except (AttributeError, OSError):
            return False
    return True


def notify(title, lines):
    """Show a desktop notification. Returns None on success, else a short error text."""
    if sys.platform != "win32":
        return "desktop notifications are only implemented for Windows"
    texts = "".join(f"<text>{escape(t)}</text>" for t in (title, *lines))
    toast = f"<toast duration='long'><visual><binding template='ToastGeneric'>{texts}</binding></visual></toast>"
    command = base64.b64encode(TOAST_SCRIPT.encode("utf-16-le")).decode("ascii")
    try:
        done = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-OutputFormat", "Text", "-EncodedCommand", command],
            env={**os.environ, "REDOMAT_TOAST_XML": toast},
            capture_output=True, text=True, errors="replace", timeout=30,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if done.returncode == 0:
        return None
    return (done.stderr or done.stdout).strip() or f"powershell exited with {done.returncode}"


def scan(args):
    """Download the page and work out the free slots for the requested PU/service/persons."""
    loc = pick(args.pu, fetch_locations(), "PU")
    if not loc.get("isReservation"):
        raise UserError(f"{loc['name']} does not offer 'Rezervacija rednog broja' at the moment")
    service = pick(args.service, reservable_services(loc), "Service")
    need, note = slots_needed(loc, service["id"], args.persons)
    slots = find_slots(loc, reservation_group(loc, service["id"]), need)
    return loc, service, need, note, slots


def report_once(loc, slots, previous, args):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    keys = {f"{date} {time_}" for date, _, time_ in slots}
    new = keys - previous if previous is not None else set()
    gone = previous - keys if previous is not None else set()
    if not slots:
        print(f"[{stamp}] Svi termini zauzeti - no free slots in the next {loc.get('td_Mobile_ReservationDays')} days")
    else:
        days = len({date for date, _, _ in slots})
        print(f"[{stamp}] {len(slots)} free slot(s) on {days} day(s); showing first {min(args.limit, len(slots))}")
        for date, weekday, time_ in slots[: args.limit]:
            flag = "NEW" if f"{date} {time_}" in new else ""
            print(f"  {date}  {weekday:<12} {time_}  {flag}".rstrip())
    if previous is not None:
        print(f"since last check: {len(new)} new, {len(gone)} no longer free")


def report_watch(loc, service, slots, previous, args):
    """One line per check: timestamp and earliest slot; alert when it is earlier than at the last check."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now = f"{slots[0][0]} {slots[0][2]}" if slots else None  # slots are sorted, so this is the earliest
    was = min(previous) if previous else None  # None on the first check, or if nothing was free before
    if slots:
        date, weekday, time_ = slots[0]
        days = len({d for d, _, _ in slots})
        line = f"[{stamp}] earliest: {date} {weekday} {time_}  ({len(slots)} slots on {days} days)"
    else:
        line = f"[{stamp}] Svi termini zauzeti - no free slots"

    if now and previous is not None and (was is None or now < was):
        line += f"  *** EARLIER than {was} ***" if was else "  *** SLOT FREED - none were free before ***"
        print(f"{HIGHLIGHT}{line}{RESET}" if args.color else line)
        if not args.no_notify:
            body = [f"{loc['name']} - {service['name']}", f"{date} {weekday} {time_}" + (f" (was {was})" if was else "")]
            error = notify("Redomat: earlier slot available", body)
            if error:
                print(f"warning: desktop notification failed: {error}", file=sys.stderr)
    else:
        if was and was != now:
            line += f"  (earliest was {was})"
        print(line)


def check(args, previous):
    """Fetch once, print the report, return the set of free-slot keys."""
    first = previous is None
    loc, service, need, note, slots = scan(args)
    keys = {f"{date} {time_}" for date, _, time_ in slots}
    signature = [loc["id"], service["id"], need]
    if previous is None and args.state_file:
        previous = load_state(args.state_file, signature)

    if first:
        print(f"{loc['name']} (id {loc['id']}) | {service['name']} (id {service['id']}) | persons: {need}")
        if note:
            print(f"note: {note}")
        if args.watch:
            print(f"checking every {args.watch} s, Ctrl+C to stop")
    if args.watch:
        report_watch(loc, service, slots, previous, args)
    else:
        report_once(loc, slots, previous, args)

    if args.state_file:
        save_state(args.state_file, signature, keys)
    return keys


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pu", help="police department: id or (part of) name, e.g. 17 or zagrebačka")
    parser.add_argument("--service", help="'Usluga': id or (part of) name, e.g. Prebivalište")
    parser.add_argument("--persons", type=int, default=1, help="'Broj osoba' (default 1)")
    parser.add_argument("--limit", type=int, default=50, help="max slots to print (default 50)")
    parser.add_argument("--list", action="store_true", help="list PUs, or the services of --pu, and exit")
    parser.add_argument("--state-file", help="JSON file remembering the last result so new slots are marked NEW")
    parser.add_argument("--watch", type=int, metavar="SECONDS",
                        help=f"monitor mode: check every SECONDS (min {MIN_WATCH_SECONDS}), print one line per check, "
                             "highlight + desktop notification when the earliest slot gets earlier")
    parser.add_argument("--no-notify", action="store_true", help="with --watch: console alert only, no desktop notification")
    parser.add_argument("--notify-test", action="store_true", help="show a test desktop notification and exit")
    args = parser.parse_args()
    if not (args.list or args.notify_test) and not (args.pu and args.service):
        parser.error("--pu and --service are required (use --list to see valid values)")
    if args.persons < 1 or args.limit < 1:
        parser.error("--persons and --limit must be at least 1")
    if args.watch is not None and args.watch < MIN_WATCH_SECONDS:
        parser.error(f"--watch must be at least {MIN_WATCH_SECONDS} seconds, please don't hammer the site")
    return parser, args


def main():
    # Croatian letters must survive redirection to a file/pipe on Windows (default cp1252).
    # Line-buffered so `--watch ... >> log` shows each report immediately.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser, args = parse_args()
    args.color = enable_color()
    if args.notify_test:
        error = notify("Redomat slot checker", ["Test notification - desktop alerts work."])
        print(f"notification failed: {error}" if error else "test notification sent")
        return 1 if error else 0
    try:
        if args.list:
            locations = fetch_locations()
            if args.pu:
                list_services(pick(args.pu, locations, "PU"))
            else:
                list_locations(locations)
            return 0
        previous = None
        while True:
            started = time.monotonic()
            try:
                previous = check(args, previous)
            except FetchError as exc:
                if not args.watch:
                    raise
                print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {exc} - will retry", file=sys.stderr)
            if not args.watch:
                return 0
            time.sleep(max(0.0, args.watch - (time.monotonic() - started)))  # keep the interval steady
    except UserError as exc:
        parser.error(str(exc))
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
