#include "lcd_bsp.h"
#include "FT3168.h"
#include <FFat.h>
#include <mbedtls/base64.h>
#include <stdarg.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// USB CDC text protocol. See README.md and satellite_sender.py.
// A received END atomically replaces the displayed satellite set.
constexpr uint8_t MAX_SATELLITES = 8;
constexpr uint32_t OFFLINE_AFTER_MS = 24UL * 60UL * 60UL * 1000UL;
constexpr uint16_t IMAGE_WIDTH = 130;
constexpr uint16_t IMAGE_HEIGHT = 130;
constexpr size_t IMAGE_BYTES = IMAGE_WIDTH * IMAGE_HEIGHT * 2;

// BLE UART (Nordic UART Service). Same text protocol as USB CDC.
constexpr const char *BLE_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E";
constexpr const char *BLE_CHARACTERISTIC_UUID_RX = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E";
constexpr const char *BLE_CHARACTERISTIC_UUID_TX = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E";
constexpr size_t BLE_TX_CHUNK_SIZE = 20;
constexpr size_t BLE_RX_BUFFER_SIZE = 1024;

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
File incomingImageFile;
uint8_t *satelliteImageData = nullptr;
String loadedImageNorad;
lv_img_dsc_t *satelliteImageDsc = nullptr;

BLEServer *bleServer = nullptr;
BLECharacteristic *bleTxCharacteristic = nullptr;
volatile bool bleConnected = false;
uint8_t bleRxBuffer[BLE_RX_BUFFER_SIZE];
volatile uint16_t bleRxHead = 0;
volatile uint16_t bleRxTail = 0;

lv_obj_t *statusIndicator;
lv_obj_t *satelliteIcon;
lv_obj_t *satelliteCounterLabel;
lv_obj_t *nameLabel;
lv_obj_t *noradLabel;
lv_obj_t *valueLabels[7];

const char *FIELD_LABELS[] = {
  "SMA:", "PER:", "INCL:", "RAAN:", "PASS:", "ROT:", "LTAN:"
};

// Reserved extension point for persisting orbital snapshots in internal flash.
void persistSnapshotPlaceholder() {}

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

void previousSatelliteButtonEvent(lv_event_t *event) {
  pendingSatelliteStep = -1;
}

void nextSatelliteButtonEvent(lv_event_t *event) {
  pendingSatelliteStep = 1;
}

