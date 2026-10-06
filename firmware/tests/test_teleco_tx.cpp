#include "../teleco_tx/teleco_tx.ino"

static void command(const std::string& text) {
  Serial.input = text;
  Serial.offset = 0;
  mock_output.clear();
  loop();
}

static void resetState() {
  mock_time_us = 100000;
  mock_tx_count = 0;
  mock_write_count = 0;
  mock_radio_error = 0;
  mock_standby_error = 0;
  mock_reset_level = 1;
  mock_rmt_ok = true;
  mock_eot_ok = true;
  g_radio_ready = true;
  g_rmt_ready = true;
  g_has_transmitted = false;
  g_last_tx_end = 0;
  g_cooldown_ms = 0;
  g_duty_min_ms = 5000;
  g_upper_level = 1;
  g_freq_mhz = 868.30f;
  g_power_dbm = 2;
}

static void testFrameTiming() {
  uint8_t frame[8] = {};
  for (int polarity = 0; polarity < 2; ++polarity) {
    g_upper_level = polarity;
    for (int bit_index = -1; bit_index < 64; ++bit_index) {
      memset(frame, 0, sizeof(frame));
      if (bit_index >= 0) frame[bit_index / 8] = 1u << (7 - bit_index % 8);
      buildFrame(frame);
      assert(g_sym_count == N_SYMBOLS && g_slot == 0);
      uint32_t total = 0;
      for (int pulse_index = 0; pulse_index < N_PULSES; ++pulse_index) {
        const auto symbol = g_sym[pulse_index / 2];
        const uint16_t duration = pulse_index % 2 ? symbol.duration1 : symbol.duration0;
        const uint16_t level = pulse_index % 2 ? symbol.level1 : symbol.level0;
        assert(level == (pulse_index % 2 ? (polarity ^ 1) : polarity));
        const uint16_t expected = pulse_index == 0 ? 2060 : pulse_index == 65 ? 8240 :
            bit_index >= 0 && pulse_index == bit_index + 1 ? 1030 : 515;
        assert(duration == expected);
        total += duration;
      }
      assert(total == (84u + popcount64(frame)) * UNIT_US);
    }
  }
}

static void testSerialValidation() {
  resetState();
  command(std::string(80, 'X') + "TX 0000000000000000\nPING\n");
  assert(mock_tx_count == 0 && mock_output.find("OK PONG") != std::string::npos);
  command(std::string(80, 'X'));
  command("TX 0000000000000000\n");
  assert(mock_tx_count == 0);
  command(std::string("TX 0000000000000000\0", 20) + "\n");
  assert(mock_tx_count == 0);
  for (const char* text : {
      "TX 0000000000000001\n", "TX short\n", "TX z000000000000000\n",
      "TX 0000000000000000 extra\n", "TXN 0000000000000000 2x\n",
      "TXN 0000000000000000 0\n", "TXN 0000000000000000 41\n",
      "TXN 0000000000000000 1 extra\n", "DUTY typo\n", "DUTY -1\n",
      "DUTY 3601\n", "DUTY 999999999999999999999999999999\n", "POL typo\n",
      "PWR 2x\n", "FREQ nan\n", "FREQ inf\n", "FREQ 868.3x\n",
      "FREQ 1e99\n", "FREQ 862\n", "PING extra\n", "INFO extra\n"}) {
    command(text);
    assert(mock_output.find("ERR") != std::string::npos);
    assert(mock_tx_count == 0 && g_duty_min_ms == 5000 && g_freq_mhz == 868.30f);
  }
  command("POL 0\nPWR 3\nFREQ 868.4\nDUTY 3600\n");
  assert(g_upper_level == 0 && g_power_dbm == 3 && g_freq_mhz == 868.4f);
  assert(g_duty_min_ms == 3600000);
}

