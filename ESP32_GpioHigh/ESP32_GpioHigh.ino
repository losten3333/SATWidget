// Minimal ESP32-C6 sketch: assert four external control lines at boot.

constexpr uint8_t OUTPUT_PINS[] = {12, 13, 23, 6};
constexpr uint32_t OUTPUT_DELAY_MS = 60UL * 1000UL;
bool outputsEnabled = false;

void setup() {
  // Keep the pins untouched for one minute.  GPIO 12/13 then remain usable
  // by the ESP32-C6 native USB bootloader during this recovery window.
}

void loop() {
  if (outputsEnabled || millis() < OUTPUT_DELAY_MS) return;
  for (const uint8_t pin : OUTPUT_PINS) {
    pinMode(pin, OUTPUT);
    digitalWrite(pin, HIGH);
  }
  outputsEnabled = true;
}
