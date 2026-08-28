# 설계 결정 기록

작업하면서 정한 결정들을 이유와 함께 기록. 새로 정하는 게 있으면 아래에 계속 추가.

## 아키텍처

- **컨테이너 4개로 분리**: `receiver` / `inference` / `web` / `redis`
  - 이유: Python 쓰레드는 GIL 때문에 CPU 바운드 작업(ONNX 추론)에서 진짜 병렬 처리가 안 됨. Docker 컨테이너(별도 프로세스)로 나눠야 추론 부하가 웹 스트리밍 응답성에 영향 안 줌.
- **receiver ↔ inference는 Redis 큐로 비동기 연결** (직접 동기 호출 아님)
  - 이유: ESP32는 네트워크/전원이 상대적으로 불안정한 엣지 기기. receiver가 추론 끝날 때까지 기다렸다가 응답하면 ESP32 타임아웃 위험. receiver는 사진 받으면 큐에 넣고 즉시 `200 OK` 응답.

## 네트워크

- **내부망 우선, 외부 노출은 필요할 때 Cloudflare Tunnel**
  - 이유: 포트포워딩은 한국 통신사 CGNAT 때문에 애초에 안 될 수 있음. Tailscale/ngrok도 검토했으나 Cloudflare Tunnel이 무료+CGNAT 무관+HTTPS 자동이라 데모/외부 테스트용으로 적합.
  - 여러 대 ESP32를 다른 네트워크에서 테스트하고 싶다는 요구사항 있음 → 나중에 Tunnel로 해결.
- Pi 고정 IP: DHCP reservation 권장 (ESP32는 mDNS `.local` 클라이언트가 기본 내장 안 되어 있는 경우가 많음)

## ESP32 ↔ receiver 통신

- **HTTP POST (MQTT 아님)**
  - 이유: MQTT는 바이너리(이미지) 전송 시 base64 인코딩 오버헤드(~33%)와 브로커 packet size 제한 이슈. HTTP `multipart/form-data`가 이미지 업로드에 더 적합.
- **인증: 고정 API 키(`X-API-Key` 헤더)**
  - 이유: ESP32는 TLS/HMAC 같은 복잡한 암호화 연산 부담이 큼. 내부망 트래픽이라 우선순위는 "누가 보냈는지 확인"이지 "도청 방지"가 아님. 외부 노출 시 재검토.
- **전송 형식: raw `image/jpeg` 본문 + 메타데이터는 HTTP 헤더** (multipart 아님)
  - 처음엔 multipart로 잡았으나, 팀원이 작성한 펌웨어 규약에 맞춰 receiver를 고쳤다. 임베디드에서
    multipart를 직접 조립하면 코드와 RAM만 늘어난다. receiver는 40줄짜리라 바꾸는 쪽이 훨씬 싸다.
  - curl 수동 테스트를 위해 multipart도 계속 받아준다.
- **timestamp는 ESP32가 NTP로 맞춘 `X-Captured-At`(UTC epoch)을 우선 사용.** 미동기 시 0을 보내면
  receiver가 수신 시각으로 대체한다. (초기엔 "ESP32에 RTC가 없다"고 보고 수신 시각만 쓰기로 했는데,
  펌웨어가 NTP 동기를 하고 있어 촬영 시각이 더 정확하다.)
- **응답 코드로 백프레셔를 표현**: `202` 접수 / `400` 잘못된 요청 / `401` 키 불일치 /
  `413` 페이로드 초과(ESP32가 JPEG 품질을 낮춤) / `503` 큐 포화(ESP32가 지수 백오프).
  - 큐를 계속 받아두기만 하면 Redis 메모리는 불어나는데 추론이 못 따라간다. 밀어내는 게 맞다.

## 모델 (YOLO / ONNX Runtime)

