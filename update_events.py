"""
Daily updater for the NZ Markets Calendar.
- Reads data/events_base.json (the curated event list).
- Converts every event to NZ time (daylight saving handled automatically).
- OPTIONAL: if a FINNHUB_API_KEY is set, replaces *estimated* US earnings dates with confirmed ones.
- Writes data/events.json (read by index.html) and calendar.ics (subscribe in your phone calendar, 24h alert on every event).
It uses only Python's standard library, and it never crashes if the data provider is down.
"""
import json, os, time, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

NZ = ZoneInfo("Pacific/Auckland")
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, "data", "events_base.json")
OUT = os.path.join(HERE, "data", "events.json")
ICS = os.path.join(HERE, "calendar.ics")
API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
HOUR_MAP = {"bmo": "06:30", "amc": "16:05", "dmh": "12:00"}  # before open / after close / during hours (US Eastern)


def to_nz(src_date, src_time, tz):
    d = date.fromisoformat(src_date)
    if not src_time:
        return datetime(d.year, d.month, d.day, 0, 0, tzinfo=NZ), True
    h, m = (int(x) for x in src_time.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=ZoneInfo(tz)).astimezone(NZ), False


def fetch_earnings(symbol, start, end):
    qs = urllib.parse.urlencode({"from": start, "to": end, "symbol": symbol, "token": API_KEY})
    req = urllib.request.Request("https://finnhub.io/api/v1/calendar/earnings?" + qs, headers={"User-Agent": "market-calendar"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("earningsCalendar", []) or []


def update_earnings(events):
    """Swap estimated US earnings dates for confirmed ones. Returns number of events changed."""
    if not API_KEY:
        print("No FINNHUB_API_KEY set - skipping earnings update (calendar still builds fine).")
        return 0
    today = date.today()
    end = today + timedelta(days=120)
    tickers = sorted({e["tk"] for e in events if e["cat"] == "Earnings" and e["tk"] and e["src_tz"] == "America/New_York"
                      and e["status"].startswith("Estimated") and today <= date.fromisoformat(e["src_date"]) <= end + timedelta(days=21)})
    changed = 0
    for tk in tickers:
        try:
            rows = fetch_earnings(tk, today.isoformat(), end.isoformat())
        except Exception as ex:  # network problems, rate limits, bad key...
            print(f"  {tk}: skipped ({ex})")
            time.sleep(1.2)
            continue
        found = [r for r in rows if r.get("date")]
        for e in events:
            if e["tk"] != tk or e["cat"] != "Earnings" or not e["status"].startswith("Estimated") or not found:
                continue
            est = date.fromisoformat(e["src_date"])
            best = min(found, key=lambda r: abs((date.fromisoformat(r["date"]) - est).days))
            if abs((date.fromisoformat(best["date"]) - est).days) <= 21:
                e["src_date"] = best["date"]
                e["src_time"] = HOUR_MAP.get((best.get("hour") or "").lower(), None)
                e["status"] = "Confirmed (Finnhub)"
                e["notes"] = (e.get("notes", "") + f" Date confirmed via Finnhub on {today.isoformat()}.").strip()
                changed += 1
        time.sleep(1.2)  # stay inside the free rate limit
    print(f"Earnings dates confirmed/updated: {changed}")
    return changed


def esc(s):
    return s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def build():
    events = json.load(open(BASE, encoding="utf-8"))
    changed = 0
    try:
        changed = update_earnings(events)
    except Exception as ex:
        print("Earnings update failed, continuing with base dates:", ex)

    out = []
    for e in events:
        dt, allday = to_nz(e["src_date"], e["src_time"], e["src_tz"])
        e2 = dict(e)
        e2["date_nz"] = dt.date().isoformat()
        e2["time_nz"] = None if allday else dt.strftime("%H:%M")
        e2["nz_iso"] = dt.isoformat()
        e2["local"] = ("All day (local date " + date.fromisoformat(e["src_date"]).strftime("%d-%b-%Y") + ")") if allday else \
            dt.astimezone(ZoneInfo(e["src_tz"])).strftime("%a %d-%b-%Y %H:%M %Z")
        out.append(e2)
    out.sort(key=lambda x: (datetime.fromisoformat(x["nz_iso"]), x["name"]))

    now = datetime.now(timezone.utc)
    meta = {"updated_utc": now.strftime("%Y-%m-%d %H:%M UTC"), "updated_nz": now.astimezone(NZ).strftime("%a %d %b %Y %H:%M %Z"),
            "earnings_dates_updated_this_run": changed, "finnhub_key_used": bool(API_KEY), "event_count": len(out)}
    json.dump({"meta": meta, "events": out}, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//NZ Market Calendar//EN", "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:NZ Markets & Crypto Calendar (24h alerts)", "REFRESH-INTERVAL;VALUE=DURATION:PT12H", "X-PUBLISHED-TTL:PT12H"]
    for e in out:
        lines += ["BEGIN:VEVENT", f"UID:{e['id']}@nz-market-calendar", f"DTSTAMP:{stamp}"]
        dt = datetime.fromisoformat(e["nz_iso"])
        if e["time_nz"] is None:
            d = dt.date()
            lines += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{(d + timedelta(days=1)):%Y%m%d}"]
        else:
            lines += [f"DTSTART:{dt.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}", "DURATION:PT30M"]
        sent = f" Sentiment: {e['sl']} - {e['sd']}" if e.get("sl") else ""
        title = f"[{e['imp']}] {e['name']}" + (f" ({e['tk']})" if e.get("tk") else "")
        lines += [f"SUMMARY:{esc(title)}",
                  f"DESCRIPTION:{esc(e['cat'] + ' | ' + e['status'] + ' | Source time: ' + e['local'] + '. Lean: ' + e['lean'] + sent)}",
                  f"CATEGORIES:{esc(e['cat'])}", "BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{esc('Tomorrow: ' + e['name'])}",
                  "TRIGGER:-P1D", "END:VALARM", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    with open(ICS, "w", newline="", encoding="utf-8") as fh:
        fh.write("\r\n".join(lines) + "\r\n")
    print(f"Wrote {len(out)} events. Updated {meta['updated_nz']}.")


if __name__ == "__main__":
    build()
