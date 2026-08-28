/*
 * Smart Industrial Safety System - ZONE 1 Firmware
 * FOMO person detection -> event-triggered JPEG upload over HTTP POST
 *
 * Based on the Edge Impulse ESP32 camera example (MIT License, (c) 2022 EdgeImpulse Inc.)
 * Model: Person_detection_FOMO (Edge Impulse Arduino library export)
 *
 * Behaviour:
 *   1. Continuously capture VGA JPEG frames.
 *   2. Decode a half-scale copy and run the FOMO person-detection model.
 *   3. If a person is detected, POST the ORIGINAL full-resolution JPEG that
 *      produced the detection to the edge gateway (receiver container).
 *   4. Cool down, then resume scanning.
 *
 * No MQTT. No periodic upload. Frames without a person never leave the device.
 */

/* Includes --------------------------------------------------------------- */
#include <Person_detection_FOMO_inferencing.h>
#include "edge-impulse-sdk/dsp/image/image.hpp"

#include "esp_camera.h"
#include "img_converters.h"
#include <HTTPClient.h>
#include <WiFi.h>
#include <time.h>

/* ── Board selection ────────────────────────────────────────────────────── */
//#define CAMERA_MODEL_AI_THINKER
//#define CAMERA_MODEL_ESP_EYE
#define CAMERA_MODEL_XIAO_ESP32S3

/* ── User settings ──────────────────────────────────────────────────────── */
const char* WIFI_SSID = "";
const char* WIFI_PASSWORD = "";

const char* SERVER_IP = "172.30.1.16"; // Raspberry Pi 5 LAN IPv4
const uint16_t SERVER_PORT = 8080;      // receiver container
const char* UPLOAD_PATH = "/api/frames";
const char* DEVICE_ID = "esp32-cam-01";

const char* NTP_SERVER = "pool.ntp.org";

/* Detection policy (PRD 9.1) */
const char* PERSON_LABEL = "person"; // 부팅 로그의 model labels와 대조할 것
const bool MATCH_ANY_LABEL = false;  // true면 라벨 무시하고 모든 박스를 사람으로 취급
const float PERSON_CONFIDENCE_MIN = 0.60f;
const uint32_t COOLDOWN_MS = 3000; // 동일 장면 폭주 방지

/* Upload policy (PRD 7.1) */
const uint8_t MAX_UPLOAD_ATTEMPTS = 3;
const uint32_t BACKOFF_BASE_MS = 200;
const uint16_t HTTP_TIMEOUT_MS = 3000;
const uint16_t HTTP_CONNECT_TIMEOUT_MS = 1000;

/* ── Camera pin map ─────────────────────────────────────────────────────── */
#if defined(CAMERA_MODEL_AI_THINKER)
#define PWDN_GPIO_NUM 32
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 0
#define SIOD_GPIO_NUM 26
#define SIOC_GPIO_NUM 27
#define Y9_GPIO_NUM 35
#define Y8_GPIO_NUM 34
#define Y7_GPIO_NUM 39
#define Y6_GPIO_NUM 36
#define Y5_GPIO_NUM 21
#define Y4_GPIO_NUM 19
#define Y3_GPIO_NUM 18
#define Y2_GPIO_NUM 5
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM 23
#define PCLK_GPIO_NUM 22
#define STATUS_LED_GPIO 33 // active low

#elif defined(CAMERA_MODEL_ESP_EYE)
#define PWDN_GPIO_NUM -1
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 4
#define SIOD_GPIO_NUM 18
#define SIOC_GPIO_NUM 23
#define Y9_GPIO_NUM 36
#define Y8_GPIO_NUM 37
#define Y7_GPIO_NUM 38
#define Y6_GPIO_NUM 39
#define Y5_GPIO_NUM 35
#define Y4_GPIO_NUM 14
#define Y3_GPIO_NUM 13
#define Y2_GPIO_NUM 34
#define VSYNC_GPIO_NUM 5
#define HREF_GPIO_NUM 27
#define PCLK_GPIO_NUM 25