- **강사님이 제공한 모델**: [Hansung-Cho/yolov8-ppe-detection](https://huggingface.co/Hansung-Cho/yolov8-ppe-detection) (PPE 미착용 탐지, YOLOv8n, mAP@0.5 0.744)
  - `best.pt` → ONNX 변환 완료 (11.7MB). 클래스: `Hardhat, Mask, NO-Hardhat, NO-Mask, NO-Safety Vest, Person, Safety Cone, Safety Vest, machinery, vehicle`
  - `NO-` 접두사 = 위반 클래스로 `inference/app.py`의 `is_violation()`에서 그대로 사용
  - 출력 shape `(1, 14, 8400)` = 4(bbox) + 10(클래스) 확인됨
  - 변환은 **RTX 4060 있는 개발 PC에서** 수행 (Pi는 배포 시 `onnxruntime`만 필요, `torch`/`ultralytics` 불필요 — 변환 단계에만 필요한 무거운 의존성이라 Pi에 안 깖)
  - **Pi 5 CPU 추론 속도 벤치마크 완료** (640x640, onnxruntime CPUExecutionProvider, 20회 평균): 평균 183.2ms, p95 211.4ms, 약 5.5 FPS
    - 이벤트 트리거 방식(사람 감지 시에만 전송)이라 이 속도로 충분. 상시 스트리밍 추론이 필요해지면 그때 INT8 양자화 검토
  - `raspberry-pi/inference/models/best.onnx`에 배치 완료 (`.gitignore`로 git 추적 제외, 용량 큼)

## 저장 (Supabase)

- DB + Storage 둘 다 Supabase로 통합 (별도 S3/GCS 연동 안 함)
- **버킷을 2개로 분리**: `violations`(Public, 블러+bbox) / `violations-original`(Private, 블러 없는 원본)
  - 이유: 같은 버킷에 두면 원본 URL을 아는 사람은 누구나 얼굴이 그대로인 사진에 접근 가능. 원본은 URL이 아니라 경로만 DB에 저장하고, 열람이 필요하면 service_role로 signed URL 발급.
- `service_role` 키는 Pi 백엔드에서만 사용, 클라이언트(web 프론트)는 `anon` + RLS 정책으로 제한
- **Supabase 업로드 실패 시 재시도**: 3회 지수 백오프 후 Redis dead-letter(`violation_jobs:failed`)로 이동. job 데이터는 성공할 때까지 Redis에 유지 (Pi 와이파이 끊김 대비)

실제 스키마는 [`supabase_schema.sql`](supabase_schema.sql) 참고.

## 웹 스트리밍

- **MJPEG** (`multipart/x-mixed-replace`) 선택, WebSocket 아님
  - 이유: 단방향 영상엔 WebSocket과 실질적 성능 차이 크지 않음. 브라우저가 `<img>` 태그로 네이티브 디코딩해서 클라이언트 부담도 적음. mjpg-streamer 등 임베디드 진영에서 검증된 방식.
- **Gunicorn + gevent 워커**로 배포 (Flask 기본 개발 서버는 스트림 하나가 워커를 붙잡아서 다른 요청 막힘)
- 스트리밍 프레임: 해상도 640x480, JPEG 품질 80% (기본값, 나중에 튜닝)
- inference → web 프레임 전달: **Redis에 `latest_frame:<device_id>` 키로 최신 프레임 캐싱** (계속 덮어쓰기)
  - device_id를 키에 포함시켜서, 나중에 ESP32가 여러 대로 늘어나도 로직 안 바꿔도 됨.

## 추론 파이프라인 구현 시 주의사항

검토 중 발견해서 고친 것들. 다시 건드릴 때 되돌리지 말 것.

- **전처리는 letterbox(종횡비 유지 + 패딩)** 필수. 그냥 `cv2.resize`로 640x640에 욱여넣으면 종횡비가 찌그러져 정확도가 떨어진다. YOLOv8이 letterbox로 학습됐기 때문.
- **NMS는 클래스별로 따로** 걸어야 한다. 클래스를 섞어서 억제하면 같은 사람 위에 겹치는 `Person`과 `NO-Safety Vest`가 서로를 지운다.
  - 실측: 전신 Person 박스와 몸통 NO-Safety Vest 박스의 **IoU 0.743** (임계값 0.45). 클래스 무시 NMS였으면 위반이 통째로 사라졌음.
- **블러 → bbox 순서**로 그린다. 반대로 하면 블러가 박스 선 위에 덮인다.
- 모델 메타데이터 파싱은 `eval` 대신 `ast.literal_eval`.
- MJPEG은 프레임이 바뀔 때만 전송. 이벤트 트리거라 갱신 간격이 길어서, 매번 보내면 같은 이미지를 반복 전송하게 된다.

## 얼굴 블러 (Haar → YuNet 교체 완료)

**교체 이유 — Haar Cascade는 사실상 동작하지 않았다.**
실측(2026-08-28): 1280x1920 사진에서 실제 얼굴(상단부)은 **미검출**, 대신 y=1664(세로 87% 지점)
재킷 위를 얼굴로 **오탐**해 엉뚱한 곳에 블러를 걸었다. 결과적으로 공개 버킷에 얼굴이 그대로 올라갔다.
"블러가 부정확"이 아니라 "개인정보 보호가 없는 것과 같은" 상태였다.

**현재 방식**: `cv2.FaceDetectorYN`(YuNet, `models/face_detection_yunet_2023mar.onnx`, 228KB)로
검출한 얼굴 + PPE 모델이 뱉는 머리/얼굴 부위 박스(`Hardhat`/`NO-Hardhat`/`Mask`/`NO-Mask`)의 **합집합**을 블러.
얼굴 검출이 실패해도 PPE 머리박스가 커버하는 이중 안전장치.

구현 시 주의:
- 블러 커널을 영역 크기에 비례시킨다(`min(w,h)//4`). 고정 커널(51)은 고해상도에서 얼굴이 크면 윤곽이 남는다.
- 박스를 15% 넓혀서 가린다. 검출 박스가 얼굴을 빠듯하게 잡는 경우가 있다.

검증 결과(테스트 사진 5장):

| 사진 | YuNet 얼굴 | PPE 머리박스 | 판정 |
|---|---|---|---|
| ben_kerckx | 1 | 2 | 가려짐 |
| ignartonosbg | 0 | 0 | 뒤통수만 보이는 사진이라 정상 |
| mostafa | 3 | 2 | 가려짐 |
| quanlecntt | 2 | 4 | 3명 전원 가려짐 |
| zeebolos | 1 | 1 | 가려짐 (Haar가 놓쳤던 사진) |

## 알려진 한계

- OpenCV 5.0에서 `CascadeClassifier`가 제거됐다. 지금은 Haar를 안 쓰므로 무관하지만, 컨테이너가 4.10에
  고정돼 있다는 점은 기억해둘 것.
- Gaussian 블러는 이론상 일부 복원이 가능하다. 더 강한 보호가 필요하면 픽셀레이션(축소 후 확대)으로 교체.
- 모델이 탐지하는 PPE는 **안전모/마스크/안전조끼 3종뿐**. 작업화(안전화)는 클래스에 없어서 별도 모델이나 재학습이 필요하다.

## 보류/나중에 결정

- Docker 리소스 배분(`cpus`, `mem_limit` 등) — 실사용 확인 후 튜닝
- web 대시보드 자체 로그인 — 외부 노출 시점에 Supabase Auth 검토
