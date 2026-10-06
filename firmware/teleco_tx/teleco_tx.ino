// teleco_tx - minimal 868 MHz 2-FSK transmitter for the Teleco radio link.
//
// Target: TTGO/LilyGO LoRa32 "T3 v1.6.1" (ESP32-PICO-D4 + Semtech SX1276).
// It reproduces the physical layer documented in docs/radio.md:
//
//   868.30 MHz, 2-FSK, deviation ~+/-20 kHz, 515 us time unit.
//   sync = upper tone, 4 units
//   64 segments, alternating lower/upper tone, first segment lower:
//     1 unit = bit 0, 2 units = bit 1   (pulse-width coded)
//   gap  = lower tone, ~16 units
//   the whole 8-byte frame (MSB first) is repeated back to back for ~1.6 s.
//
// The SX1276 runs in FSK *continuous* mode (packet engine bypassed), so there is
// no preamble, sync word, CRC, whitening or header. The chip's DIO2 pin is the
// data line: its level selects the tone. The ESP32 RMT peripheral drives DIO2
// with hardware timing, so each tone is held for an exact number of 515 us units
// with no FreeRTOS / interrupt jitter.
//
// This firmware NEVER transmits on boot. It emits exactly one bounded burst per
// explicit USB-serial command, after validating the frame. Transmitting on
// 868 MHz is regulated (EU 868.0-868.6 MHz band, 1% duty cycle): that is the
// operator's responsibility. Build frames with the project's Python library.
//
// SPDX-License-Identifier: MIT

#include <RadioLib.h>
#include <errno.h>
#include <math.h>
#include <stdlib.h>

// ---- SX1276 wiring on the T3 v1.6.1 (Paxcounter "ttgov21new" pinmap) ----
static const int PIN_NSS  = 18;
static const int PIN_DIO0 = 26;  // IRQ (unused here)
static const int PIN_RST  = 23;
static const int PIN_DIO1 = 33;  // DCLK in continuous mode (unused here)
static const int PIN_DIO2 = 32;  // DATA in continuous mode  <-- RMT drives this

// ---- protocol timing ----
static const uint32_t UNIT_US    = 515;        // one time unit
static const uint32_t RMT_HZ     = 1000000;    // 1 tick = 1 us
static const uint16_t UNIT_TICKS = UNIT_US;    // ticks per unit at 1 MHz
static const uint16_t SYNC_TICKS = 4  * UNIT_TICKS;
static const uint16_t GAP_TICKS  = 16 * UNIT_TICKS;
static const int      N_SEGMENTS = 64;
static const int      N_PULSES   = 1 + N_SEGMENTS + 1;   // sync + segments + gap
static const int      N_SYMBOLS  = (N_PULSES + 1) / 2;   // 2 pulses per rmt symbol

// ---- radio defaults (all bench-test safe) ----
static const char*  FW_VERSION  = "teleco_tx 0.1";
static float   g_freq_mhz   = 868.30f;   // carrier (two tones sit at +/- deviation)
static float   g_dev_khz    = 20.0f;     // FSK deviation
static int8_t  g_power_dbm   = 2;         // lowest PA_BOOST level, avoids saturating the SDR
static uint8_t g_upper_level = 1;         // DIO2 level that produces the UPPER tone (flip with POL)
static uint32_t g_duty_min_ms = 5000;     // minimum spacing between bursts (cadence guard)

static const int8_t  POWER_MIN = 2, POWER_MAX = 17;
static const int     REPS_MAX  = 40;
static const float   FREQ_MIN = 863.0f, FREQ_MAX = 870.0f;

SX1276 radio = new Module(PIN_NSS, PIN_DIO0, PIN_RST, PIN_DIO1);

static bool     g_radio_ready = false;
static bool     g_rmt_ready   = false;
static uint32_t g_last_tx_end = 0;
static bool     g_has_transmitted = false;
static uint32_t g_cooldown_ms = 0;
static rmt_data_t g_sym[N_SYMBOLS * REPS_MAX];

// ---------------------------------------------------------------- RMT builder
static int g_sym_count = 0;
static int g_slot = 0;  // 0 => fill level0/duration0, 1 => fill level1/duration1

static void pulsesReset() { g_sym_count = 0; g_slot = 0; }

static void pushPulse(uint16_t ticks, uint8_t level) {
  if (g_slot == 0) {
    g_sym[g_sym_count].duration0 = ticks;
    g_sym[g_sym_count].level0    = level;
    g_sym[g_sym_count].duration1 = 0;
    g_sym[g_sym_count].level1    = 0;
    g_slot = 1;
  } else {
    g_sym[g_sym_count].duration1 = ticks;
    g_sym[g_sym_count].level1    = level;
    g_sym_count++;
    g_slot = 0;
  }
}

