#pragma once

#include <cassert>
#include <cctype>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

static uint64_t mock_time_us = 0;
static int mock_tx_count = 0;
static int mock_write_count = 0;
static int mock_radio_error = 0;
static int mock_standby_error = 0;
static int mock_reset_level = 1;
static bool mock_rmt_ok = true;
static bool mock_eot_ok = true;
static std::string mock_output;

struct rmt_data_t {
  uint32_t duration0 : 15;
  uint32_t level0 : 1;
  uint32_t duration1 : 15;
  uint32_t level1 : 1;
};

static std::vector<rmt_data_t> mock_symbols;

struct SerialMock {
  std::string input;
  size_t offset = 0;
  void begin(int) {}
  int available() { return offset < input.size(); }
  int read() { return static_cast<unsigned char>(input[offset++]); }
  void println(const char* text = "") { mock_output += std::string(text) + "\n"; }
  void printf(const char* format, ...) {
    char buffer[512];
    va_list arguments;
    va_start(arguments, format);
    vsnprintf(buffer, sizeof(buffer), format, arguments);
    va_end(arguments);
    mock_output += buffer;
  }
};

static SerialMock Serial;
constexpr int OUTPUT = 1, LOW = 0;
constexpr int RMT_TX_MODE = 0, RMT_MEM_NUM_BLOCKS_1 = 1;
constexpr int RADIOLIB_ERR_NONE = 0, RADIOLIB_SHAPING_NONE = 0;

inline uint32_t millis() { return static_cast<uint32_t>(mock_time_us / 1000); }
inline uint32_t micros() { return static_cast<uint32_t>(mock_time_us); }
inline void delay(uint32_t duration) { mock_time_us += duration * 1000ULL; }
inline void pinMode(int, int) {}
inline void digitalWrite(int, int level) { mock_reset_level = level; }
inline bool rmtInit(int, int, int, uint32_t) { return true; }
inline bool rmtSetEOT(int, uint8_t) { return mock_eot_ok; }
inline bool rmtWrite(int, rmt_data_t* symbols, size_t count, uint32_t timeout_ms) {
  ++mock_write_count;
  mock_symbols.assign(symbols, symbols + count);
  if (!mock_rmt_ok) {
    mock_time_us += timeout_ms * 1000ULL;
    return false;
  }
  for (size_t index = 0; index < count; ++index) {
    mock_time_us += symbols[index].duration0 + symbols[index].duration1;
  }
  return true;
}

struct Module { Module(int, int, int, int) {} };
struct SX1276 {
  SX1276(Module*) {}
  int beginFSK(float, double, float, double, int, int, bool) { return mock_radio_error; }
  int setDataShaping(int) { return mock_radio_error; }
  int setFrequency(float) { return mock_radio_error; }
  int setFrequencyDeviation(float) { return mock_radio_error; }
  int setOutputPower(int, bool) { return mock_radio_error; }
  int standby() { return mock_standby_error; }
  int transmitDirect() { ++mock_tx_count; return mock_radio_error; }
};