void createNavigationButton(lv_obj_t *screen, int16_t hitboxX, int16_t visualX,
                            const char *symbol, lv_event_cb_t callback) {
  // A large transparent button makes the small visual control easy to tap.
  lv_obj_t *hitbox = lv_btn_create(screen);
  lv_obj_set_pos(hitbox, hitboxX, 38);
  lv_obj_set_size(hitbox, 70, 130);
  lv_obj_set_style_bg_opa(hitbox, LV_OPA_TRANSP, 0);
  lv_obj_set_style_bg_opa(hitbox, LV_OPA_TRANSP, LV_STATE_PRESSED);
  lv_obj_set_style_border_width(hitbox, 0, 0);
  lv_obj_set_style_shadow_width(hitbox, 0, 0);
  lv_obj_clear_flag(hitbox, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_add_event_cb(hitbox, callback, LV_EVENT_CLICKED, nullptr);

  lv_obj_t *button = lv_obj_create(hitbox);
  lv_obj_set_pos(button, visualX - hitboxX, 48);
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

bool validNoradId(const String &norad) {
  if (norad.isEmpty()) return false;
  for (size_t i = 0; i < norad.length(); ++i) {
    if (!isDigit(norad[i])) return false;
  }
  return true;
}

String imagePathForNorad(const String &norad) {
  return "/image/" + norad + ".rgb565";
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

void clearSatelliteImage() {
  lv_img_set_src(satelliteIcon, nullptr);
  if (satelliteImageData != nullptr) {
    free(satelliteImageData);
    satelliteImageData = nullptr;
  }
  if (satelliteImageDsc != nullptr) {
    free(satelliteImageDsc);
    satelliteImageDsc = nullptr;
  }
  loadedImageNorad = "";
}

bool loadSatelliteImage(const String &norad) {
  if (loadedImageNorad == norad && satelliteImageData != nullptr) return true;
  if (!ffatReady || !validNoradId(norad)) {
    clearSatelliteImage();
    return false;
  }

  const String path = imagePathForNorad(norad);
  File imageFile = FFat.open(path.c_str(), FILE_READ);
  if (!imageFile || imageFile.size() != IMAGE_BYTES) {
    if (imageFile) imageFile.close();
    clearSatelliteImage();
    return false;
  }

  uint8_t *data = static_cast<uint8_t *>(malloc(IMAGE_BYTES));
  if (data == nullptr || imageFile.read(data, IMAGE_BYTES) != IMAGE_BYTES) {
    if (data != nullptr) free(data);
    imageFile.close();
    clearSatelliteImage();
    return false;
  }
  imageFile.close();

  lv_img_dsc_t *descriptor = static_cast<lv_img_dsc_t *>(malloc(sizeof(lv_img_dsc_t)));
  if (descriptor == nullptr) {
    free(data);
    clearSatelliteImage();
    return false;
  }

  clearSatelliteImage();
  satelliteImageData = data;
  satelliteImageDsc = descriptor;
  satelliteImageDsc->header.always_zero = 0;
  satelliteImageDsc->header.w = IMAGE_WIDTH;
  satelliteImageDsc->header.h = IMAGE_HEIGHT;
  satelliteImageDsc->header.cf = LV_IMG_CF_TRUE_COLOR;
  satelliteImageDsc->data_size = IMAGE_BYTES;
  satelliteImageDsc->data = satelliteImageData;
  loadedImageNorad = norad;
  lv_img_set_src(satelliteIcon, satelliteImageDsc);
  lv_obj_invalidate(satelliteIcon);
  return true;
}

void renderCurrentSatellite() {
  if (!example_lvgl_lock(-1)) return;

  const bool isOffline = !haveSnapshot ||
    (uint32_t)(millis() - lastSuccessfulUpdateMs) >= OFFLINE_AFTER_MS;
  lv_obj_set_style_bg_color(statusIndicator,
    isOffline ? lv_color_hex(0xF02B2B) : lv_color_hex(0x20B850), 0);

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
  lv_obj_set_style_bg_color(satelliteIcon, sat.color, 0);
  loadSatelliteImage(sat.norad);
  lv_label_set_text_fmt(satelliteCounterLabel, "%u/%u", currentSatellite + 1, satelliteCount);
  setLabelText(nameLabel, sat.name);
  setLabelText(noradLabel, "NORAD " + sat.norad);
  const String values[] = {sat.sma, sat.period, sat.incl, sat.raan,
                           sat.pass, sat.rotations, sat.ltan};
  for (uint8_t i = 0; i < 7; ++i) setLabelText(valueLabels[i], values[i]);
  example_lvgl_unlock();
}

void createWidget() {
  lv_obj_t *screen = lv_scr_act();
  lv_obj_set_style_bg_color(screen, lv_color_black(), 0);
  lv_obj_set_style_bg_opa(screen, LV_OPA_COVER, 0);

  statusIndicator = lv_obj_create(screen);
  lv_obj_set_pos(statusIndicator, 12, 12);
  lv_obj_set_size(statusIndicator, 12, 12);
  lv_obj_set_style_radius(statusIndicator, LV_RADIUS_CIRCLE, 0);
  lv_obj_set_style_border_width(statusIndicator, 0, 0);
  lv_obj_clear_flag(statusIndicator, LV_OBJ_FLAG_SCROLLABLE);

  satelliteCounterLabel = lv_label_create(screen);
  lv_obj_set_pos(satelliteCounterLabel, 75, 8);
  lv_obj_set_width(satelliteCounterLabel, 130);
  lv_obj_set_style_text_align(satelliteCounterLabel, LV_TEXT_ALIGN_CENTER, 0);
  makeLargeWhiteLabel(satelliteCounterLabel);

  satelliteIcon = lv_img_create(screen);
  lv_obj_set_pos(satelliteIcon, 75, 38);
  lv_obj_set_size(satelliteIcon, 130, 130);
  lv_obj_set_style_radius(satelliteIcon, 4, 0);
  lv_obj_set_style_border_width(satelliteIcon, 0, 0);
  lv_obj_set_style_bg_opa(satelliteIcon, LV_OPA_COVER, 0);
  lv_obj_clear_flag(satelliteIcon, LV_OBJ_FLAG_SCROLLABLE);

  createNavigationButton(screen, 0, 12, "<", previousSatelliteButtonEvent);
  createNavigationButton(screen, 210, 212, ">", nextSatelliteButtonEvent);

  nameLabel = lv_label_create(screen);
  lv_obj_set_width(nameLabel, 256);
  lv_obj_set_pos(nameLabel, 12, 177);
  lv_obj_set_style_text_align(nameLabel, LV_TEXT_ALIGN_CENTER, 0);
  lv_label_set_long_mode(nameLabel, LV_LABEL_LONG_DOT);
  lv_obj_set_style_text_color(nameLabel, lv_color_white(), 0);
  lv_obj_set_style_text_font(nameLabel, &lv_font_montserrat_24, 0);

  noradLabel = lv_label_create(screen);
  lv_obj_set_width(noradLabel, 256);
  lv_obj_set_pos(noradLabel, 12, 207);
  lv_obj_set_style_text_align(noradLabel, LV_TEXT_ALIGN_CENTER, 0);
  lv_obj_set_style_text_color(noradLabel, lv_color_white(), 0);
  lv_obj_set_style_text_font(noradLabel, &lv_font_montserrat_16, 0);

  for (uint8_t i = 0; i < 7; ++i) {
    lv_obj_t *caption = lv_label_create(screen);
    lv_label_set_text(caption, FIELD_LABELS[i]);
    lv_obj_set_pos(caption, 10, 236 + i * 31);
    makeLargeWhiteLabel(caption);

    valueLabels[i] = lv_label_create(screen);
    lv_obj_set_pos(valueLabels[i], 87, 236 + i * 31);
    lv_obj_set_width(valueLabels[i], 185);
    lv_label_set_long_mode(valueLabels[i], LV_LABEL_LONG_DOT);
    makeLargeWhiteLabel(valueLabels[i]);
  }
  renderCurrentSatellite();
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

void abortImageUpload() {
  if (incomingImageFile) incomingImageFile.close();
  if (ffatReady && !incomingImageNorad.isEmpty()) {
    const String temporaryPath = imagePathForNorad(incomingImageNorad) + ".tmp";
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
        if (validNoradId(norad)) {
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

bool processImageLine(const String &line) {
  if (line == "IMG_STATUS") {
    sendImageStatus();
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
    const long byteCount = line.substring(separator + 1).toInt();
    if (!validNoradId(norad) || byteCount != IMAGE_BYTES) {
      sendToHost("IMG_ERROR|invalid metadata");
      return true;
    }
    incomingImageNorad = norad;
    const String temporaryPath = imagePathForNorad(norad) + ".tmp";
    FFat.remove(temporaryPath.c_str());
    incomingImageFile = FFat.open(temporaryPath.c_str(), FILE_WRITE);
    if (!incomingImageFile) {
      abortImageUpload();
      sendToHost("IMG_ERROR|storage");
      return true;
    }
    incomingImageBytes = 0;
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
    sendToHost("IMG_NEXT");
    return true;
  }

  if (line == "IMG_END") {
    if (!receivingImage || incomingImageBytes != IMAGE_BYTES) {
      abortImageUpload();
      sendToHost("IMG_ERROR|size");
      return true;
    }
    const String norad = incomingImageNorad;
    const String temporaryPath = imagePathForNorad(norad) + ".tmp";
    const String finalPath = imagePathForNorad(norad);
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
    sendToHost("IMG_OK|%s", norad.c_str());
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

void setup() {
  Serial.begin(115200);
  delay(500);
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
  const uint32_t now = millis();
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
