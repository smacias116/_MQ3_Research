import os
import subprocess
import threading
import _thread
import time
import csv
import re
import sys
from datetime import datetime
import pandas as pd

target_device = "192.168.0.240:5555"
SAMPLE_INTERVAL = 1.0      # seconds between samples
MIN_WINDOW_S = 10          # minimum window between charge-counter changes
EXTRA_ROWS_AFTER_STOP = 30 # rows still recorded after the first Ctrl+C

STATUS_MAP = {1: "Unknown", 2: "Charging", 3: "Discharging", 4: "Not charging", 5: "Full"}
HEALTH_MAP = {1: "Unknown", 2: "Good", 3: "Overheat", 4: "Dead", 5: "Over voltage",
              6: "Unspecified failure", 7: "Cold"}
PLAYBACK_MAP = {0: "None", 1: "Stopped", 2: "Paused", 3: "Playing", 4: "Fast-forwarding",
                5: "Rewinding", 6: "Buffering", 7: "Error", 8: "Connecting",
                9: "Skip to previous", 10: "Skip to next", 11: "Skip to queue item"}

VIDEO_COLUMNS = ["Foreground App", "Media App", "Video Title", "Title Source",
                 "Playback State", "Video Clock", "Video Session",
                 "Video Position (s)", "Video Position (mm:ss)", "Position Source"]

DEVICE_COLUMNS = ["Volume (index)", "Volume Max", "Volume (%)",
                  "Brightness (0-255)", "Brightness (%)", "Brightness Mode"]

# Columns moved right after "Charge Counter (uAh)" in the processed file
CALC_COLUMNS = ["Calculated Current (mA)", "Calculated Power (mW)",
                "Accumulated Power (mWh)", "Accumulated Current (mAh)"]

# One single adb call returns everything (sections are split by @@NAME@@ markers)
REMOTE_CMD = (
    "dumpsys battery; "
    "echo @@MEDIA@@; dumpsys media_session; "
    "echo @@AUDIO@@; dumpsys audio | grep 'state:started'; "
    "echo @@FOCUS@@; dumpsys window | grep mCurrentFocus; "
    "echo @@VOLUME@@; cmd media_session volume --stream 3 --get; "
    "echo @@BRIGHT@@; settings get system screen_brightness; "
    "settings get system screen_brightness_mode; "
    "echo @@UPTIME@@; cat /proc/uptime"
)


# ---------- helpers ----------
def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return "N/A"

def ua_to_ma(v):
    n = to_int(v)
    return round(n / 1000.0, 2) if n != "N/A" else "N/A"

def temp_c(v):
    n = to_int(v)
    return round(n / 10.0, 1) if n != "N/A" else "N/A"

def fmt_mmss(secs):
    return f"{int(secs // 60):02d}:{int(secs % 60):02d}"

# dumpsys key (lowercase) -> (CSV column name, converter)
KNOWN = [
    ("level",                "Level (%)",                  to_int),
    ("voltage",              "Voltage (mV)",               to_int),
    ("temperature",          "Temp (C)",                   temp_c),
    ("charge counter",       "Charge Counter (uAh)",       to_int),
    ("status",               "Status",                     to_int),
    ("health",               "Health",                     to_int),
    ("present",              "Present",                    str),
    ("scale",                "Scale",                      to_int),
    ("ac powered",           "AC Powered",                 str),
    ("usb powered",          "USB Powered",                str),
    ("wireless powered",     "Wireless Powered",           str),
    ("dock powered",         "Dock Powered",               str),
    ("max charging current", "Max Charging Current (mA)",  ua_to_ma),
    ("max charging voltage", "Max Charging Voltage (mV)",  ua_to_ma),
    ("weak charger",         "Weak Charger",               str),
    ("technology",           "Technology",                 str),
]
KNOWN_KEYS = {k for k, _, _ in KNOWN}


def parse_battery(text):
    data = {}
    for line in text.splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            key, val = key.strip(), val.strip()
            if key and val:
                data[key.lower()] = (key, val)
    return data


