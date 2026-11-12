"""
OP Sniper: books One Playground classes the moment their 72-hour window opens.

  python3 opsniper.py setup            save your login (password goes in the macOS Keychain)
  python3 opsniper.py check            sign in and out again to prove the login works; books nothing
  python3 opsniper.py classes          browse the timetable, with booking IDs
  python3 opsniper.py add ID           queue a one-off class
  python3 opsniper.py remove ID        take it off the queue
  python3 opsniper.py status           what's armed, booked, watching
  python3 opsniper.py run [--dry-run]  the sniper itself; leave it running
  python3 opsniper.py web [--dry-run]  the sniper plus a dashboard in your browser

Talks to the same endpoints the oneplayground.com.au timetable uses (worked out
by sidharthjoly/ClassSniper from the site's JS bundle). Your login is the email
and password you use on that site, not a member number.
"""

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------- constants

SYD = ZoneInfo("Australia/Sydney")
API = "https://cms.oneplayground.com.au/api/timetable"
TIMETABLE = "https://oneplayground.com.au/classes/timetable/"
HEADERS = {
    "Origin": "https://oneplayground.com.au",
    "Referer": TIMETABLE,
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
}
SESSION_ERRORS = ("SESSION_EXPIRED", "SESSION_REQUIRED")
NON_CLASS_WORDS = ("sauna", "recovery", "plunge", "ice bath")

WINDOW = timedelta(hours=72)
MAX_TRIES_PER_CLASS = 5        # strikes + watch grabs, per class, ever
KEYCHAIN_SERVICE = "op-sniper"

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"
RULES_FILE = HERE / "rules.json"
QUEUE_FILE = HERE / "queue.json"
STATE_FILE = HERE / "state.json"
LOG_FILE = HERE / "sniper.log"

DEFAULT_CONFIG = {
    "email": None,
    # Cancelling inside 24h costs a fee, so never book (or keep watching for)
    # anything closer than this. Same safety margin the original uses.
    "min_lead_hours": 30,
    # How long after the window opens to send the request. A few ms of slack
    # covers small clock differences; the retry loop covers bigger ones.
    "fire_delay_ms": 40,
    "tick_seconds": 60,
    "lookahead_days": 14,      # pre-book up to two weeks out
    "notifications": True,
}

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
TIME_RE = re.compile(r"^\s*(\d{1,2})(?:[:.](\d{2}))?\s*([ap])?\.?\s*m?\.?\s*$", re.I)

state_lock = threading.Lock()
QUIET = bool(os.getenv("OP_QUIET"))
LOG_LINES = deque(maxlen=200)


class AuthError(Exception):
    """Wrong email/password. Retrying won't fix it, so the daemon stops."""


# ---------------------------------------------------------------- small helpers

def now():
    return datetime.now(SYD)


