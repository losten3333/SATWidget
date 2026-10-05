#include "lcd_bsp.h"
#include "lcd_config.h"
#include "FT3168.h"
#include <FFat.h>
#include <mbedtls/base64.h>
#include <stdarg.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#include "driver/ledc.h"
#include <Adafruit_NeoPixel.h>
#include <Preferences.h>

// USB CDC text protocol. See README.md and satellite_sender.py.
// A received END atomically replaces the displayed satellite set.
constexpr uint8_t MAX_SATELLITES = 8;
constexpr uint32_t OFFLINE_AFTER_MS = 24UL * 60UL * 60UL * 1000UL;
constexpr uint16_t IMAGE_WIDTH = 150;
constexpr uint16_t IMAGE_HEIGHT = 150;
constexpr size_t IMAGE_BYTES = IMAGE_WIDTH * IMAGE_HEIGHT * 2;
// Source files are displayed at their native 150x150 size.
constexpr uint16_t DISPLAY_IMAGE_SIZE = 150;
constexpr const char *TEMPLATE_IMAGE_ID = "template";
constexpr uint8_t POWER_ADC_PIN = 0;
constexpr uint32_t POWER_VOLTAGE_UPDATE_MS = 1000UL;
constexpr uint8_t POWER_ADC_DIVIDER = 3;
constexpr uint8_t POWER_ADC_SAMPLES = 8;
constexpr const lv_font_t *PARAMETER_FONT = &lv_font_montserrat_20;
constexpr uint8_t ACTIVE_HIGH_PINS[] = {12, 13, 9, 6};
constexpr uint32_t ACTIVE_HIGH_DELAY_MS = 60UL * 1000UL;
constexpr uint32_t LED_WAVE_PULSE_MS = 100UL;
constexpr uint8_t LED_WAVE_ORDER[] = {1, 0, 2, 3};
constexpr uint8_t LAMP_PWM_PIN = 2;
constexpr ledc_mode_t LAMP_PWM_MODE = LEDC_LOW_SPEED_MODE;
constexpr ledc_channel_t LAMP_PWM_CHANNEL = LEDC_CHANNEL_0;
constexpr ledc_timer_t LAMP_PWM_TIMER = LEDC_TIMER_0;
constexpr uint32_t LAMP_PWM_FREQUENCY_HZ = 1000;
constexpr ledc_timer_bit_t LAMP_PWM_RESOLUTION = LEDC_TIMER_8_BIT;
constexpr uint8_t LAMP_PWM_DUTY_OFF = 0;
constexpr uint8_t LAMP_PWM_DUTY_50 = 128;
constexpr uint8_t LAMP_PWM_DUTY_100 = 255;
constexpr uint32_t LAMP_AUTO_50_MS = 160UL * 1000UL;
constexpr uint32_t LAMP_AUTO_COOLDOWN_MS = 60UL * 1000UL;
constexpr uint16_t LED_BLINKING_DEFAULT_SECONDS = 30;
constexpr uint8_t VPS_DURATION_DEFAULT_MINUTES = 5;
constexpr uint8_t VPS_CHILL_DEFAULT_MINUTES = 25;
constexpr uint32_t SETTINGS_IDLE_TIMEOUT_MS = 20UL * 1000UL;
constexpr uint8_t WS2812_PIN = 3;
constexpr uint8_t WS2812_LED_COUNT = 2;
constexpr uint32_t WS2812_UPDATE_INTERVAL_MS = 50UL;
constexpr uint32_t WS2812_COLOR_CYCLE_MS = 60UL * 1000UL;
constexpr uint8_t WS2812_BRIGHT_DEFAULT_MAX_PERCENT = 80;
constexpr uint16_t WS2812_GREEN_HUE_OFFSET = 21845;
constexpr uint32_t SETTINGS_SAVE_DELAY_MS = 1000UL;

// BLE UART (Nordic UART Service). Same text protocol as USB CDC.
constexpr const char *BLE_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E";
constexpr const char *BLE_CHARACTERISTIC_UUID_RX = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E";
constexpr const char *BLE_CHARACTERISTIC_UUID_TX = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E";
constexpr size_t BLE_TX_CHUNK_SIZE = 20;
constexpr size_t BLE_RX_BUFFER_SIZE = 4096;

struct Satellite {
  String name;
  String norad;
  String sma;
  String period;
  String incl;
  String raan;
  String pass;
  String rotations;
  String ltan;
  lv_color_t color;
};

Satellite satellites[MAX_SATELLITES];
Satellite pendingSatellites[MAX_SATELLITES];
uint8_t satelliteCount = 0;
uint8_t pendingCount = 0;
uint8_t currentSatellite = 0;
uint16_t dwellMinutes = 1;
uint32_t lastSuccessfulUpdateMs = 0;
uint32_t satelliteShownSinceMs = 0;
bool receivingSnapshot = false;
bool haveSnapshot = false;
String serialLine;
volatile int8_t pendingSatelliteStep = 0;
bool ffatReady = false;
bool receivingImage = false;
String incomingImageNorad;
size_t incomingImageBytes = 0;
uint16_t incomingImageChunkCount = 0;
uint8_t incomingImageAckWindow = 1;
bool activeHighPinsEnabled = false;
bool lampPwmReady = false;
uint32_t lastWs2812UpdateMs = 0;
Adafruit_NeoPixel ws2812Strip(WS2812_LED_COUNT, WS2812_PIN, NEO_GRB + NEO_KHZ800);
File incomingImageFile;
uint8_t *satelliteImageData = nullptr;
String loadedImageNorad;
lv_img_dsc_t *satelliteImageDsc = nullptr;
uint8_t lampPwmDuty = 0;
uint32_t lastPowerVoltageUpdateMs = 0;

BLEServer *bleServer = nullptr;
BLECharacteristic *bleTxCharacteristic = nullptr;
volatile bool bleConnected = false;
uint8_t bleRxBuffer[BLE_RX_BUFFER_SIZE];
volatile uint16_t bleRxHead = 0;
volatile uint16_t bleRxTail = 0;

lv_obj_t *statusIndicator;
lv_obj_t *satelliteIcon;
lv_obj_t *satelliteCounterLabel;
lv_obj_t *timeLabel;
lv_obj_t *powerVoltageLabel;
lv_obj_t *nameLabel;
lv_obj_t *noradLabel;
lv_obj_t *valueLabels[7];
lv_obj_t *captionLabels[7];
lv_obj_t *settingsOverlay = nullptr;
lv_obj_t *settingsHotspot = nullptr;
lv_obj_t *settingsCheckLabels[8];
lv_obj_t *rgbSatSlider = nullptr;
lv_obj_t *rgbHue1Slider = nullptr;
lv_obj_t *rgbHue2Slider = nullptr;
lv_obj_t *bulbPwmSlider = nullptr;
lv_obj_t *ledBlinkingSlider = nullptr;
lv_obj_t *vpsDurationSlider = nullptr;
lv_obj_t *vpsChillSlider = nullptr;
lv_obj_t *rgbSatValueLabel = nullptr;
lv_obj_t *rgbHue1ValueLabel = nullptr;
lv_obj_t *rgbHue2ValueLabel = nullptr;
lv_obj_t *bulbPwmValueLabel = nullptr;
lv_obj_t *ledBlinkingValueLabel = nullptr;
lv_obj_t *vpsDurationValueLabel = nullptr;
lv_obj_t *vpsChillValueLabel = nullptr;
int16_t clockMinutes = -1;
int16_t lastRenderedClockMinutes = -1;
uint32_t clockSyncedMs = 0;
uint32_t settingsLastInteractionMs = 0;
bool settingsVisible = false;

enum BulbMode : uint8_t {
  BULB_MANUAL,
  BULB_AUTO
};

bool ledEnabled = true;
BulbMode bulbMode = BULB_AUTO;
uint8_t bulbPwmPercent = 50;
uint8_t ws2812SaturationPercent = WS2812_BRIGHT_DEFAULT_MAX_PERCENT;
uint8_t ws2812Hue1Percent = 0;
uint8_t ws2812Hue2Percent = 0;
uint16_t ledBlinkingSeconds = LED_BLINKING_DEFAULT_SECONDS;
uint8_t vpsDurationMinutes = VPS_DURATION_DEFAULT_MINUTES;
uint8_t vpsChillMinutes = VPS_CHILL_DEFAULT_MINUTES;
Preferences settingsPrefs;
uint32_t settingsDirtySinceMs = 0;
bool settingsDirty = false;