def parse_media(text):
    """Parse `dumpsys media_session`. Returns the preferred session (YouTube first)
    as a dict, or None."""
    sessions = []
    cur = None
    pending_uid = None
    for line in text.splitlines():
        m = re.search(r"ownerPid=\d+, ownerUid=(\d+)", line)
        if m:
            pending_uid = int(m.group(1))
            continue
        m = re.match(r"^\s*package=(\S+)", line)
        if m:
            cur = {"package": m.group(1), "uid": pending_uid, "state": None,
                   "position": None, "speed": 1.0, "updated": None, "title": None}
            sessions.append(cur)
            pending_uid = None
            continue
        if cur is None:
            continue

        m = re.search(r"PlaybackState \{(.*)\}", line)
        if m:
            inner = m.group(1)
            s = re.search(r"^state=(?:[A-Za-z_]+\()?(\d+)", inner)
            p = re.search(r"(?<!buffered )position=(-?\d+)", inner)
            sp = re.search(r"speed=(-?[\d.]+)", inner)
            u = re.search(r"updated=(\d+)", inner)
            cur["state"] = int(s.group(1)) if s else None
            cur["position"] = int(p.group(1)) if p else None
            cur["speed"] = float(sp.group(1)) if sp else 1.0
            cur["updated"] = int(u.group(1)) if u else None
            continue

        m = re.search(r"metadata:.*?description=(.*)$", line)
        if m:
            title = re.sub(r"(,\s*null)+$", "", m.group(1).strip()).strip()
            cur["title"] = None if title in ("", "null") else title

    if not sessions:
        return None
    chosen = next((s for s in sessions if "youtube" in s["package"].lower()), None)
    if chosen is None:
        chosen = next((s for s in sessions if s["title"]), None)
    return chosen


def parse_started_uids(text):
    """UIDs that currently have an AudioTrack in state:started."""
    return {int(m.group(1)) for m in
            re.finditer(r"AudioTrack u/pid:(\d+)/\d+ state:started", text)}


def parse_focus(text):
    m = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([\w.]+)/", text)
    return m.group(1) if m else "N/A"


def parse_device_settings(volume_txt, bright_txt):
    """Volume (media stream) and screen brightness. Missing values become N/A."""
    out = {c: "N/A" for c in DEVICE_COLUMNS}

    # e.g. "volume is 8 in range [0..15]"
    m = re.search(r"volume is (\d+) in range \[(\d+)\.\.(\d+)\]", volume_txt)
    if m:
        cur, lo, hi = int(m.group(1)), int(m.group(2)), int(m.group(3))
        out["Volume (index)"] = cur
        out["Volume Max"] = hi
        if hi > lo:
            out["Volume (%)"] = round((cur - lo) * 100.0 / (hi - lo), 1)

    # First line: screen_brightness (0-255), second line: screen_brightness_mode (0=manual, 1=auto)
    lines = [l.strip() for l in bright_txt.strip().splitlines() if l.strip()]
    if lines and lines[0].lstrip("-").isdigit():
        b = int(lines[0])
        out["Brightness (0-255)"] = b
        out["Brightness (%)"] = round(b * 100.0 / 255.0, 1)
    if len(lines) > 1 and lines[1].isdigit():
        out["Brightness Mode"] = "Auto" if lines[1] == "1" else "Manual"
    return out


def read_all():
    output = subprocess.check_output(
        ["adb", "-s", target_device, "shell", REMOTE_CMD], timeout=20
    ).decode("utf-8", errors="ignore")

    parts = re.split(r"@@(\w+)@@", output)
    sections = {"BATTERY": parts[0]}
    for i in range(1, len(parts) - 1, 2):
        sections[parts[i]] = parts[i + 1]

    uptime_ms = None
    try:
        uptime_ms = float(sections.get("UPTIME", "").split()[0]) * 1000.0
    except (IndexError, ValueError):
        pass

    return {
        "battery": parse_battery(sections["BATTERY"]),
        "media": parse_media(sections.get("MEDIA", "")),
        "audio_uids": parse_started_uids(sections.get("AUDIO", "")),
        "focus": parse_focus(sections.get("FOCUS", "")),
        "device": parse_device_settings(sections.get("VOLUME", ""), sections.get("BRIGHT", "")),
        "uptime_ms": uptime_ms,
    }


# ---------- manual video clock (controlled with any key) ----------
class VideoClock:
    """1st key press: clock starts at 0. 2nd key press: clock stops.
    A 3rd press starts a new video session (clock back to 0), and so on."""
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "Idle"      # Idle -> Running -> Stopped -> Running ...
        self.session = 0
        self.start = None

    def press(self):
        with self.lock:
            now = time.monotonic()
            if self.state == "Running":
                self.state = "Stopped"
                return "stopped", now - self.start, self.session
            self.session += 1
            self.state = "Running"
            self.start = now
            return "started", 0.0, self.session

    def snapshot(self):
        with self.lock:
            secs = (time.monotonic() - self.start) if self.state == "Running" else None
            return self.state, self.session, secs


def _handle_key(clock):
    action, secs, session = clock.press()
    if action == "started":
        print(f"\n>>> VIDEO CLOCK STARTED (video session {session}). "
              f"Press any key again when the video ends.")
    else:
        print(f"\n>>> VIDEO CLOCK STOPPED at {fmt_mmss(secs)} (video session {session}). "
              f"Press any key to start a new video.")


