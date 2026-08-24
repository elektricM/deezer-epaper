// Fetch the current frame over WiFi and put it on the panel.
//
// The firmware is flashed once and pulls frames for the rest of its life, so a
// track change costs no rebuild and no cable.
//
// Long polling rather than interval polling: the server holds the request open
// until the track changes, so a new song starts drawing about a second after it
// begins. What remains is the ~20.5 s six-ink refresh, which is the panel.
//
// Plain HTTP, not HTTPS - the server is on the same LAN and mbedTLS costs about
// 45 KB of heap this chip cannot spare.
//
// Two constraints worth knowing:
//   * the frame is never held in RAM. 120,000 bytes does not fit: the largest
//     contiguous free block on an ESP32-D0WDQ6 is around 110,580, so bytes go
//     from the socket to the panel through a small chunk buffer.
//   * nothing reaches the glass until the refresh command, so a transfer that
//     dies half way simply never triggers one.

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <ESPmDNS.h>
#include "EPD_4in0e.h"
#include <esp_heap_caps.h>
#include <esp_system.h>
#include "secrets.h"

#ifndef LONG_POLL_SECONDS
// How long the server may hold a request open while nothing changes. Shorter
// than any NAT/router idle timeout, long enough that idle traffic is trivial.
#define LONG_POLL_SECONDS 50
#endif
#ifndef WIFI_TIMEOUT_MS
#define WIFI_TIMEOUT_MS 20000
#endif

#define IMG_BYTES ((size_t)EPD_4IN0E_WIDTH * EPD_4IN0E_HEIGHT / 2)

#define CHUNK 2048          // small enough to always allocate, big enough to be quick

static Preferences prefs;
static uint8_t chunk[CHUNK];
static String etag;

static bool joinWifi() {
  if (WiFi.status() == WL_CONNECTED) return true;
  Serial.print("connecting to wifi");
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - t0 > WIFI_TIMEOUT_MS) { Serial.println(" failed"); return false; }
    delay(250);
    Serial.print(".");
  }
  Serial.printf(" ok, ip %s, rssi %d\n",
                WiFi.localIP().toString().c_str(), WiFi.RSSI());
  return true;
}

// On battery there is no serial cable, so record what happened and report it
// on the next boot.
static const char *resetReason(esp_reset_reason_t r) {
  switch (r) {
    case ESP_RST_POWERON:  return "power on";
    case ESP_RST_EXT:      return "external reset";
    case ESP_RST_SW:       return "software reset";
    case ESP_RST_PANIC:    return "crash (panic)";
    case ESP_RST_INT_WDT:  return "interrupt watchdog";
    case ESP_RST_TASK_WDT: return "task watchdog";
    case ESP_RST_WDT:      return "watchdog";
    case ESP_RST_DEEPSLEEP: return "woke from deep sleep";
    case ESP_RST_BROWNOUT: return "BROWNOUT - supply sagged";
    case ESP_RST_SDIO:     return "sdio";
    default:               return "unknown";
  }
}

// Coarse marker of what the board was doing when it last stopped.
static void mark(const char *stage) {
  prefs.putString("stage", stage);
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n--- reframe panel ---");
  prefs.begin("reframe", false);

  esp_reset_reason_t rr = esp_reset_reason();
  uint32_t boots = prefs.getUInt("boots", 0) + 1;
  prefs.putUInt("boots", boots);
  String lastStage = prefs.getString("stage", "none");
  Serial.printf("boot #%u, reset: %s, last stage before this boot: %s\n",
                (unsigned)boots, resetReason(rr), lastStage.c_str());
  if (rr == ESP_RST_BROWNOUT) {
    Serial.println("*** the power supply could not hold up. A phone charger or");
    Serial.println("*** power bank that cannot deliver ~500 mA, or one that cuts");
    Serial.println("*** out at low draw, will do this.");
  }
  mark("booted");
  Serial.printf("free heap %u, largest contiguous block %u, frame needs %u\n",
                (unsigned)ESP.getFreeHeap(),
                (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT),
                (unsigned)IMG_BYTES);
  etag = prefs.getString("etag", "");
  Serial.printf("frame url: %s\n", FRAME_URL);
  mark("wifi connect");
  joinWifi();
  mark("idle");
}

