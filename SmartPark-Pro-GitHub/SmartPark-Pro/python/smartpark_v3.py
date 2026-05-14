import cv2
import requests
import serial
import pandas as pd
import time
import easyocr
import re
import os
from datetime import datetime
from threading import Thread, Lock
from flask import Flask, request, jsonify
from flask_cors import CORS

# ============================================================
# CONFIGURATION
# ============================================================

SCRIPT_URL      = "YOUR_GOOGLE_APPS_SCRIPT_URL_HERE"        # Deploy → Web App → Anyone → copy URL here
SHEET_CSV_URL   = "YOUR_GOOGLE_SHEET_CSV_EXPORT_URL_HERE"   # File → Share → Publish to web → CSV → copy URL here
SECRET_TOKEN    = "YOUR_SECRET_TOKEN_HERE"                  # Must match SECRET_TOKEN in Apps Script (code.txt)
ARDUINO_PORT    = 'COM5'
DROIDCAM_SOURCE = 1
MAX_SLOTS       = 3

TXT_DB_FILE     = "bookings.txt"
LOCAL_API_PORT  = 5000

# ============================================================
# AUTOSCAN SETTINGS
# ============================================================

AUTOSCAN_INTERVAL    = 3
SAME_PLATE_COOLDOWN  = 10
MIN_CONFIDENCE       = 0.55
SHEET_REFRESH_SECS   = 15

# ============================================================
# IN-MEMORY DATABASE CACHE
# ============================================================

db_lock         = Lock()
plate_to_slot   = {}
local_plate_db  = {}
last_db_refresh = 0.0
pending_exits   = set()   # slots currently being cleared — blocks ghost restore


# ============================================================
# ✅ FLOAT FIX — single helper used EVERYWHERE a slot is handled
# ============================================================

def normalize_slot(raw_slot) -> str:
    """
    Convert ANY slot representation to a clean integer string.
    "2.0" → "2",  2.0 → "2",  "2" → "2",  2 → "2"
    Returns "" if conversion fails so callers can skip invalid rows.
    """
    try:
        return str(int(float(str(raw_slot).strip())))
    except (ValueError, TypeError):
        return ""


# ============================================================
# DB PARSE HELPERS
# ============================================================

