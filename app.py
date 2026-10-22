import os
import re
import time as _time
import json
import math
import pandas as pd
import streamlit as st
import folium
import requests
from streamlit_folium import st_folium
from datetime import time, date
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from datetime import date as _date, datetime as _datetime
from decimal import Decimal
from collections import defaultdict

# Set page layout
st.set_page_config(page_title="SkyHigh Voyagers", layout="wide")

# --- Custom CSS for Menu Toggle ---
st.markdown("""
<style>
/* More breathing room between options */
[data-testid="stRadioGroup"] {
    gap: 4px !important;
}

/* Make the WHOLE row (not just the circle) a big, obvious tap target */
[data-testid="stRadioOption"] {
    padding: 14px 12px !important;
    min-height: 48px !important;
    border-radius: 10px !important;
    width: 100% !important;
    box-sizing: border-box !important;
    transition: background-color 0.15s ease !important;
}

[data-testid="stRadioOption"]:hover {
    background-color: rgba(150, 150, 150, 0.12) !important;
}

/* Bigger, bolder label text */
[data-testid="stRadioOption"] [data-testid="stMarkdownContainer"] p {
    font-size: 18px !important;
}

/* Bigger radio circle (outer ring) */
[data-testid="stRadioOption"] > div > div > div:first-child {
    width: 24px !important;
    height: 24px !important;
}

/* Bigger inner dot to match */
[data-testid="stRadioOption"] > div > div > div:first-child > div {
    width: 14px !important;
    height: 14px !important;
}
div[data-testid="stSidebarCollapseButton"],
[data-testid="stToolbar"] > div > div:first-child {
    width: auto !important;
    height: auto !important;
    min-width: 140px !important;
    overflow: visible !important;
}

div[data-testid="stSidebarCollapseButton"] button,
[data-testid="stToolbar"] > div > div:first-child button {
    width: auto !important;
    height: auto !important;
    min-height: 48px !important;
    padding: 12px 20px !important;
    display: flex !important;
    flex-direction: row !important;
    align-items: center !important;
    gap: 10px !important;
    background-color: rgba(150, 150, 150, 0.15) !important;
    border: 1px solid rgba(150, 150, 150, 0.25) !important;
    border-radius: 10px !important;
}

div[data-testid="stSidebarCollapseButton"] [data-testid="stIconMaterial"],
[data-testid="stToolbar"] > div > div:first-child [data-testid="stIconMaterial"] {
    font-size: 28px !important;
}

div[data-testid="stSidebarCollapseButton"] button::after,
[data-testid="stToolbar"] > div > div:first-child button::after {
    content: "Menu" !important;
    font-size: 18px !important;
    font-weight: 600 !important;
    color: inherit !important;
    white-space: nowrap !important;
}
</style>
""", unsafe_allow_html=True)

# --- Config / Secrets ---
def get_secret(name: str, default: str = "") -> str:
    """Read from environment variables first (Railway), then fall back to
    Streamlit secrets (local secrets.toml). Never raises if secrets.toml is missing."""
    value = os.getenv(name)
    if value:
        return value
    try:
        return st.secrets[name]
    except Exception:
        return default

# --- Database Connection (Railway Postgres) ---
@st.cache_resource
def init_pool() -> ConnectionPool:
    url = get_secret("DATABASE_URL")
    if not url:
        st.error("DATABASE_URL is not set. Add it as an environment variable (reference your Railway Postgres service).")
        st.stop()
    # check=ConnectionPool.check_connection transparently replaces dead connections
    return ConnectionPool(
        url,
        min_size=1,
        max_size=5,
        kwargs={"row_factory": dict_row, "autocommit": True},
        check=ConnectionPool.check_connection,
        open=True,
    )

pool = init_pool()

# --- IGC file storage (Railway volume) ---
# Mount a Railway Volume at /skyhigh_voyagers/storage (or override with STORAGE_ROOT).
# Layout: {STORAGE_ROOT}/task_{task_id}/date_{YYYY-MM-DD}/{safa_number}.igc
STORAGE_ROOT = get_secret("STORAGE_ROOT", "/skyhigh_voyagers/storage")


@st.cache_resource
def ensure_schema():
    """Adds any missing columns (safe to run every start):
       - flights.igc_path  : where the raw IGC file is stored
       - tasks.take_off    : name of the take-off site of the task"""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE flights ADD COLUMN IF NOT EXISTS igc_path TEXT")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS take_off TEXT")
    return True

ensure_schema()


def save_igc_file(task_id, flight_date: str, safa_number, igc_bytes: bytes) -> str:
    """Saves the raw IGC file to
        {STORAGE_ROOT}/task_{task_id}/date_{flight_date}/{safa_number}.igc
    and returns the full path. Re-uploading for the same pilot/task/date overwrites the file.
    All path parts are built from validated ints / ISO dates, so no user text reaches the path."""
    task_id = int(task_id)
    safa_number = int(safa_number)
    flight_date = _date.fromisoformat(str(flight_date)).isoformat()

    folder = os.path.join(STORAGE_ROOT, f"task_{task_id}", f"date_{flight_date}")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{safa_number}.igc")

    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as fh:
        fh.write(igc_bytes)
    os.replace(tmp_path, path)  # atomic: never leaves a half-written file
    return path


# --- Task image storage (Railway volume) ---
# Layout: {STORAGE_ROOT}/task_images/{task_hash}.png
# The picture comes from the XContest API. We keep a local copy so the gallery loads fast
# and doesn't fire dozens of API requests on every page view.
TASK_IMAGE_DIR = os.path.join(STORAGE_ROOT, "task_images")
_SAFE_HASH_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_IMAGE_RETRY_SECONDS = 300  # after a failed download, wait this long before trying again


def task_image_url(task_hash) -> str:
    return f"https://tools.xcontest.org/api/xctsk/image/{task_hash}"


def task_image_path(task_hash):
    """Local path of the saved picture, or None if the hash isn't safe to use in a file name."""
    h = str(task_hash or "").strip()
    if not _SAFE_HASH_RE.match(h):
        return None
    return os.path.join(TASK_IMAGE_DIR, f"{h}.png")


def task_image_exists(task_hash) -> bool:
    path = task_image_path(task_hash)
    return bool(path and os.path.exists(path))


def _looks_like_image(content: bytes, content_type: str) -> bool:
    if content.startswith(b"\x89PNG") or content.startswith(b"\xff\xd8"):
        return True
    return (content_type or "").lower().startswith("image/")


def download_task_image(task_hash) -> bool:
    """Downloads the task picture from XContest and stores it on the volume. Returns True on success."""
    path = task_image_path(task_hash)
    if not path:
        return False
    try:
        resp = requests.get(task_image_url(str(task_hash).strip()), timeout=8)
        if resp.status_code != 200 or not resp.content:
            return False
        if not _looks_like_image(resp.content, resp.headers.get("Content-Type", "")):
            return False
        os.makedirs(TASK_IMAGE_DIR, exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "wb") as fh:
            fh.write(resp.content)
        os.replace(tmp_path, path)  # atomic: never leaves a half-written file
        return True
    except Exception:
        return False


def delete_task_image(task_hash) -> None:
    path = task_image_path(task_hash)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@st.cache_resource
def _image_failures() -> dict:
    """task_hash -> time of the last failed download (shared by all sessions)."""
    return {}


def get_task_image(task_hash, force: bool = False):
    """Returns something st.image() can show: the local file if we have it (downloading it
    first if needed), otherwise the XContest URL as a fallback. None if there is no hash."""
    h = str(task_hash or "").strip()
    if not h:
        return None
    path = task_image_path(h)
    if path and (force or not os.path.exists(path)):
        failures = _image_failures()
        last_fail = failures.get(h)
        if force or last_fail is None or (_time.time() - last_fail) > _IMAGE_RETRY_SECONDS:
            if download_task_image(h):
                failures.pop(h, None)
            else:
                failures[h] = _time.time()
    if path and os.path.exists(path):
        return path
    return task_image_url(h)


