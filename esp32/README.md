# ESP32 (ZONE 1 — 엣지 단말)

XIAO ESP32S3 Sense. FOMO로 사람을 1차 감지하고, 감지됐을 때만 원본 JPEG을
Raspberry Pi의 receiver로 HTTP POST 한다. 사람이 없는 프레임은 기기 밖으로 나가지 않는다.

## 처음 세팅할 때

### 1. Arduino IDE

Preferences → Additional Board Manager URLs:
```
https://espressif.github.io/arduino-esp32/package_esp32_index.json
```

Tools 메뉴에서:

| 항목 | 값 |
|---|---|
| Board | **XIAO_ESP32S3** |
| **PSRAM** | **OPI PSRAM** ← 끄면 부팅 시 `[boot] PSRAM is required` 로 멈춘다 |
| Partition Scheme | 8M 이상 |

### 2. Edge Impulse 라이브러리 패치 (필수)

Studio에서 받은 Arduino 라이브러리를 그대로 쓰면 추론이 실패한다.
```
ERR: Failed to allocate persistent buffer of size 8, does not fit in tensor arena
     and reached EI_MAX_OVERFLOW_BUFFER_COUNT
```

`~/Arduino/libraries/Person_detection_FOMO_s_inferencing/src/tflite-model/tflite_learn_*_compiled.cpp`
에서 두 값을 올린다.

| 항목 | 원본 | 패치 후 |
|---|---|---|
| `kTensorArenaSize` (else 분기) | `407840` | `700000` |
| `EI_MAX_OVERFLOW_BUFFER_COUNT` | `10` | `30` |

기본 아레나 398KB가 이 모델에 모자라서 나는 오류다. 아레나는 힙에서 잡히고
XIAO ESP32S3에는 PSRAM이 8MB 있어서 크게 잡아도 된다.

> **모델을 다시 export하면 이 패치가 날아간다.** 매번 다시 적용해야 한다.

### 3. secrets.h 만들기

와이파이 비밀번호와 API 키는 `.ino` 에 쓰지 않는다. **이 저장소는 public이라 그대로 공개된다.**

```bash
cd esp32/Esp32_camera_
cp secrets_example.h secrets.h
```

`secrets.h` 를 열어 실제 값을 채운다. 이 파일은 `.gitignore` 대상이라 커밋되지 않는다.
`SECRET_API_KEY` 는 Pi의 `raspberry-pi/.env` 에 있는 `API_KEY` 와 **글자 하나까지 같아야 한다.**
틀리면 업로드가 전부 401로 거부된다.

## 내부망 / 외부 전환

`.ino` 상단의 `USE_TLS` 한 줄로 갈린다.

```cpp
#define USE_TLS 0   // 같은 공유기 안. mDNS로 Pi를 찾아 http로 전송
#define USE_TLS 1   // 어디서든. Cloudflare Tunnel로 https 전송
```

**`USE_TLS 0` (내부망)** — 건드릴 게 없다. `raspberrypi.local` 을 mDNS로 찾으므로
Pi가 DHCP로 다른 IP를 받아도 다시 굽지 않아도 된다. 해석에 실패하면
`SERVER_IP_FALLBACK` 으로 폴백한다.

**`USE_TLS 1` (외부)** — `TUNNEL_HOST` 에 현재 터널 주소를 넣어야 한다.
```cpp
const char* TUNNEL_HOST = "xxxx-xxxx.trycloudflare.com";  // https:// 는 붙이지 않는다
```
Pi에서 현재 주소 확인:
```bash
grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" ~/tunnel/receiver.log | head -1
```

> 지금 터널은 임시 주소라 **cloudflared를 재시작하면 주소가 바뀌고, 보드를 다시 구워야 한다.**
> 고정하는 방법은 [`../docs/HANDOFF.md`](../docs/HANDOFF.md) 참고.

## 보드가 여러 대일 때

`DEVICE_ID` 를 보드마다 다르게 준다 (`esp32-01`, `esp32-02`, …).
대시보드가 이 값으로 보드를 구분하고, 위반 기록에도 그대로 남는다.

## 정상 부팅 로그

```
[wifi] connected, ip=192.168.0.8
[mdns] raspberrypi.local -> 192.168.0.9
[boot] model labels: "hum"  (matching against "hum")
[boot] device=esp32-02 target=http://192.168.0.9:8000/upload (내부망 mDNS)
[boot] capture=640x480 decode=640x480 infer=64x64 threshold=0.60 cooldown=4000ms
[infer] person=1 conf=0.91 (1075 ms) -> upload
[http] 202 accepted (12162 bytes, attempt 1)
[stat] triggered=3 uploaded=3 failed=0 heap=201108 psram=7222864
```

`USE_TLS 1` 이면 `target=https://...trycloudflare.com/upload (TLS)` 로 나오고
`[mdns]` 줄은 나오지 않는다 (터널 주소는 공인 DNS로 풀리므로 mDNS가 필요 없다).

## 문제 해결

| 증상 | 원인 / 조치 |
|---|---|
| `ERR: Failed to allocate persistent buffer` | 위 2번 라이브러리 패치를 안 했다 |
| `[boot] PSRAM is required` | Tools → PSRAM: OPI PSRAM 을 켠다 |
| `[infer]` 줄이 안 나옴 | 부팅 로그의 model labels와 `PERSON_LABEL` 이 다르다. 임시로 `MATCH_ANY_LABEL = true` |
| `[http] 401 unauthorized` | `secrets.h` 의 `SECRET_API_KEY` 가 Pi의 `.env` 와 다르다 |
| `[http] 413` | 사진이 너무 크다. 펌웨어가 자동으로 JPEG 품질을 낮춘다 (조치 불필요) |
| `[http] 503` | Pi 추론 큐가 밀렸다. 백오프 후 자동 재시도 (조치 불필요) |
| `[mdns] 해석 실패` | Pi의 avahi-daemon 확인. 폴백 IP로 붙으므로 동작은 한다 |
| `[cam] init failed` | 카메라 확장보드 체결 확인. Sense 버전이어야 카메라가 있다 |
| TLS 모드에서 전송 실패 | `[stat]` 의 heap 값 확인. mbedTLS가 약 45KB를 쓴다 |

## 주요 설정값

| 상수 | 현재 | 의미 |
|---|---|---|
| `PERSON_CONFIDENCE_MIN` | 0.60 | FOMO 감지 임계값 |
| `COOLDOWN_MS` | 4000 | 업로드 후 다음 촬영까지 쉬는 시간. 짧으면 중복이 늘지만 놓칠 확률은 준다 |
| `MAX_UPLOAD_ATTEMPTS` | 3 | 실패 시 지수 백오프로 재시도 |
| `TRIGGER_FOV_MODE` | CROP | Edge Impulse Studio의 Resize mode와 맞출 것 |

같은 장면이 반복 업로드되는 건 Pi 쪽에서 중복 제거로 걸러내므로, 쿨다운을 굳이 길게
잡지 않아도 DB가 지저분해지지 않는다.
