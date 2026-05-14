// ============================================================
//  SmartPark Pro — Arduino Controller v4 (Multi-Slot LDR Fix)
//
//  ROOT CAUSE FIX:
//    v3 used a single `activeSlot` integer. Each new car booking
//    overwrote it, so only the LAST booked slot was ever monitored
//    by the LDR. When that slot's car left, activeSlot became -1
//    and slots 1 & 2 were NEVER detected as empty.
//
//  FIX: All slot state is now stored in per-slot ARRAYS so every
//    occupied slot is independently monitored at the same time.
// ============================================================

#include <Servo.h>

Servo gate;

// ── Pin assignments ──────────────────────────────────────────
const int ldrPins[] = {A0, A1, A2};
const int ledPins[] = {5,  6,  7};
const int NUM_SLOTS = 3;

// ── LDR threshold ─────────────────────────────────────────────
const int LDR_THRESHOLD = 500;

// ── Per-slot state arrays (THE FIX) ──────────────────────────
//    Every variable that was previously a single value is now
//    an array so all slots are tracked independently.

bool          slotActive[NUM_SLOTS];          // is this slot currently booked?
bool          inEntryWindow[NUM_SLOTS];       // waiting for car to finish parking
unsigned long entryWindowStart[NUM_SLOTS];
const unsigned long ENTRY_WINDOW_DURATION = 7000UL;

bool          lastOccupied[NUM_SLOTS];        // LDR state from previous loop

bool          emptyDetected[NUM_SLOTS];       // light first seen but not confirmed yet
unsigned long emptyDetectedAt[NUM_SLOTS];
const unsigned long EMPTY_STABLE_TIME = 300UL;

int           emptyRetryCount[NUM_SLOTS];     // how many EMPTY signals sent so far
unsigned long lastEmptyRetrySent[NUM_SLOTS];
const int           EMPTY_RETRY_MAX      = 3;
const unsigned long EMPTY_RETRY_INTERVAL = 500UL;

// ── LED timer (per slot) ──────────────────────────────────────
bool          ledTimerActive[NUM_SLOTS];
unsigned long ledTimerStart[NUM_SLOTS];
const unsigned long ENTRY_LED_DURATION = 5000UL;

// ── Gate timer (shared — only one gate) ──────────────────────
bool          gateOpen     = false;
unsigned long gateOpenedAt = 0;
const unsigned long GATE_OPEN_DURATION = 5000UL;

// ── Debug print interval ──────────────────────────────────────
unsigned long lastDebugPrint = 0;
const unsigned long DEBUG_INTERVAL = 2000UL;

// ============================================================
void setup() {
  Serial.begin(9600);

  gate.attach(9);
  gate.write(0);

  for (int i = 0; i < NUM_SLOTS; i++) {
    pinMode(ledPins[i], OUTPUT);
    digitalWrite(ledPins[i], LOW);

    // ── Initialise all per-slot arrays ──
    slotActive[i]        = false;
    inEntryWindow[i]     = false;
    entryWindowStart[i]  = 0;
    lastOccupied[i]      = (analogRead(ldrPins[i]) < LDR_THRESHOLD);
    emptyDetected[i]     = false;
    emptyDetectedAt[i]   = 0;
    emptyRetryCount[i]   = 0;
    lastEmptyRetrySent[i]= 0;
    ledTimerActive[i]    = false;
    ledTimerStart[i]     = 0;
  }

  Serial.println("[BOOT] SmartPark Pro v4 (Multi-Slot Fix) ready.");
  Serial.println("[BOOT] LDR values at startup:");
  for (int i = 0; i < NUM_SLOTS; i++) {
    int val = analogRead(ldrPins[i]);
    Serial.print("  Slot "); Serial.print(i + 1);
    Serial.print(" -> LDR: "); Serial.print(val);
    Serial.println(lastOccupied[i] ? " -> OCCUPIED" : " -> EMPTY");
  }
  Serial.println("[BOOT] Waiting for Python signal...");
}