def _parse_txt_db() -> dict:
    result = {}
    if not os.path.exists(TXT_DB_FILE):
        return result
    with open(TXT_DB_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("-") or line.upper().startswith("TIMESTAMP"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 4:
                plate = parts[2].upper().strip()
                slot  = normalize_slot(parts[3])           # ✅ float fix
                if not slot:
                    continue
                if slot in pending_exits:
                    print(f"[DB] txt: skipping slot {slot} (pending exit)")
                    continue
                if plate and plate != "NAN":
                    result[plate] = slot
    return result


def _parse_sheet_db() -> dict:
    result = {}
    try:
        df = pd.read_csv(SHEET_CSV_URL)
        if df.empty:
            return result
        plates_col = df.iloc[:, 2].astype(str).str.upper().str.strip()
        slots_col  = df.iloc[:, 3].astype(str).str.strip()
        for plate, slot in zip(plates_col, slots_col):
            plate = plate.strip()
            slot  = normalize_slot(slot)                   # ✅ float fix
            if not slot:
                continue
            if slot in pending_exits:
                print(f"[DB] sheet: skipping slot {slot} (pending exit)")
                continue
            if plate and plate != "NAN":
                result[plate] = slot
    except Exception as e:
        print(f"[ERROR] Could not read Google Sheet: {e}")
    return result


def refresh_db():
    global plate_to_slot, local_plate_db, last_db_refresh
    print("[DB] Refreshing...")
    sheet_data = _parse_sheet_db()
    txt_data   = _parse_txt_db()
    with db_lock:
        plate_to_slot   = sheet_data
        local_plate_db  = txt_data
        last_db_refresh = time.time()
    print(f"[DB] Sheet: {len(sheet_data)} | TXT: {len(txt_data)}")


def refresh_db_if_stale():
    if time.time() - last_db_refresh >= SHEET_REFRESH_SECS:
        refresh_db()


def lookup_plate(plate: str) -> str | None:
    plate = plate.upper().strip()
    with db_lock:
        local_slot = local_plate_db.get(plate)
        sheet_slot = plate_to_slot.get(plate)
        if local_slot is not None and sheet_slot is not None:
            if local_slot != sheet_slot:
                print(f"[LOOKUP] Conflict {plate}: local={local_slot} sheet={sheet_slot} → LOCAL wins")
                return local_slot
            return local_slot
        return local_slot or sheet_slot


def get_occupied_count() -> int:
    with db_lock:
        combined = {**local_plate_db, **plate_to_slot}
        return len(set(combined.values()))


# ============================================================
# TXT FILE HELPERS
# ============================================================

def init_txt_db():
    if not os.path.exists(TXT_DB_FILE):
        with open(TXT_DB_FILE, "w") as f:
            f.write("TIMESTAMP           | NAME                 | VEHICLE_NO   | SLOT\n")
            f.write("-" * 65 + "\n")
        print(f"[TXT] Created {TXT_DB_FILE}")
    else:
        print(f"[TXT] Using existing {TXT_DB_FILE}")


def append_booking_to_txt(name: str, plate: str, raw_slot):
    """
    ✅ FLOAT FIX — normalize slot FIRST so "2.0" never reaches the file
    or the Google Sheet.  Everything downstream sees "2".
    """
    slot      = normalize_slot(raw_slot)                   # ✅ float fix
    plate     = plate.upper().strip()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line      = f"{timestamp:<20}| {name:<21}| {plate:<13}| {slot}\n"

    with open(TXT_DB_FILE, "a") as f:
        f.write(line)

    with db_lock:
        local_plate_db[plate] = slot                       # ✅ clean int string

    print(f"[TXT] Saved → {plate} | Slot {slot} | {name}")

    try:
        requests.post(SCRIPT_URL,
                      json={"action": "BOOK", "token": SECRET_TOKEN,
                            "name":   name,
                            "plate":  plate,
                            "slot":   slot},               # ✅ always "2", never "2.0"
                      timeout=5)
        print(f"[SHEET] Booking sent: {plate} | Slot {slot}")
    except Exception as e:
        print(f"[ERROR] Sheet update failed: {e}")


def remove_slot_from_txt(slot_num: str):
    slot_num = normalize_slot(slot_num)                    # ✅ float fix
    if not os.path.exists(TXT_DB_FILE):
        return
    kept_lines, removed = [], 0
    with open(TXT_DB_FILE, "r") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("-") or stripped.upper().startswith("TIMESTAMP"):
                kept_lines.append(line)
                continue
            parts = [p.strip() for p in stripped.split("|")]
            if len(parts) >= 4:
                line_slot = normalize_slot(parts[3])       # ✅ float fix
                if line_slot == slot_num:
                    removed += 1
                    continue
            kept_lines.append(line)
    with open(TXT_DB_FILE, "w") as f:
        f.writelines(kept_lines)
    print(f"[TXT] Removed {removed} booking(s) for slot {slot_num}")


# ============================================================
# FLASK
# ============================================================

flask_app = Flask(__name__)
CORS(flask_app)


@flask_app.route("/book", methods=["POST"])
def local_book():
    try:
        data  = request.get_json(force=True)
        name  = str(data.get("user",  "")).strip()
        plate = str(data.get("car",   "")).upper().strip()
        slot  = normalize_slot(data.get("slot", ""))       # ✅ float fix — HTML might send "2.0"
        if not name or not plate or not slot:
            return jsonify({"status": "error", "msg": "Missing/invalid fields"}), 400
        append_booking_to_txt(name, plate, slot)
        return jsonify({"status": "ok", "slot": slot}), 200
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500


@flask_app.route("/db", methods=["GET"])
def get_live_db():
    with db_lock:
        combined = {}
        for plate, slot in local_plate_db.items():
            combined[slot] = {"plate": plate, "source": "local"}
        for plate, slot in plate_to_slot.items():
            combined[slot] = {"plate": plate, "source": "sheet"}

    slots_info = []
    for n in range(1, MAX_SLOTS + 1):
        s = str(n)
        if s in combined:
            slots_info.append({"slot": s, "status": "OCCUPIED",
                               "plate": combined[s]["plate"],
                               "source": combined[s]["source"]})
        else:
            slots_info.append({"slot": s, "status": "FREE", "plate": "", "source": ""})

    return jsonify({
        "slots":     slots_info,
        "occupied":  len(combined),
        "total":     MAX_SLOTS,
        "timestamp": datetime.now().strftime("%H:%M:%S")
    })


def start_flask():
    print(f"[API] Listening on http://localhost:{LOCAL_API_PORT}")
    flask_app.run(host="0.0.0.0", port=LOCAL_API_PORT, debug=False, use_reloader=False)


# ============================================================
# PENDING EXIT GUARD LIFTER
# ============================================================

def _lift_pending_exit(slot_num: str, delay: float = 30.0):
    """Remove slot from pending_exits after delay so new bookings are accepted."""
    time.sleep(delay)
    pending_exits.discard(slot_num)
    print(f"[PENDING] Guard lifted for slot {slot_num} — slot open for new bookings.")


# ============================================================
# ARDUINO SIGNAL PROCESSOR
# ============================================================

def process_arduino_signals():
    if arduino and arduino.in_waiting > 0:
        try:
            raw_line = arduino.readline().decode('utf-8').strip()
            print(f"[ARDUINO RAW] '{raw_line}'")           # ✅ debug — remove after confirming format

            if "EMPTY:" in raw_line:
                slot_num = normalize_slot(               # ✅ float fix for Arduino messages too
                    raw_line.split("EMPTY:")[1].strip()
                )
                if not slot_num:
                    print("[ERROR] Could not parse slot from Arduino message")
                    return

                print(f"\n[LDR] Car left Slot {slot_num}")

                # ── 1. Guard FIRST so every subsequent refresh is safe ──
                pending_exits.add(slot_num)
                print(f"[PENDING] Slot {slot_num} guarded — refresh cannot restore ghost data")

                # ── 2. Clear both in-memory caches ──
                cleared_plates = []
                with db_lock:
                    for p in [p for p, s in local_plate_db.items() if s == slot_num]:
                        del local_plate_db[p]
                        cleared_plates.append(p)
                        print(f"[DB] Removed '{p}' from local cache")
                    for p in [p for p, s in plate_to_slot.items() if s == slot_num]:
                        del plate_to_slot[p]
                        if p not in cleared_plates:
                            cleared_plates.append(p)
                        print(f"[DB] Removed '{p}' from sheet cache")

                # ── 3. Reset cooldown so same car can return with a new slot ──
                for plate in cleared_plates:
                    last_triggered_plates.pop(plate, None)
                    print(f"[COOLDOWN] Reset for '{plate}'")

                # ── 4. Remove from bookings.txt ──
                remove_slot_from_txt(slot_num)

                # ── 5. Notify Google Sheet ──
                try:
                    requests.post(SCRIPT_URL,
                                  json={"action": "EXIT", "slot": slot_num, "token": SECRET_TOKEN},
                                  timeout=5)
                    print(f"[SHEET] Slot {slot_num} EXIT sent to Google Sheet")
                except Exception as e:
                    print(f"[ERROR] Sheet EXIT failed: {e}")

                # ── 6. Refresh now — pending_exits blocks any ghost data ──
                print(f"[DB] Refreshing (slot {slot_num} is guarded)...")
                refresh_db()

                # ── 7. Lift guard after 30s in background thread ──
                Thread(target=_lift_pending_exit, args=(slot_num, 30.0), daemon=True).start()
                print(f"[PENDING] Guard lifts in 30s for slot {slot_num}")

        except Exception as e:
            print(f"[ERROR] Arduino read: {e}")


# ============================================================
# STARTUP
# ============================================================

init_txt_db()
refresh_db()

Thread(target=start_flask, daemon=True).start()

try:
    arduino = serial.Serial(ARDUINO_PORT, 9600, timeout=1)
    time.sleep(2)
    print("[OK] Arduino on", ARDUINO_PORT)
except Exception:
    print("[WARNING] Arduino not found — gate/LED disabled")
    arduino = None

print("[...] Loading OCR...")
reader = easyocr.Reader(['en'], gpu=False)
print("[OK] OCR ready")

cap = cv2.VideoCapture(DROIDCAM_SOURCE)
if not cap.isOpened():
    print("[ERROR] Camera not found")
    exit()
print("[OK] Camera connected")
print()
print("=" * 60)
print("  SmartPark Pro v3 — Autoscan + Float-Safe Slot Handling")
print("=" * 60)
print(f"  TXT backup : {TXT_DB_FILE}")
print(f"  DB refresh : every {SHEET_REFRESH_SECS}s")
print("  Press Q to quit")
print("=" * 60)

# ============================================================
# OCR HELPERS
# ============================================================

_OVERLAY_BLACKLIST = {
    "ACCESS", "DENIED", "GRANTED", "COOLDOWN", "SLOT", "AUTOSCAN",
    "ALIGN", "PLATE", "HERE", "AVAILABLE", "SLOTS", "NEXT", "SCAN",
    "SMARTPARK", "PRO", "MODE", "LAST", "SCANNING", "WAITING",
    "CLEARED", "EMPTY", "EXIT", "VEHICLE", "DATABASE", "GATE",
    "OPENING", "STATUS", "SIGNAL",
}


def clean_plate(raw: str) -> str:
    c = re.sub(r'[\s\-\n]', '', raw).upper()
    return re.sub(r'[^A-Z0-9]', '', c)


def _is_overlay_text(text: str) -> bool:
    upper = text.upper().strip()
    if any(w in upper for w in _OVERLAY_BLACKLIST):
        return True
    for pattern in [r'^\d+s$', r'^next\s+scan', r'auto.*scan', r'available.*slot']:
        if re.search(pattern, upper, re.IGNORECASE):
            return True
    return False


def scan_number_plate(frame):
    gray     = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    enhanced = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    results  = reader.readtext(enhanced)
    detected = []
    for (_, text, conf) in results:
        if conf < MIN_CONFIDENCE:
            continue
        if _is_overlay_text(text):
            continue
        cleaned = clean_plate(text)
        if not (any(c.isalpha() for c in cleaned) and any(c.isdigit() for c in cleaned)):
            continue
        if 6 <= len(cleaned) <= 12:
            detected.append((cleaned, conf))
            print(f"  [OCR] Valid: '{cleaned}' ({conf:.0%})")
    if not detected:
        return None
    detected.sort(key=lambda x: x[1], reverse=True)
    return detected[0][0]


def open_gate_for_slot(slot: str):
    if arduino:
        arduino.write(slot.encode())
        print(f"[GATE] Signal '{slot}' → Arduino")
    else:
        print(f"[GATE] (Simulated) LED {slot} ON")


# ============================================================
# STATE
# ============================================================

last_scan_time          = 0
last_triggered_plates   = {}
last_scan_result        = ""
scan_status_msg         = ""
scan_status_time        = 0
STATUS_DISPLAY_DURATION = 4

# ============================================================
# MAIN LOOP
# ============================================================

while True:
    process_arduino_signals()
    refresh_db_if_stale()

    ret, frame = cap.read()
    if not ret:
        print("[ERROR] Lost camera feed.")
        break

    now  = time.time()
    h, w = frame.shape[:2]

    box_x1, box_y1 = w // 4,   h // 3
    box_x2, box_y2 = 3*w // 4, 2*h // 3
    cv2.rectangle(frame, (box_x1, box_y1), (box_x2, box_y2), (0, 255, 255), 2)
    cv2.putText(frame, "Align plate here",
                (box_x1, box_y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1)

    occupied     = get_occupied_count()
    remaining    = MAX_SLOTS - occupied
    status_color = (0, 200, 0) if remaining > 0 else (0, 0, 220)
    cv2.putText(frame, f"Available Slots: {remaining}/{MAX_SLOTS}",
                (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, status_color, 2)

    countdown = max(0, AUTOSCAN_INTERVAL - (now - last_scan_time))
    cv2.putText(frame, f"[AUTO] Next scan: {countdown:.1f}s",
                (20, h - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (180, 180, 180), 1)

    if last_scan_result:
        cv2.putText(frame, f"Last: {last_scan_result}",
                    (20, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 0), 2)

    if scan_status_msg and (now - scan_status_time) < STATUS_DISPLAY_DURATION:
        color = (0, 200, 0) if "GRANTED" in scan_status_msg else \
                (0, 0, 220) if "DENIED"  in scan_status_msg else (255, 165, 0)
        cv2.putText(frame, scan_status_msg,
                    (w // 2 - 180, h // 2), cv2.FONT_HERSHEY_SIMPLEX, 1.2, color, 3)
    elif (now - scan_status_time) >= STATUS_DISPLAY_DURATION:
        scan_status_msg = ""

    cv2.imshow("SmartPark Pro v3", frame)

    if now - last_scan_time >= AUTOSCAN_INTERVAL:
        last_scan_time = now
        print("\n[AUTOSCAN] Scanning...")

        crop  = frame[box_y1:box_y2, box_x1:box_x2]
        plate = scan_number_plate(crop)

        if plate is None:
            print("[AUTOSCAN] No plate — waiting...")
            last_scan_result = ""
        else:
            print(f"[AUTOSCAN] Plate: {plate}")
            last_scan_result = plate

            last_trigger = last_triggered_plates.get(plate, 0)
            if now - last_trigger < SAME_PLATE_COOLDOWN:
                cd = SAME_PLATE_COOLDOWN - (now - last_trigger)
                print(f"[COOLDOWN] {plate} — wait {cd:.1f}s")
                scan_status_msg  = f"COOLDOWN: {cd:.0f}s"
                scan_status_time = now
            else:
                slot = lookup_plate(plate)
                if slot:
                    print(f"[ACCESS] GRANTED ✅  {plate} → Slot {slot}")
                    scan_status_msg              = f"ACCESS GRANTED — SLOT {slot}"
                    scan_status_time             = now
                    last_triggered_plates[plate] = now
                    open_gate_for_slot(slot)
                else:
                    print(f"[ACCESS] DENIED ❌  {plate} not in DB")
                    scan_status_msg              = "ACCESS DENIED"
                    scan_status_time             = now
                    last_triggered_plates[plate] = now

    if cv2.waitKey(1) & 0xFF == ord('q'):
        print("\n[EXIT] Shutting down...")
        break

# ============================================================
# CLEANUP
# ============================================================
cap.release()
cv2.destroyAllWindows()
if arduino:
    arduino.close()
print("[DONE] System offline.")