const char *FIELD_LABELS[] = {
  "SMA:", "PER:", "INCL:", "RAAN:", "PASS:", "ROT:", "LTAN:"
};

enum SettingsItem : uint8_t {
  SETTINGS_LED,
  SETTINGS_BULB_AUTO,
  SETTINGS_ITEM_COUNT
};

struct SettingsRow {
  const char *label;
  SettingsItem item;
};

const SettingsRow SETTINGS_ROWS[] = {
  {"LED", SETTINGS_LED},
  {"Bulb Auto", SETTINGS_BULB_AUTO},
};

// Reserved extension point for persisting orbital snapshots in internal flash.
void persistSnapshotPlaceholder() {}

uint8_t clampPercent(uint8_t value) {
  return value > 100 ? 100 : value;
}

uint16_t clampRange(uint16_t value, uint16_t minimum, uint16_t maximum) {
  if (value < minimum) return minimum;
  return value > maximum ? maximum : value;
}

void markSettingsDirty() {
  settingsDirty = true;
  settingsDirtySinceMs = millis();
}

void loadSettings() {
  settingsPrefs.begin("satwidget", false);
  ledEnabled = settingsPrefs.getBool("led", ledEnabled);
  bulbMode = settingsPrefs.getBool("bulbAuto", bulbMode == BULB_AUTO) ? BULB_AUTO : BULB_MANUAL;
  bulbPwmPercent = clampPercent(settingsPrefs.getUChar("bulbPwm", bulbPwmPercent));
  ws2812SaturationPercent = clampPercent(settingsPrefs.getUChar("rgbSat", ws2812SaturationPercent));
  ws2812Hue1Percent = clampPercent(settingsPrefs.getUChar("rgbHue1", ws2812Hue1Percent));
  ws2812Hue2Percent = clampPercent(settingsPrefs.getUChar("rgbHue2", ws2812Hue2Percent));
  ledBlinkingSeconds = clampRange(settingsPrefs.getUShort("ledBlinkSec", ledBlinkingSeconds), 1, 300);
  vpsDurationMinutes = (uint8_t)clampRange(
    settingsPrefs.getUChar("vpsDurMin", vpsDurationMinutes), 1, 30);
  vpsChillMinutes = (uint8_t)clampRange(
    settingsPrefs.getUChar("vpsChillMin", vpsChillMinutes), 1, 60);
}

void saveSettingsIfNeeded(uint32_t now) {
  if (!settingsDirty || (uint32_t)(now - settingsDirtySinceMs) < SETTINGS_SAVE_DELAY_MS) return;
  settingsPrefs.putBool("led", ledEnabled);
  settingsPrefs.putBool("bulbAuto", bulbMode == BULB_AUTO);
  settingsPrefs.putUChar("bulbPwm", bulbPwmPercent);
  settingsPrefs.putUChar("rgbSat", ws2812SaturationPercent);
  settingsPrefs.putUChar("rgbHue1", ws2812Hue1Percent);
  settingsPrefs.putUChar("rgbHue2", ws2812Hue2Percent);
  settingsPrefs.putUShort("ledBlinkSec", ledBlinkingSeconds);
  settingsPrefs.putUChar("vpsDurMin", vpsDurationMinutes);
  settingsPrefs.putUChar("vpsChillMin", vpsChillMinutes);
  settingsDirty = false;
}

void initLampPwm() {
  if (lampPwmReady) return;
  ledc_timer_config_t timerConfig = {};
  timerConfig.speed_mode = LAMP_PWM_MODE;
  timerConfig.duty_resolution = LAMP_PWM_RESOLUTION;
  timerConfig.timer_num = LAMP_PWM_TIMER;
  timerConfig.freq_hz = LAMP_PWM_FREQUENCY_HZ;
  timerConfig.clk_cfg = LEDC_AUTO_CLK;
  ledc_timer_config(&timerConfig);

  ledc_channel_config_t channelConfig = {};
  channelConfig.gpio_num = LAMP_PWM_PIN;
  channelConfig.speed_mode = LAMP_PWM_MODE;
  channelConfig.channel = LAMP_PWM_CHANNEL;
  channelConfig.intr_type = LEDC_INTR_DISABLE;
  channelConfig.timer_sel = LAMP_PWM_TIMER;
  channelConfig.duty = lampPwmDuty;
  channelConfig.hpoint = 0;
  ledc_channel_config(&channelConfig);
  lampPwmReady = true;
}

void setLampPwmDuty(uint8_t duty) {
  if (!lampPwmReady) return;
  if (lampPwmDuty == duty) return;
  lampPwmDuty = duty;
  ledc_set_duty(LAMP_PWM_MODE, LAMP_PWM_CHANNEL, lampPwmDuty);
  ledc_update_duty(LAMP_PWM_MODE, LAMP_PWM_CHANNEL);
}

void initWs2812() {
  ws2812Strip.begin();
  ws2812Strip.clear();
  ws2812Strip.show();
}

void setWs2812Color(uint16_t firstHue, uint16_t secondHue, uint8_t brightness) {
  ws2812Strip.setPixelColor(0, ws2812Strip.ColorHSV(firstHue, 255, brightness));
  ws2812Strip.setPixelColor(1, ws2812Strip.ColorHSV(secondHue, 255, brightness));
  ws2812Strip.show();
}

uint8_t ws2812Brightness() {
  return (uint8_t)(ws2812SaturationPercent * 255UL / 100UL);
}

uint16_t hueFromPercent(uint8_t percent) {
  if (percent == 0) return 0;
  return (uint16_t)((percent - 1) * 65535UL / 99UL);
}

uint16_t automaticWs2812Hue(uint32_t now, uint16_t offset) {
  const uint32_t colorMs = now % WS2812_COLOR_CYCLE_MS;
  return (uint16_t)(colorMs * 65536UL / WS2812_COLOR_CYCLE_MS) + offset;
}

uint16_t ws2812HueForSetting(uint8_t percent, uint32_t now, uint16_t autoOffset) {
  if (percent == 0) return automaticWs2812Hue(now, autoOffset);
  return hueFromPercent(percent);
}

void updateWs2812Cycle(uint32_t now) {
  if (now - lastWs2812UpdateMs < WS2812_UPDATE_INTERVAL_MS) return;
  lastWs2812UpdateMs = now;
  const uint16_t firstHue = ws2812HueForSetting(ws2812Hue1Percent, now, 0);
  const uint16_t secondHue = ws2812HueForSetting(ws2812Hue2Percent, now, WS2812_GREEN_HUE_OFFSET);
  setWs2812Color(firstHue, secondHue, ws2812Brightness());
}

void sendBleBytes(const uint8_t *data, size_t length) {
  if (bleTxCharacteristic == nullptr) return;
  size_t offset = 0;
  while (offset < length) {
    const size_t chunkSize = min((size_t)BLE_TX_CHUNK_SIZE, length - offset);
    bleTxCharacteristic->setValue(data + offset, chunkSize);
    bleTxCharacteristic->notify();
    offset += chunkSize;
    delay(2);
  }
}

// Every reply goes to both transports: USB CDC and BLE (if connected).
void sendToHost(const String &line) {
  Serial.println(line);
  if (bleConnected && bleTxCharacteristic != nullptr) {
    sendBleBytes(reinterpret_cast<const uint8_t *>(line.c_str()), line.length());
    const uint8_t newline = '\n';
    sendBleBytes(&newline, 1);
  }
}

void sendToHost(const char *format, ...) {
  char buffer[192];
  va_list args;
  va_start(args, format);
  vsnprintf(buffer, sizeof(buffer), format, args);
  va_end(args);
  sendToHost(String(buffer));
}

void bleEnqueueBytes(const uint8_t *data, size_t length) {
  for (size_t i = 0; i < length; ++i) {
    const uint16_t next = (uint16_t)((bleRxHead + 1) % BLE_RX_BUFFER_SIZE);
    if (next == bleRxTail) return;  // ring buffer full, drop to keep ordering
    bleRxBuffer[bleRxHead] = data[i];
    bleRxHead = next;
  }
}

class SatWidgetServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *server) override {
    bleConnected = true;
    BLEDevice::stopAdvertising();
  }
  void onDisconnect(BLEServer *server) override {
    bleConnected = false;
    BLEDevice::startAdvertising();
  }
};

class SatWidgetRxCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *characteristic) override {
    const String value = characteristic->getValue();
    bleEnqueueBytes(reinterpret_cast<const uint8_t *>(value.c_str()), value.length());
  }
};

void initBle() {
  BLEDevice::init("ESP32_SatWidget");
  // Let a central negotiate large GATT packets for image transfer.
  BLEDevice::setMTU(517);
  bleServer = BLEDevice::createServer();
  bleServer->setCallbacks(new SatWidgetServerCallbacks());
  BLEService *service = bleServer->createService(BLE_SERVICE_UUID);
  bleTxCharacteristic = service->createCharacteristic(
    BLE_CHARACTERISTIC_UUID_TX, BLECharacteristic::PROPERTY_NOTIFY);
  bleTxCharacteristic->addDescriptor(new BLE2902());
  BLECharacteristic *rxCharacteristic = service->createCharacteristic(
    BLE_CHARACTERISTIC_UUID_RX,
    BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  rxCharacteristic->setCallbacks(new SatWidgetRxCallbacks());
  service->start();
  BLEAdvertising *advertising = BLEDevice::getAdvertising();
  advertising->addServiceUUID(BLE_SERVICE_UUID);
  advertising->setScanResponse(true);
  advertising->setMinPreferred(0x06);
  advertising->setMaxPreferred(0x12);
  BLEDevice::startAdvertising();
}

lv_color_t parseColor(const char *text) {
  if (text == nullptr || text[0] != '#') return lv_color_hex(0x808080);
  return lv_color_hex(strtoul(text + 1, nullptr, 16) & 0xFFFFFF);
}

void setLabelText(lv_obj_t *label, const String &text) {
  lv_label_set_text(label, text.c_str());
}

void makeLargeWhiteLabel(lv_obj_t *label) {
  lv_obj_set_style_text_color(label, lv_color_white(), 0);
  lv_obj_set_style_text_font(label, &lv_font_montserrat_20, 0);
}

void makeCardLabel(lv_obj_t *label, const lv_font_t *font) {
  lv_obj_set_style_text_color(label, lv_color_black(), 0);
  lv_obj_set_style_text_font(label, font, 0);
  lv_obj_set_style_text_align(label, LV_TEXT_ALIGN_CENTER, 0);
  lv_label_set_long_mode(label, LV_LABEL_LONG_DOT);
}

void setCardValueText(lv_obj_t *label, const String &text) {
  // Every orbital parameter uses one fixed readable size.  20px keeps the
  // longest current SMA/INCL/RAAN strings inside their cards.
  lv_obj_set_style_text_font(label, PARAMETER_FONT, 0);
  lv_label_set_text(label, text.c_str());
}

lv_obj_t *createCard(lv_obj_t *screen, int16_t x, int16_t y,
                     int16_t width, int16_t height, uint32_t color) {
  lv_obj_t *card = lv_obj_create(screen);
  lv_obj_set_pos(card, x, y);
  lv_obj_set_size(card, width, height);
  lv_obj_set_style_bg_color(card, lv_color_hex(color), 0);
  lv_obj_set_style_bg_opa(card, LV_OPA_COVER, 0);
  lv_obj_set_style_border_width(card, 0, 0);
  lv_obj_set_style_radius(card, 0, 0);
  // lv_obj_create() adds 10px default padding.  Children of the small cards
  // were therefore clipped to a tiny inner area; cards use absolute layout.
  lv_obj_set_style_pad_all(card, 0, 0);
  lv_obj_clear_flag(card, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_clear_flag(card, LV_OBJ_FLAG_CLICKABLE);
  return card;
}

void renderClock() {
  if (timeLabel == nullptr || clockMinutes < 0) return;
  const uint32_t elapsedMinutes = (millis() - clockSyncedMs) / 60000UL;
  const int16_t displayed = (clockMinutes + elapsedMinutes) % (24 * 60);
  if (displayed == lastRenderedClockMinutes) return;
  if (!example_lvgl_lock(-1)) return;
  lv_label_set_text_fmt(timeLabel, "%02d:%02d", displayed / 60, displayed % 60);
  example_lvgl_unlock();
  lastRenderedClockMinutes = displayed;
}

uint16_t readPowerVoltageMv() {
  // GPIO0 is connected to the board's built-in 1:3 power-voltage divider.
  // Arduino's calibrated reading matches Waveshare's ADC example, which uses
  // ADC curve fitting before restoring the divider ratio.
  uint32_t adcMillivolts = 0;
  for (uint8_t sample = 0; sample < POWER_ADC_SAMPLES; ++sample) {
    adcMillivolts += analogReadMilliVolts(POWER_ADC_PIN);
  }
  return (uint16_t)((adcMillivolts / POWER_ADC_SAMPLES) * POWER_ADC_DIVIDER);
}

void updatePowerVoltageLabel(uint32_t now) {
  if (powerVoltageLabel == nullptr ||
      (uint32_t)(now - lastPowerVoltageUpdateMs) < POWER_VOLTAGE_UPDATE_MS) {
    return;
  }
  lastPowerVoltageUpdateMs = now;
  const uint16_t voltageMv = readPowerVoltageMv();
  char buffer[12];
  snprintf(buffer, sizeof(buffer), "%u,%02u V", voltageMv / 1000, (voltageMv % 1000) / 10);
  if (!example_lvgl_lock(-1)) return;
  lv_label_set_text(powerVoltageLabel, buffer);
  example_lvgl_unlock();
}

void previousSatelliteButtonEvent(lv_event_t *event) {
  pendingSatelliteStep = -1;
}

void nextSatelliteButtonEvent(lv_event_t *event) {
  pendingSatelliteStep = 1;
}

void createNavigationButton(lv_obj_t *screen, int16_t hitboxX, int16_t visualX,
                            const char *symbol, lv_event_cb_t callback) {
  // Keep the tappable areas at the screen edges to leave the centre free for
  // the larger satellite image.
  lv_obj_t *hitbox = lv_btn_create(screen);
  lv_obj_set_pos(hitbox, hitboxX, 128);
  lv_obj_set_size(hitbox, 55, 96);
  lv_obj_set_style_bg_opa(hitbox, LV_OPA_TRANSP, 0);
  lv_obj_set_style_bg_opa(hitbox, LV_OPA_TRANSP, LV_STATE_PRESSED);
  lv_obj_set_style_border_width(hitbox, 0, 0);
  lv_obj_set_style_shadow_width(hitbox, 0, 0);
  lv_obj_clear_flag(hitbox, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_add_event_cb(hitbox, callback, LV_EVENT_CLICKED, nullptr);

  lv_obj_t *button = lv_obj_create(hitbox);
  lv_obj_set_pos(button, visualX - hitboxX, 31);
  lv_obj_set_size(button, 34, 34);
  lv_obj_set_style_bg_color(button, lv_color_hex(0x303030), 0);
  lv_obj_set_style_bg_opa(button, LV_OPA_70, 0);
  lv_obj_set_style_border_color(button, lv_color_hex(0xB0B0B0), 0);
  lv_obj_set_style_border_width(button, 1, 0);
  lv_obj_set_style_radius(button, 6, 0);
  lv_obj_clear_flag(button, LV_OBJ_FLAG_CLICKABLE);
  lv_obj_clear_flag(button, LV_OBJ_FLAG_SCROLLABLE);

  lv_obj_t *label = lv_label_create(button);
  lv_label_set_text(label, symbol);
  makeLargeWhiteLabel(label);
  lv_obj_center(label);
}

void createStar(lv_obj_t *screen, int16_t x, int16_t y, const char *symbol,
                const lv_font_t *font) {
  lv_obj_t *star = lv_label_create(screen);
  lv_label_set_text(star, symbol);
  lv_obj_set_pos(star, x, y);
  lv_obj_set_style_text_color(star, lv_color_white(), 0);
  lv_obj_set_style_text_font(star, font, 0);
}

void createStarField(lv_obj_t *screen) {
  createStar(screen, 54, 137, "+", &lv_font_montserrat_18);
  createStar(screen, 34, 215, "+", &lv_font_montserrat_18);
  createStar(screen, 74, 240, "+", &lv_font_montserrat_18);
  createStar(screen, 251, 160, "+", &lv_font_montserrat_18);
  createStar(screen, 251, 226, "+", &lv_font_montserrat_20);
}

bool settingsItemChecked(SettingsItem item) {
  if (item == SETTINGS_LED) return ledEnabled;
  if (item == SETTINGS_BULB_AUTO) return bulbMode == BULB_AUTO;
  return false;
}

void refreshSettingsChecks() {
  for (uint8_t i = 0; i < SETTINGS_ITEM_COUNT; ++i) {
    if (settingsCheckLabels[i] == nullptr) continue;
    lv_label_set_text(settingsCheckLabels[i], settingsItemChecked((SettingsItem)i) ? "X" : "");
  }
}

void refreshRgbSliderLabels() {
  if (bulbPwmValueLabel != nullptr) {
    lv_label_set_text_fmt(bulbPwmValueLabel, "%u%%", bulbPwmPercent);
  }
  if (rgbSatValueLabel != nullptr) {
    lv_label_set_text_fmt(rgbSatValueLabel, "%u%%", ws2812SaturationPercent);
  }
  if (rgbHue1ValueLabel != nullptr) {
    lv_label_set_text_fmt(rgbHue1ValueLabel, "%u%%", ws2812Hue1Percent);
  }
  if (rgbHue2ValueLabel != nullptr) {
    lv_label_set_text_fmt(rgbHue2ValueLabel, "%u%%", ws2812Hue2Percent);
  }
  if (ledBlinkingValueLabel != nullptr) {
    lv_label_set_text_fmt(ledBlinkingValueLabel, "%u s", ledBlinkingSeconds);
  }
  if (vpsDurationValueLabel != nullptr) {
    lv_label_set_text_fmt(vpsDurationValueLabel, "%u min", vpsDurationMinutes);
  }
  if (vpsChillValueLabel != nullptr) {
    lv_label_set_text_fmt(vpsChillValueLabel, "%u min", vpsChillMinutes);
  }
}

void noteSettingsInteraction() {
  settingsLastInteractionMs = millis();
}

void showSettingsOverlay() {
  if (settingsOverlay == nullptr) return;
  settingsVisible = true;
  noteSettingsInteraction();
  refreshSettingsChecks();
  refreshRgbSliderLabels();
  lv_obj_scroll_to_y(settingsOverlay, 0, LV_ANIM_OFF);
  lv_obj_clear_flag(settingsOverlay, LV_OBJ_FLAG_HIDDEN);
  lv_obj_move_foreground(settingsOverlay);
}

void hideSettingsOverlay() {
  if (settingsOverlay == nullptr) return;
  settingsVisible = false;
  lv_obj_add_flag(settingsOverlay, LV_OBJ_FLAG_HIDDEN);
}

void settingsHotspotEvent(lv_event_t *event) {
  if (lv_event_get_code(event) == LV_EVENT_LONG_PRESSED) showSettingsOverlay();
}

void settingsOverlayEvent(lv_event_t *event) {
  if (lv_event_get_code(event) == LV_EVENT_CLICKED ||
      lv_event_get_code(event) == LV_EVENT_PRESSED ||
      lv_event_get_code(event) == LV_EVENT_SCROLL) {
    noteSettingsInteraction();
  }
}

void settingsSliderEvent(lv_event_t *event) {
  lv_obj_t *slider = lv_event_get_target(event);
  noteSettingsInteraction();
  if (slider == bulbPwmSlider) {
    bulbPwmPercent = (uint8_t)lv_slider_get_value(slider);
    bulbMode = BULB_MANUAL;
    refreshSettingsChecks();
  } else if (slider == rgbSatSlider) {
    ws2812SaturationPercent = (uint8_t)lv_slider_get_value(slider);
  } else if (slider == rgbHue1Slider) {
    ws2812Hue1Percent = (uint8_t)lv_slider_get_value(slider);
  } else if (slider == rgbHue2Slider) {
    ws2812Hue2Percent = (uint8_t)lv_slider_get_value(slider);
  } else if (slider == ledBlinkingSlider) {
    ledBlinkingSeconds = (uint16_t)lv_slider_get_value(slider);
  } else if (slider == vpsDurationSlider) {
    vpsDurationMinutes = (uint8_t)lv_slider_get_value(slider);
  } else if (slider == vpsChillSlider) {
    vpsChillMinutes = (uint8_t)lv_slider_get_value(slider);
  }
  refreshRgbSliderLabels();
  markSettingsDirty();
}

void createRgbSliders(lv_obj_t *screen);

void settingsRowEvent(lv_event_t *event) {
  if (lv_event_get_code(event) != LV_EVENT_CLICKED) return;
  const SettingsItem item = (SettingsItem)(uintptr_t)lv_event_get_user_data(event);
  noteSettingsInteraction();
  if (item == SETTINGS_LED) {
    ledEnabled = !ledEnabled;
  } else if (item == SETTINGS_BULB_AUTO) {
    bulbMode = bulbMode == BULB_AUTO ? BULB_MANUAL : BULB_AUTO;
  }
  refreshSettingsChecks();
  markSettingsDirty();
}

void createSettingsOverlay(lv_obj_t *screen) {
  settingsOverlay = lv_obj_create(screen);
  lv_obj_set_pos(settingsOverlay, 0, 0);
  lv_obj_set_size(settingsOverlay, EXAMPLE_LCD_H_RES, EXAMPLE_LCD_V_RES);
  lv_obj_set_style_bg_color(settingsOverlay, lv_color_hex(0xBF7C55), 0);
  lv_obj_set_style_bg_opa(settingsOverlay, LV_OPA_COVER, 0);
  lv_obj_set_style_border_width(settingsOverlay, 0, 0);
  lv_obj_set_style_radius(settingsOverlay, 0, 0);
  lv_obj_set_style_pad_all(settingsOverlay, 0, 0);
  lv_obj_add_flag(settingsOverlay, LV_OBJ_FLAG_CLICKABLE);
  lv_obj_set_scroll_dir(settingsOverlay, LV_DIR_VER);
  lv_obj_set_scrollbar_mode(settingsOverlay, LV_SCROLLBAR_MODE_AUTO);
  lv_obj_add_flag(settingsOverlay, LV_OBJ_FLAG_HIDDEN);
  lv_obj_add_event_cb(settingsOverlay, settingsOverlayEvent, LV_EVENT_PRESSED, nullptr);
  lv_obj_add_event_cb(settingsOverlay, settingsOverlayEvent, LV_EVENT_CLICKED, nullptr);
  lv_obj_add_event_cb(settingsOverlay, settingsOverlayEvent, LV_EVENT_SCROLL, nullptr);

  constexpr int16_t rowStartY = 24;
  constexpr int16_t rowSpacing = 42;
  constexpr int16_t rowHeight = 38;
  constexpr int16_t boxX = 205;
  constexpr int16_t boxSize = 34;
  for (uint8_t i = 0; i < SETTINGS_ITEM_COUNT; ++i) {
    const SettingsRow &rowDef = SETTINGS_ROWS[i];
    const int16_t y = rowStartY + i * rowSpacing;
    lv_obj_t *row = lv_obj_create(settingsOverlay);
    lv_obj_set_pos(row, 18, y - 4);
    lv_obj_set_size(row, 244, rowHeight);
    lv_obj_set_style_bg_opa(row, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(row, 0, 0);
    lv_obj_set_style_radius(row, 0, 0);
    lv_obj_set_style_pad_all(row, 0, 0);
    lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(row, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(row, settingsRowEvent, LV_EVENT_CLICKED,
                        (void *)(uintptr_t)rowDef.item);

    lv_obj_t *label = lv_label_create(row);
    lv_label_set_text(label, rowDef.label);
    lv_obj_set_pos(label, 0, 1);
    lv_obj_set_style_text_color(label, lv_color_black(), 0);
    lv_obj_set_style_text_font(label, &lv_font_montserrat_26, 0);

    lv_obj_t *box = lv_obj_create(row);
    lv_obj_set_pos(box, boxX - 18, 0);
    lv_obj_set_size(box, boxSize, boxSize);
    lv_obj_set_style_bg_opa(box, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_color(box, lv_color_black(), 0);
    lv_obj_set_style_border_width(box, 4, 0);
    lv_obj_set_style_radius(box, 0, 0);
    lv_obj_set_style_pad_all(box, 0, 0);
    lv_obj_clear_flag(box, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_clear_flag(box, LV_OBJ_FLAG_CLICKABLE);

    settingsCheckLabels[rowDef.item] = lv_label_create(box);
    lv_obj_set_style_text_color(settingsCheckLabels[rowDef.item], lv_color_black(), 0);
    lv_obj_set_style_text_font(settingsCheckLabels[rowDef.item], &lv_font_montserrat_26, 0);
    lv_obj_center(settingsCheckLabels[rowDef.item]);
  }
  createRgbSliders(settingsOverlay);
  refreshSettingsChecks();
  refreshRgbSliderLabels();
}

lv_obj_t *createSettingsSliderControl(lv_obj_t *parent, const char *labelText, int16_t y,
                                      uint16_t minimum, uint16_t maximum,
                                      uint16_t initialValue, const char *unit,
                                      lv_obj_t **valueLabel) {
  lv_obj_t *label = lv_label_create(parent);
  lv_label_set_text(label, labelText);
  lv_obj_set_pos(label, 18, y);
  lv_obj_set_style_text_color(label, lv_color_black(), 0);
  lv_obj_set_style_text_font(label, &lv_font_montserrat_20, 0);

  *valueLabel = lv_label_create(parent);
  lv_label_set_text_fmt(*valueLabel, "%u%s", initialValue, unit);
  lv_obj_set_pos(*valueLabel, 208, y);
  lv_obj_set_width(*valueLabel, 54);
  lv_obj_set_style_text_align(*valueLabel, LV_TEXT_ALIGN_RIGHT, 0);
  lv_obj_set_style_text_color(*valueLabel, lv_color_black(), 0);
  lv_obj_set_style_text_font(*valueLabel, &lv_font_montserrat_20, 0);

  lv_obj_t *slider = lv_slider_create(parent);
  lv_obj_set_pos(slider, 18, y + 27);
  lv_obj_set_size(slider, 244, 14);
  lv_slider_set_range(slider, minimum, maximum);
  lv_slider_set_value(slider, initialValue, LV_ANIM_OFF);
  lv_obj_set_style_bg_color(slider, lv_color_hex(0x8F5738), LV_PART_MAIN);
  lv_obj_set_style_bg_color(slider, lv_color_black(), LV_PART_INDICATOR);
  lv_obj_set_style_bg_color(slider, lv_color_black(), LV_PART_KNOB);
  lv_obj_clear_flag(slider, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_add_event_cb(slider, settingsSliderEvent, LV_EVENT_VALUE_CHANGED, nullptr);
  lv_obj_add_event_cb(slider, settingsSliderEvent, LV_EVENT_PRESSED, nullptr);
  return slider;
}

void createRgbSliders(lv_obj_t *screen) {
  bulbPwmSlider = createSettingsSliderControl(screen, "BulbPWM", 112, 0, 100,
    bulbPwmPercent, "%", &bulbPwmValueLabel);
  rgbSatSlider = createSettingsSliderControl(screen, "RGBsat", 194, 0, 100,
    ws2812SaturationPercent, "%", &rgbSatValueLabel);
  rgbHue1Slider = createSettingsSliderControl(screen, "RGBhue 1", 276, 0, 100,
    ws2812Hue1Percent, "%", &rgbHue1ValueLabel);
  rgbHue2Slider = createSettingsSliderControl(screen, "RGBhue 2", 358, 0, 100,
    ws2812Hue2Percent, "%", &rgbHue2ValueLabel);
  ledBlinkingSlider = createSettingsSliderControl(screen, "LEDblinking (s)", 440, 1, 300,
    ledBlinkingSeconds, " s", &ledBlinkingValueLabel);
  vpsDurationSlider = createSettingsSliderControl(screen, "VPSdur (min)", 522, 1, 30,
    vpsDurationMinutes, " min", &vpsDurationValueLabel);
  vpsChillSlider = createSettingsSliderControl(screen, "VPSchill (min)", 604, 1, 60,
    vpsChillMinutes, " min", &vpsChillValueLabel);
}

void createSettingsHotspot(lv_obj_t *screen) {
  settingsHotspot = lv_obj_create(screen);
  lv_obj_set_pos(settingsHotspot, 90, 130);
  lv_obj_set_size(settingsHotspot, 100, 120);
  lv_obj_set_style_bg_opa(settingsHotspot, LV_OPA_TRANSP, 0);
  lv_obj_set_style_border_width(settingsHotspot, 0, 0);
  lv_obj_set_style_shadow_width(settingsHotspot, 0, 0);
  lv_obj_set_style_radius(settingsHotspot, 0, 0);
  lv_obj_set_style_pad_all(settingsHotspot, 0, 0);
  lv_obj_clear_flag(settingsHotspot, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_add_flag(settingsHotspot, LV_OBJ_FLAG_CLICKABLE);
  lv_obj_add_event_cb(settingsHotspot, settingsHotspotEvent, LV_EVENT_LONG_PRESSED, nullptr);
}

bool validNoradId(const String &imageId) {
  if (imageId.isEmpty()) return false;
  for (size_t i = 0; i < imageId.length(); ++i) {
    if (!isDigit(imageId[i])) return false;
  }
  return true;
}

bool validImageId(const String &imageId) {
  if (validNoradId(imageId)) return true;
  return imageId == TEMPLATE_IMAGE_ID;
}

String imagePathForId(const String &imageId) {
  return "/image/" + imageId + ".rgb565";
}

// Source files use the common RGB565 little-endian byte order.  The active
// LVGL configuration has LV_COLOR_16_SWAP enabled, so its image data needs
// the two bytes of every pixel in the opposite order.
void swapRgb565ByteOrder(uint8_t *data, size_t size) {
  for (size_t i = 0; i + 1 < size; i += 2) {
    const uint8_t first = data[i];
    data[i] = data[i + 1];
    data[i + 1] = first;
  }
}

void clearRgb565Image(lv_obj_t *image, uint8_t **data, lv_img_dsc_t **descriptor) {
  lv_img_set_src(image, nullptr);
  if (*data != nullptr) {
    free(*data);
    *data = nullptr;
  }
  if (*descriptor != nullptr) {
    free(*descriptor);
    *descriptor = nullptr;
  }
}

bool loadRgb565Image(const String &imageId, lv_obj_t *image,
                     uint8_t **imageData, lv_img_dsc_t **imageDsc) {
  if (!ffatReady || !validImageId(imageId)) {
    clearRgb565Image(image, imageData, imageDsc);
    return false;
  }

  const String path = imagePathForId(imageId);
  File imageFile = FFat.open(path.c_str(), FILE_READ);
  if (!imageFile || imageFile.size() != IMAGE_BYTES) {
    if (imageFile) imageFile.close();
    clearRgb565Image(image, imageData, imageDsc);
    return false;
  }

  uint8_t *data = static_cast<uint8_t *>(malloc(IMAGE_BYTES));
  if (data == nullptr || imageFile.read(data, IMAGE_BYTES) != IMAGE_BYTES) {
    if (data != nullptr) free(data);
    imageFile.close();
    clearRgb565Image(image, imageData, imageDsc);
    return false;
  }
  imageFile.close();

  lv_img_dsc_t *descriptor = static_cast<lv_img_dsc_t *>(malloc(sizeof(lv_img_dsc_t)));
  if (descriptor == nullptr) {
    free(data);
    clearRgb565Image(image, imageData, imageDsc);
    return false;
  }

  clearRgb565Image(image, imageData, imageDsc);
  *imageData = data;
  *imageDsc = descriptor;
  (*imageDsc)->header.always_zero = 0;
  (*imageDsc)->header.w = IMAGE_WIDTH;
  (*imageDsc)->header.h = IMAGE_HEIGHT;
  (*imageDsc)->header.cf = LV_IMG_CF_TRUE_COLOR;
  (*imageDsc)->data_size = IMAGE_BYTES;
  (*imageDsc)->data = *imageData;
  lv_img_set_src(image, *imageDsc);
  lv_obj_invalidate(image);
  return true;
}

void clearSatelliteImage() {
  clearRgb565Image(satelliteIcon, &satelliteImageData, &satelliteImageDsc);
  loadedImageNorad = "";
}

bool loadSatelliteImage(const String &norad) {
  if (loadedImageNorad == norad && satelliteImageData != nullptr) return true;
  bool loaded = loadRgb565Image(norad, satelliteIcon, &satelliteImageData, &satelliteImageDsc);
  if (!loaded) {
    loaded = loadRgb565Image(TEMPLATE_IMAGE_ID, satelliteIcon,
      &satelliteImageData, &satelliteImageDsc);
  }
  loadedImageNorad = loaded ? norad : "";
  return loaded;
}

void renderCurrentSatellite() {
  if (!example_lvgl_lock(-1)) return;

  if (!haveSnapshot || satelliteCount == 0) {
    lv_obj_set_style_bg_color(satelliteIcon, lv_color_hex(0x303030), 0);
    clearSatelliteImage();
    lv_label_set_text(satelliteCounterLabel, "0/0");
    lv_label_set_text(nameLabel, "USB DATA");
    lv_label_set_text(noradLabel, "WAITING FOR SNAPSHOT");
    for (uint8_t i = 0; i < 7; ++i) lv_label_set_text(valueLabels[i], "-");
    example_lvgl_unlock();
    return;
  }

  const Satellite &sat = satellites[currentSatellite];
  // The satellite colour used to become an opaque red frame for KHAYYAM
  // around transparent pixels in its image.  Keep the image area black.
  lv_obj_set_style_bg_color(satelliteIcon, lv_color_black(), 0);
  loadSatelliteImage(sat.norad);
  lv_label_set_text_fmt(satelliteCounterLabel, "%u/%u", currentSatellite + 1, satelliteCount);
  setLabelText(nameLabel, sat.name);
  setLabelText(noradLabel, "ID " + sat.norad);
  const String values[] = {sat.sma, sat.period, sat.incl, sat.raan,
                           sat.pass, sat.rotations, sat.ltan};
  for (uint8_t i = 0; i < 7; ++i) setCardValueText(valueLabels[i], values[i]);
  example_lvgl_unlock();
}

void createWidget() {
  lv_obj_t *screen = lv_scr_act();
  lv_obj_set_style_bg_color(screen, lv_color_black(), 0);
  lv_obj_set_style_bg_opa(screen, LV_OPA_COVER, 0);
  lv_obj_clear_flag(screen, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_set_scrollbar_mode(screen, LV_SCROLLBAR_MODE_OFF);

  timeLabel = lv_label_create(screen);
  lv_obj_set_pos(timeLabel, 8, 5);
  lv_obj_set_width(timeLabel, 96);
  lv_obj_set_style_text_color(timeLabel, lv_color_white(), 0);
  lv_obj_set_style_text_font(timeLabel, &lv_font_montserrat_18, 0);
  lv_label_set_text(timeLabel, "--:--");

  powerVoltageLabel = lv_label_create(screen);
  lv_obj_set_pos(powerVoltageLabel, 180, 5);
  lv_obj_set_width(powerVoltageLabel, 92);
  lv_obj_set_style_text_align(powerVoltageLabel, LV_TEXT_ALIGN_RIGHT, 0);
  lv_obj_set_style_text_color(powerVoltageLabel, lv_color_white(), 0);
  lv_obj_set_style_text_font(powerVoltageLabel, &lv_font_montserrat_18, 0);
  lv_label_set_text(powerVoltageLabel, "--,-- V");

  // The count and status dot are deliberately hidden in the card layout;
  // the upper-left area is reserved for the current time like the mock-up.
  satelliteCounterLabel = lv_label_create(screen);
  lv_obj_add_flag(satelliteCounterLabel, LV_OBJ_FLAG_HIDDEN);

  satelliteIcon = lv_img_create(screen);
  lv_obj_set_pos(satelliteIcon, 65, 116);
  lv_obj_set_size(satelliteIcon, DISPLAY_IMAGE_SIZE, DISPLAY_IMAGE_SIZE);
  lv_img_set_zoom(satelliteIcon, LV_IMG_ZOOM_NONE);
  lv_obj_set_style_radius(satelliteIcon, 4, 0);
  lv_obj_set_style_border_width(satelliteIcon, 0, 0);
  lv_obj_set_style_bg_color(satelliteIcon, lv_color_black(), 0);
  lv_obj_set_style_bg_opa(satelliteIcon, LV_OPA_TRANSP, 0);
  lv_obj_clear_flag(satelliteIcon, LV_OBJ_FLAG_SCROLLABLE);

  createStarField(screen);
  createNavigationButton(screen, 0, 2, "<", previousSatelliteButtonEvent);
  // Keep the visual button inside the visible 280px panel area; its hitbox
  // remains at the edge, while the icon itself no longer clips on the right.
  createNavigationButton(screen, 220, 216, ">", nextSatelliteButtonEvent);

  lv_obj_t *nameCard = createCard(screen, 22, 42, 236, 31, 0xFFCB1A);
  nameLabel = lv_label_create(nameCard);
  lv_obj_set_pos(nameLabel, 3, 0);
  lv_obj_set_width(nameLabel, 230);
  lv_obj_set_style_text_align(nameLabel, LV_TEXT_ALIGN_CENTER, 0);
  lv_label_set_long_mode(nameLabel, LV_LABEL_LONG_DOT);
  lv_obj_set_style_text_color(nameLabel, lv_color_black(), 0);
  lv_obj_set_style_text_font(nameLabel, &lv_font_montserrat_26, 0);

  lv_obj_t *idCard = createCard(screen, 22, 76, 236, 28, 0xFF822A);
  noradLabel = lv_label_create(idCard);
  lv_obj_set_pos(noradLabel, 3, 3);
  lv_obj_set_width(noradLabel, 230);
  lv_obj_set_style_text_align(noradLabel, LV_TEXT_ALIGN_CENTER, 0);
  lv_obj_set_style_text_color(noradLabel, lv_color_black(), 0);
  lv_obj_set_style_text_font(noradLabel, &lv_font_montserrat_18, 0);

  const uint32_t topColors[] = {0xD115E1, 0x95D5E7, 0xDDD5F0};
  const uint8_t topFields[] = {4, 6, 5};  // PASS, LTAN, ROT
  for (uint8_t i = 0; i < 3; ++i) {
    lv_obj_t *card = createCard(screen, 8 + i * 89, 378, 86, 40, topColors[i]);
    const uint8_t field = topFields[i];
    captionLabels[field] = lv_label_create(card);
    lv_label_set_text(captionLabels[field], FIELD_LABELS[field]);
    lv_obj_set_pos(captionLabels[field], 1, 0);
    lv_obj_set_width(captionLabels[field], 84);
    makeCardLabel(captionLabels[field], PARAMETER_FONT);
    valueLabels[field] = lv_label_create(card);
    lv_obj_set_pos(valueLabels[field], 1, 20);
    lv_obj_set_width(valueLabels[field], 84);
    makeCardLabel(valueLabels[field], PARAMETER_FONT);
  }

  const uint32_t rowColors[] = {0x83F000, 0x95D5E7, 0xF0D9F0, 0xEFE8D0};
  const uint8_t rowFields[] = {0, 2, 1, 3};  // SMA, INCL, PER, RAAN
  for (uint8_t i = 0; i < 4; ++i) {
    const int16_t y = i < 3 ? 276 + i * 34 : 422;
    lv_obj_t *card = createCard(screen, 8, y, 264, 30, rowColors[i]);
    const uint8_t field = rowFields[i];
    captionLabels[field] = lv_label_create(card);
    lv_label_set_text(captionLabels[field], FIELD_LABELS[field]);
    lv_obj_set_pos(captionLabels[field], 5, 3);
    lv_obj_set_width(captionLabels[field], 66);
    makeCardLabel(captionLabels[field], PARAMETER_FONT);
    valueLabels[field] = lv_label_create(card);
    lv_obj_set_pos(valueLabels[field], 74, 3);
    lv_obj_set_width(valueLabels[field], 184);
    makeCardLabel(valueLabels[field], PARAMETER_FONT);
  }
  createSettingsOverlay(screen);
  createSettingsHotspot(screen);
  renderCurrentSatellite();
  renderClock();
}

bool parseSatellite(char *line) {
  // SAT|name|norad|sma|period|incl|raan|pass|rotations|ltan|#RRGGBB
  char *save = nullptr;
  char *token = strtok_r(line, "|", &save);
  if (token == nullptr || strcmp(token, "SAT") != 0 || pendingCount >= MAX_SATELLITES) return false;
  char *fields[10];
  for (uint8_t i = 0; i < 10; ++i) {
    fields[i] = strtok_r(nullptr, "|", &save);
    if (fields[i] == nullptr) return false;
  }
  Satellite &sat = pendingSatellites[pendingCount++];
  sat.name = fields[0]; sat.norad = fields[1]; sat.sma = fields[2];
  sat.period = fields[3]; sat.incl = fields[4]; sat.raan = fields[5];
  sat.pass = fields[6]; sat.rotations = fields[7]; sat.ltan = fields[8];
  sat.color = parseColor(fields[9]);
  return true;
}

bool parseClock(const String &line) {
  // TIME|HH:MM is sent by the desktop widget with each atomic snapshot.
  if (!line.startsWith("TIME|") || line.length() != 10 ||
      line[7] != ':' || !isDigit(line[5]) || !isDigit(line[6]) ||
      !isDigit(line[8]) || !isDigit(line[9])) return false;
  const int hours = line.substring(5, 7).toInt();
  const int minutes = line.substring(8, 10).toInt();
  if (hours > 23 || minutes > 59) return false;
  clockMinutes = hours * 60 + minutes;
  clockSyncedMs = millis();
  lastRenderedClockMinutes = -1;
  renderClock();
  return true;
}

void abortImageUpload() {
  if (incomingImageFile) incomingImageFile.close();
  if (ffatReady && !incomingImageNorad.isEmpty()) {
    const String temporaryPath = imagePathForId(incomingImageNorad) + ".tmp";
    FFat.remove(temporaryPath.c_str());
  }
  receivingImage = false;
  incomingImageNorad = "";
  incomingImageBytes = 0;
}

void sendImageStatus() {
  if (!ffatReady) {
    sendToHost("IMG_STATUS|ERROR|ffat unavailable");
    return;
  }
  if (receivingImage) {
    sendToHost("IMG_STATUS|ERROR|upload in progress");
    return;
  }

  File directory = FFat.open("/image");
  if (!directory || !directory.isDirectory()) {
    if (directory) directory.close();
    sendToHost("IMG_STATUS|OK");
    return;
  }

  String response = "IMG_STATUS|OK";
  bool firstImage = true;
  File entry = directory.openNextFile();
  while (entry) {
    if (!entry.isDirectory() && entry.size() == IMAGE_BYTES) {
      String filename = entry.name();
      const int slash = filename.lastIndexOf('/');
      if (slash >= 0) filename = filename.substring(slash + 1);
      if (filename.endsWith(".rgb565")) {
        const String norad = filename.substring(0, filename.length() - 7);
        if (validImageId(norad)) {
          response += firstImage ? "|" : ",";
          response += norad;
          firstImage = false;
        }
      }
    }
    entry.close();
    entry = directory.openNextFile();
  }
  directory.close();
  sendToHost(response);
}

void clearStoredImages() {
  if (!ffatReady || receivingImage) {
    sendToHost("IMG_CLEAR|ERROR|busy");
    return;
  }

  FFat.mkdir("/image");
  File directory = FFat.open("/image");
  if (!directory || !directory.isDirectory()) {
    if (directory) directory.close();
    clearSatelliteImage();
    renderCurrentSatellite();
    sendToHost("IMG_CLEAR|ERROR|open");
    return;
  }

  uint16_t removed = 0;
  uint16_t failed = 0;
  File entry = directory.openNextFile();
  while (entry) {
    if (!entry.isDirectory()) {
      String filename = entry.name();
      const int slash = filename.lastIndexOf('/');
      if (slash < 0) filename = "/image/" + filename;
      entry.close();
      if (FFat.remove(filename.c_str())) ++removed;
      else ++failed;
    } else {
      entry.close();
    }
    entry = directory.openNextFile();
  }
  directory.close();
  clearSatelliteImage();
  renderCurrentSatellite();
  sendToHost("IMG_CLEAR|OK|%u|%u", removed, failed);
}

bool processImageLine(const String &line) {
  if (line == "IMG_STATUS") {
    sendImageStatus();
    return true;
  }

  if (line == "IMG_CLEAR" || line == "CLEAR") {
    clearStoredImages();
    return true;
  }

  if (line.startsWith("IMG_BEGIN|")) {
    if (!ffatReady || receivingImage) {
      sendToHost("IMG_ERROR|busy");
      return true;
    }
    const int separator = line.indexOf('|', 10);
    if (separator < 0) {
      sendToHost("IMG_ERROR|invalid begin");
      return true;
    }
    const String norad = line.substring(10, separator);
    const String metadata = line.substring(separator + 1);
    const int windowSeparator = metadata.indexOf('|');
    const long byteCount = (windowSeparator < 0 ? metadata :
      metadata.substring(0, windowSeparator)).toInt();
    const long ackWindow = windowSeparator < 0 ? 1 :
      metadata.substring(windowSeparator + 1).toInt();
    if (!validImageId(norad) || byteCount != IMAGE_BYTES ||
        ackWindow < 1 || ackWindow > 32) {
      sendToHost("IMG_ERROR|invalid metadata");
      return true;
    }
    incomingImageNorad = norad;
    const String temporaryPath = imagePathForId(norad) + ".tmp";
    FFat.remove(temporaryPath.c_str());
    incomingImageFile = FFat.open(temporaryPath.c_str(), FILE_WRITE);
    if (!incomingImageFile) {
      abortImageUpload();
      sendToHost("IMG_ERROR|storage");
      return true;
    }
    incomingImageBytes = 0;
    incomingImageChunkCount = 0;
    incomingImageAckWindow = (uint8_t)ackWindow;
    receivingImage = true;
    sendToHost("IMG_READY|%s", norad.c_str());
    return true;
  }

  if (line.startsWith("IMG_DATA|")) {
    if (!receivingImage) {
      sendToHost("IMG_ERROR|no upload");
      return true;
    }
    const String encoded = line.substring(9);
    uint8_t decoded[384];
    size_t decodedSize = 0;
    const int result = mbedtls_base64_decode(decoded, sizeof(decoded), &decodedSize,
      reinterpret_cast<const unsigned char *>(encoded.c_str()), encoded.length());
    if (result != 0 || decodedSize == 0 || incomingImageBytes + decodedSize > IMAGE_BYTES) {
      abortImageUpload();
      sendToHost("IMG_ERROR|data");
      return true;
    }
    swapRgb565ByteOrder(decoded, decodedSize);
    if (incomingImageFile.write(decoded, decodedSize) != decodedSize) {
      abortImageUpload();
      sendToHost("IMG_ERROR|storage");
      return true;
    }
    incomingImageBytes += decodedSize;
    ++incomingImageChunkCount;
    // A sender that advertises an acknowledgement window waits for one reply
    // per batch.  Old senders omit it and retain per-line acknowledgement.
    if ((incomingImageChunkCount % incomingImageAckWindow) == 0 ||
        incomingImageBytes == IMAGE_BYTES) {
      sendToHost("IMG_NEXT");
    }
    return true;
  }

  if (line == "IMG_END") {
    if (!receivingImage || incomingImageBytes != IMAGE_BYTES) {
      abortImageUpload();
      sendToHost("IMG_ERROR|size");
      return true;
    }
    const String norad = incomingImageNorad;
    const String temporaryPath = imagePathForId(norad) + ".tmp";
    const String finalPath = imagePathForId(norad);
    incomingImageFile.close();
    FFat.remove(finalPath.c_str());
    if (!FFat.rename(temporaryPath.c_str(), finalPath.c_str())) {
      abortImageUpload();
      sendToHost("IMG_ERROR|storage");
      return true;
    }
    receivingImage = false;
    incomingImageNorad = "";
    incomingImageBytes = 0;
    incomingImageChunkCount = 0;
    incomingImageAckWindow = 1;
    sendToHost("IMG_OK|%s", norad.c_str());
    if (norad == loadedImageNorad || norad == TEMPLATE_IMAGE_ID) clearSatelliteImage();
    renderCurrentSatellite();
    return true;
  }

  return false;
}

void processSerialLine(String line) {
  line.trim();
  if (processImageLine(line)) return;
  if (line == "BEGIN") {
    pendingCount = 0;
    receivingSnapshot = true;
    sendToHost("READY");
    return;
  }
  if (!receivingSnapshot) return;
  if (line.startsWith("CONFIG|")) {
    const long value = line.substring(7).toInt();
    if (value >= 1 && value <= 1440) dwellMinutes = (uint16_t)value;
    return;
  }
  if (line.startsWith("TIME|")) {
    if (!parseClock(line)) sendToHost("ERROR|invalid TIME");
    return;
  }
  if (line == "END") {
    // An empty snapshot is a valid state: the desktop app sends it after the
    // user unchecks the final satellite selected for ESP32 transmission.
    for (uint8_t i = 0; i < pendingCount; ++i) satellites[i] = pendingSatellites[i];
    satelliteCount = pendingCount;
    currentSatellite = 0;
    satelliteShownSinceMs = millis();
    lastSuccessfulUpdateMs = millis();
    haveSnapshot = pendingCount > 0;
    persistSnapshotPlaceholder();
    renderCurrentSatellite();
    sendToHost("OK|%u|%u", satelliteCount, dwellMinutes);
    receivingSnapshot = false;
    return;
  }
  char buffer[384];
  if (line.length() >= sizeof(buffer)) {
    sendToHost("ERROR|line too long");
    return;
  }
  line.toCharArray(buffer, sizeof(buffer));
  if (!parseSatellite(buffer)) sendToHost("ERROR|invalid SAT record");
}

void processIncomingByte(const char ch) {
  if (ch == '\r') return;
  if (ch == '\n') {
    if (serialLine.length()) processSerialLine(serialLine);
    serialLine = "";
    return;
  }
  if (serialLine.length() < 512) {
    serialLine += ch;
  } else {
    serialLine = "";
    sendToHost("ERROR|line too long");
  }
}

void readUsbSerial() {
  while (Serial.available()) processIncomingByte((char)Serial.read());
}

void readBleSerial() {
  while (bleRxTail != bleRxHead) {
    const uint8_t ch = bleRxBuffer[bleRxTail];
    bleRxTail = (uint16_t)((bleRxTail + 1) % BLE_RX_BUFFER_SIZE);
    processIncomingByte((char)ch);
  }
}

void initLedPinsAfterDelay() {
  if (activeHighPinsEnabled || millis() < ACTIVE_HIGH_DELAY_MS) return;
  for (uint8_t i = 0; i < 4; ++i) {
    pinMode(ACTIVE_HIGH_PINS[i], OUTPUT);
    digitalWrite(ACTIVE_HIGH_PINS[i], HIGH);
  }
  activeHighPinsEnabled = true;
}

void updateLedOutputs(uint32_t now) {
  if (!activeHighPinsEnabled) return;
  if (!ledEnabled) {
    for (const uint8_t pin : ACTIVE_HIGH_PINS) digitalWrite(pin, LOW);
    return;
  }

  const uint32_t elapsedMs = now - ACTIVE_HIGH_DELAY_MS;
  const uint32_t waveIntervalMs = (uint32_t)ledBlinkingSeconds * 1000UL;
  const uint32_t waveMs = elapsedMs % waveIntervalMs;
  int8_t lowLed = -1;
  if (waveMs < LED_WAVE_PULSE_MS * 4) {
    lowLed = LED_WAVE_ORDER[waveMs / LED_WAVE_PULSE_MS];
  }
  for (uint8_t i = 0; i < 4; ++i) {
    digitalWrite(ACTIVE_HIGH_PINS[i], i == lowLed ? LOW : HIGH);
  }
}

uint8_t autoLampDuty(uint32_t elapsedMs) {
  const uint32_t vpsDurationMs = (uint32_t)vpsDurationMinutes * 60UL * 1000UL;
  const uint32_t vpsChillMs = (uint32_t)vpsChillMinutes * 60UL * 1000UL;
  const uint32_t cycleMs = (uint32_t)(elapsedMs % (
    LAMP_AUTO_50_MS + vpsDurationMs + LAMP_AUTO_COOLDOWN_MS + vpsChillMs));
  if (cycleMs < LAMP_AUTO_50_MS) {
    // Soft-start the incandescent bulb from 0% to 50% during the warm-up.
    return (uint8_t)(LAMP_PWM_DUTY_50 * cycleMs / LAMP_AUTO_50_MS);
  }
  if (cycleMs < LAMP_AUTO_50_MS + vpsDurationMs) return LAMP_PWM_DUTY_100;
  if (cycleMs < LAMP_AUTO_50_MS + vpsDurationMs + LAMP_AUTO_COOLDOWN_MS) {
    const uint32_t cooldownMs = cycleMs - LAMP_AUTO_50_MS - vpsDurationMs;
    return (uint8_t)(LAMP_PWM_DUTY_50 -
      (LAMP_PWM_DUTY_50 * cooldownMs / LAMP_AUTO_COOLDOWN_MS));
  }
  return LAMP_PWM_DUTY_OFF;
}

void updateLampOutput(uint32_t now) {
  initLampPwm();
  if (bulbMode == BULB_AUTO) {
    setLampPwmDuty(autoLampDuty(now));
  } else {
    setLampPwmDuty((uint8_t)(bulbPwmPercent * 255UL / 100UL));
  }
}

void closeSettingsAfterIdle() {
  if (!settingsVisible) return;
  if ((uint32_t)(millis() - settingsLastInteractionMs) < SETTINGS_IDLE_TIMEOUT_MS) return;
  if (!example_lvgl_lock(-1)) return;
  hideSettingsOverlay();
  example_lvgl_unlock();
}

void setup() {
  // Do not touch GPIO 6/9/12/13 during the first minute.  In particular,
  // GPIO 12 and 13 remain available for native-USB firmware flashing.
  Serial.begin(115200);
  delay(500);
  analogReadResolution(12);
  analogSetPinAttenuation(POWER_ADC_PIN, ADC_11db);
  loadSettings();
  initWs2812();
  initBle();
  sendToHost("BLE READY");
  Touch_Init();
  lcd_lvgl_Init();
  createWidget();
  // A newly flashed data partition has no filesystem yet.  Formatting it on
  // the first mount lets the sender upload the RGB565 files immediately;
  // without this, the device silently falls back to coloured squares.
  ffatReady = FFat.begin(true);
  if (ffatReady) {
    FFat.mkdir("/image");
    sendToHost("FFAT READY");
  } else {
    sendToHost("FFAT ERROR");
  }
  sendToHost("SATWIDGET/1 READY");
}

void loop() {
  readUsbSerial();
  readBleSerial();
  initLedPinsAfterDelay();
  closeSettingsAfterIdle();
  renderClock();
  const uint32_t now = millis();
  saveSettingsIfNeeded(now);
  updatePowerVoltageLabel(now);
  updateWs2812Cycle(now);
  updateLedOutputs(now);
  updateLampOutput(now);
  const int8_t manualStep = pendingSatelliteStep;
  if (manualStep != 0) {
    pendingSatelliteStep = 0;
    if (haveSnapshot && satelliteCount > 0) {
      if (manualStep < 0) {
        currentSatellite = (currentSatellite + satelliteCount - 1) % satelliteCount;
      } else {
        currentSatellite = (currentSatellite + 1) % satelliteCount;
      }
      // Start a full display interval for the manually selected satellite.
      satelliteShownSinceMs = now;
      renderCurrentSatellite();
    }
  }
  if (haveSnapshot && satelliteCount > 1 &&
      (uint32_t)(now - satelliteShownSinceMs) >= (uint32_t)dwellMinutes * 60000UL) {
    currentSatellite = (currentSatellite + 1) % satelliteCount;
    satelliteShownSinceMs = now;
    renderCurrentSatellite();
  }

  static bool previousOffline = true;
  const bool offline = !haveSnapshot || (uint32_t)(now - lastSuccessfulUpdateMs) >= OFFLINE_AFTER_MS;
  if (offline != previousOffline) {
    previousOffline = offline;
    renderCurrentSatellite();
  }
  delay(5);
}