def _clean_takeoff(value) -> str:
    """Trims and collapses whitespace so 'Mlolongo ' and 'Mlolongo' are the same take-off."""
    return " ".join(str(value).split()) if value else ""


def _task_label(t: dict) -> str:
    """Label used in task dropdowns: description, take-off (if set) and max score."""
    take_off = _clean_takeoff(t.get("take_off"))
    if take_off:
        return f"{t['description']} – {take_off} (Max Score: {t['max_score']})"
    return f"{t['description']} (Max Score: {t['max_score']})"


def _clean(value):
    """Convert Postgres types to the plain Python types the rest of the app expects."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (_datetime, _date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


class DBResult:
    """Mimics the old Supabase response so existing `.data` code keeps working."""
    def __init__(self, data):
        self.data = data


def db(sql: str, params=None) -> DBResult:
    """Run any SQL statement. SELECT / RETURNING queries give back a list of dicts in `.data`."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = [_clean(r) for r in cur.fetchall()] if cur.description else []
    return DBResult(rows)

# -------------------------------------------------------------------
# Helper Functions: Geodesic Calculations & Parsers
# -------------------------------------------------------------------
def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates the great-circle distance between two points in meters."""
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2)

    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


# --- IGC header parsing (tolerant of different loggers / apps) ---
# Date header variants seen in the wild:
#   HFDTE191026           classic IGC
#   HFDTE:191026          some loggers
#   HFDTEDATE:191026,00   XCTrack / newer IGC spec (",00" is the flight number of the day)
_DTE_RE = re.compile(r"^H[FOPS]?DTE(?:DATE)?\s*[:=]?\s*(\d{2})(\d{2})(\d{2})", re.IGNORECASE)
# A YYYY-MM-DD (or YYYYMMDD, YYYY_MM_DD, YYYY.MM.DD) date inside the file name
_FILENAME_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})(?!\d)")
# Pilot header variants: HFPLTPILOT:, HFPLTPILOTINCHARGE:, HFPLT:
_PILOT_RE = re.compile(r"^H[FOPS]?PLT(?:PILOT(?:INCHARGE)?)?\s*:?\s*(.*)$", re.IGNORECASE)


def extract_igc_date(igc_text, filename=None):
    """Returns (date | None, source).

    Tries, in order:
      1. the IGC header date record (all common variants above)
      2. a date embedded in the file name (e.g. 2026-10-19-XCT-MLO-00.igc)
    """
    for line in igc_text.splitlines():
        m = _DTE_RE.match(line.strip())
        if m:
            dd, mm, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
            year = 2000 + yy if yy < 70 else 1900 + yy
            try:
                return _date(year, mm, dd), "IGC header"
            except ValueError:
                pass  # corrupt header value, fall through to next source

    if filename:
        m = _FILENAME_DATE_RE.search(filename)
        if m:
            try:
                return _date(int(m.group(1)), int(m.group(2)), int(m.group(3))), "file name"
            except ValueError:
                pass
    return None, None


def parse_igc(igc_text, filename=None):
    """Parses IGC file B-records into a Pandas DataFrame.

    Returns (df, pilot_name, flight_date_iso_or_'Unknown', date_source).
    """
    records = []
    pilot_name = "Unknown"

    detected_date, date_source = extract_igc_date(igc_text, filename)
    date_str = detected_date.isoformat() if detected_date else "Unknown"

    for line in igc_text.splitlines():
        line = line.strip()

        if line.upper().startswith(("HFPLT", "HOPLT", "HPPLT")):
            m = _PILOT_RE.match(line)
            if m and m.group(1).strip():
                pilot_name = m.group(1).strip()

        if line.startswith("B") and len(line) >= 35:
            try:
                time_str = f"{line[1:3]}:{line[3:5]}:{line[5:7]}"

                # Parse Time object for validation logic
                h, m_, s = int(line[1:3]), int(line[3:5]), int(line[5:7])
                parsed_time = time(h, m_, s)

                lat_deg = int(line[7:9])
                lat_min = float(f"{line[9:11]}.{line[11:14]}")
                lat = lat_deg + (lat_min / 60.0)
                if line[14] == "S": lat = -lat

                lon_deg = int(line[15:18])
                lon_min = float(f"{line[18:20]}.{line[20:23]}")
                lon = lon_deg + (lon_min / 60.0)
                if line[23] == "W": lon = -lon

                records.append({
                    "time_str": time_str,
                    "time": parsed_time,
                    "lat": lat,
                    "lon": lon,
                })
            except Exception:
                continue
    return pd.DataFrame(records), pilot_name, date_str, date_source

def validate_task_flight(task_data: dict, igc_fixes: list) -> dict:
    """Validates IGC fixes sequentially against task turnpoints and calculates times/distances.

    Distance covered is credited two ways:
      1. Full leg credit for every turnpoint the pilot actually validated (as before).
      2. Partial credit for the leg currently in progress, based on the closest the
         pilot got to the next required turnpoint before the track ends (or they
         land out). This ensures pilots who don't finish the task still get an
         accurate "distance made" figure instead of 0, and — critically — this
         distance figure is completely independent of whether goal was reached.
         Reaching goal only affects the speed score (via flight_time_mins below).
    """
    turnpoints = task_data.get('turnpoints', [])
    sss_info = task_data.get('sss', {})
    goal_info = task_data.get('goal', {})

    goal_deadline = None
    if goal_info.get('deadline'):
        dl_str = goal_info['deadline'].replace('Z', '')
        h, m, s = map(int, dl_str.split(':'))
        goal_deadline = time(h, m, s)

    current_tp_idx = 0
    total_tps = len(turnpoints)
    validated_turnpoints = []
    was_inside_sss = False

    # Tracking for scores & times
    start_time = None
    end_time = None

    # Pre-calculate distances between each turnpoint leg
    leg_distances = []
    for i in range(1, total_tps):
        lat1, lon1 = float(turnpoints[i-1]['waypoint']['lat']), float(turnpoints[i-1]['waypoint']['lon'])
        lat2, lon2 = float(turnpoints[i]['waypoint']['lat']), float(turnpoints[i]['waypoint']['lon'])
        leg_distances.append(haversine_distance(lat1, lon1, lat2, lon2))

    total_task_distance = sum(leg_distances) if leg_distances else 1.0

    # Cumulative distance covered by the time each turnpoint index is validated
    # (cumulative_distance[i] = total distance flown once turnpoint i is reached).
    cumulative_distance = [0.0] * total_tps
    for i in range(1, total_tps):
        cumulative_distance[i] = cumulative_distance[i - 1] + leg_distances[i - 1]

    # Closest the pilot has gotten to the NEXT (not-yet-validated) turnpoint. Used to
    # award partial credit for the in-progress leg if the pilot never officially
    # validates it (landed out, missed the gate trigger, etc).
    closest_approach = None

    for fix in igc_fixes:
        if current_tp_idx >= total_tps: break

        tp = turnpoints[current_tp_idx]
        tp_type = tp.get('type', 'TURNPOINT')
        radius = float(tp['radius'])
        tp_lat = float(tp['waypoint']['lat'])
        tp_lon = float(tp['waypoint']['lon'])

        dist = haversine_distance(fix['lat'], fix['lon'], tp_lat, tp_lon)
        is_inside = dist <= radius

        # Track progress toward the current target turnpoint. Skipped for TAKEOFF
        # since that "leg" has no meaningful distance-made component.
        if tp_type != 'TAKEOFF':
            if closest_approach is None or dist < closest_approach:
                closest_approach = dist

        validated_now = False

        if tp_type == 'TAKEOFF':
            if is_inside:
                validated_now = True
        elif tp_type == 'SSS':
            direction = sss_info.get('direction', 'EXIT')
            gate_open = True

            if direction == 'EXIT':
                if is_inside: was_inside_sss = True
                elif was_inside_sss and gate_open:
                    validated_now = True
                    start_time = fix['time']
            else:
                if is_inside and gate_open:
                    validated_now = True
                    start_time = fix['time']
        else:
            if goal_deadline and fix['time'] > goal_deadline: continue
            if is_inside:
                validated_now = True
                if tp_type in ('GOAL', 'ESS') or current_tp_idx == total_tps - 1:
                    end_time = fix['time']

        if validated_now:
            validated_turnpoints.append({
                'tp_index': current_tp_idx,
                'name': tp['waypoint']['name'],
                'type': tp_type,
                'achieved_at': fix['time_str']
            })
            current_tp_idx += 1
            closest_approach = None  # start tracking progress toward the new next tp

    # --- Distance covered ---
    # Full task distance if every turnpoint (including goal) was validated.
    # Otherwise: full credit for completed legs + partial credit for the leg
    # currently in progress, based on how close the pilot got to the next turnpoint.
    if current_tp_idx >= total_tps:
        pilot_distance = total_task_distance
    else:
        pilot_distance = cumulative_distance[current_tp_idx - 1] if current_tp_idx > 0 else 0.0
        if current_tp_idx > 0 and closest_approach is not None:
            leg_len = leg_distances[current_tp_idx - 1]
            partial_credit = max(0.0, leg_len - closest_approach)
            pilot_distance += min(partial_credit, leg_len)

    flight_time_mins = None
    if start_time and end_time and current_tp_idx == total_tps:
        t1 = start_time.hour * 60 + start_time.minute + start_time.second / 60.0
        t2 = end_time.hour * 60 + end_time.minute + end_time.second / 60.0
        flight_time_mins = t2 - t1
        if flight_time_mins < 0:
            flight_time_mins += 24 * 60

    return {
        'task_completed': current_tp_idx == total_tps,
        'turnpoints_completed': current_tp_idx,
        'total_turnpoints': total_tps,
        'achieved_details': validated_turnpoints,
        'pilot_distance': pilot_distance,
        'total_task_distance': total_task_distance,
        'flight_time_mins': flight_time_mins
    }

@st.cache_data(ttl=3600, show_spinner=False)
def _load_task_json(task_hash: str):
    """Cached XContest call. Raises on failure so errors are never cached."""
    url = f"https://tools.xcontest.org/api/xctsk/load/{task_hash}"
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return response.json()


def fetch_task_from_api(task_hash: str):
    """Fetches the JSON task data from XContest API (cached for an hour)."""
    try:
        return _load_task_json(task_hash)
    except Exception as e:
        st.error(f"Failed to fetch task {task_hash}: {e}")
    return None


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_task_qr_svg(task_hash: str) -> str:
    """Cached QR code (SVG text) for a task. Raises on failure so errors are never cached."""
    task_json = _load_task_json(task_hash)
    qr_response = requests.post(
        "https://tools.xcontest.org/api/xctsk/qr",
        json=task_json,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    qr_response.raise_for_status()
    return qr_response.text

# -------------------------------------------------------------------
# Page Renderers
# -------------------------------------------------------------------

def render_welcome_page():
    st.title("🪂 Welcome to the SkyHigh Voyagers Project")
    st.markdown("""
    ### Elevate Your Cross-Country Flying
    Welcome pilots! This platform is designed to help our paragliding community enhance their cross country flying experience through a friendly, safe, and supportive environment promoting group flying.
    """)

    st.markdown("---")

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("### 🎯 Our Mission")
        st.info("""
        Our mission is to support pilot skill progression, foster friendly task flying, and build a vibrant XC community. By providing accessible task downloads, dedicated Telegram group for communication and coordination, real-time tracklog validation, and informal scoring, we make task flying cross country accessible, simple and fun for everyone.
        """)

    with col2:
        st.markdown("### 🛠️ How to Set Up & Fly a Task")
        st.markdown("""
        1. **Register as a Pilot:** Navigate to **Pilot Registration** and enter your details to create your profile.
        2. **Browse Tasks:** Visit the **Task Gallery** to explore task routes and waypoint coordinates. Click to download the task files directly to your device or scan the QR code for quick access. Import the downloaded task into your flight computer or navigation app (e.g., *XCTrack*, *Oudie*, or *SkyFlyHy*).
        3. **Find Flying Buddies:** Coordinate with fellow pilots to fly together on the same day for maximum scoring potential. You can join our dedicated **Telegram group** to connect with fellow interested pilots for task discussions, flight planning, and community support using the following link: https://t.me/+ZsnQ_8zBf3FiYzM1
        4. **Fly the Route:** Take off, cross the SSS (Start Gate) pass through all sequential turnpoints, and reach Goal.
        5. **Submit Tracklog:** Head over to **Flight Upload**, select your task, upload your tracklog file, and record your score on the leaderboard.
        """)

    st.markdown("---")
    st.success("Ready to fly? **Register** yourself and head over to **Task Gallery** to pick your task, or **Flight Upload** to evaluate a completed flight!")

def render_how_it_works_page():
    st.title("⚙️ How It Works")
    st.markdown(r"""
    We are a passionate team of pilots dedicated to promoting skill development within our sport. Our mission is to support pilot progression, foster friendly task flying, and build a vibrant Cross Country (XC) community.
    ### Meet the Teams Behind the Scenes
    We are a team of volunteers. To keep everything running smoothly and safely, our operations are divided into three dedicated groups:

    * **The Admin Team:** This team manages the app and ensures all technical aspects run flawlessly. They actively build and maintain strong relationships with local clubs and SAFA, ensuring all tasks are in strict accordance with regulations.
    * **The Task Team:** Focused on course design, this team creates diverse and engaging tasks aimed at developing paragliding skills while prioritizing safety. They are also responsible for continually reviewing and refining the scoring system.
    * **The Safety Team:** An independent group of experienced pilots who review tasks to guarantee they are safe for our community to fly. They also provide valuable feedback and recommendations to the Task Team to help optimize the scoring system.

    ### How the scoring system works?

    The scoring system is designed to encourage group flying, skill progression, and safe task flying. It rewards pilots for both distance and speed while also considering the number of pilots flying the same task on the same day.
    All tasks are speedruns with a maximum possible score based on the task's distance and difficulty.
    You can fly any task on any day you want at your own pace.
    To maximize your score, aim to fly with at least 4 more pilots and reach the goal as quickly as possible while staying safe and within your skill level.
    Your results will be compared against all other pilots who flew the same task in the whole season.
    As the score will be adjusted based on how many pilots participated that day, please upload your tracklog even if you had a slow flight or did not reach the goal. Every flight counts toward the community and your own skill development!
    We highly recommend to reach out to the safety officers or experienced pilots and ask them about tips and tricks of flying the routes safely.
    If you are new to the route, we also recommend to fly with experienced local pilots who have flown the route before.


    #### Details of the scoring system are as follows:
    The following formula will be used to calculate the final score for each pilot's flight in each task:
    $$
    \text{Day factor} =  0.5 + 0.5 \times \text{minimum} \left(\text{1,} \frac{\text{number of pilots participated in the task in the day}}{\text{5}} \right)
    $$
    $$
    \text{Distance score} = \text{Day factor} \times \text{Task max score} \times 0.5 \times \left( \frac{\text{Distance covered by pilot}}{\text{Total task distance}} \right)
    $$
    $$
    \text{Speed score} = \text{Day factor} \times  \text{Task max score} \times 0.5 \times \left( \text{If reached goal 1 otherwise 0} \right)  \times \left( \frac{\text{Fastest time}}{\text{Pilot time}} \right)
    $$
    $$
    \text{Total score of a task} = \text{Distance score} + \text{Speed score}
    $$
    $$
    \text{Leaderboard overall score} = \text{Sum of the best seven task scores of the pilot}
    $$

    The score components are defined as follows:

    * **Day factor:** A multiplier based on how many pilots fly the same task on the same day. It is 1.0 for 5 or more pilots and reduces proportionally for fewer pilots to reward group participation. If task was flown by 3 pilots, the day factor would be 0.8 (0.5 + 0.5 * 3/5).
    * **Task max score:** Each task has a maximum score based on its nominal distance and difficulty (e.g., 300 points for short and easy tasks, 1000 points for longer and difficult tasks). The maximum score is split evenly between distance and speed components and set by task designers.
    * **Distance score:** The portion of the score earned for the distance covered by the pilot relative to the total task distance. It is calculated using the day factor, task max score, and the distance ratio. So if you fly half the task distance, you earn only half of the possible maximum distance score.
    * **Speed score:** The portion of the score earned for reaching the goal and completing the speed section quickly compared to the fastest pilot. It is only awarded if the pilot reached the goal. The speed score for  pilots is calculated based on their time relative to the fastest pilot's time. So if it takes twice as long for you to finish the task compared to fastest pilot, your speed score will be half of the maximum speed score, while faster pilots earn the full speed score. The day factor is also applied to the speed score.
    * **Total score of a task:** The sum of the distance score and the speed score after applying the day factor and task maximum score.
    * **Leaderboard overall score:** The overall score for each pilot across all tasks is calculated by summing the best **seven** scores of all tasks they have flown. This overall score determines their ranking on the leaderboard.

    """)


# --- Task Gallery state helpers -------------------------------------------------
GALLERY_COLUMNS = 4        # thumbnails per row
GALLERY_PAGE_SIZE = 20     # thumbnails per page
NO_TAKEOFF_LABEL = "(not set)"


def _open_task(task_id):
    st.session_state["gallery_selected_task_id"] = task_id


def _close_task():
    st.session_state["gallery_selected_task_id"] = None


def _gallery_change_page(delta: int):
    st.session_state["gallery_page"] = max(1, st.session_state.get("gallery_page", 1) + delta)


def _gallery_filter_changed(field: str, widget_key: str):
    """on_change callback: copy the widget's value into our own filter store and go back to page 1."""
    value = st.session_state[widget_key]
    if field == "score":
        value = tuple(value)
    elif field == "takeoff":
        value = list(value)
    st.session_state["gallery_filters"][field] = value
    st.session_state["gallery_page"] = 1