// Build the RMT symbol list for one frame (sync + 64 segments + gap).
static void buildFrame(const uint8_t frame[8]) {
  const uint8_t upper = g_upper_level;
  const uint8_t lower = g_upper_level ^ 1u;

  pulsesReset();
  pushPulse(SYNC_TICKS, upper);                       // sync: upper tone, 4 units
  for (int i = 0; i < N_SEGMENTS; i++) {
    const uint8_t tone = (i & 1) ? upper : lower;     // seg 0 lower, then alternate
    const uint8_t bit  = (frame[i >> 3] >> (7 - (i & 7))) & 1u;  // MSB first
    pushPulse(bit ? (2 * UNIT_TICKS) : (1 * UNIT_TICKS), tone);  // 2 units=1, 1 unit=0
  }
  pushPulse(GAP_TICKS, lower);                        // gap: lower tone, 16 units
}

static int popcount64(const uint8_t frame[8]) {
  int n = 0;
  for (int i = 0; i < 8; i++) n += __builtin_popcount(frame[i]);
  return n;
}

// Frames-per-burst = how many ~(84+popcount)-unit frames fit in ~1.609 s.
static int autoReps(const uint8_t frame[8]) {
  const uint32_t frame_us = (84u + popcount64(frame)) * UNIT_US;
  int reps = (int)((1609000.0 / (double)frame_us) + 0.5);
  if (reps < 1) reps = 1;
  if (reps > REPS_MAX) reps = REPS_MAX;
  return reps;
}

// ---------------------------------------------------------------- radio setup
static bool radioStatus(int status, const char* operation) {
  if (status == RADIOLIB_ERR_NONE) return true;
  g_radio_ready = false;
  Serial.printf("ERR %s=%d; radio disabled until restart\n", operation, status);
  pinMode(PIN_RST, OUTPUT);
  digitalWrite(PIN_RST, LOW);
  return false;
}

static bool radioConfigure() {
  int st = radio.beginFSK(g_freq_mhz, 4.8 /*br, unused in continuous*/,
                          g_dev_khz, 125.0 /*rxBw*/, g_power_dbm,
                          16 /*preamble*/, false /*OOK*/);
  if (!radioStatus(st, "beginFSK")) return false;
  if (!radioStatus(radio.setDataShaping(RADIOLIB_SHAPING_NONE), "setDataShaping")) return false;
  if (!radioStatus(radio.setFrequency(g_freq_mhz), "setFrequency")) return false;
  if (!radioStatus(radio.setFrequencyDeviation(g_dev_khz), "setFrequencyDeviation")) return false;
  if (!radioStatus(radio.setOutputPower(g_power_dbm, false), "setOutputPower")) return false;
  return radioStatus(radio.standby(), "standby");
}

// One bounded burst. Returns false (and goes to standby) on any error.
static bool emitBurst(const uint8_t frame[8], int reps, uint32_t* dur_ms_out) {
  if (!g_radio_ready || !g_rmt_ready) { Serial.println("ERR radio/rmt not ready"); return false; }
  if (reps < 1 || reps > REPS_MAX) { Serial.println("ERR reps out of range"); return false; }

  const uint32_t now = millis();
  const uint32_t since = now - g_last_tx_end;
  const uint32_t wait_ms = g_cooldown_ms > g_duty_min_ms ? g_cooldown_ms : g_duty_min_ms;
  if (g_has_transmitted && since < wait_ms) {
    Serial.printf("ERR duty: wait %lu ms\n", (unsigned long)(wait_ms - since));
    return false;
  }

  buildFrame(frame);
  for (int repeat = 1; repeat < reps; repeat++) {
    memcpy(g_sym + repeat * N_SYMBOLS, g_sym, N_SYMBOLS * sizeof(g_sym[0]));
  }
  const uint32_t expected_us = (84u + popcount64(frame)) * UNIT_US * reps;
  const uint32_t expected_ms = (expected_us + 999u) / 1000u;

  // Between/after frames the RMT pin rests at the EOT level. Hold it at the
  // lower tone so the gap and the pre-sync rest never glitch to the upper tone,
  // whatever the current polarity (POL).
  if (!rmtSetEOT(PIN_DIO2, (uint8_t)(g_upper_level ^ 1u))) {
    g_rmt_ready = false;
    Serial.println("ERR rmtSetEOT; RMT disabled until restart");
    return false;
  }

  const uint32_t started_us = micros();
  int st = radio.transmitDirect();   // key carrier, continuous mode, DIO2 = DATA
  bool ok = radioStatus(st, "transmitDirect");
  if (ok && !rmtWrite(PIN_DIO2, g_sym, N_SYMBOLS * reps, expected_ms + 300u)) {
    g_rmt_ready = false;
    Serial.println("ERR rmtWrite; RMT disabled until restart");
    ok = false;
  }

  if (g_radio_ready && !radioStatus(radio.standby(), "standby")) ok = false;
  const uint32_t elapsed_ms = (micros() - started_us + 999u) / 1000u;
  const uint32_t charged_ms = elapsed_ms > expected_ms ? elapsed_ms : expected_ms;
  g_cooldown_ms = charged_ms * 99u;
  g_last_tx_end = millis();
  g_has_transmitted = true;
  if (dur_ms_out) *dur_ms_out = elapsed_ms;
  return ok;
}