static void testBurstAndCooldown() {
  for (int polarity = 0; polarity < 2; ++polarity) {
    for (int reps = 1; reps <= REPS_MAX; ++reps) {
      resetState();
      g_upper_level = polarity;
      command("TXN 0000000000000000 " + std::to_string(reps) + "\n");
      assert(mock_tx_count == 1 && mock_write_count == 1);
      assert(mock_output.find("OK TX") != std::string::npos);
      assert(mock_symbols.size() == static_cast<size_t>(N_SYMBOLS * reps));
      for (int repeat = 1; repeat < reps; ++repeat) {
        assert(memcmp(mock_symbols.data(), mock_symbols.data() + repeat * N_SYMBOLS,
                      N_SYMBOLS * sizeof(rmt_data_t)) == 0);
      }
      assert(g_cooldown_ms >= ((84u * UNIT_US * reps + 999u) / 1000u) * 99u);
      command("DUTY 0\nTXN 0000000000000000 1\n");
      assert(mock_tx_count == 1);
      mock_time_us = (static_cast<uint64_t>(g_last_tx_end) + g_cooldown_ms - 1) * 1000;
      command("TXN 0000000000000000 1\n");
      assert(mock_tx_count == 1);
      mock_time_us += 1000;
      command("TXN 0000000000000000 1\n");
      assert(mock_tx_count == 2);
    }
  }
  resetState();
  command("TX 0000000000000000\n");
  assert(mock_symbols.size() == static_cast<size_t>(N_SYMBOLS * 37));
  resetState();
  command("TXN ffffffffffffff07 40\n");
  assert(mock_tx_count == 1 && mock_write_count == 1);
  assert(mock_output.find("OK TX") != std::string::npos);
  assert(mock_time_us - 100000 == (84u + 59u) * UNIT_US * 40u);
  resetState();
  mock_time_us = (UINT64_C(1) << 32) * 1000 - 20000;
  command("TXN 0000000000000000 1\n");
  command("TXN 0000000000000000 1\n");
  assert(mock_tx_count == 1);
  mock_time_us += (g_cooldown_ms > g_duty_min_ms ? g_cooldown_ms : g_duty_min_ms) * 1000ULL;
  command("TXN 0000000000000000 1\n");
  assert(mock_tx_count == 2);
}

static void testFailures() {
  resetState();
  mock_rmt_ok = false;
  command("TXN 0000000000000000 1\n");
  assert(!g_rmt_ready && g_has_transmitted && g_cooldown_ms >= 344u * 99u);
  assert(mock_output.find("OK TX") == std::string::npos);
  command("TX 0000000000000000\n");
  assert(mock_tx_count == 1);

  resetState();
  mock_standby_error = -1;
  command("TXN 0000000000000000 1\n");
  assert(!g_radio_ready && mock_reset_level == LOW);
  assert(mock_output.find("OK TX") == std::string::npos);
  command("TX 0000000000000000\n");
  assert(mock_tx_count == 1);

  resetState();
  mock_radio_error = -1;
  command("TXN 0000000000000000 1\n");
  assert(!g_radio_ready && mock_reset_level == LOW && g_has_transmitted);
  assert(mock_write_count == 0 && mock_output.find("OK TX") == std::string::npos);

  for (const char* text : {"PWR 3\n", "FREQ 868.4\n"}) {
    resetState();
    mock_radio_error = -1;
    command(text);
    assert(!g_radio_ready && mock_reset_level == LOW);
    assert(g_power_dbm == 2 && g_freq_mhz == 868.30f);
    assert(mock_output.find("OK") == std::string::npos);
  }
  resetState();
  mock_radio_error = -1;
  assert(!radioConfigure() && mock_reset_level == LOW);

  resetState();
  mock_eot_ok = false;
  command("TX 0000000000000000\n");
  assert(mock_tx_count == 0 && !g_rmt_ready);
}

int main() {
  resetState();
  setup();
  assert(mock_tx_count == 0 && g_radio_ready && g_rmt_ready);
  testFrameTiming();
  testSerialValidation();
  testBurstAndCooldown();
  testFailures();
  puts("PASS: boot, 130 timing cases, serial validation, 80 bursts, cooldown and faults");
}