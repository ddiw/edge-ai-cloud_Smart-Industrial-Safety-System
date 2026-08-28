# ESP32 HTTP POST 연동 테스트

실제 `receiver` 컨테이너(라즈베리파이 담당자 몫)가 아직 없으므로, README.md §7.1 인터페이스
스펙(`POST /api/frames`)을 그대로 흉내 내는 가짜 서버로 ESP32의 HTTP POST가 실제로
도달하고 정상 동작하는지 먼저 검증한다. Python 표준 라이브러리만 사용하므로 추가 설치가 필요 없다.

## 파일

| 파일 | 역할 |
|---|---|
| `mock_receiver.py` | `receiver` 컨테이너를 흉내 내는 테스트 서버. 202/400/413/503 응답을 스펙대로 재현. |
| `send_test_frame.py` | ESP32 없이 PC에서 동일한 헤더/바디로 테스트 프레임을 전송. 서버 자체의 동작 확인용. |

## 1단계 — 서버 단독 검증 (ESP32 없이)

```bash
# 테스트 PC에서
python mock_receiver.py --port 8080
```

다른 터미널에서:

```bash
python send_test_frame.py --host 127.0.0.1 --port 8080
```

`202 Accepted`가 출력되고 `test/received/` 폴더에 jpg가 저장되면 서버 자체는 정상이다.

**엣지 케이스 테스트**

```bash
# 503 -> 재시도 로직 확인용 (ESP32 쪽 지수 백오프, 최대 3회)
python mock_receiver.py --port 8080 --fail-first-n 2
```

## 2단계 — 실제 ESP32와 연동

1. 테스트 서버를 돌릴 PC를 ESP32와 **동일한 Wi-Fi**(`Esp32 camera.ino`의 `WIFI_SSID`)에 연결한다.
2. 그 PC의 로컬 IPv4 주소를 확인한다.
   - Windows: `ipconfig` (Wi-Fi 어댑터의 IPv4 주소)
3. `Esp32 camera.ino`의 `SERVER_IP`를 **이 PC의 IP**로 임시 수정한다 (라즈베리파이 IP가 아님).
   ```cpp
   const char* SERVER_IP = "172.30.1.xx"; // 테스트 PC의 Wi-Fi IPv4
   ```
4. Windows 방화벽에서 해당 포트(기본 8080) 인바운드를 허용한다. 안 열려 있으면 ESP32의
   `HTTPClient`가 연결 자체를 못 맺고 시리얼에 transport error가 찍힌다.
5. 서버 실행:
   ```bash
   python mock_receiver.py --port 8080
   ```
6. ESP32에 펌웨어 업로드 후 시리얼 모니터(115200bps)와 서버 콘솔을 동시에 관찰한다.
   - 사람이 카메라에 잡히면 시리얼에 `[infer] person=... -> upload`, 이어서 `[http] 202 accepted`가 찍혀야 한다.
   - 동시에 서버 콘솔에 `POST /api/frames` 로그와 저장 경로가 찍힌다.
   - `test/received/`에 저장된 jpg를 열어 실제로 감지 순간의 원본 사진이 맞는지 육안 확인한다.

## 확인 포인트 (PRD G1: ESP32 → Pi5 이미지 POST 성공률)

- [ ] 사람이 없을 때는 아무 요청도 서버에 도달하지 않는다 (로그 없음).
- [ ] 사람이 감지되면 `202`가 반환되고 시리얼의 `statUploaded` 카운터가 증가한다.
- [ ] 헤더(`X-Device-Id`, `X-Captured-At`, `X-Person-Count`, `X-Fomo-Confidence`)가 예상값으로 채워진다.
      `X-Captured-At`이 계속 `0`이면 NTP 동기화 실패 — Wi-Fi는 붙었는데 `configTime` 이후 시간이
      확정되기 전에 촬영된 경우이거나 인터넷 아웃바운드(NTP UDP 123)가 막혀 있는 경우다.
- [ ] `--fail-first-n`으로 503을 강제했을 때 ESP32가 백오프 후 재시도해 최종적으로 202를 받는다.
- [ ] Wi-Fi를 잠시 끊었다 붙였을 때 `connectWifi()`가 재연결하고 업로드가 재개된다.

## 자주 발생하는 문제

| 증상 | 원인 |
|---|---|
| 시리얼에 `[http] transport error -1` 반복 | `SERVER_IP`가 테스트 PC의 실제 IP와 다름 / 방화벽 차단 / 다른 대역(예: 5GHz·2.4GHz 분리 라우터에서 서로 다른 서브넷) |
| 서버 로그에 아무것도 안 찍힘 | ESP32가 Wi-Fi 자체에 연결 못함 (시리얼의 `[wifi] connected` 로그 여부로 우선 확인) |
| `400 bad request` | Content-Type이 `image/jpeg`가 아니거나 `X-Device-Id` 누락 — `.ino`의 `uploadFrame()` 헤더 설정 확인 |
| `413`이 반복되며 화질만 계속 낮아짐 | VGA 해상도 자체가 512KB를 넘는 경우는 거의 없음. 실제로는 JPEG 품질 초기값(12)이 아니라 다른 원인(오디오/PSRAM 손상 등)으로 프레임 크기가 비정상적으로 큰 것일 수 있으니 저장된 jpg 파일 크기를 확인할 것 |
