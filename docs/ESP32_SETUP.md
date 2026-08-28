# ESP32 (XIAO ESP32S3 Sense) 환경 구축

## Arduino IDE 설정

Additional Board Manager URL:
```
https://espressif.github.io/arduino-esp32/package_esp32_index.json
```

- Board: **XIAO_ESP32S3**
- **PSRAM: OPI PSRAM** ← 반드시 켤 것. 끄면 부팅 시 `[boot] PSRAM is required`로 멈춘다.
- Partition Scheme: 8M 이상

## Edge Impulse 라이브러리 패치 (필수)

Studio에서 받은 Arduino 라이브러리(`Person_detection_FOMO_s_inferencing`)를 **그대로 쓰면 추론이 실패한다.**

```
ERR: Failed to allocate persistent buffer of size 8, does not fit in tensor arena
     and reached EI_MAX_OVERFLOW_BUFFER_COUNT
```

기본 텐서 아레나(398KB)가 이 모델에 부족해서 나는 오류다. 아레나는 힙에서 잡히고
XIAO ESP32S3에는 PSRAM이 8MB 있으므로 크게 잡아도 된다.

**패치 위치**: `~/Arduino/libraries/Person_detection_FOMO_s_inferencing/src/tflite-model/tflite_learn_*_compiled.cpp`

| 항목 | 원본 | 패치 후 |
|---|---|---|
| `kTensorArenaSize` (else 분기) | `407840` | `700000` |
| `EI_MAX_OVERFLOW_BUFFER_COUNT` | `10` | `30` |

> 라이브러리는 이 저장소 밖에 있어서 git에 안 들어간다.
> **Edge Impulse에서 모델을 다시 export하면 패치가 날아가므로 매번 다시 적용해야 한다.**

## 모델 라벨 확인

부팅 로그를 반드시 확인할 것:
```
[boot] model labels: "hum"  (matching against "hum")
```

이 FOMO 모델의 라벨은 **`hum`**이다(`person`이 아님). `.ino`의 `PERSON_LABEL`이 이 값과
다르면 사람을 감지해도 업로드가 절대 트리거되지 않는다.
급하면 `MATCH_ANY_LABEL = true`로 라벨 검사를 우회할 수 있다.

## 서버 주소

`SERVER_HOSTNAME = "raspberrypi"`로 mDNS 자동 탐색한다. Pi가 다른 IP를 받거나 공유기를
옮겨도 펌웨어를 다시 굽지 않아도 된다. 해석 실패 시 `SERVER_IP_FALLBACK`으로 폴백한다.

정상 부팅 로그:
```
[wifi] connected, ip=192.168.0.xxx
[mdns] raspberrypi.local -> 192.168.0.9
[boot] device=esp32-01 target=http://192.168.0.9:8000/upload (mdns=raspberrypi.local)
[infer] person=1 conf=0.87 (xx ms) -> upload
[http] 202 accepted (xxxxx bytes, attempt 1)
```