def _gallery_clear_filters():
    st.session_state["gallery_filters"] = {"title": "", "takeoff": [], "score": None}
    # New widget keys => widgets start fresh from the cleared store
    st.session_state["gallery_reset_n"] = st.session_state.get("gallery_reset_n", 0) + 1
    st.session_state["gallery_page"] = 1


def _task_total_distance_km(task_json) -> float:
    turnpoints = task_json.get("turnpoints", []) if task_json else []
    total = 0.0
    for i in range(1, len(turnpoints)):
        try:
            total += haversine_distance(
                float(turnpoints[i - 1]["waypoint"]["lat"]), float(turnpoints[i - 1]["waypoint"]["lon"]),
                float(turnpoints[i]["waypoint"]["lat"]), float(turnpoints[i]["waypoint"]["lon"]),
            )
        except Exception:
            continue
    return total / 1000.0


def _render_gallery_pager(current: int, total: int, position: str):
    if total <= 1:
        return
    c1, c2, c3 = st.columns([1, 2, 1])
    with c1:
        st.button("◀ Previous", key=f"gallery_prev_{position}", on_click=_gallery_change_page,
                  args=(-1,), disabled=current <= 1)
    with c2:
        st.markdown(f"<div style='text-align:center; padding-top:0.4rem'>Page {current} of {total}</div>",
                    unsafe_allow_html=True)
    with c3:
        st.button("Next ▶", key=f"gallery_next_{position}", on_click=_gallery_change_page,
                  args=(1,), disabled=current >= total)