#elif defined(CAMERA_MODEL_XIAO_ESP32S3)
#define PWDN_GPIO_NUM -1
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 10
#define SIOD_GPIO_NUM 40
#define SIOC_GPIO_NUM 39
#define Y9_GPIO_NUM 48
#define Y8_GPIO_NUM 11
#define Y7_GPIO_NUM 12
#define Y6_GPIO_NUM 14
#define Y5_GPIO_NUM 16
#define Y4_GPIO_NUM 18
#define Y3_GPIO_NUM 17
#define Y2_GPIO_NUM 15
#define VSYNC_GPIO_NUM 38
#define HREF_GPIO_NUM 47
#define PCLK_GPIO_NUM 13
#define STATUS_LED_GPIO 21 // active low

#else
#error "Camera model not selected"
#endif

/* ── Frame geometry ─────────────────────────────────────────────────────── *
 * 업로드는 원본 VGA JPEG, 추론은 1/2 스케일 디코드본으로 수행한다.
 * (PRD 9.1: 추론용 저해상도가 아닌 원본을 전송)
 * ------------------------------------------------------------------------ */
#define CAPTURE_FRAMESIZE FRAMESIZE_VGA
#define CAPTURE_WIDTH 640
#define CAPTURE_HEIGHT 480

// esp32-camera가 공개하는 스케일 디코더는 jpg2rgb565뿐이다.
// RGB888은 fmt2rgb888로 원본 해상도 전체를 디코드해야 한다.
#define DECODE_WIDTH CAPTURE_WIDTH
#define DECODE_HEIGHT CAPTURE_HEIGHT

#define DECODE_BUF_BYTES ((size_t)DECODE_WIDTH * DECODE_HEIGHT * 3) // 921,600 bytes
#define JPEG_BUF_BYTES (96 * 1024) // VGA q12 실측 30~50KB. 여유 포함.

/* 320x240 -> 96x96 축소 방식. Edge Impulse Studio의 Resize mode와 맞출 것.
 *   FOV_MODE_CROP   : 중앙 정사각 크롭 후 보간 ("Fit shortest axis"). 좌우 25% FOV 손실.
 *   FOV_MODE_SQUASH : 종횡비 무시하고 전체를 눌러 담음 ("Squash"). FOV 전량 보존.
 */
#define FOV_MODE_CROP 0
#define FOV_MODE_SQUASH 1
#define TRIGGER_FOV_MODE FOV_MODE_CROP

/* ── State ──────────────────────────────────────────────────────────────── */
struct PersonState {
  bool detected;
  uint8_t count;
  float maxConfidence;
  uint32_t inferenceMs;
};

static uint8_t* jpegBuf = nullptr;   // 추론에 사용된 바로 그 원본 JPEG
static size_t jpegLength = 0;
static uint8_t* decodeBuf = nullptr; // RGB888 작업 버퍼
static bool cameraInitialised = false;

static uint32_t statTriggered = 0;
static uint32_t statUploaded = 0;
static uint32_t statFailed = 0;

static camera_config_t cameraConfig = {
  .pin_pwdn = PWDN_GPIO_NUM,
  .pin_reset = RESET_GPIO_NUM,
  .pin_xclk = XCLK_GPIO_NUM,
  .pin_sscb_sda = SIOD_GPIO_NUM,
  .pin_sscb_scl = SIOC_GPIO_NUM,
  .pin_d7 = Y9_GPIO_NUM,
  .pin_d6 = Y8_GPIO_NUM,
  .pin_d5 = Y7_GPIO_NUM,
  .pin_d4 = Y6_GPIO_NUM,
  .pin_d3 = Y5_GPIO_NUM,
  .pin_d2 = Y4_GPIO_NUM,
  .pin_d1 = Y3_GPIO_NUM,
  .pin_d0 = Y2_GPIO_NUM,
  .pin_vsync = VSYNC_GPIO_NUM,
  .pin_href = HREF_GPIO_NUM,
  .pin_pclk = PCLK_GPIO_NUM,
  .xclk_freq_hz = 20000000,
  .ledc_timer = LEDC_TIMER_0,
  .ledc_channel = LEDC_CHANNEL_0,
  .pixel_format = PIXFORMAT_JPEG,
  .frame_size = CAPTURE_FRAMESIZE,
  .jpeg_quality = 12, // 0-63, 낮을수록 고화질
  .fb_count = 2,
  .fb_location = CAMERA_FB_IN_PSRAM,
  .grab_mode = CAMERA_GRAB_LATEST, // 항상 최신 프레임으로 추론
};