def key_listener(clock, stop_event):
    """Background thread: any key press toggles the video clock."""
    if os.name == "nt":
        import msvcrt
        while not stop_event.is_set():
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch == "\x03":              # Ctrl+C delivered as a character
                    _thread.interrupt_main()
                    continue
                if ch in ("\x00", "\xe0"):    # special key (arrows, F-keys): 2nd code follows
                    msvcrt.getwch()
                _handle_key(clock)
            else:
                time.sleep(0.03)
    else:
        try:
            import termios, tty, select
            if not sys.stdin.isatty():
                return
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
        except Exception:
            return
        try:
            tty.setcbreak(fd)
            while not stop_event.is_set():
                r, _, _ = select.select([sys.stdin], [], [], 0.05)
                if r:
                    sys.stdin.read(1)
                    _handle_key(clock)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def video_columns(info, clock):
    """Build the video-related columns from the adb data and the manual clock."""
    media = info["media"]
    out = {c: "N/A" for c in VIDEO_COLUMNS}
    out["Foreground App"] = info["focus"]

    state, session, clock_secs = clock.snapshot()
    out["Video Clock"] = state
    out["Video Session"] = session if state == "Running" else 0

    playing = None  # True / False / None (unknown)
    auto_valid = False
    if media:
        out["Media App"] = media["package"]
        if media["uid"] is not None:
            playing = media["uid"] in info["audio_uids"]
        auto_valid = media["state"] not in (None, 0)
        if media["title"]:
            out["Video Title"], out["Title Source"] = media["title"], "auto"

    # Playback state (from the media session if it is populated, else from audio activity)
    if auto_valid:
        out["Playback State"] = PLAYBACK_MAP.get(media["state"], "N/A")
        if media["state"] == 3:
            playing = True
    elif playing is True:
        out["Playback State"] = "Playing (audio active)"
    elif playing is False:
        out["Playback State"] = "Not playing"

    # Position: manual clock while running, otherwise automatic if the app publishes it
    secs = None
    if clock_secs is not None:
        secs, out["Position Source"] = clock_secs, "manual clock"
    elif auto_valid and media["position"] is not None and media["position"] >= 0:
        pos = media["position"]
        if media["state"] == 3 and media["updated"] is not None and info["uptime_ms"]:
            delta = info["uptime_ms"] - media["updated"]
            if 0 <= delta < 3600 * 1000:
                pos += delta * media["speed"]
        secs, out["Position Source"] = max(0.0, pos / 1000.0), "auto"

    if secs is not None:
        out["Video Position (s)"] = round(secs, 1)
        out["Video Position (mm:ss)"] = fmt_mmss(secs)
    return out


def build_row(info, video, extra_keys):
    data = info["battery"]
    row = {}
    for key, col, conv in KNOWN:
        row[col] = conv(data[key][1]) if key in data else "N/A"

    status, health = row.get("Status"), row.get("Health")
    row["Status (text)"] = STATUS_MAP.get(status, "N/A") if isinstance(status, int) else "N/A"
    row["Health (text)"] = HEALTH_MAP.get(health, "N/A") if isinstance(health, int) else "N/A"

    for k in extra_keys:
        row[data[k][0] if k in data else k] = data[k][1] if k in data else "N/A"

    row.update(info.get("device", {c: "N/A" for c in DEVICE_COLUMNS}))
    row.update(video)
    return row