def _render_gallery_grid(tasks_data):
    # ---- Values the filters can offer, taken from the data
    takeoff_options = sorted({_clean_takeoff(t.get("take_off")) for t in tasks_data} - {""}, key=str.lower)
    if any(not _clean_takeoff(t.get("take_off")) for t in tasks_data):
        takeoff_options.append(NO_TAKEOFF_LABEL)

    scores = [float(t.get("max_score") or 0) for t in tasks_data]
    score_lo = int(math.floor(min(scores) / 10.0) * 10)
    score_hi = int(math.ceil(max(scores) / 10.0) * 10)
    if score_hi <= score_lo:
        score_hi = score_lo + 10

    # ---- Filter values live in our own session_state entry, so they survive opening a task
    filters = st.session_state.setdefault("gallery_filters", {"title": "", "takeoff": [], "score": None})
    n = st.session_state.get("gallery_reset_n", 0)

    s_lo, s_hi = filters.get("score") or (score_lo, score_hi)
    s_lo = max(score_lo, min(int(s_lo), score_hi))
    s_hi = max(score_lo, min(int(s_hi), score_hi))
    if s_lo > s_hi:
        s_lo, s_hi = score_lo, score_hi
    takeoff_default = [x for x in filters.get("takeoff", []) if x in takeoff_options]

    key_title, key_takeoff, key_score = f"gf_title_{n}", f"gf_takeoff_{n}", f"gf_score_{n}"

    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([2, 2, 2, 1])
        with c1:
            title_q = st.text_input("🔎 Title", value=filters.get("title", ""),
                                    placeholder="Search by task title", key=key_title,
                                    on_change=_gallery_filter_changed, args=("title", key_title))
        with c2:
            takeoff_sel = st.multiselect("📍 Take-off", options=takeoff_options, default=takeoff_default,
                                         placeholder="All take-offs", key=key_takeoff,
                                         on_change=_gallery_filter_changed, args=("takeoff", key_takeoff))
        with c3:
            score_range = st.slider("🏆 Max score", min_value=score_lo, max_value=score_hi,
                                    value=(s_lo, s_hi), step=10, key=key_score,
                                    on_change=_gallery_filter_changed, args=("score", key_score))
        with c4:
            st.markdown("<div style='height:1.75rem'></div>", unsafe_allow_html=True)
            st.button("Clear filters", key="gallery_clear_filters", on_click=_gallery_clear_filters)

    # ---- Apply filters
    query = (title_q or "").strip().lower()
    min_score, max_score = score_range
    filtered = []
    for t in tasks_data:
        if query and query not in (t.get("description") or "").lower():
            continue
        task_takeoff = _clean_takeoff(t.get("take_off")) or NO_TAKEOFF_LABEL
        if takeoff_sel and task_takeoff not in takeoff_sel:
            continue
        if not (min_score <= float(t.get("max_score") or 0) <= max_score):
            continue
        filtered.append(t)

    st.caption(f"Showing {len(filtered)} of {len(tasks_data)} tasks")
    if not filtered:
        st.info("No tasks match your filters. Try widening the score range or clearing the filters.")
        return

    # ---- Pagination
    total_pages = max(1, math.ceil(len(filtered) / GALLERY_PAGE_SIZE))
    page = min(max(st.session_state.get("gallery_page", 1), 1), total_pages)
    st.session_state["gallery_page"] = page
    page_tasks = filtered[(page - 1) * GALLERY_PAGE_SIZE: page * GALLERY_PAGE_SIZE]

    _render_gallery_pager(page, total_pages, "top")

    # ---- Thumbnail grid
    for row_start in range(0, len(page_tasks), GALLERY_COLUMNS):
        cols = st.columns(GALLERY_COLUMNS)
        for col, task in zip(cols, page_tasks[row_start: row_start + GALLERY_COLUMNS]):
            with col:
                with st.container(border=True):
                    img = get_task_image(task.get("task_hash"))
                    if img:
                        st.image(img, width="stretch")
                    else:
                        st.caption("🖼️ No image")
                    st.markdown(f"**{task.get('description', '')}**")
                    take_off = _clean_takeoff(task.get("take_off")) or "not set"
                    st.caption(f"📍 {take_off}  ·  🏆 {task.get('max_score', 0):g} pts")
                    st.button("View details", key=f"view_task_{task['id']}",
                              on_click=_open_task, args=(task["id"],))

    _render_gallery_pager(page, total_pages, "bottom")