/* ── Edge Impulse signal callback ───────────────────────────────────────── */
static int eiGetData(size_t offset, size_t length, float* outPtr) {
  size_t pixelIndex = offset * 3;
  for (size_t i = 0; i < length; i++, pixelIndex += 3) {
    // BGR -> RGB swap (espressif/esp32-camera#379)
    outPtr[i] = (decodeBuf[pixelIndex + 2] << 16)
      | (decodeBuf[pixelIndex + 1] << 8)
      | decodeBuf[pixelIndex];
  }
  return 0;
}

/* ── Infrastructure ─────────────────────────────────────────────────────── */
static void setStatusLed(bool on) {
#ifdef STATUS_LED_GPIO
  digitalWrite(STATUS_LED_GPIO, on ? LOW : HIGH); // active low
#endif
}

static bool connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return true;
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  for (int i = 0; i < 30 && WiFi.status() != WL_CONNECTED; i++) delay(500);
  if (WiFi.status() != WL_CONNECTED) return false;
  Serial.printf("[wifi] connected, ip=%s\n", WiFi.localIP().toString().c_str());
  configTime(0, 0, NTP_SERVER); // X-Captured-At 용 UTC epoch
  return true;
}

static uint32_t currentEpoch() {
  time_t now = time(nullptr);
  // NTP 미동기 상태에서는 0을 보내고 receiver가 received_at으로 대체한다.
  return (now > 1700000000) ? (uint32_t)now : 0;
}

static bool initialiseCamera() {
  if (cameraInitialised) return true;

#if defined(CAMERA_MODEL_ESP_EYE)
  pinMode(13, INPUT_PULLUP);
  pinMode(14, INPUT_PULLUP);
#endif

  esp_err_t err = esp_camera_init(&cameraConfig);
  if (err != ESP_OK) {
    Serial.printf("[cam] init failed: 0x%x\n", err);
    return false;
  }

  sensor_t* sensor = esp_camera_sensor_get();
  if (sensor->id.PID == OV3660_PID) {
    sensor->set_vflip(sensor, 1);
    sensor->set_brightness(sensor, 1);
    sensor->set_saturation(sensor, 0);
  }
#if defined(CAMERA_MODEL_ESP_EYE)
  sensor->set_vflip(sensor, 1);
  sensor->set_hmirror(sensor, 1);
  sensor->set_awb_gain(sensor, 1);
#endif

  // 초기 프레임은 AE/AWB가 수렴하지 않아 버린다.
  for (int i = 0; i < 3; i++) {
    camera_fb_t* fb = esp_camera_fb_get();
    if (fb) esp_camera_fb_return(fb);
  }
  cameraInitialised = true;
  return true;
}

/* 413 대응: 해상도 대신 JPEG 품질을 낮춰 페이로드를 줄인다. */
static void reduceJpegSize() {
  sensor_t* sensor = esp_camera_sensor_get();
  if (!sensor) return;
  int quality = cameraConfig.jpeg_quality + 6;
  if (quality > 40) quality = 40;
  cameraConfig.jpeg_quality = quality;
  sensor->set_quality(sensor, quality);
  Serial.printf("[cam] payload too large, jpeg_quality -> %d\n", quality);
}

static void printModelLabels() {
  Serial.print("[boot] model labels:");
  for (uint16_t i = 0; i < EI_CLASSIFIER_LABEL_COUNT; i++) {
    Serial.printf(" \"%s\"", ei_classifier_inferencing_categories[i]);
  }
  Serial.printf("  (matching against \"%s\")\n", MATCH_ANY_LABEL ? "*" : PERSON_LABEL);
}

/* ── Pipeline ───────────────────────────────────────────────────────────── */

/* 원본 JPEG을 PSRAM으로 복사해 둔다. 이 복사본이 추론 대상이자 업로드 대상이다. */
static bool grabFrame() {
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) return false;
  if (fb->len > JPEG_BUF_BYTES) {
    Serial.printf("[cam] frame %u bytes exceeds buffer, dropped\n", (unsigned)fb->len);
    esp_camera_fb_return(fb);
    reduceJpegSize();
    return false;
  }
  memcpy(jpegBuf, fb->buf, fb->len);
  jpegLength = fb->len;
  esp_camera_fb_return(fb);
  return true;
}