// ---------------------------------------------------------------- serial I/O
static bool parseInteger(const char* text, long minimum, long maximum, long* result) {
  if (!text || !*text) return false;
  char* end = nullptr;
  errno = 0;
  const long value = strtol(text, &end, 10);
  if (errno || *end || value < minimum || value > maximum) return false;
  *result = value;
  return true;
}

static bool parseFrequency(const char* text, float* result) {
  if (!text || !*text) return false;
  char* end = nullptr;
  errno = 0;
  const float value = strtof(text, &end);
  if (errno || *end || !isfinite(value) || value < FREQ_MIN || value > FREQ_MAX) return false;
  *result = value;
  return true;
}

static int hexNibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

// Parse exactly 16 hex chars into 8 bytes. Returns false on bad length/char.
static bool parseFrame(const char* s, uint8_t out[8]) {
  int n = 0;
  while (s[n]) n++;
  if (n != 16) return false;
  for (int i = 0; i < 8; i++) {
    int hi = hexNibble(s[2 * i]), lo = hexNibble(s[2 * i + 1]);
    if (hi < 0 || lo < 0) return false;
    out[i] = (uint8_t)((hi << 4) | lo);
  }
  return true;
}

static bool checksumOk(const uint8_t frame[8]) {
  uint32_t sum = 0;
  for (int i = 0; i < 8; i++) sum += frame[i];
  return (sum & 0xFFu) == 0;   // byte7 makes all 8 bytes sum to 0 mod 256
}

static void doTx(const char* hex, int reps_override) {
  uint8_t frame[8];
  if (!parseFrame(hex, frame)) { Serial.println("ERR frame: need 16 hex chars (8 bytes)"); return; }
  if (!checksumOk(frame)) {
    uint32_t sum = 0; for (int i = 0; i < 8; i++) sum += frame[i];
    Serial.printf("ERR checksum: 8 bytes sum to 0x%02lX, must be 0x00 mod 256\n", (unsigned long)(sum & 0xFF));
    return;
  }
  int reps = (reps_override > 0) ? reps_override : autoReps(frame);
  uint32_t dur = 0;
  if (emitBurst(frame, reps, &dur)) {
    Serial.printf("OK TX reps=%d pop=%d dur_ms=%lu\n", reps, popcount64(frame), (unsigned long)dur);
  }
}

static void printInfo() {
  Serial.printf("%s\n", FW_VERSION);
  Serial.printf("radio=%s SX1276 freq=%.3f MHz dev=%.1f kHz power=%d dBm(PA_BOOST)\n",
                g_radio_ready ? "ready" : "FAIL", g_freq_mhz, g_dev_khz, (int)g_power_dbm);
  Serial.printf("unit=%lu us sync=4u gap=16u upper_tone_level=DIO2:%d\n",
                (unsigned long)UNIT_US, (int)g_upper_level);
  Serial.printf("pins NSS=%d DIO0=%d RST=%d DIO1=%d DIO2(DATA)=%d\n",
                PIN_NSS, PIN_DIO0, PIN_RST, PIN_DIO1, PIN_DIO2);
  Serial.printf("duty_min=%lu ms cooldown=%lu ms duty_limit=1%% reps_max=%d rmt=%s\n",
                (unsigned long)g_duty_min_ms, (unsigned long)g_cooldown_ms,
                REPS_MAX, g_rmt_ready ? "ready" : "FAIL");
  Serial.println("cmds: PING | INFO | TX <16hex> | TXN <16hex> <reps> | POL <0|1> | PWR <dBm> | FREQ <MHz> | DUTY <sec>");
}