def _render_task_detail(task):
    st.button("← Back to all tasks", key="gallery_back_top", on_click=_close_task)

    task_hash = task.get("task_hash")
    if not task_hash:
        st.error("Invalid task configuration. Missing task hash.")
        return

    take_off = _clean_takeoff(task.get("take_off")) or "Not set"
    st.header(task["description"])

    task_json = fetch_task_from_api(task_hash)
    turnpoints = task_json.get("turnpoints", []) if task_json else []

    col1, col2 = st.columns([1, 2])

    with col1:
        st.markdown(f"**Designer:** {task.get('designer') or 'N/A'}")
        st.markdown(f"**Take-off:** {take_off}")
        st.markdown(f"**Max Task Score:** {task.get('max_score', 1000)} pts")
        if task_json:
            st.markdown(f"**Task Distance:** {_task_total_distance_km(task_json):.1f} km")
            st.markdown(f"**Turnpoints:** {len(turnpoints)}")

        xcontest_url = f"https://tools.xcontest.org/xctsk/load?taskCode={task_hash}"
        st.markdown(f"[🌍 View Full Task & Turnpoints on XContest]({xcontest_url})")

        st.markdown("---")

        if task_json:
            st.download_button(
                label="📥 Download Task File (.xctsk)",
                data=json.dumps(task_json, indent=2),
                file_name=f"task_{task['description'].replace(' ', '_')}.xctsk",
                mime="application/xctsk",
                type="primary"
            )

            st.markdown("### Scan to Load Task")
            try:
                st.markdown(f"<div>{fetch_task_qr_svg(task_hash)}</div>", unsafe_allow_html=True)
            except Exception:
                st.warning("Could not generate QR code at this time.")
        else:
            st.warning("The task file could not be loaded from XContest right now, so the download and QR code are unavailable.")

    with col2:
        st.subheader("Task Route Map")
        img = get_task_image(task_hash)
        if img:
            st.image(img, caption=f"Map for {task['description']}", width="stretch")

    if turnpoints:
        with st.expander("Turnpoints"):
            rows = []
            for i, tp in enumerate(turnpoints, start=1):
                wp = tp.get("waypoint", {})
                rows.append({
                    "#": i,
                    "Name": wp.get("name", ""),
                    "Type": tp.get("type", "TURNPOINT"),
                    "Radius (m)": tp.get("radius"),
                    "Lat": wp.get("lat"),
                    "Lon": wp.get("lon"),
                })
            st.dataframe(pd.DataFrame(rows), hide_index=True)


def render_task_gallery_page():
    st.title("🗺️ Task Gallery")

    response = db("SELECT * FROM tasks")
    tasks_data = response.data

    if not tasks_data:
        st.info("No tasks available in the gallery yet. Check back soon!")
        return

    # Sort tasks by description (case-insensitive)
    tasks_data = sorted(tasks_data, key=lambda x: (x.get("description") or "").lower())

    selected_id = st.session_state.get("gallery_selected_task_id")
    if selected_id is not None:
        task = next((t for t in tasks_data if t["id"] == selected_id), None)
        if task:
            _render_task_detail(task)
            return
        _close_task()  # task no longer exists (e.g. deleted by an admin)

    st.markdown("Browse available cross-country tasks. Filter by title, take-off or max score, then click a task to see its full details, QR code and download link.")
    _render_gallery_grid(tasks_data)


def render_pilot_registration_page():
    st.title("📝 Pilot Registration")
    st.markdown("New to the platform? Register here by providing your details.")

    with st.form("pilot_registration_form"):
        col1, col2 = st.columns(2)
        with col1:
            first_name = st.text_input("First Name")
            last_name = st.text_input("Last Name")
        with col2:
            safa_number = st.number_input("SAFA Number (ID)", min_value=1, step=1, value=None)
            pg_level = st.selectbox("PG Level", options=["PG2", "PG3", "PG4", "PG5"], index=None)

        tnc_agreed = st.checkbox("Terms and Conditions: I agree to follow the SAFA and local club rules. I understand that flying is inherently risky and I am responsible for my own safety. I will fly responsibly within my skill levels and always prioritize safety.", value=False)
        submit_registration = st.form_submit_button("Register Pilot", type="primary")

    if submit_registration:
        if not tnc_agreed:
            st.error("You must agree to the Terms and Conditions before proceeding.")
        elif not first_name or not last_name:
            st.error("Please enter both First Name and Last Name.")
        elif safa_number is None:
            st.error("Please enter your SAFA Number.")
        elif pg_level is None:
            st.error("Please select your PG Level.")
        else:
            full_name = f"{first_name.strip()} {last_name.strip()}"
            pilot_data = {
                "safa_number": safa_number,
                "name": full_name,
                "pg_level": pg_level
            }
            try:
                db(
                    """INSERT INTO pilots (safa_number, name, pg_level)
                       VALUES (%(safa_number)s, %(name)s, %(pg_level)s)
                       ON CONFLICT (safa_number)
                       DO UPDATE SET name = EXCLUDED.name, pg_level = EXCLUDED.pg_level""",
                    {**pilot_data, "safa_number": int(pilot_data["safa_number"])},
                )
                st.success(f"Successfully registered {full_name} (SAFA: {safa_number})!")
                st.info("You can now navigate to the 'Flight Upload' page to submit your flights.")
            except Exception as e:
                st.error(f"Error saving registration to database: {e}")