#if TRIGGER_FOV_MODE == FOV_MODE_SQUASH
/* 종횡비를 무시하고 전체 프레임을 모델 입력 크기로 눌러 담는다 (in-place).
 * dst 인덱스가 항상 src 인덱스 이하이므로 전진 스캔 시 덮어쓰기 안전. */
static void squashRgb888(uint8_t* buf, int srcW, int srcH, int dstW, int dstH) {
  for (int y = 0; y < dstH; y++) {
    int sy = (int)(((int64_t)y * srcH) / dstH);
    for (int x = 0; x < dstW; x++) {
      int sx = (int)(((int64_t)x * srcW) / dstW);
      const uint8_t* src = buf + ((size_t)sy * srcW + sx) * 3;
      uint8_t* dst = buf + ((size_t)y * dstW + x) * 3;
      dst[0] = src[0];
      dst[1] = src[1];
      dst[2] = src[2];
    }
  }
}
#endif

static bool detectPerson(PersonState& out) {
  out = {};

  if (!fmt2rgb888(jpegBuf, jpegLength, PIXFORMAT_JPEG, decodeBuf)) {
    Serial.println("[infer] jpeg decode failed");
    return false;
  }

#if TRIGGER_FOV_MODE == FOV_MODE_SQUASH
  squashRgb888(decodeBuf, DECODE_WIDTH, DECODE_HEIGHT,
    EI_CLASSIFIER_INPUT_WIDTH, EI_CLASSIFIER_INPUT_HEIGHT);
#else
  ei::image::processing::crop_and_interpolate_rgb888(
    decodeBuf, DECODE_WIDTH, DECODE_HEIGHT, decodeBuf,
    EI_CLASSIFIER_INPUT_WIDTH, EI_CLASSIFIER_INPUT_HEIGHT
  );
#endif

  ei::signal_t signal;
  signal.total_length = EI_CLASSIFIER_INPUT_WIDTH * EI_CLASSIFIER_INPUT_HEIGHT;
  signal.get_data = &eiGetData;

  ei_impulse_result_t result = {};
  EI_IMPULSE_ERROR err = run_classifier(&signal, &result, false);
  if (err != EI_IMPULSE_OK) {
    Serial.printf("[infer] run_classifier failed (%d)\n", err);
    return false;
  }

  out.inferenceMs = result.timing.dsp + result.timing.classification;
  for (uint32_t i = 0; i < result.bounding_boxes_count; i++) {
    const ei_impulse_result_bounding_box_t& box = result.bounding_boxes[i];
    if (box.value < PERSON_CONFIDENCE_MIN) continue;
    if (!MATCH_ANY_LABEL && strcasecmp(box.label, PERSON_LABEL) != 0) continue;
    out.count++;
    if (box.value > out.maxConfidence) out.maxConfidence = box.value;
  }
  out.detected = out.count > 0;
  return true;
}

/* 반환값: 업로드 성공 여부. PRD 7.1의 응답 코드 계약을 따른다. */
static bool uploadFrame(const PersonState& person, uint32_t capturedAt) {
  const String url = String("http://") + SERVER_IP + ":" + String(SERVER_PORT) + UPLOAD_PATH;
  uint32_t backoff = BACKOFF_BASE_MS;

  for (uint8_t attempt = 1; attempt <= MAX_UPLOAD_ATTEMPTS; attempt++) {
    if (!connectWifi()) {
      delay(backoff);
      backoff *= 2;
      continue;
    }

    HTTPClient http;
    if (!http.begin(url)) {
      Serial.println("[http] begin failed");
      return false;
    }
    http.setConnectTimeout(HTTP_CONNECT_TIMEOUT_MS);
    http.setTimeout(HTTP_TIMEOUT_MS);
    http.addHeader("Content-Type", "image/jpeg");
    http.addHeader("X-Device-Id", DEVICE_ID);
    http.addHeader("X-Captured-At", String(capturedAt));
    http.addHeader("X-Person-Count", String(person.count));
    http.addHeader("X-Fomo-Confidence", String(person.maxConfidence, 2));

    int status = http.POST(jpegBuf, jpegLength);
    http.end();

    switch (status) {
      case 202: // Accepted - 큐 적재 성공
        Serial.printf("[http] 202 accepted (%u bytes, attempt %u)\n",
          (unsigned)jpegLength, attempt);
        return true;

      case 400: // Bad Request - 재시도해도 동일. 폐기.
        Serial.println("[http] 400 bad request, frame dropped");
        return false;

      case 413: // Payload Too Large - 다음 프레임부터 크기를 줄인다.
        reduceJpegSize();
        return false;

      case 503: // Service Unavailable - 큐 포화. 백오프 후 재시도.
        Serial.printf("[http] 503 queue full, retry in %u ms\n", (unsigned)backoff);
        delay(backoff);
        backoff *= 2;
        continue;

      default:
        if (status < 0) { // 전송 계층 오류
          Serial.printf("[http] transport error %d, retry in %u ms\n", status, (unsigned)backoff);
          delay(backoff);
          backoff *= 2;
          continue;
        }
        Serial.printf("[http] unexpected status %d, frame dropped\n", status);
        return false;
    }
  }
  return false;
}

