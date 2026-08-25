// Pull the current frame over WiFi and put it on the glass.
//
// This replaces baking the image into the firmware as a C array. That worked,
// but cost a full recompile and USB re-flash for every single track - roughly
// 13 s of build and upload before the 22 s refresh could even begin - and it
// chained the panel to a cable. Here the firmware is flashed once and fetches
// frames for the rest of its life.
//
// Long polling, not interval polling. The panel asks for the frame and the
// server HOLDS the request open until the track actually changes, so a new song
// starts drawing about a second after it starts playing. Interval polling meant
// waiting out the poll period first, on top of the refresh. What remains is the
// refresh itself - 22.4 s measured, the full six-pigment waveform - and that is
// physics, not something firmware can shorten.
//
// Deliberately plain HTTP, not HTTPS. The server is the Mac on the same LAN,
// and mbedTLS costs about 45 KB of heap this chip does not have to spare once a
// 120 KB frame buffer and the WiFi stack are resident.
//
// Two ordering constraints that are not obvious:
//   * the frame is NEVER held in RAM. 120,000 bytes does not fit: on this
//     ESP32-D0WDQ6 the largest contiguous free block at boot is 110,580,
//     measured with the allocation done before anything else touched the heap.
//     Free heap was never the problem, fragmentation was, so allocating earlier
//     could not have helped. Bytes go from the socket to the panel as they
//     arrive, through a small chunk buffer.
//   * nothing reaches the glass until the refresh command, so a transfer that
//     dies half way is harmless - it just never triggers a refresh.

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <ESPmDNS.h>
// No extern "C" here: the vendored driver is compiled as C++ (EPD_4in0e.cpp,
// DEV_Config.cpp) and its header carries no linkage guards, so its symbols are
// C++-mangled. Wrapping the include made the linker hunt for unmangled names.
#include "EPD_4in0e.h"
#include <esp_heap_caps.h>
#include <esp_system.h>
#include "secrets.h"

#ifndef LONG_POLL_SECONDS
// How long the server may hold a request open while nothing changes. Shorter
// than any NAT/router idle timeout, long enough that idle traffic is trivial.
#define LONG_POLL_SECONDS 50
#endif
// The direct join either works almost at once or the hint is stale; waiting
// the full timeout on it would make a stale hint cost more than it saves.
#ifndef WIFI_FAST_MS
#define WIFI_FAST_MS 6000
#endif
#ifndef WIFI_TIMEOUT_MS
#define WIFI_TIMEOUT_MS 20000
#endif

#define IMG_BYTES ((size_t)EPD_4IN0E_WIDTH * EPD_4IN0E_HEIGHT / 2)

#define CHUNK 2048          // small enough to always allocate, big enough to be quick

static Preferences prefs;
static String gBase;          // FRAME_URL with the host resolved to an address

// True if addr is one we could actually route to: non-zero, and sharing the
// subnet the DHCP lease put us on.
static bool onOurSubnet(const IPAddress &addr) {
  if (addr == IPAddress((uint32_t)0)) return false;
  uint32_t mask = (uint32_t)WiFi.subnetMask();
  if (!mask) return false;
  return ((uint32_t)addr & mask) == ((uint32_t)WiFi.localIP() & mask);
}

// Resolve the host in FRAME_URL, so a DHCP change does not silently strand the
// panel. A .local name is resolved by mDNS; the result is cached in NVS and
// reused if mDNS later fails, and a literal IP in FRAME_URL is used as-is.
//
// A Mac publishes one A record per interface - loopback, VPNs and virtual
// machine bridges included. This one answers with seven, only one of which is
// reachable from here. queryHost() hands back whichever arrives first, so the
// answer is checked against our own subnet before it is trusted, and only a
// checked address is ever written to NVS. Caching an unreachable one would
// strand the panel across reboots, which is the failure this whole function
// exists to prevent.
static String resolveBase() {
  String url = FRAME_URL;
  int hs = url.indexOf("//");
  if (hs < 0) return url;
  hs += 2;
  int he = url.indexOf('/', hs);
  if (he < 0) he = url.length();
  String hostport = url.substring(hs, he);
  int colon = hostport.indexOf(':');
  String host = colon < 0 ? hostport : hostport.substring(0, colon);
  String port = colon < 0 ? "" : hostport.substring(colon);

  if (!host.endsWith(".local")) return url;      // literal address, nothing to do

  String bare = host.substring(0, host.length() - 6);
  IPAddress ip;
  if (MDNS.begin("reframe-panel")) {
    ip = MDNS.queryHost(bare, 4000);
  }
  if (!onOurSubnet(ip)) {
    if (ip != IPAddress((uint32_t)0))
      Serial.printf("ignoring %s, not on our subnet\n", ip.toString().c_str());
    IPAddress cached;
    if (cached.fromString(prefs.getString("hostip", "")) && onOurSubnet(cached)) {
      Serial.printf("using cached %s\n", cached.toString().c_str());
      return url.substring(0, hs) + cached.toString() + port + url.substring(he);
    }
    Serial.println("no usable address - keeping the name, will retry next cycle");
    return url;
  }
  String addr = ip.toString();
  if (addr != prefs.getString("hostip", "")) {
    prefs.putString("hostip", addr);
    Serial.printf("resolved %s -> %s\n", host.c_str(), addr.c_str());
  }
  return url.substring(0, hs) + addr + port + url.substring(he);
}
static uint8_t chunk[CHUNK];
static String etag;