def render_flight_upload_page():
    st.title("👨‍✈️ Flight Upload")
    st.markdown("Select a task, upload your IGC file, preview your flight, and submit your score.")

    # Fetch tasks from DB
    response = db("SELECT * FROM tasks")
    tasks_data = response.data

    if not tasks_data:
        st.warning("No tasks available. An admin needs to create tasks first.")
        return

    # Sort tasks by description
    tasks_data = sorted(tasks_data, key=lambda x: x.get('description', ''))

    with st.form("pilot_inputs_form"):
        col1, col2, col3 = st.columns(3)
        with col1:
            safa_number = st.number_input("SAFA Number (ID)", min_value=1, step=1, value=None)
        with col2:
            glider_class = st.selectbox("Glider Class", options=['A', 'B', 'C', 'D', 'CCC'], index=None)
        with col3:
            paraglider = st.text_input("Paraglider (e.g., Flow Cosmos 2)")

        flight_date_input = st.date_input(
            "Date of Flight (optional)",
            value=None,
            max_value=date.today(),
            help=(
                "Leave empty to use the date detected from your IGC file (header or file name). "
                "Fill it in only if your flight instrument recorded the wrong date / time zone — "
                "a date entered here always takes priority."
            )
        )

        # Task Selection
        task_options = {_task_label(t): t for t in tasks_data}
        selected_task_label = st.selectbox("Select Task", options=list(task_options.keys()))
        selected_task = task_options[selected_task_label]

        igc_file = st.file_uploader("Upload Tracklog File (.igc)")

        preview_button = st.form_submit_button("Preview & Evaluate")

    if preview_button:
        if safa_number is None:
            st.error("Please enter your SAFA Number.")
        elif glider_class is None:
            st.error("Please select your Glider Class.")
        elif not igc_file:
            st.error("Please upload an IGC file.")
        elif igc_file.size > 2 * 1024 * 1024:
            st.error("File size exceeds the 2 MB limit.")
        else:
            pilot_check = db("SELECT name FROM pilots WHERE safa_number = %s", (int(safa_number),))

            if not pilot_check.data:
                st.error("SAFA number not found. Please register first in the Pilot Registration page.")
            else:
                pilot_name = pilot_check.data[0]['name']
                task_hash = selected_task.get("task_hash")
                task_json = fetch_task_from_api(task_hash)

                if not task_json:
                    st.error("Could not retrieve task parameters from XContest API. Validation cannot proceed.")
                else:
                    igc_bytes = igc_file.getvalue()  # raw bytes, kept so we can store the original file on submit
                    igc_content = igc_bytes.decode("utf-8", errors="ignore")
                    igc_df, parsed_name, igc_flight_date, date_source = parse_igc(igc_content, igc_file.name)

                    # Date priority:
                    #   1. date the pilot typed into the form (explicit override)
                    #   2. date detected from the IGC header
                    #   3. date detected from the IGC file name
                    if flight_date_input is not None:
                        flight_date = flight_date_input.isoformat()
                        flight_date_source = "entered manually"
                    elif igc_flight_date != "Unknown":
                        flight_date = igc_flight_date
                        flight_date_source = date_source
                    else:
                        flight_date = None
                        flight_date_source = None

                    if igc_df.empty:
                        st.error("Invalid IGC file or no valid fixes found.")
                    elif flight_date is None:
                        st.error("Could not detect a flight date in this IGC file. Please enter the Date of Flight manually.")
                    else:
                        validation_results = validate_task_flight(task_json, igc_df.to_dict('records'))
                        max_score = float(selected_task['max_score'])

                        # Calculate Base Distance Score
                        # NOTE: this is intentionally independent of whether goal was
                        # reached — only the speed score below depends on that.
                        dist_ratio = validation_results['pilot_distance'] / max(validation_results['total_task_distance'], 1)
                        dist_ratio = min(dist_ratio, 1.0)
                        base_distance_score = (max_score * 0.5) * dist_ratio

                        st.session_state["eval_data"] = {
                            "pilot_name": pilot_name,
                            "safa_number": safa_number,
                            "glider_class": glider_class,
                            "paraglider": paraglider,
                            "selected_task": selected_task,
                            "task_json": task_json,
                            "igc_df": igc_df,
                            "igc_bytes": igc_bytes,
                            "flight_date": flight_date,
                            "flight_date_source": flight_date_source,
                            "igc_flight_date": igc_flight_date,
                            "validation_results": validation_results,
                            "base_distance_score": base_distance_score,
                            "estimated_score": base_distance_score
                        }

    if "eval_data" in st.session_state:
        data = st.session_state["eval_data"]
        validation_results = data["validation_results"]
        estimated_score = data["estimated_score"]
        selected_task = data["selected_task"]
        igc_df = data["igc_df"]
        task_json = data["task_json"]

        st.markdown("---")
        st.subheader("📑 Evaluation Preview")
        st.write(f"**Pilot:** {data['pilot_name']} (SAFA: {data['safa_number']})")
        st.write(f"**Flight Date:** {data['flight_date']} _(source: {data.get('flight_date_source')})_")
        igc_flight_date = data.get("igc_flight_date")
        if igc_flight_date and igc_flight_date != "Unknown" and igc_flight_date != data['flight_date']:
            st.warning(f"The IGC file reports {igc_flight_date}, but the date you entered ({data['flight_date']}) will be recorded.")
        st.write(f"**Turnpoints Reached:** {validation_results['turnpoints_completed']} / {validation_results['total_turnpoints']}")
        st.write(f"**Estimated Distance Score:** {round(estimated_score, 2)}")
        if not validation_results['task_completed']:
            st.caption("Task not completed — distance score reflects progress made toward the next turnpoint; no speed score is awarded.")

        if validation_results['achieved_details']:
            st.dataframe(pd.DataFrame(validation_results['achieved_details']))

        turnpoints = task_json.get('turnpoints', [])
        if turnpoints and not igc_df.empty:
            start_lat = float(turnpoints[0]['waypoint']['lat'])
            start_lon = float(turnpoints[0]['waypoint']['lon'])
            m = folium.Map(location=[start_lat, start_lon], zoom_start=11)

            for tp in turnpoints:
                folium.Circle(
                    location=[float(tp['waypoint']['lat']), float(tp['waypoint']['lon'])],
                    radius=float(tp['radius']),
                    color="blue", fill=True
                ).add_to(m)

            track_coords = igc_df[["lat", "lon"]].values.tolist()
            folium.PolyLine(track_coords, color="orange", weight=3, opacity=0.8).add_to(m)

            st_folium(m, width=1200, height=500, key="preview_flight_map")

        if st.button("Submit Flight to Database", type="primary"):
            flight_time = data["validation_results"].get('flight_time_mins')

            # Save the original IGC file first; if that fails, don't create a DB record without it.
            try:
                igc_path = save_igc_file(
                    selected_task['id'], data["flight_date"], data["safa_number"], data["igc_bytes"]
                )
            except Exception as e:
                st.error(f"Could not save the IGC file to storage: {e}")
                st.stop()

            flight_data = {
                "pilot_id": data["safa_number"],
                "task_id": selected_task['id'],
                "flight_date": data["flight_date"],
                "glider_class": data["glider_class"],
                "paraglider": data["paraglider"],
                "base_distance_score": round(data["base_distance_score"], 2),
                "speed_score": 0,
                "distance_score": round(data["base_distance_score"], 2),
                "total_score": round(data["base_distance_score"], 2),
                "flight_time_minutes": flight_time,
                "participants": 1,
                "igc_path": igc_path
            }
            db(
                """INSERT INTO flights
                   (pilot_id, task_id, flight_date, glider_class, paraglider,
                    base_distance_score, speed_score, distance_score, total_score,
                    flight_time_minutes, participants, igc_path)
                   VALUES (%(pilot_id)s, %(task_id)s, %(flight_date)s, %(glider_class)s, %(paraglider)s,
                           %(base_distance_score)s, %(speed_score)s, %(distance_score)s, %(total_score)s,
                           %(flight_time_minutes)s, %(participants)s, %(igc_path)s)""",
                {**flight_data, "pilot_id": int(flight_data["pilot_id"])},
            )

            # Master Recalculation Engine - Updated Formula Implementation
            flights_res = db("SELECT * FROM flights WHERE task_id = %s", (selected_task['id'],))
            all_task_flights = flights_res.data

            if all_task_flights:
                completed_flights = [f for f in all_task_flights if f.get("flight_time_minutes") is not None]
                fastest_time = min(f["flight_time_minutes"] for f in completed_flights) if completed_flights else None

                max_score = float(selected_task["max_score"])

                daily_pilots = defaultdict(set)
                for f in all_task_flights:
                    if f.get("flight_date"):
                        daily_pilots[f["flight_date"]].add(f["pilot_id"])

                for f in all_task_flights:
                    f_date = f.get("flight_date")
                    unique_count = len(daily_pilots[f_date]) if f_date else 1

                    # Updated Day Factor: 0.5 + 0.5 * min(1.0, count / 5)
                    day_factor = 0.5 + 0.5 * min(1.0, unique_count / 5.0)

                    # IMPORTANT: use None-checks here, not `or`, since a pilot who made
                    # 0 real distance progress has a *legitimate* base_distance_score
                    # of 0.0 — and `0.0 or x` would incorrectly fall through to x.
                    base_dist = f.get("base_distance_score")
                    if base_dist is None:
                        base_dist = f.get("distance_score")
                    if base_dist is None:
                        base_dist = 0.0
                    base_dist = float(base_dist)

                    base_speed = 0.0

                    # Speed Score = (Task max score * 0.5) * (Fastest time / Pilot time) if goal reached
                    if f.get("flight_time_minutes") is not None and fastest_time is not None and f["flight_time_minutes"] > 0:
                        p_time = f["flight_time_minutes"]
                        base_speed = (max_score * 0.5) * (fastest_time / p_time)

                    final_dist = base_dist * day_factor
                    final_speed = base_speed * day_factor
                    final_total = final_dist + final_speed

                    db(
                        """UPDATE flights
                           SET speed_score = %s, distance_score = %s, total_score = %s, participants = %s
                           WHERE id = %s""",
                        (round(final_speed, 2), round(final_dist, 2), round(final_total, 2), unique_count, f["id"]),
                    )

            st.success(f"Flight submitted! Scores for {selected_task['description']} updated based on global speed and daily team sizes.")
            del st.session_state["eval_data"]

def _get_takeoff_options():
    """All take-off names currently used by tasks (cleaned, de-duplicated, alphabetical)."""
    res = db("SELECT DISTINCT take_off FROM tasks WHERE take_off IS NOT NULL")
    names = {_clean_takeoff(r["take_off"]) for r in res.data}
    return sorted(names - {""}, key=str.lower)


def _resolve_takeoff(selected, typed, existing) -> str:
    """A typed take-off wins over the dropdown. If it matches an existing one (ignoring case)
    the existing spelling is reused, so the filter never shows 'Mlolongo' and 'mlolongo' separately."""
    typed = _clean_takeoff(typed)
    if typed:
        for option in existing:
            if option.lower() == typed.lower():
                return option
        return typed
    return _clean_takeoff(selected)