static void handleLine(char* line) {
  // split command and up to two args
  char* cmd = strtok(line, " \t");
  if (!cmd) return;
  for (char* p = cmd; *p; p++) *p = toupper((unsigned char)*p);
  char* a1 = strtok(NULL, " \t");
  char* a2 = strtok(NULL, " \t");
  char* a3 = strtok(NULL, " \t");
  if (a3 || (a2 && strcmp(cmd, "TXN"))) { Serial.println("ERR extra arguments"); return; }

  if (!strcmp(cmd, "PING")) {
    if (a1) { Serial.println("ERR usage: PING"); return; }
    Serial.println("OK PONG"); return;
  }
  if (!strcmp(cmd, "INFO")) {
    if (a1) { Serial.println("ERR usage: INFO"); return; }
    printInfo(); return;
  }

  if (!strcmp(cmd, "TX")) {
    if (!a1) { Serial.println("ERR usage: TX <16hex>"); return; }
    doTx(a1, 0);
    return;
  }
  if (!strcmp(cmd, "TXN")) {
    if (!a1 || !a2) { Serial.println("ERR usage: TXN <16hex> <reps>"); return; }
    long reps;
    if (!parseInteger(a2, 1, REPS_MAX, &reps)) { Serial.printf("ERR reps 1..%d\n", REPS_MAX); return; }
    doTx(a1, (int)reps);
    return;
  }
  if (!strcmp(cmd, "POL")) {
    if (!a1) { Serial.println("ERR usage: POL <0|1>"); return; }
    long v;
    if (!parseInteger(a1, 0, 1, &v)) { Serial.println("ERR POL 0 or 1"); return; }
    g_upper_level = (uint8_t)v;
    Serial.printf("OK POL upper_tone_level=DIO2:%ld\n", v);
    return;
  }
  if (!strcmp(cmd, "PWR")) {
    if (!a1) { Serial.println("ERR usage: PWR <dBm>"); return; }
    long v;
    if (!parseInteger(a1, POWER_MIN, POWER_MAX, &v)) { Serial.printf("ERR PWR %d..%d\n", POWER_MIN, POWER_MAX); return; }
    if (!g_radio_ready) { Serial.println("ERR radio disabled until restart"); return; }
    if (!radioStatus(radio.setOutputPower((int8_t)v, false), "setOutputPower")) return;
    if (!radioStatus(radio.standby(), "standby")) return;
    g_power_dbm = (int8_t)v;
    Serial.printf("OK PWR=%ld dBm\n", v);
    return;
  }
  if (!strcmp(cmd, "FREQ")) {
    if (!a1) { Serial.println("ERR usage: FREQ <MHz>"); return; }
    float frequency;
    if (!parseFrequency(a1, &frequency)) { Serial.printf("ERR FREQ %.1f..%.1f\n", FREQ_MIN, FREQ_MAX); return; }
    if (!g_radio_ready) { Serial.println("ERR radio disabled until restart"); return; }
    if (!radioStatus(radio.setFrequency(frequency), "setFrequency")) return;
    if (!radioStatus(radio.standby(), "standby")) return;
    g_freq_mhz = frequency;
    Serial.printf("OK FREQ=%.3f MHz\n", frequency);
    return;
  }
  if (!strcmp(cmd, "DUTY")) {
    if (!a1) { Serial.println("ERR usage: DUTY <sec>"); return; }
    long seconds;
    if (!parseInteger(a1, 0, 3600, &seconds)) { Serial.println("ERR DUTY 0..3600 s"); return; }
    g_duty_min_ms = (uint32_t)seconds * 1000u;
    Serial.printf("OK DUTY=%ld s (1%% cooldown remains enforced)\n", seconds);
    return;
  }
  Serial.println("ERR unknown command (try INFO)");
}

// ---------------------------------------------------------------- Arduino
void setup() {
  Serial.begin(115200);
  delay(200);

  // DIO2 driven by the RMT peripheral; idle low = lower tone, so the carrier
  // never jumps to the upper tone between frames.
  g_rmt_ready = rmtInit(PIN_DIO2, RMT_TX_MODE, RMT_MEM_NUM_BLOCKS_1, RMT_HZ);
  if (!g_rmt_ready) Serial.println("ERR rmtInit");
  if (g_rmt_ready && !rmtSetEOT(PIN_DIO2, (uint8_t)(g_upper_level ^ 1u))) {
    g_rmt_ready = false;
    Serial.println("ERR rmtSetEOT");
  }

  g_radio_ready = radioConfigure();

  Serial.println();
  Serial.println("== teleco_tx : 868 MHz 2-FSK test transmitter ==");
  Serial.println("Does NOT transmit on boot. One bounded burst per TX command.");
  Serial.println("868 MHz is regulated (EU 868.0-868.6, 1% duty) - your responsibility.");
  printInfo();
  Serial.println("ready>");
}

void loop() {
  static char buf[80];
  static size_t len = 0;
  static bool dropping_line = false;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      buf[len] = '\0';
      if (!dropping_line && len > 0) handleLine(buf);
      len = 0;
      dropping_line = false;
    } else if (dropping_line) {
      continue;
    } else if (c == '\0') {
      len = 0;
      dropping_line = true;
      Serial.println("ERR invalid character");
    } else if (len < sizeof(buf) - 1) {
      buf[len++] = c;
    } else {
      len = 0;
      dropping_line = true;
      Serial.println("ERR line too long");
    }
  }
}