// ============================================================
void loop() {
  unsigned long now = millis();

  // ----------------------------------------------------------
  // 1. Receive slot digit from Python ('1', '2', or '3')
  //    Activates THAT slot's monitoring — does NOT touch others
  // ----------------------------------------------------------
  if (Serial.available() > 0) {
    char signal = Serial.read();
    if (signal >= '1' && signal <= '3') {
      int idx = signal - '1';   // '1'->0, '2'->1, '3'->2

      // Open gate (shared)
      gate.write(90);
      gateOpen     = true;
      gateOpenedAt = now;

      // Activate THIS slot's LED
      digitalWrite(ledPins[idx], HIGH);
      ledTimerActive[idx] = true;
      ledTimerStart[idx]  = now;

      // Activate THIS slot's monitoring
      slotActive[idx]          = true;
      inEntryWindow[idx]       = true;
      entryWindowStart[idx]    = now;

      // Reset empty-detection state for THIS slot
      emptyDetected[idx]      = false;
      emptyRetryCount[idx]    = 0;
      lastEmptyRetrySent[idx] = 0;

      // Mark as occupied immediately
      lastOccupied[idx] = true;

      Serial.print("[ENTRY] Gate OPEN + LED");
      Serial.print(idx + 1);
      Serial.print(" ON — now monitoring Slot ");
      Serial.println(idx + 1);

      // Show all currently active slots
      Serial.print("[INFO] Active slots: ");
      for (int i = 0; i < NUM_SLOTS; i++) {
        if (slotActive[i]) { Serial.print(i + 1); Serial.print(" "); }
      }
      Serial.println();
    }
  }

  // ----------------------------------------------------------
  // 2. Close gate after 5 seconds
  // ----------------------------------------------------------
  if (gateOpen && (now - gateOpenedAt >= GATE_OPEN_DURATION)) {
    gate.write(0);
    gateOpen = false;
    Serial.println("[GATE] Closed.");
  }

  // ----------------------------------------------------------
  // 3. Turn off entry LEDs after 5 seconds
  // ----------------------------------------------------------
  for (int i = 0; i < NUM_SLOTS; i++) {
    if (ledTimerActive[i] && (now - ledTimerStart[i] >= ENTRY_LED_DURATION)) {
      digitalWrite(ledPins[i], LOW);
      ledTimerActive[i] = false;
      Serial.print("[LED] OFF -> Slot "); Serial.println(i + 1);
    }
  }

  // ----------------------------------------------------------
  // 4. Entry window expiry — per slot
  //    KEY FIX: After the entry window, READ the LDR.
  //    If no car is actually blocking it (slot is light/empty),
  //    CANCEL monitoring entirely. This prevents a torch or
  //    ambient light from falsely triggering an EMPTY signal
  //    when the car never physically arrived in the slot.
  // ----------------------------------------------------------
  for (int i = 0; i < NUM_SLOTS; i++) {
    if (inEntryWindow[i] && (now - entryWindowStart[i] >= ENTRY_WINDOW_DURATION)) {
      inEntryWindow[i] = false;

      int  val        = analogRead(ldrPins[i]);
      bool carPresent = (val < LDR_THRESHOLD);

      Serial.print("[LDR] Entry window done for Slot "); Serial.print(i + 1);
      Serial.print(" | LDR="); Serial.print(val);

      if (carPresent) {
        // Car IS in the slot — start watching for departure
        lastOccupied[i] = true;
        Serial.println(" -> OCCUPIED. Monitoring for departure.");
      } else {
        // No car in slot — gate opened but car never parked
        // (false OCR, car went elsewhere, or booking without car)
        // Cancel monitoring so torch/light cannot delete the booking
        slotActive[i]      = false;
        emptyDetected[i]   = false;
        emptyRetryCount[i] = 0;
        Serial.println(" -> NO CAR DETECTED. Monitoring cancelled.");
        Serial.print("[WARN] Slot "); Serial.print(i + 1);
        Serial.println(" booking kept — car did not park. Gate may have opened falsely.");
      }
    }
  }

  // ----------------------------------------------------------
  // 5. LDR monitoring — ALL active slots simultaneously (THE FIX)
  //    Previously only checked the single `activeSlot`.
  //    Now loops over all slots independently.
  // ----------------------------------------------------------
  for (int i = 0; i < NUM_SLOTS; i++) {
    // Skip if slot not booked or still in entry window
    if (!slotActive[i] || inEntryWindow[i]) continue;

    int  ldrValue   = analogRead(ldrPins[i]);
    bool isOccupied = (ldrValue < LDR_THRESHOLD);

    // Stage A: First time light is seen → start stability timer
    if (!isOccupied && lastOccupied[i] && !emptyDetected[i]) {
      emptyDetected[i]   = true;
      emptyDetectedAt[i] = now;
      Serial.print("[LDR] Slot "); Serial.print(i + 1);
      Serial.print(" light detected (LDR="); Serial.print(ldrValue);
      Serial.println(") — confirming in 300ms...");
    }

    // If car came back before stable time → cancel detection
    if (emptyDetected[i] && isOccupied) {
      emptyDetected[i]    = false;
      emptyRetryCount[i]  = 0;
      Serial.print("[LDR] Slot "); Serial.print(i + 1);
      Serial.println(" false trigger — car still present.");
    }

    // Stage B: Light stable for 300ms → send EMPTY with retries
    if (emptyDetected[i] && (now - emptyDetectedAt[i] >= EMPTY_STABLE_TIME)) {
      if (emptyRetryCount[i] < EMPTY_RETRY_MAX) {
        if (emptyRetryCount[i] == 0 ||
            (now - lastEmptyRetrySent[i] >= EMPTY_RETRY_INTERVAL)) {

          Serial.print("EMPTY:");
          Serial.println(i + 1);          // sends "EMPTY:1", "EMPTY:2", or "EMPTY:3"

          Serial.print("[LDR] EMPTY sent (");
          Serial.print(emptyRetryCount[i] + 1);
          Serial.print("/"); Serial.print(EMPTY_RETRY_MAX);
          Serial.print(") — Slot "); Serial.print(i + 1);
          Serial.print(" LDR: "); Serial.println(ldrValue);

          emptyRetryCount[i]++;
          lastEmptyRetrySent[i] = now;
        }
      } else {
        // All retries sent → deactivate THIS slot only
        Serial.print("[LDR] All retries done. Slot ");
        Serial.print(i + 1);
        Serial.println(" monitoring OFF.");

        slotActive[i]       = false;
        emptyDetected[i]    = false;
        emptyRetryCount[i]  = 0;

        // Show remaining active slots
        Serial.print("[INFO] Still monitoring slots: ");
        bool anyActive = false;
        for (int j = 0; j < NUM_SLOTS; j++) {
          if (slotActive[j]) { Serial.print(j + 1); Serial.print(" "); anyActive = true; }
        }
        if (!anyActive) Serial.print("none");
        Serial.println();
      }
    }

    // Update lastOccupied for next loop
    lastOccupied[i] = isOccupied;
  }

  // ----------------------------------------------------------
  // 6. Debug — print active slot LDR values every 2 seconds
  // ----------------------------------------------------------
  if (now - lastDebugPrint >= DEBUG_INTERVAL) {
    lastDebugPrint = now;
    bool anyActive = false;
    for (int i = 0; i < NUM_SLOTS; i++) {
      if (slotActive[i] && !inEntryWindow[i]) {
        int val = analogRead(ldrPins[i]);
        Serial.print("[DEBUG] Slot "); Serial.print(i + 1);
        Serial.print(" LDR="); Serial.print(val);
        Serial.println(val < LDR_THRESHOLD ? " OCCUPIED" : " EMPTY/LIGHT");
        anyActive = true;
      }
    }
    // Uncomment below if you want a heartbeat even when no slots are active:
    // if (!anyActive) Serial.println("[DEBUG] No slots active.");
  }

  delay(100);
}