# ---------- phase 1: live recording ----------
def record(raw_filename):
    clock = VideoClock()
    stop_event = threading.Event()
    listener = threading.Thread(target=key_listener, args=(clock, stop_event), daemon=True)
    listener.start()

    print(">>> Press any key when the video starts (press any key again when it ends).\n")

    start = time.monotonic()
    writer = None
    extra_keys = []
    row_num = 0
    stopping = False
    remaining = 0
    fails = 0

    with open(raw_filename, mode="w", newline="", encoding="utf-8") as f:
        while True:
            try:
                loop_start = time.monotonic()
                try:
                    info = read_all()
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                    fails += 1
                    print(f"[WARN] ADB read failed, skipping sample: {e}")
                    if stopping and fails >= 10:
                        print("Too many ADB failures while finishing, stopping.")
                        break
                    time.sleep(SAMPLE_INTERVAL)
                    continue
                fails = 0

                video = video_columns(info, clock)

                if writer is None:
                    extra_keys = [k for k in info["battery"] if k not in KNOWN_KEYS]
                    sample = build_row(info, video, extra_keys)
                    columns = ["id_line", "Timestamp", "Elapsed (s)"] + list(sample.keys())
                    writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
                    writer.writeheader()

                row_num += 1
                row = build_row(info, video, extra_keys)
                row["id_line"] = row_num
                row["Timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                row["Elapsed (s)"] = round(time.monotonic() - start, 2)
                writer.writerow(row)
                f.flush()

                tail = ""
                if stopping:
                    remaining -= 1
                    tail = f" | STOPPING: {remaining} rows left"
                print(f"#{row_num} [{row['Timestamp']}] Level: {row['Level (%)']}% | "
                      f"{row['Voltage (mV)']}mV | {row['Temp (C)']}C | "
                      f"Charge: {row['Charge Counter (uAh)']}uAh | {row['Status (text)']} | "
                      f"Vol: {row['Volume (index)']}/{row['Volume Max']} | "
                      f"Bright: {row['Brightness (%)']}% | "
                      f"App: {row['Foreground App'].split('.')[-1]} | "
                      f"{row['Playback State']} | "
                      f"Video clock: {row['Video Clock']} {row['Video Position (mm:ss)']}{tail}")

                if stopping and remaining <= 0:
                    break
                time.sleep(max(0.0, SAMPLE_INTERVAL - (time.monotonic() - loop_start)))

            except KeyboardInterrupt:
                if stopping:
                    print("\nSecond Ctrl+C received: stopping immediately.")
                    break
                stopping = True
                remaining = EXTRA_ROWS_AFTER_STOP
                print(f"\n\nStop requested. Recording {EXTRA_ROWS_AFTER_STOP} more rows "
                      f"(press Ctrl+C again to stop immediately)...")

    stop_event.set()
    listener.join(timeout=1.0)
    print(f"\nRecording finished ({row_num} rows). Raw data saved to: {raw_filename}")


# ---------- phase 2: post-processing ----------
def post_process(raw_filename, processed_filename):
    print("\nStarting post-processing time-alignment...")
    try:
        df = pd.read_csv(raw_filename)

        if len(df) < 2:
            print("Not enough data logged to perform post-processing.")
            return

        cc = "Charge Counter (uAh)"
        df[cc] = pd.to_numeric(df[cc], errors="coerce")

        change_indices = df.index[df[cc].diff() != 0].tolist()
        if 0 not in change_indices:
            change_indices.insert(0, 0)
        if (len(df) - 1) not in change_indices:
            change_indices.append(len(df) - 1)

        df["Calculated Current (mA)"] = 0.0
        df["Calculated Power (mW)"] = 0.0

        for i in range(1, len(change_indices)):
            start_idx = change_indices[i - 1]
            end_idx = change_indices[i]

            delta_charge = df.loc[start_idx, cc] - df.loc[end_idx, cc]
            delta_t = df.loc[end_idx, "Elapsed (s)"] - df.loc[start_idx, "Elapsed (s)"]

            if delta_t >= MIN_WINDOW_S and delta_charge > 0:
                avg_current = round((delta_charge * 3600 / delta_t) / 1000, 2)
                df.loc[start_idx:end_idx - 1, "Calculated Current (mA)"] = avg_current
                df.loc[start_idx:end_idx - 1, "Calculated Power (mW)"] = (
                    (df.loc[start_idx:end_idx - 1, "Voltage (mV)"] / 1000.0) * avg_current
                ).round(2)

        # Last row has no following window: carry the previous value forward
        last, prev = df.index[-1], df.index[-2]
        df.loc[last, "Calculated Current (mA)"] = df.loc[prev, "Calculated Current (mA)"]
        df.loc[last, "Calculated Power (mW)"] = df.loc[prev, "Calculated Power (mW)"]

        # Accumulated values, integrated over the real time between samples
        dt = df["Elapsed (s)"].diff().shift(-1)
        dt.iloc[-1] = dt.iloc[:-1].median()
        df["Accumulated Power (mWh)"] = (df["Calculated Power (mW)"] * dt / 3600.0).cumsum().round(3)
        df["Accumulated Current (mAh)"] = (df["Calculated Current (mA)"] * dt / 3600.0).cumsum().round(3)

        # Place the calculated columns right after "Charge Counter (uAh)"
        others = [c for c in df.columns if c not in CALC_COLUMNS]
        pos = others.index(cc) + 1
        df = df[others[:pos] + CALC_COLUMNS + others[pos:]]

        df.to_csv(processed_filename, index=False, encoding="utf-8")
        print(f"Post-processing complete! Perfectly aligned data saved to: {processed_filename}")

    except Exception as e:
        print(f"An error occurred during post-processing: {e}")


if __name__ == "__main__":
    session_timestamp = int(time.time())
    raw_filename = f"metrics_log_{session_timestamp}.csv"
    processed_filename = f"metrics_log_power_{session_timestamp}.csv"

    print("Starting Quest 3 Logger...")
    print(f"Target Device: {target_device}")
    print(f"Raw telemetry file: {raw_filename}\n")

    record(raw_filename)
    post_process(raw_filename, processed_filename)