def render_admin_page():
    st.title("🖥️ Admin Control Panel")

    admin_pass = get_secret("ADMIN_PASSWORD")
    password = st.text_input("Enter Admin Password", type="password")
    if password != admin_pass:
        if password: st.error("Incorrect password.")
        return

    st.success("Authenticated.")

    tab1, tab2 = st.tabs(["Add Task", "Manage Tasks"])

    # ------------------------------------------------------------------ Add Task
    with tab1:
        st.subheader("Add New Task")
        existing_takeoffs = _get_takeoff_options()

        with st.form("add_task_form"):
            desc = st.text_input("Description")
            designer = st.text_input("Designer")
            max_score = st.number_input("Max Score", min_value=0, max_value=1000)

            col_a, col_b = st.columns(2)
            with col_a:
                takeoff_selected = st.selectbox(
                    "Take-off (required)", options=existing_takeoffs, index=None,
                    placeholder="Pick an existing take-off",
                )
            with col_b:
                takeoff_typed = st.text_input(
                    "…or type a new take-off",
                    help="Only fill this in if the take-off isn't in the list. If you type something, it is used instead of the dropdown.",
                )

            task_hash = st.text_input("Task Hash (e.g., c3262b39a20f1215)")
            submitted = st.form_submit_button("Save Task")

        if submitted:
            take_off = _resolve_takeoff(takeoff_selected, takeoff_typed, existing_takeoffs)
            if not task_hash or not desc:
                st.error("Please provide both a description and a task hash.")
            elif not take_off:
                st.error("Take-off is required. Pick an existing one or type a new one.")
            else:
                validate_task = fetch_task_from_api(task_hash.strip())
                if not validate_task:
                    st.error(f"Task with hash '{task_hash}' could not be loaded from XContest. Double check the code.")
                else:
                    task_data = {
                        "description": desc.strip(),
                        "designer": designer,
                        "max_score": max_score,
                        "take_off": take_off,
                        "task_hash": task_hash.strip()
                    }
                    db(
                        """INSERT INTO tasks (description, designer, max_score, take_off, task_hash)
                           VALUES (%(description)s, %(designer)s, %(max_score)s, %(take_off)s, %(task_hash)s)""",
                        task_data,
                    )
                    if download_task_image(task_data["task_hash"]):
                        st.success(f"Task added successfully (take-off: {take_off}) and its picture was saved.")
                    else:
                        st.success(f"Task added successfully (take-off: {take_off}).")
                        st.warning("The task picture could not be downloaded right now. "
                                   "Use 'Sync missing images' in the Manage Tasks tab to retry.")

    # ------------------------------------------------------------------ Manage Tasks
    with tab2:
        st.subheader("Existing Tasks")
        response = db("SELECT id, description, designer, max_score, take_off, task_hash FROM tasks")
        if not response.data:
            st.info("No tasks yet.")
            return

        admin_tasks = sorted(response.data, key=lambda t: (t.get("description") or "").lower())
        df = pd.DataFrame(admin_tasks)
        df["image_saved"] = df["task_hash"].apply(task_image_exists)
        st.dataframe(df, hide_index=True)

        missing_takeoff = [t for t in admin_tasks if not _clean_takeoff(t.get("take_off"))]
        if missing_takeoff:
            st.warning(f"{len(missing_takeoff)} task(s) have no take-off yet. Use 'Edit task' below to fill it in "
                       "(they show up as '(not set)' in the gallery filter until then).")

        label_by_id = {t["id"]: f"#{t['id']} – {t['description']}" for t in admin_tasks}

        # ---- Images
        st.markdown("#### Task images")
        n_missing_img = int((~df["image_saved"]).sum())
        st.caption(f"{len(df) - n_missing_img} of {len(df)} task pictures are saved on the server.")
        if st.button("🖼️ Sync missing images", disabled=n_missing_img == 0):
            to_fetch = [t for t in admin_tasks if t.get("task_hash") and not task_image_exists(t["task_hash"])]
            progress = st.progress(0.0, text="Downloading task pictures…")
            ok = 0
            for i, t in enumerate(to_fetch, start=1):
                if download_task_image(t["task_hash"]):
                    ok += 1
                progress.progress(i / len(to_fetch), text=f"Downloading task pictures… {i}/{len(to_fetch)}")
            progress.empty()
            if ok == len(to_fetch):
                st.success(f"Saved {ok} picture(s). Refresh to update the table.")
            else:
                st.warning(f"Saved {ok} of {len(to_fetch)} picture(s). The rest failed, so try again in a moment.")

        # ---- Edit
        st.markdown("#### Edit task")
        edit_id = st.selectbox("Task to edit", options=list(label_by_id.keys()),
                               format_func=lambda i: label_by_id[i], key="admin_edit_task_id")
        edit_task = next(t for t in admin_tasks if t["id"] == edit_id)
        edit_options = _get_takeoff_options()
        current_takeoff = _clean_takeoff(edit_task.get("take_off"))
        current_index = edit_options.index(current_takeoff) if current_takeoff in edit_options else None

        with st.form(f"edit_task_form_{edit_id}"):
            e_desc = st.text_input("Description", value=edit_task.get("description") or "", key=f"edit_desc_{edit_id}")
            e_designer = st.text_input("Designer", value=edit_task.get("designer") or "", key=f"edit_designer_{edit_id}")
            e_max = st.number_input("Max Score", min_value=0, max_value=1000,
                                    value=min(1000, max(0, int(float(edit_task.get("max_score") or 0)))),
                                    key=f"edit_max_{edit_id}")
            col_a, col_b = st.columns(2)
            with col_a:
                e_takeoff_sel = st.selectbox("Take-off (required)", options=edit_options, index=current_index,
                                             placeholder="Pick an existing take-off", key=f"edit_takeoff_sel_{edit_id}")
            with col_b:
                e_takeoff_typed = st.text_input("…or type a new take-off", key=f"edit_takeoff_new_{edit_id}")
            st.caption("The task hash can't be edited. To use a different XContest task, delete this one and add it again.")
            save_edit = st.form_submit_button("Save changes")

        if save_edit:
            new_takeoff = _resolve_takeoff(e_takeoff_sel, e_takeoff_typed, edit_options)
            if not e_desc.strip():
                st.error("Description can't be empty.")
            elif not new_takeoff:
                st.error("Take-off is required. Pick an existing one or type a new one.")
            else:
                db(
                    """UPDATE tasks SET description = %s, designer = %s, max_score = %s, take_off = %s
                       WHERE id = %s""",
                    (e_desc.strip(), e_designer, e_max, new_takeoff, int(edit_id)),
                )
                st.success("Task updated. Refresh to update the table.")

        # ---- Delete
        st.markdown("#### Delete task")
        del_id = st.selectbox("Task to delete", options=list(label_by_id.keys()), index=None,
                              placeholder="Choose a task", format_func=lambda i: label_by_id[i],
                              key="admin_delete_task_id")
        confirm_delete = st.checkbox("Yes, permanently delete this task", key="admin_delete_confirm")
        if st.button("Delete Task", type="primary", disabled=(del_id is None or not confirm_delete)):
            try:
                doomed = next(t for t in admin_tasks if t["id"] == del_id)
                db("DELETE FROM tasks WHERE id = %s", (int(del_id),))
                still_used = db("SELECT 1 FROM tasks WHERE task_hash = %s LIMIT 1", (doomed.get("task_hash"),)).data
                if not still_used:
                    delete_task_image(doomed.get("task_hash"))
                st.success("Task deleted. Refresh to update table.")
            except Exception as e:
                st.error(f"Could not delete the task (it may still have flights attached): {e}")