// Wait for association, or give up. Polls often enough that the wait costs
// little more than the association itself.
static bool awaitLink(uint32_t timeout) {
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - t0 > timeout) return false;
    delay(50);
    if ((millis() - t0) % 500 < 50) Serial.print(".");
  }
  return true;
}

// Remember which access point answered, so the next join can go straight to it.
static void rememberAp() {
  uint8_t *b = WiFi.BSSID();
  if (!b) return;
  uint8_t known[6];
  size_t n = prefs.getBytes("bssid", known, sizeof known);
  if (n != sizeof known || memcmp(known, b, sizeof known) != 0)
    prefs.putBytes("bssid", b, sizeof known);
  if (prefs.getUChar("chan", 0) != WiFi.channel())
    prefs.putUChar("chan", WiFi.channel());
}

static bool joinWifi() {
  if (WiFi.status() == WL_CONNECTED) return true;
  Serial.print("connecting to wifi");
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(true);

  // A plain begin() scans every channel before it finds the AP, which is most
  // of the join. Naming the AP and its channel skips the scan outright. The
  // hint goes stale when the router reboots or the panel roams, so a failure
  // here is expected occasionally and simply falls through to a full scan.
  uint8_t bssid[6];
  uint8_t chan = prefs.getUChar("chan", 0);
  if (chan && prefs.getBytes("bssid", bssid, sizeof bssid) == sizeof bssid) {
    WiFi.begin(WIFI_SSID, WIFI_PASS, chan, bssid);
    if (awaitLink(WIFI_FAST_MS)) {
      Serial.printf(" ok (direct, ch %u), ip %s, rssi %d\n",
                    chan, WiFi.localIP().toString().c_str(), WiFi.RSSI());
      return true;
    }
    Serial.print(" (ap moved)");
    WiFi.disconnect(true);
  }

  WiFi.begin(WIFI_SSID, WIFI_PASS);
  if (!awaitLink(WIFI_TIMEOUT_MS)) { Serial.println(" failed"); return false; }
  rememberAp();
  Serial.printf(" ok, ip %s, rssi %d\n",
                WiFi.localIP().toString().c_str(), WiFi.RSSI());
  return true;
}

// Why the panel died last time, in words. Running on a battery there is no
// serial cable attached, so the only way to learn anything is for the board to
// write down what happened and report it next time it is plugged in.
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

// Coarse marker of what the board was doing, so a supply that collapses during
// the panel refresh - the heaviest moment - is distinguishable from one that
// cannot even hold a WiFi association.
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
  gBase = resolveBase();
  Serial.printf("fetching from: %s\n", gBase.c_str());
  mark("idle");
}

void loop() {
  if (!joinWifi()) { delay(10000); return; }

  HTTPClient http;
  if (!gBase.length()) gBase = resolveBase();
  String url = gBase + "?wait=" + String(LONG_POLL_SECONDS);
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
    // Normal idle path. Logged because otherwise a working panel is
    // indistinguishable from a hung one on the serial monitor.
    Serial.printf("no change (held %lus)\n", (millis() - asked) / 1000);
    http.end();
    return;                                          // ask again at once
  }
  if (code == 204) { http.end(); delay(5000); return; }   // nothing playing
  if (code <= 0) {
    Serial.printf("request failed (%d), re-resolving and backing off\n", code);
    http.end();
    gBase = "";          // the server may have moved; resolve again next cycle
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

  // Radio OFF before the refresh. The frame is already in the controller's RAM,
  // so nothing needs the network for the next 20 seconds, and the refresh is
  // the heaviest current draw in the cycle. Keeping WiFi associated through it
  // was a regression: the first design powered the radio down before driving
  // the panel, and long-polling quietly dropped that. Symptom was the board
  // resetting a few seconds into the SECOND refresh - recorded as
  // "last stage before this boot: refreshing" - on USB and on battery alike.
  WiFi.disconnect(true, false);
  WiFi.mode(WIFI_OFF);
  delay(150);                         // let the supply recover before the load

  Serial.println("frame loaded, refreshing (~22 s)...");
  mark("refreshing");
  uint32_t t0 = millis();
  EPD_4IN0E_DisplayFinish();
  uint32_t took = millis() - t0;
  Serial.printf("refresh took %lu ms\n", took);
  // A real six-pigment refresh is ~20.5 s. Anything under a few seconds means
  // the controller never got the frame and there was nothing to wait on, so the
  // glass still shows the old image - do not record this frame as displayed.
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