static void pipelineTask(void* parameter) {
  while (true) {
    if (!grabFrame()) {
      vTaskDelay(pdMS_TO_TICKS(100));
      continue;
    }

    PersonState person;
    if (!detectPerson(person)) {
      vTaskDelay(pdMS_TO_TICKS(100));
      continue;
    }

    if (!person.detected) {
      // 사람 없음 -> 로컬 폐기. 네트워크로 아무것도 나가지 않는다.
      taskYIELD();
      continue;
    }

    statTriggered++;
    Serial.printf("[infer] person=%u conf=%.2f (%lu ms) -> upload\n",
      person.count, person.maxConfidence, person.inferenceMs);

    setStatusLed(true);
    bool ok = uploadFrame(person, currentEpoch());
    setStatusLed(false);
    ok ? statUploaded++ : statFailed++;

    Serial.printf("[stat] triggered=%lu uploaded=%lu failed=%lu heap=%u psram=%u\n",
      statTriggered, statUploaded, statFailed,
      (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getFreePsram());

    vTaskDelay(pdMS_TO_TICKS(COOLDOWN_MS)); // 동일 장면 반복 전송 억제
  }
}

/* ── Entry points ───────────────────────────────────────────────────────── */
void setup() {
  Serial.begin(115200);
  delay(300);

#ifdef STATUS_LED_GPIO
  pinMode(STATUS_LED_GPIO, OUTPUT);
  setStatusLed(false);
#endif

  if (!psramFound()) {
    Serial.println("[boot] PSRAM is required");
    return;
  }

  jpegBuf = (uint8_t*)ps_malloc(JPEG_BUF_BYTES);
  decodeBuf = (uint8_t*)ps_malloc(DECODE_BUF_BYTES);
  if (!jpegBuf || !decodeBuf) {
    Serial.println("[boot] PSRAM allocation failed");
    return;
  }

  if (!initialiseCamera()) {
    Serial.println("[boot] camera init failed");
    return;
  }

  connectWifi();
  printModelLabels();

  Serial.printf("[boot] device=%s target=http://%s:%u%s\n",
    DEVICE_ID, SERVER_IP, SERVER_PORT, UPLOAD_PATH);
  Serial.printf("[boot] capture=%dx%d decode=%dx%d infer=%dx%d threshold=%.2f cooldown=%lums\n",
    CAPTURE_WIDTH, CAPTURE_HEIGHT, DECODE_WIDTH, DECODE_HEIGHT,
    EI_CLASSIFIER_INPUT_WIDTH, EI_CLASSIFIER_INPUT_HEIGHT,
    PERSON_CONFIDENCE_MIN, COOLDOWN_MS);

  xTaskCreatePinnedToCore(pipelineTask, "pipeline", 12288, nullptr, 1, nullptr, 1);
}

void loop() {
  delay(1000);
}

/* ── Compile-time guards ────────────────────────────────────────────────── */
#if !defined(EI_CLASSIFIER_SENSOR) || EI_CLASSIFIER_SENSOR != EI_CLASSIFIER_SENSOR_CAMERA
#error "Invalid model for current sensor"
#endif

#if EI_CLASSIFIER_OBJECT_DETECTION != 1
#error "FOMO object detection model required (classification model will not work)"
#endif