def render_leaderboard_page():
    st.title("🏆 Leaderboard")

    category_options = ["Overall", "Only As", "As and Bs", "Only PG2s", "PG2s and PG3s", "PG4 or less"]
    category_filter = st.selectbox("Category", options=category_options)

    flights_res = db("""
        SELECT f.*,
               json_build_object('name', p.name, 'safa_number', p.safa_number, 'pg_level', p.pg_level) AS pilots,
               json_build_object('id', t.id, 'description', t.description,
                                 'max_score', t.max_score, 'task_hash', t.task_hash) AS tasks
        FROM flights f
        LEFT JOIN pilots p ON p.safa_number = f.pilot_id
        LEFT JOIN tasks  t ON t.id = f.task_id
    """)

    if not flights_res.data:
        st.info("No flights recorded yet.")
        return

    # Filter flights data based on category selection
    filtered_flights = []
    for f in flights_res.data:
        pilot_data = f.get("pilots", {})
        glider_class = f.get("glider_class")
        pg_level = pilot_data.get("pg_level") if pilot_data else None

        if category_filter == "Only As":
            if glider_class == "A":
                filtered_flights.append(f)
        elif category_filter == "As and Bs":
            if glider_class in ["A", "B"]:
                filtered_flights.append(f)
        elif category_filter == "Only PG2s":
            if pg_level == "PG2":
                filtered_flights.append(f)
        elif category_filter == "PG2s and PG3s":
            if pg_level in ["PG2", "PG3"]:
                filtered_flights.append(f)
        elif category_filter == "PG4 or less":
            if pg_level in ["PG2", "PG3", "PG4"]:
                filtered_flights.append(f)
        else:  # "Overall" / "Everyone"
            filtered_flights.append(f)

    if not filtered_flights:
        st.info(f"No flights found for category: {category_filter}")
        return

    tasks_res = db("SELECT * FROM tasks")
    tasks_data = tasks_res.data if tasks_res.data else []

    # Sort tasks by description
    tasks_data = sorted(tasks_data, key=lambda x: x.get('description', ''))

    task_options = ["Overall"] + [t["description"] for t in tasks_data]
    selected_option = st.selectbox("Select View", options=task_options)

    if selected_option == "Overall":
        st.subheader(f"📊 Overall Leaderboard ({category_filter})")

        pilot_task_scores = defaultdict(dict)
        pilot_info = {}

        for f in filtered_flights:
            pilot_data = f.get("pilots", {})
            task_data = f.get("tasks", {})
            if not pilot_data or not task_data:
                continue

            safa = pilot_data.get("safa_number")
            name = pilot_data.get("name")
            task_desc = task_data.get("description")
            score = float(f.get("total_score", 0))

            pilot_info[safa] = name

            current_max = pilot_task_scores[safa].get(task_desc, 0.0)
            if score > current_max:
                pilot_task_scores[safa][task_desc] = score

        table_rows = []
        all_task_names = [t["description"] for t in tasks_data]

        for safa, name in pilot_info.items():
            row = {"Pilot Name": name}

            scores_list = []
            for t_name in all_task_names:
                s = pilot_task_scores[safa].get(t_name, 0.0)
                row[t_name] = s
                scores_list.append(s)

            # Leaderboard Overall Score: Sum of the BEST SEVEN task scores of the pilot
            best_seven_scores = sorted(scores_list, reverse=True)[:7]
            row["Total Score"] = sum(best_seven_scores)
            table_rows.append(row)

        if table_rows:
            df_overall = pd.DataFrame(table_rows)
            df_overall = df_overall.sort_values(by="Total Score", ascending=False).reset_index(drop=True)
            # Make sure to format all numeric columns properly, excluding 'Pilot Name'
            st.dataframe(df_overall.style.format({col: "{:.1f}" for col in df_overall.columns if col not in ["Pilot Name"]}), use_container_width=True)
        else:
            st.info("No overall results found for this category.")

    else:
        st.subheader(f"📌 Task Breakdown & Validation: {selected_option} ({category_filter})")

        selected_task_obj = next((t for t in tasks_data if t["description"] == selected_option), None)
        if not selected_task_obj:
            st.warning("Selected task details not found.")
            return

        max_score = float(selected_task_obj["max_score"])
        task_hash = selected_task_obj["task_hash"]

        task_json = fetch_task_from_api(task_hash)
        if not task_json:
            st.error("Failed to load task distances for breakdown.")
            return

        turnpoints = task_json.get('turnpoints', [])
        leg_distances = []
        for i in range(1, len(turnpoints)):
            lat1, lon1 = float(turnpoints[i-1]['waypoint']['lat']), float(turnpoints[i-1]['waypoint']['lon'])
            lat2, lon2 = float(turnpoints[i]['waypoint']['lat']), float(turnpoints[i]['waypoint']['lon'])
            leg_distances.append(haversine_distance(lat1, lon1, lat2, lon2))
        total_task_distance = sum(leg_distances) if leg_distances else 1.0

        task_flights = [f for f in filtered_flights if f.get("tasks", {}).get("description") == selected_option]

        if not task_flights:
            st.info("No flights recorded for this task under the selected category.")
            return

        completed_times = [f["flight_time_minutes"] for f in task_flights if f.get("flight_time_minutes") is not None]
        fastest_time = min(completed_times) if completed_times else None

        breakdown_rows = []
        for f in task_flights:
            pilot_data = f.get("pilots", {})
            name = pilot_data.get("name")
            g_class = f.get("glider_class")
            paraglider = f.get("paraglider", "N/A")
            flight_date = f.get("flight_date", "N/A")
            participants = f.get("participants", 1)

            distance_score = float(f.get("distance_score", 0))
            speed_score = float(f.get("speed_score", 0))
            total_score = float(f.get("total_score", 0))
            flight_time = f.get("flight_time_minutes")

            # Updated Day Factor calculation
            day_factor = round(0.5 + 0.5 * min(1.0, participants / 5.0), 2)

            base_dist = distance_score / day_factor if day_factor > 0 else distance_score
            max_dist_score = max_score * 0.5
            if max_dist_score > 0:
                dist_ratio = min(1.0, base_dist / max_dist_score)
                distance_covered_km = (dist_ratio * total_task_distance) / 1000.0
            else:
                distance_covered_km = 0.0

            if flight_time is not None and fastest_time is not None:
                time_diff = round(flight_time - fastest_time, 2)
                duration_str = f"{round(flight_time, 2)} mins"
                time_diff_str = f"{'+' if time_diff > 0 else ''}{time_diff} mins"
            else:
                duration_str = "N/A"
                time_diff_str = "N/A"

            breakdown_rows.append({
                "Pilot Name": name,
                "Glider Class": g_class,
                "Glider": paraglider,
                "Flight Date": flight_date,
                "Number of Participants": participants,
                "Max Score of Task": max_score,
                "Day Factor": day_factor,
                "Distance Covered (km)": round(distance_covered_km, 2),
                "Distance Score": round(distance_score, 2),
                "Speed Score": round(speed_score, 2),
                "Total Duration": duration_str,
                "Time Diff vs Fastest": time_diff_str,
                "Total Score": round(total_score, 2)
            })

        df_breakdown = pd.DataFrame(breakdown_rows)
        if not df_breakdown.empty:
            df_breakdown = df_breakdown.sort_values(by="Total Score", ascending=False).reset_index(drop=True)
            st.dataframe(df_breakdown, use_container_width=True)
        else:
            st.info("No score data available for this task.")

# -------------------------------------------------------------------
# Navigation Routing
# -------------------------------------------------------------------
page = st.sidebar.radio("Navigation", ["Welcome", "Pilot Registration", "Task Gallery", "Flight Upload", "Leaderboard", "How It Works", "Admin"])

# Whenever the user switches page, make sure the gallery opens on the grid, not on a previously opened task
if st.session_state.get("_last_page") != page:
    st.session_state["gallery_selected_task_id"] = None
    st.session_state["_last_page"] = page

if page == "Welcome":
    render_welcome_page()
elif page == "How It Works":
    render_how_it_works_page()
elif page == "Task Gallery":
    render_task_gallery_page()
elif page == "Pilot Registration":
    render_pilot_registration_page()
elif page == "Flight Upload":
    render_flight_upload_page()
elif page == "Leaderboard":
    render_leaderboard_page()
elif page == "Admin":
    render_admin_page()