void loop() {
  if (!joinWifi()) { delay(10000); return; }

  HTTPClient http;
  String url = String(FRAME_URL) + "?wait=" + String(LONG_POLL_SECONDS);
  http.setConnectTimeout(8000);
  // Must outlast the server's hold, or we hang up on our own long poll - but
  // HTTPClient::setTimeout takes a uint16_t, so anything above 65535 ms wraps.
  // (50+20)*1000 silently became 4464 ms, and every long poll died with -11
  // after about 4.5 s. Cap it well inside the type.
  uint32_t want = (uint32_t)(LONG_POLL_SECONDS + 10) * 1000UL;
  http.setTimeout((uint16_t)(want > 60000UL ? 60000UL : want));
  http.setReuse(false);
  if (!http.begin(url)) { delay(5000); return; }
  if (etag.length()) http.addHeader("If-None-Match", etag);
  const char *collect[] = {"ETag", "X-Track", "X-Artist", "X-State"};
  http.collectHeaders(collect, 4);

  uint32_t asked = millis();
  int code = http.GET();

  if (code == 304) {
    // Normal idle path, logged so a working panel is distinguishable from a
    // hung one on the serial monitor.
    Serial.printf("no change (held %lus)\n", (millis() - asked) / 1000);
    http.end();
    return;                                          // ask again at once
  }
  if (code == 204) { http.end(); delay(5000); return; }   // nothing playing
  if (code <= 0) {
    Serial.printf("request failed (%d), backing off\n", code);
    http.end();
    delay(5000);
    return;
  }
  if (code != 200) {
    Serial.printf("http %d, backing off\n", code);
    http.end();
    delay(10000);
    return;
  }

  int len = http.getSize();
  if (len != (int)IMG_BYTES) {
    Serial.printf("unexpected length %d, want %u\n", len, (unsigned)IMG_BYTES);
    http.end();
    delay(5000);
    return;
  }
  Serial.printf("new frame: %s - %s\n",
                http.header("X-Artist").c_str(), http.header("X-Track").c_str());

  // Wake the panel and open the RAM write before pulling any bytes, so each
  // chunk can be handed straight over instead of being stored.
  mark("loading frame");
  DEV_Module_Init();
  EPD_4IN0E_Init();
  EPD_4IN0E_DisplayBegin();

  WiFiClient *stream = http.getStreamPtr();
  size_t got = 0;
  uint32_t last = millis();
  while (got < IMG_BYTES && (http.connected() || stream->available())) {
    size_t want = IMG_BYTES - got;
    if (want > CHUNK) want = CHUNK;
    int n = stream->readBytes(chunk, want);
    if (n > 0) {
      EPD_4IN0E_DisplayFeed(chunk, n);
      got += n;
      last = millis();
    } else if (millis() - last > 15000) {
      break;                                   // stalled
    } else {
      delay(2);
    }
  }
  String newTag = http.header("ETag");
  http.end();

  if (got != IMG_BYTES) {
    // The controller holds a partial frame, but no refresh was triggered, so
    // the glass still shows the last good image. Reset it and try again.
    Serial.printf("short read: %u of %u - not refreshing\n",
                  (unsigned)got, (unsigned)IMG_BYTES);
    EPD_4IN0E_Sleep();
    DEV_Module_Exit();
    delay(3000);
    return;
  }

  // Radio off before the refresh. The frame is already in the controller's
  // RAM, nothing needs the network for the next 20 s, and the refresh is the
  // heaviest current draw in the cycle - leaving WiFi up through it browns out
  // marginal supplies.
  WiFi.disconnect(true, false);
  WiFi.mode(WIFI_OFF);
  delay(150);                         // let the supply recover before the load

  Serial.println("frame loaded, refreshing (~22 s)...");
  mark("refreshing");
  uint32_t t0 = millis();
  EPD_4IN0E_DisplayFinish();
  uint32_t took = millis() - t0;
  Serial.printf("refresh took %lu ms\n", took);
  // A real refresh is ~20.5 s. Anything under a few seconds means the
  // controller never got the frame, so do not record it as displayed.
  bool bogus = took < 5000;
  if (bogus) Serial.println("*** refresh returned far too fast - panel got no data");
  EPD_4IN0E_Sleep();
  DEV_Module_Exit();
  delay(250);                         // and again before the radio comes back

  // Only remember the ETag if the refresh actually completed. If the driver
  // timed out, the glass does not hold this frame, and recording it would mean
  // never retrying - the panel would sit on a half-drawn image indefinitely.
  if (EPD_4IN0E_timed_out || bogus) {
    Serial.println("refresh not trusted - not saving etag, will retry");
  } else if (newTag.length()) {
    etag = newTag;
    prefs.putString("etag", etag);
  }
  mark("idle");
}