def log(msg):
    line = f"[{now():%a %d %b %H:%M:%S}] {msg}"
    # On GitHub the run logs are public, so only the event word goes there;
    # the full line (class, gym, time) goes to the private state instead.
    print(f"[{now():%H:%M:%S}] {msg.split()[0] if msg else ''}" if QUIET else line, flush=True)
    LOG_LINES.append(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def notify(cfg, title, msg):
    if not cfg.get("notifications"):
        return
    if sys.platform == "win32":
        print("\a", end="", flush=True)   # beep; the line itself is already in the window and the log
        return
    if sys.platform != "darwin":
        return
    script = f"display notification {json.dumps(msg)} with title {json.dumps(title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True)


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def load_config():
    return {**DEFAULT_CONFIG, **load_json(CONFIG_FILE, {})}


def parse_time(text):
    m = TIME_RE.match(str(text))
    if not m:
        raise ValueError(f"can't read the time {text!r} (try 6:00 AM, 6.30pm or 18:00)")
    hour, minute, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ap:
        if not 1 <= hour <= 12:
            raise ValueError(f"bad time {text!r}")
        hour = hour % 12 + (12 if ap == "p" else 0)
    elif hour > 23:
        raise ValueError(f"bad time {text!r}")
    return hour, minute


def fmt(dt, with_date=True, seconds=False):
    """'Mon 18 Nov 6:00 AM' on every OS (Windows has no %-I)."""
    clock = dt.strftime("%I:%M:%S %p" if seconds else "%I:%M %p").lstrip("0")
    return f"{dt:%a %d %b} {clock}" if with_date else clock


def keep_awake():
    """Stop Windows from sleeping while the sniper runs (screen can still turn off)."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # CONTINUOUS | SYSTEM_REQUIRED


def start_of(row):
    return datetime.strptime(row["booking_start_datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=SYD)


def class_name(row):
    return row.get("booking_name") or row.get("activity_name") or "?"


def label(row):
    who = f" with {row['instructors']}" if row.get("instructors") else ""
    return f"{class_name(row)}{who}, {row.get('center_name')}, {fmt(start_of(row))}"


def is_class(row):
    group = (row.get("activity_group_name") or "").lower()
    return (row.get("booking_id") and row.get("booking_start_datetime")
            and row.get("booking_state") == "ACTIVE"
            and not any(w in group for w in NON_CLASS_WORDS))


def sleep_until(when):
    while True:
        left = (when - now()).total_seconds()
        if left <= 0:
            return
        time.sleep(min(left, 30) if left > 1 else left)


def ms_since(when):
    return (now() - when).total_seconds() * 1000


def get_password(email):
    if os.getenv("OP_PASSWORD"):
        return os.environ["OP_PASSWORD"]
    try:
        import keyring
    except ImportError:
        sys.exit("Missing 'keyring'. Run: python3 -m pip install -r requirements.txt")
    pw = keyring.get_password(KEYCHAIN_SERVICE, email)
    if not pw:
        sys.exit("No saved password. Run: python3 opsniper.py setup")
    return pw


# ---------------------------------------------------------------- the venue API

class OP:
    def __init__(self, email=None, password=None):
        self.email, self.password = email, password
        self.public = requests.Session()      # timetable reads, no cookie
        self.public.headers.update(HEADERS)
        self.auth = requests.Session()        # signed-in calls
        self.auth.headers.update(HEADERS)
        self.login_lock = threading.Lock()
        self.login_times = []

    # -- timetable (no login needed)

    def centers(self):
        r = self.public.get(f"{API}/centers", timeout=15)
        r.raise_for_status()
        return {c["name"]: c["id"] for c in r.json().get("centers", [])}

    def sessions(self, center_id, from_date, days):
        r = self.public.post(f"{API}/get-sessions-by-center-and-date", json={
            "center_id": center_id,
            "from_date": from_date.isoformat(),
            "to_date": (from_date + timedelta(days=days)).isoformat(),  # exclusive
        }, timeout=15)
        r.raise_for_status()
        return r.json().get("sessions", [])

    def find_row(self, row):
        """Fresh copy of one class (current pk/sk and remaining spots)."""
        day = start_of(row).date()
        try:
            rows = self.sessions(row["center_id"], day, 1)
        except requests.RequestException:
            return None
        return next((r for r in rows if r.get("booking_id") == row["booking_id"]), None)

    # -- account

    def is_signed_in(self):
        try:
            r = self.auth.get(f"{API}/session", timeout=10)
            return r.status_code == 200 and r.json().get("authenticated") is True
        except (requests.RequestException, ValueError):
            return False

    def login(self, force=False):
        with self.login_lock:
            if not force and self.is_signed_in():
                return
            # The login endpoint rate-limits at about 5 tries. Stay well under.
            t = time.time()
            self.login_times = [x for x in self.login_times if t - x < 600]
            if len(self.login_times) >= 3:
                raise RuntimeError("3 sign-ins in 10 minutes already; holding off so OP doesn't lock the account")
            self.login_times.append(t)
            r = self.auth.post(f"{API}/login", json={"email": self.email, "password": self.password}, timeout=10)
            if r.status_code != 200:
                # Never log the body of this one: it's the reply most likely to echo your details.
                try:
                    code = r.json().get("error", r.status_code)
                except ValueError:
                    code = r.status_code
                if r.status_code in (400, 401, 403):
                    raise AuthError(f"OP rejected the sign-in ({code}). Check with: python3 opsniper.py check")
                raise RuntimeError(f"sign-in failed ({code})")
            if not self.auth.cookies:
                raise RuntimeError("signed in but no session cookie came back")

    def logout(self):
        try:
            self.auth.post(f"{API}/logout", timeout=10)
        except requests.RequestException:
            pass

    def book(self, row):
        # The member comes from the session cookie; the payload just names the class.
        return self.auth.post(f"{API}/create-participation-and-send-message", json={
            "pk": row["pk"], "sk": row["sk"], "booking_id": row["booking_id"],
            "send_confirmation_message": True,
        }, timeout=10)


def fetch_rows(op, center_ids, days):
    """Classes from today through `days` ahead, asked for a week at a time."""
    today = now().date()
    jobs = [(cid, today + timedelta(days=k), min(7, days + 1 - k))
            for cid in center_ids for k in range(0, days + 1, 7)]
    seen, rows, errors = set(), [], []
    with ThreadPoolExecutor(max_workers=max(1, min(len(jobs), 8))) as pool:
        futures = {pool.submit(op.sessions, cid, start, n): cid for cid, start, n in jobs}
        for f in as_completed(futures):
            try:
                for r in f.result():
                    if is_class(r) and r["booking_id"] not in seen:
                        seen.add(r["booking_id"])
                        rows.append(r)
            except requests.RequestException as e:
                errors.append(f"center {futures[f]}: {e}")
    rows.sort(key=lambda r: (start_of(r), r.get("center_name", "")))
    return rows, errors


def resolve_location(name, centers):
    exact = {k.lower(): k for k in centers}
    if name.lower() in exact:
        return exact[name.lower()]
    hits = [k for k in centers if name.lower() in k.lower()]
    if len(hits) == 1:
        return hits[0]
    raise SystemExit(f"Unknown location {name!r}. OP has: {', '.join(sorted(centers))}")


# ---------------------------------------------------------------- rules + queue

def load_rules(centers):
    rules = load_json(RULES_FILE, [])
    for i, rule in enumerate(rules, 1):
        missing = [k for k in ("location", "day", "time", "class") if not rule.get(k)]
        if missing:
            # A loose rule could arm a whole day's timetable. Make it explicit.
            raise SystemExit(f"rules.json rule {i} is missing {', '.join(missing)}")
        rule["location"] = resolve_location(rule["location"], centers)
        if rule["day"][:3].lower() not in DAYS:
            raise SystemExit(f"rules.json rule {i}: bad day {rule['day']!r}")
        try:
            rule["_hm"] = parse_time(rule["time"])
        except ValueError as e:
            raise SystemExit(f"rules.json rule {i}: {e}")
    return rules


def matches(rule, row):
    st = start_of(row)
    if row.get("center_name", "").lower() != rule["location"].lower():
        return False
    if DAYS.index(rule["day"][:3].lower()) != st.weekday():
        return False
    if rule["_hm"] != (st.hour, st.minute):
        return False
    names = " ".join(filter(None, [row.get("booking_name"), row.get("activity_name"),
                                   row.get("activity_group_name")])).lower()
    if rule["class"].lower() not in names:
        return False
    if rule.get("instructor") and rule["instructor"].lower() not in (row.get("instructors") or "").lower():
        return False
    return True


def collect_targets(rows, rules, queue):
    by_id = {r["booking_id"]: r for r in rows}
    targets = {}
    for q in queue:
        if q["booking_id"] in by_id:
            targets[q["booking_id"]] = (by_id[q["booking_id"]], q.get("watch", True))
    for rule in rules:
        for r in rows:
            if matches(rule, r):
                targets.setdefault(r["booking_id"], (r, rule.get("watch", True)))
    return targets


# ---------------------------------------------------------------- striking

def fire(op, row, opens, dry_run, attempts=8, gap=0.35):
    """Send the booking. Retries briefly only on a definite refusal (covers our
    clock being a hair ahead of theirs); never on a request that may have landed."""
    relogged, last = False, ""
    for i in range(1, attempts + 1):
        lag = ms_since(opens)
        if dry_run:
            return "DRY_RUN", f"would have sent the booking {lag:.0f} ms after the window opened"
        try:
            r = op.book(row)
        except requests.RequestException as e:
            return "UNSURE", f"the booking request didn't come back ({e}). Check the OP app."
        body = r.text[:200]
        if r.status_code == 200:
            return "BOOKED", f"sent {lag:.0f} ms after the window opened (attempt {i})"
        if r.status_code == 401 and not relogged and any(c in body for c in SESSION_ERRORS):
            relogged = True
            op.login(force=True)
            continue
        if "already" in body.lower() and "full" not in body.lower():
            return "BOOKED", f"OP says you're already in: {body}"
        last = f"{r.status_code} {body}"
        time.sleep(gap)
    fresh = op.find_row(row)
    if fresh and (fresh.get("remaining_spots") or 0) <= 0:
        return "FULL", f"filled before we got in (OP's last reply: {last})"
    return "FAILED", last


def set_state(state, bid, row, **fields):
    with state_lock:
        entry = state.setdefault(bid, {"label": label(row), "start": row["booking_start_datetime"], "tries": 0})
        entry.update(fields, updated=now().isoformat(timespec="seconds"))


def settle(cfg, state, row, watch, result, detail):
    bid, name = row["booking_id"], label(row)
    if result == "BOOKED":
        set_state(state, bid, row, status="booked", detail=detail)
        log(f"BOOKED  {name}  ({detail})")
        notify(cfg, "Booked", name)
    elif result == "DRY_RUN":
        set_state(state, bid, row, status="dry_run", detail=detail)
        log(f"DRY RUN {name}  ({detail})")
    elif result == "UNSURE":
        set_state(state, bid, row, status="unsure", detail=detail)
        log(f"UNSURE  {name}  {detail}")
        notify(cfg, "Check the OP app", f"Not sure if this booked: {name}")
    elif result == "FULL":
        status = "watching" if watch else "full"
        set_state(state, bid, row, status=status, detail=detail)
        log(f"FULL    {name}  {'watching for a spot' if watch else 'not watching'}")
        notify(cfg, "Missed it", f"{name} is full{', watching for a cancellation' if watch else ''}")
    else:
        set_state(state, bid, row, status="retry", detail=detail)
        log(f"FAILED  {name}  {detail}")


def strike(op, cfg, state, row, watch, dry_run, cancel=None):
    """Sleep until the window, then book. Runs in its own thread."""
    opens = start_of(row) - WINDOW
    try:
        sleep_until(opens - timedelta(seconds=45))
        op.login()                                   # sign in early, off the critical path
        fresh = op.find_row(row) or row              # current pk/sk
        if fresh.get("booking_state") != "ACTIVE":
            set_state(state, row["booking_id"], row, status="cancelled", detail="OP cancelled the class")
            log(f"SKIP    {label(row)}  OP cancelled it")
            return
        sleep_until(opens - timedelta(seconds=4))
        if not op.is_signed_in():                    # also warms the connection
            op.login(force=True)
        sleep_until(opens + timedelta(milliseconds=cfg["fire_delay_ms"]))
        if cancel is not None and cancel.is_set():
            with state_lock:
                state.pop(row["booking_id"], None)
            log(f"REMOVED {label(row)}  taken off before the window opened")
            return
        result, detail = fire(op, fresh, opens, dry_run)
    except AuthError:
        raise
    except Exception as e:
        result, detail = "FAILED", f"{type(e).__name__}: {e}"
    settle(cfg, state, row, watch, result, detail)


def grab(op, cfg, state, row, watch, dry_run, cancel=None):
    """A spot showed up on a class we lost (or a strike that errored). One quick try."""
    try:
        op.login()
        result, detail = fire(op, row, start_of(row) - WINDOW, dry_run, attempts=2)
    except AuthError:
        raise
    except Exception as e:
        result, detail = "FAILED", f"{type(e).__name__}: {e}"
    settle(cfg, state, row, watch, result, detail)


# ---------------------------------------------------------------- commands

def make_op(cfg):
    if not cfg.get("email"):
        sys.exit("No login saved yet. Run: python3 opsniper.py setup")
    return OP(cfg["email"], get_password(cfg["email"]))


def cmd_setup(args):
    try:
        import keyring
    except ImportError:
        sys.exit("Install the requirements first: python3 -m pip install -r requirements.txt")
    cfg = load_config()
    print("Use the email and password you sign in with on oneplayground.com.au (not your member number).")
    hint = f" [{cfg['email']}]" if cfg.get("email") else ""
    email = input(f"Email{hint}: ").strip() or cfg.get("email")
    if not email:
        sys.exit("Need an email.")
    pw = getpass.getpass("Password (saved in your system's password store, not in a file; typing won't show): ")
    keyring.set_password(KEYCHAIN_SERVICE, email, pw)
    cfg["email"] = email
    save_json(CONFIG_FILE, cfg)
    print("Saved. Testing it now...\n")
    cmd_check(args)


def cmd_check(args):
    op = make_op(load_config())
    print("1. timetable API:", "ok" if op.centers() else "no centers returned")
    try:
        op.login(force=True)
    except AuthError as e:
        sys.exit(f"2. sign-in: {e}")
    print("2. sign-in: ok, got a session cookie")
    print("3. session sticks:", "yes" if op.is_signed_in() else "NO (bookings would be rejected)")
    op.logout()
    print("4. signed out. Nothing was booked.")


def cmd_classes(args):
    op = OP()
    centers = op.centers()
    ids = [centers[resolve_location(args.location, centers)]] if args.location else list(centers.values())
    rows, errors = fetch_rows(op, ids, args.days)
    if args.search:
        needle = args.search.lower()
        rows = [r for r in rows if needle in f"{class_name(r)} {r.get('activity_group_name')} {r.get('instructors')}".lower()]
    t = now()
    for r in rows:
        st = start_of(r)
        if st < t:
            continue
        opens = st - WINDOW
        when = "open now" if opens <= t else f"opens {opens:%a} {fmt(opens, False)}"
        spots = f"{r.get('remaining_spots')}/{r.get('class_capacity')}"
        print(f"{st:%a %d %b} {fmt(st, False):>8}  {r.get('center_name', ''):<13} {class_name(r)[:28]:<28} "
              f"{(r.get('instructors') or '')[:18]:<18} {spots:>6}  {when:<20} {r['booking_id']}")
    for e in errors:
        print("  (couldn't load", e + ")")


def queue_add(row, watch, cfg):
    if start_of(row) - now() < timedelta(hours=cfg["min_lead_hours"]):
        raise ValueError(f"That class is under {cfg['min_lead_hours']}h away, inside the late-cancel fee zone. Book it in the OP app if you're sure.")
    queue = [q for q in load_json(QUEUE_FILE, []) if q["booking_id"] != row["booking_id"]]
    queue.append({"booking_id": row["booking_id"], "location": row["center_name"],
                  "start": row["booking_start_datetime"], "label": label(row), "watch": watch})
    save_json(QUEUE_FILE, queue)


def cmd_add(args):
    op = OP()
    centers = op.centers()
    rows, _ = fetch_rows(op, list(centers.values()), 14)
    row = next((r for r in rows if r["booking_id"] == args.booking_id), None)
    if not row:
        sys.exit("Couldn't find that booking ID in the next 14 days. Copy it from `classes`.")
    try:
        queue_add(row, not args.no_watch, load_config())
    except ValueError as e:
        sys.exit(str(e))
    opens = start_of(row) - WINDOW
    print(f"Queued: {label(row)}")
    print("Window is already open; `run` will try straight away." if opens <= now()
          else f"Window opens {fmt(opens, seconds=True)}. Have `run` going before then.")


def cmd_remove(args):
    queue = load_json(QUEUE_FILE, [])
    kept = [q for q in queue if q["booking_id"] != args.booking_id]
    save_json(QUEUE_FILE, kept)
    print("Removed." if len(kept) < len(queue) else "That ID wasn't queued (rule matches are controlled by rules.json).")


def cmd_status(args):
    queue, state = load_json(QUEUE_FILE, []), load_json(STATE_FILE, {})
    rules = load_json(RULES_FILE, [])
    print(f"{len(rules)} weekly rule(s) in rules.json, {len(queue)} one-off(s) queued.\n")
    for q in queue:
        print(f"  queued   {q['label']}  ({q['booking_id']})")
    for bid, s in sorted(state.items(), key=lambda kv: kv[1].get("start", "")):
        print(f"  {s.get('status', '?'):<8} {s['label']}  {s.get('detail') or ''}")


FINAL = ("booked", "dry_run", "unsure", "full", "cancelled", "expired", "gave_up")


class Sniper:
    """The watch loop. Used by `run` on its own and by `web` in the background."""

    def __init__(self, cfg, op, dry_run=False):
        self.cfg, self.op, self.dry_run = cfg, op, dry_run
        self.centers = op.centers()
        self.state = load_json(STATE_FILE, {})
        self.workers, self.cancels, self.announced = {}, {}, set()
        self.targets = {}
        self.error = None
        self.last_tick = None

    def tick(self):
        cfg, state = self.cfg, self.state
        rules = load_rules(self.centers)          # re-read every tick, so edits apply live
        t = now()
        queue = [q for q in load_json(QUEUE_FILE, [])
                 if datetime.strptime(q["start"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=SYD) > t]
        save_json(QUEUE_FILE, queue)
        wanted = {r["location"] for r in rules} | {q["location"] for q in queue}
        if not wanted and "idle" not in self.announced:
            log("Nothing to do yet: pick a class to snipe.")
            self.announced.add("idle")
        rows, errors = fetch_rows(self.op, [self.centers[n] for n in wanted if n in self.centers],
                                  cfg["lookahead_days"])
        for e in errors:
            log(f"timetable hiccup: {e}")

        self.targets = collect_targets(rows, rules, queue)
        for bid, ev in self.cancels.items():      # removed while a strike was waiting
            if bid not in self.targets:
                ev.set()

        for bid, (row, watch) in self.targets.items():
            if bid in self.workers and self.workers[bid].is_alive():
                continue
            s = state.get(bid, {})
            status = s.get("status")
            if status in FINAL:
                continue
            start, opens = start_of(row), start_of(row) - WINDOW
            if t >= start - timedelta(hours=cfg["min_lead_hours"]):
                set_state(state, bid, row, status="expired",
                          detail=f"inside the {cfg['min_lead_hours']}h late-cancel margin")
                log(f"STOP    {label(row)}  too close to class time to book safely")
                continue
            if s.get("tries", 0) >= MAX_TRIES_PER_CLASS:
                set_state(state, bid, row, status="gave_up", detail="5 tries used")
                log(f"GAVE UP {label(row)}")
                continue

            job = None
            if status in ("watching", "retry"):
                if (row.get("remaining_spots") or 0) > 0:
                    log(f"SPOT    {label(row)} has {row['remaining_spots']} free, grabbing")
                    job = grab
            elif opens - t <= timedelta(seconds=cfg["tick_seconds"] + 90):
                job = strike
            elif status != "armed":
                set_state(state, bid, row, status="armed", detail=f"opens {fmt(opens)}")
                log(f"ARMED   {label(row)}  window opens {fmt(opens, seconds=True)}")

            self.announced.add(bid)
            if job:
                with state_lock:
                    state.setdefault(bid, {"label": label(row), "start": row["booking_start_datetime"], "tries": 0})
                    state[bid]["tries"] = state[bid].get("tries", 0) + 1
                if job is strike:
                    set_state(state, bid, row, status="striking", detail=f"fires at {fmt(opens, seconds=True)}")
                cancel = self.cancels[bid] = threading.Event()
                th = threading.Thread(target=job, args=(self.op, cfg, state, row, watch, self.dry_run, cancel),
                                      daemon=True)
                th.start()
                self.workers[bid] = th
        self.last_tick = now()
        self.save()

    def save(self):
        with state_lock:
            save_json(STATE_FILE, self.state)

    def loop(self, stop=None):
        stop = stop or threading.Event()
        log(f"Sniper running{' (DRY RUN, nothing will be booked)' if self.dry_run else ''}.")
        while not stop.is_set():
            try:
                self.tick()
            except AuthError as e:
                self.error = str(e)
                log(self.error)
                notify(self.cfg, "OP Sniper stopped", "Sign-in was rejected")
                return
            except SystemExit as e:               # a bad rules.json line
                self.error = str(e)
                log(self.error)
            except Exception as e:
                log(f"tick error: {type(e).__name__}: {e}")
            for _ in range(self.cfg["tick_seconds"]):
                if stop.wait(1):
                    break
            self.save()


def cmd_run(args):
    cfg = load_config()
    keep_awake()
    sniper = Sniper(cfg, make_op(cfg), args.dry_run)
    print("Ctrl+C to stop.")
    sniper.loop()
    if sniper.error:
        sys.exit(1)


# ---------------------------------------------------------------- dashboard

PORT = 8765
DASHBOARD = HERE / "dashboard.html"
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def iso(naive_text):
    return datetime.strptime(naive_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SYD).isoformat()


class Dashboard:
    def __init__(self, sniper):
        self.sniper = sniper
        self.cache, self.seen = {}, {}            # (center, days) -> (time, rows); booking_id -> row
        self.lock = threading.Lock()

    def classes(self, location, days):
        centers = self.sniper.centers
        ids = [centers[resolve_location(location, centers)]] if location else list(centers.values())
        out = []
        for cid in ids:
            with self.lock:
                hit = self.cache.get((cid, days))
            if hit and time.time() - hit[0] < 30:
                rows = hit[1]
            else:
                rows, _ = fetch_rows(self.sniper.op, [cid], days)
                with self.lock:
                    self.cache[(cid, days)] = (time.time(), rows)
            out += rows
        with self.lock:
            self.seen.update({r["booking_id"]: r for r in out})
        return sorted(out, key=lambda r: (start_of(r), r.get("center_name", "")))

    def row(self, bid):
        with self.lock:
            hit = self.seen.get(bid)
        if not hit:
            raise ValueError("Refresh the timetable and try again.")
        return hit

    def rules(self):
        try:
            return load_rules(self.sniper.centers)
        except SystemExit:
            return []

    def class_json(self, r, queued, state, rules):
        st = start_of(r)
        return {
            "booking_id": r["booking_id"], "start": st.isoformat(), "opens": (st - WINDOW).isoformat(),
            "name": class_name(r), "group": r.get("activity_group_name"), "instructor": r.get("instructors"),
            "location": r.get("center_name"), "spots": r.get("remaining_spots"), "capacity": r.get("class_capacity"),
            "queued": r["booking_id"] in queued, "weekly": any(matches(rule, r) for rule in rules),
            "status": (state.get(r["booking_id"]) or {}).get("status"),
        }

    def status(self):
        sn = self.sniper
        queue = load_json(QUEUE_FILE, [])
        queued = {q["booking_id"] for q in queue}
        with state_lock:
            state = json.loads(json.dumps(sn.state))
        items = {}
        for q in queue:
            items[q["booking_id"]] = {"booking_id": q["booking_id"], "label": q["label"], "start": iso(q["start"]),
                                      "kind": "one-off", "status": "queued", "detail": ""}
        for bid, (row, _) in sn.targets.items():
            items.setdefault(bid, {"booking_id": bid, "label": label(row), "start": start_of(row).isoformat(),
                                   "kind": "weekly", "status": "queued", "detail": ""})
        recent = now() - timedelta(days=3)
        for bid, s in state.items():
            if bid in items:
                items[bid].update(status=s.get("status"), detail=s.get("detail") or "")
            elif s.get("status") in FINAL and s.get("start") and datetime.fromisoformat(iso(s["start"])) > recent:
                items[bid] = {"booking_id": bid, "label": s["label"], "start": iso(s["start"]),
                              "kind": "done", "status": s["status"], "detail": s.get("detail") or ""}
        for it in items.values():
            it["opens"] = (datetime.fromisoformat(it["start"]) - WINDOW).isoformat()
        rules = load_json(RULES_FILE, [])
        return {
            "now": now().isoformat(), "dry_run": sn.dry_run, "error": sn.error,
            "running": sn.error is None, "last_tick": sn.last_tick.isoformat() if sn.last_tick else None,
            "min_lead_hours": sn.cfg["min_lead_hours"],
            "targets": sorted(items.values(), key=lambda i: i["start"]),
            "rules": [{"index": i, **{k: v for k, v in r.items() if not k.startswith("_")}} for i, r in enumerate(rules)],
            "log": list(LOG_LINES)[-60:],
        }, queued, state

    def add_rule(self, body):
        if body.get("booking_id"):
            r = self.row(body["booking_id"])
            st = start_of(r)
            rule = {"location": r["center_name"], "day": DAY_NAMES[st.weekday()][:3], "time": fmt(st, False),
                    "class": class_name(r), "watch": True}
        else:
            rule = {k: (body.get(k) or "").strip() for k in ("location", "day", "time", "class", "instructor")}
            rule["watch"] = bool(body.get("watch", True))
            if not rule["instructor"]:
                rule.pop("instructor")
            if not all(rule.get(k) for k in ("location", "day", "time", "class")):
                raise ValueError("Fill in the gym, day, time and class.")
            parse_time(rule["time"])
            if rule["day"][:3].lower() not in DAYS:
                raise ValueError(f"{rule['day']} isn't a day of the week.")
            try:
                rule["location"] = resolve_location(rule["location"], self.sniper.centers)
            except SystemExit as e:
                raise ValueError(str(e))
        rules = load_json(RULES_FILE, [])
        same = lambda a: (a["location"].lower(), a["day"][:3].lower(), parse_time(a["time"]), a["class"].lower())
        if any(same(x) == same(rule) for x in rules):
            raise ValueError("That weekly class is already set up.")
        rules.append(rule)
        save_json(RULES_FILE, rules)
        log(f"WEEKLY  {rule['class']}, {rule['location']}, {rule['day']} {rule['time']} added")
        return rule


def make_handler(dash):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj=None, body=None, ctype="application/json; charset=utf-8"):
            data = body if body is not None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _local(self):
            # Only answer this computer, and refuse pages from other sites poking at it.
            return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

        def _guard_write(self):
            if not self._local() or self.headers.get("X-OP-Sniper") != "1":
                self._send(403, {"error": "Not allowed."})
                return False
            return True

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if not self._local():
                return self._send(403, {"error": "Not allowed."})
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/":
                    return self._send(200, body=DASHBOARD.read_bytes(), ctype="text/html; charset=utf-8")
                if url.path == "/api/centers":
                    return self._send(200, sorted(dash.sniper.centers))
                if url.path == "/api/status":
                    return self._send(200, dash.status()[0])
                if url.path == "/api/classes":
                    days = max(1, min(int(q.get("days", 7)), 14))
                    rows = dash.classes(q.get("location"), days)
                    needle = (q.get("search") or "").lower()
                    if needle:
                        rows = [r for r in rows if needle in f"{class_name(r)} {r.get('activity_group_name')} {r.get('instructors')}".lower()]
                    _, queued, state = dash.status()
                    t = now()
                    rules = dash.rules()
                    return self._send(200, [dash.class_json(r, queued, state, rules) for r in rows if start_of(r) > t])
                return self._send(404, {"error": "Not found."})
            except SystemExit as e:
                return self._send(400, {"error": str(e)})
            except requests.RequestException as e:
                return self._send(502, {"error": f"Couldn't reach One Playground: {e}"})

        def do_POST(self):
            if not self._guard_write():
                return
            try:
                body = self._body()
                if self.path == "/api/queue":
                    row = dash.row(body.get("booking_id", ""))
                    queue_add(row, bool(body.get("watch", True)), dash.sniper.cfg)
                    log(f"QUEUED  {label(row)}")
                    return self._send(200, {"ok": True, "message": f"Sniping {class_name(row)}."})
                if self.path == "/api/rules":
                    rule = dash.add_rule(body)
                    day = next((d for d in DAY_NAMES if d[:3].lower() == rule["day"][:3].lower()), rule["day"])
                    return self._send(200, {"ok": True, "message": f"Booking {rule['class']} every {day}."})
                return self._send(404, {"error": "Not found."})
            except ValueError as e:
                return self._send(400, {"error": str(e)})

        def do_DELETE(self):
            if not self._guard_write():
                return
            parts = self.path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "queue"]:
                queue = load_json(QUEUE_FILE, [])
                save_json(QUEUE_FILE, [q for q in queue if q["booking_id"] != parts[2]])
                return self._send(200, {"ok": True, "message": "Removed."})
            if len(parts) == 3 and parts[:2] == ["api", "rules"] and parts[2].isdigit():
                rules = load_json(RULES_FILE, [])
                i = int(parts[2])
                if i < len(rules):
                    gone = rules.pop(i)
                    save_json(RULES_FILE, rules)
                    log(f"WEEKLY  {gone.get('class')}, {gone.get('location')}, {gone.get('day')} removed")
                return self._send(200, {"ok": True, "message": "Weekly class removed."})
            return self._send(404, {"error": "Not found."})

    return Handler


def cmd_web(args):
    cfg = load_config()
    keep_awake()
    sniper = Sniper(cfg, make_op(cfg), args.dry_run)
    threading.Thread(target=sniper.loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), make_handler(Dashboard(sniper)))
    url = f"http://127.0.0.1:{PORT}"
    print(f"Dashboard: {url}  (keep this window open; Ctrl+C to stop)")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()


# ---------------------------------------------------------------- GitHub (cloud) mode

STAY_MINUTES = 75          # stay running if a window opens within this; covers late cron starts
MAX_RUN = timedelta(hours=5, minutes=30)
VAR_LIMIT = 46000          # GitHub caps a variable at 48 KB


class GHVars:
    """Private storage: GitHub Actions repository variables. Only readable with the token."""

    def __init__(self, repo, token):
        self.base = f"https://api.github.com/repos/{repo}/actions/variables"
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28"})

    def get(self, name, default):
        r = self.s.get(f"{self.base}/{name}", timeout=15)
        if r.status_code == 404:
            return default
        r.raise_for_status()
        try:
            return json.loads(r.json()["value"])
        except (ValueError, KeyError):
            return default

    def put(self, name, obj):
        value = json.dumps(obj, separators=(",", ":"))
        r = self.s.patch(f"{self.base}/{name}", json={"name": name, "value": value}, timeout=15)
        if r.status_code == 404:
            r = self.s.post(self.base, json={"name": name, "value": value}, timeout=15)
        if r.status_code >= 300:
            raise RuntimeError(f"saving {name} failed: {r.status_code} {r.text[:120]}")


def timetable_rows(op, centers, gyms, days=14):
    ids = [centers[g] for g in gyms]
    rows, _ = fetch_rows(op, ids, days)
    t = now()
    out = [[r["booking_id"], start_of(r).isoformat(), class_name(r), r.get("activity_group_name") or "",
            r.get("instructors") or "", r.get("center_name") or "", r.get("remaining_spots"), r.get("class_capacity")]
           for r in rows if start_of(r) > t]
    while out and len(json.dumps(out, separators=(",", ":"))) > VAR_LIMIT:
        cut = out[-1][1][:10]                 # drop the furthest day until it fits
        out = [r for r in out if r[1][:10] != cut]
    return out


def cmd_cloud(args):
    repo, token = os.getenv("GITHUB_REPOSITORY"), os.getenv("GH_TOKEN")
    email, password = os.getenv("OP_EMAIL"), os.getenv("OP_PASSWORD")
    missing = [n for n, v in (("GH_TOKEN", token), ("OP_EMAIL", email), ("OP_PASSWORD", password),
                              ("GITHUB_REPOSITORY", repo)) if not v]
    if missing:
        sys.exit(f"Missing repository secret(s): {', '.join(missing)}")
    gh = GHVars(repo, token)
    began = now()

    blob = gh.get("OP_STATE", {})
    LOG_LINES.extend(blob.get("log", []))
    save_json(STATE_FILE, blob.get("state", {}))

    def pull_plan():
        plan = gh.get("OP_PLAN", {})
        save_json(QUEUE_FILE, plan.get("queue", []))
        save_json(RULES_FILE, plan.get("rules", []))
        return plan

    plan = pull_plan()
    cfg = {**DEFAULT_CONFIG, "notifications": False, "tick_seconds": 60}
    op = OP(email, password)
    sniper = Sniper(cfg, op, args.dry_run)
    sniper.announced.add("idle")              # don't log "nothing to do" every 15 minutes
    last_timetable = None

    def push():
        with state_lock:
            state = json.loads(json.dumps(sniper.state))
        cutoff = (now() - timedelta(days=4)).strftime("%Y-%m-%d")
        state = {k: v for k, v in state.items() if v.get("start", "9")[:10] >= cutoff}
        out = {"state": state, "log": list(LOG_LINES)[-40:], "last_run": now().isoformat(),
               "error": sniper.error, "dry_run": args.dry_run}
        while len(json.dumps(out)) > VAR_LIMIT and out["log"]:
            out["log"] = out["log"][5:]
        gh.put("OP_STATE", out)

    def busy():
        if any(w.is_alive() for w in sniper.workers.values()):
            return True
        t = now()
        for bid, (row, _) in sniper.targets.items():
            st = sniper.state.get(bid, {}).get("status")
            if st in FINAL or st in ("watching", "retry"):
                continue
            if timedelta(minutes=-2) < (start_of(row) - WINDOW) - t <= timedelta(minutes=STAY_MINUTES):
                return True
        return False

    while True:
        try:
            sniper.tick()
        except AuthError as e:
            sniper.error = str(e)
            log(f"STOPPED {e}")
        except SystemExit as e:
            sniper.error = str(e)
            log(f"PROBLEM {e}")
        except Exception as e:
            log(f"tick error: {type(e).__name__}: {e}")
        if last_timetable is None or now() - last_timetable > timedelta(minutes=10):
            try:
                gyms = [g for g in (plan.get("gyms") or ["Zetland"]) if g in sniper.centers] or [next(iter(sniper.centers))]
                gh.put("OP_TIMETABLE", {"updated": now().isoformat(), "gyms": gyms,
                                        "rows": timetable_rows(op, sniper.centers, gyms)})
                last_timetable = now()
            except Exception as e:
                log(f"timetable error: {type(e).__name__}: {e}")
        push()
        if sniper.error or not busy() or now() - began > MAX_RUN:
            break
        time.sleep(cfg["tick_seconds"])
        plan = pull_plan()                    # pick up changes made on the website

    for w in sniper.workers.values():         # let any strike in flight finish
        w.join(timeout=120)
    push()
    if sniper.error:
        sys.exit(1)


def main():
    p = argparse.ArgumentParser(description="Book One Playground classes the instant they open.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="save your OP login")
    sub.add_parser("check", help="test the login without booking")
    c = sub.add_parser("classes", help="browse the timetable with booking IDs")
    c.add_argument("-l", "--location")
    c.add_argument("-s", "--search", help="class, type or instructor contains this")
    c.add_argument("-d", "--days", type=int, default=4)
    a = sub.add_parser("add", help="queue a one-off class by booking ID")
    a.add_argument("booking_id")
    a.add_argument("--no-watch", action="store_true", help="don't wait for a cancellation if you miss it")
    r = sub.add_parser("remove", help="take a queued class off")
    r.add_argument("booking_id")
    sub.add_parser("status", help="what's armed, booked, watching")
    run = sub.add_parser("run", help="start the sniper")
    run.add_argument("--dry-run", action="store_true", help="do everything except send the booking")
    cl = sub.add_parser("cloud", help="one GitHub Actions run (used by the workflow)")
    cl.add_argument("--dry-run", action="store_true")
    web = sub.add_parser("web", help="start the sniper with a dashboard in your browser")
    web.add_argument("--dry-run", action="store_true", help="do everything except send the booking")
    args = p.parse_args()
    try:
        {"setup": cmd_setup, "check": cmd_check, "classes": cmd_classes, "add": cmd_add,
         "remove": cmd_remove, "status": cmd_status, "run": cmd_run, "web": cmd_web, "cloud": cmd_cloud}[args.cmd](args)
    except KeyboardInterrupt:
        print("\nStopped.")
    except requests.RequestException as e:
        sys.exit(f"Couldn't reach One Playground: {e}")


if __name__ == "__main__":
    main()
