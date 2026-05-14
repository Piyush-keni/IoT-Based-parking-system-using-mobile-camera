// ============================================================
//  SmartPark Pro — Google Apps Script (COMPLETE)
//  Steps:
//  1. Go to script.google.com → New Project
//  2. Paste this entire code
//  3. Deploy → New Deployment → Web App → Anyone
//  4. Copy the URL → paste in main.py and index.html
// ============================================================

var SHEET_NAME = "Bookings";
var SECRET_TOKEN = "YOUR_SECRET_TOKEN_HERE"; // Set a random string here, same as in main.py

// ============================================================
//  doGet — Called when main.py fetches SHEET_CSV_URL
//          or index.html fetches slot status
// ============================================================

function doGet(e) {
  // 1. Get your ID from the browser URL of your Google Sheet
  var ss = SpreadsheetApp.openById("YOUR_SPREADSHEET_ID_HERE"); // Get this from your Sheet's browser URL
  
  // 2. Access the sheet by name
  var sheet = ss.getSheetByName("Bookings");
  
  // 3. Safety check: If sheet is still null, tell us why
  if (!sheet) {
    return ContentService.createTextOutput("Error: Could not find sheet named " + SHEET_NAME);
  }

  var data = sheet.getDataRange().getValues();
  var type = e.parameter.type;
  
  // ... rest of your logic
  var type  = e.parameter.type;

  // ── JSON mode → for index.html updateUI() ──
  // Called as: SCRIPT_URL + "?type=json"
  if (type === "json") {
    var rows = [];
    for (var i = 1; i < data.length; i++) {
      rows.push(data[i]);
    }
    return ContentService
      .createTextOutput(JSON.stringify(rows))
      .setMimeType(ContentService.MimeType.JSON);
  }

  // ── CSV mode → for main.py pandas read ──
  // Called as: SCRIPT_URL (no params)
  var rows = [];
  for (var i = 1; i < data.length; i++) {
    rows.push(data[i].join(","));
  }
  var csv = "timestamp,user,car,slot\n" + rows.join("\n");
  return ContentService
    .createTextOutput(csv)
    .setMimeType(ContentService.MimeType.TEXT);
}

// ============================================================
//  doPost — Called when:
//    1. index.html confirms a booking  → action: "BOOK"
//    2. main.py detects car left (LDR) → action: "EXIT"
// ============================================================

function doPost(e) {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);

  // Parse incoming JSON body
  var body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return ContentService.createTextOutput(
      JSON.stringify({ status: "ERROR", message: "Invalid JSON" })
    ).setMimeType(ContentService.MimeType.JSON);
  }

  // ── Token Auth: reject requests without the correct secret ──
  if ((body.token || "") !== SECRET_TOKEN) {
    return ContentService.createTextOutput(
      JSON.stringify({ status: "ERROR", message: "Unauthorized" })
    ).setMimeType(ContentService.MimeType.JSON);
  }

  var action = (body.action || "").toString().toUpperCase();

  // ── BOOK: Save new booking from index.html ──────────────
  // Receives: { action:"BOOK", user:"Rahul", car:"MH12AB1234", slot:1 }
  if (action === "BOOK") {

    // Check if slot is already occupied
    var allData = sheet.getDataRange().getValues();
    for (var i = 1; i < allData.length; i++) {
      if (allData[i][3].toString() === body.slot.toString()) {
        return ContentService.createTextOutput(
          JSON.stringify({ status: "ERROR", message: "Slot already booked" })
        ).setMimeType(ContentService.MimeType.JSON);
      }
    }

    // Check if same car plate already has a booking
    var carPlate = (body.car || "").toString().toUpperCase().trim();
    for (var i = 1; i < allData.length; i++) {
      if (allData[i][2].toString().toUpperCase() === carPlate) {
        return ContentService.createTextOutput(
          JSON.stringify({ status: "ERROR", message: "Vehicle already has a booking" })
        ).setMimeType(ContentService.MimeType.JSON);
      }
    }

    var timestamp = new Date().toLocaleString("en-IN", { timeZone: "Asia/Kolkata" });
    var user      = (body.user || "Unknown").toString();
    var slot      = (body.slot || "").toString();

    sheet.appendRow([timestamp, user, carPlate, slot]);

    return ContentService.createTextOutput(
      JSON.stringify({ status: "OK", message: "Booking saved", slot: slot, car: carPlate })
    ).setMimeType(ContentService.MimeType.JSON);
  }

  // ── EXIT: Clear slot when car leaves (LDR triggered) ────
  // Receives: { action:"EXIT", slot:"1" }
  if (action === "EXIT") {
    var slotToRemove = (body.slot || "").toString();
    var allData = sheet.getDataRange().getValues();
    var removed = false;

    // Search from bottom → removes latest booking for that slot
    for (var i = allData.length - 1; i >= 1; i--) {
      if (allData[i][3].toString() === slotToRemove) {
        sheet.deleteRow(i + 1); // +1 because sheet is 1-indexed
        removed = true;
        break;
      }
    }

    if (removed) {
      return ContentService.createTextOutput(
        JSON.stringify({ status: "OK", message: "Slot " + slotToRemove + " cleared" })
      ).setMimeType(ContentService.MimeType.JSON);
    } else {
      return ContentService.createTextOutput(
        JSON.stringify({ status: "ERROR", message: "Slot " + slotToRemove + " not found" })
      ).setMimeType(ContentService.MimeType.JSON);
    }
  }

  // ── Unknown action ───────────────────────────────────────
  return ContentService.createTextOutput(
    JSON.stringify({ status: "ERROR", message: "Unknown action: " + action })
  ).setMimeType(ContentService.MimeType.JSON);
}

// ============================================================
//  TEST FUNCTION — Run this manually to verify setup
//  Click Run → testSetup in the Apps Script editor
// ============================================================

function testSetup() {
  var ss    = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(SHEET_NAME);

  if (!sheet) {
    // Auto-create the sheet with headers if it doesn't exist
    sheet = ss.insertSheet(SHEET_NAME);
    sheet.appendRow(["timestamp", "user", "car", "slot"]);
    Logger.log("✅ Sheet '" + SHEET_NAME + "' created with headers!");
  } else {
    Logger.log("✅ Sheet '" + SHEET_NAME + "' found. Rows: " + sheet.getLastRow());
  }

  // Show the spreadsheet URL
  Logger.log("📄 Spreadsheet URL: " + ss.getUrl());
}