# redomat_slots.py

Lists free **Rezervacija rednog broja** time slots on https://redomat.mup.hr without clicking through the site.
Read-only: it never books anything. Python 3.8+, standard library only.

## How it works

`https://redomat.mup.hr/` embeds every PU, service and *free* slot of the whole booking window in its first HTML
response (`var locations = [...]`). One GET returns everything the "Datum" / "Vrijeme" dropdowns would show, so no
browser or per-day clicking is needed. The "Broj osoba" rule is copied from the site's own JavaScript: N persons need
N back-to-back slots. Results were checked against the site's own `buildTimeValues` for all 282 PU/service/persons
combinations.

## Usage

```
python redomat_slots.py --list                       # all PUs and their ids
python redomat_slots.py --list --pu zagreb           # reservable services of a PU
python redomat_slots.py --pu zagrebačka --service Prebivalište --persons 1
python redomat_slots.py --pu 17 --service 524288 --persons 2 --limit 50
```

* `--pu` / `--service`: id, full name, or unique part of the name (case and diacritics ignored).
* `--persons` (default 1), `--limit` (default 50).
* `--state-file FILE`: remembers the last result; the next run marks slots that became free as `NEW`
  and reports how many were taken.
* `--watch SECONDS` (minimum 10): monitor mode, see below.
* `--no-notify`, `--notify-test`: switch the desktop notification off, or send a test one.

Exit codes: `0` ok, `1` site unreachable or page layout changed, `2` bad arguments.

## Monitor mode (`--watch`)

```
python redomat_slots.py --pu 17 --service 524288 --persons 1 --watch 30
```

Runs until Ctrl+C and prints one line per check: timestamp, the earliest free date and time, and how many slots exist.

```
[2026-10-01 14:19:36] earliest: 2026-10-09 Petak 08:00  (277 slots on 5 days)
[2026-10-01 14:20:06] earliest: 2026-10-07 Srijeda 15:40  (278 slots on 6 days)  *** EARLIER than 2026-10-09 08:00 ***
```

When the earliest slot is earlier than at the previous check (or a slot appears after none were free), the line is
highlighted (green in a terminal, plain `***` text in a log file) and a Windows desktop notification is shown.
A later earliest slot just gets an `(earliest was ...)` note. Network errors are printed and the loop carries on.
With `--state-file` the comparison also survives a restart. Desktop notifications use Windows PowerShell's toast
API; if none appear, check Focus Assist / Do Not Disturb, and use `--notify-test` to try it.

## Running it unattended

Simplest: leave a terminal open with `--watch` (the only mode that notifies).

Windows Task Scheduler: either start `--watch 30` once at logon (*Program* `python`, *Arguments*
`redomat_slots.py --pu 17 --service 524288 --watch 30`, *Start in* `C:\Lang\redomat_mup_automate`), or repeat a plain
one-shot run every 10 minutes with `--state-file zg.json`. The one-shot run only marks `NEW` slots in its output and
does not show notifications. To keep a log, use `cmd` as the program and
`/c python redomat_slots.py ... >> slots.log 2>&1` as the arguments.

## If it stops working

The only assumption is the `var locations = ` block in the page. If the site moves it behind an API call or adds
bot protection, the script exits with code 1 and a message saying so; the fallback would be driving the page with
headless Chromium (e.g. Playwright in Docker), which is slower and heavier.
