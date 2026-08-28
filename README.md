# Edge AI 산업안전 시스템

ESP32가 현장에서 사람을 1차 감지해 사진을 올리면, Raspberry Pi 5가 PPE(안전모·마스크·안전조끼)
착용 여부를 정밀 분석하고, 위반 건만 얼굴 블러 처리해 클라우드에 남기는 시스템.

```
ZONE 1  ESP32-S3 (FOMO)      사람 감지 시에만 원본 JPEG 전송
   │                          HTTP POST + X-API-Key
   ▼
ZONE 2  Raspberry Pi 5        receiver → Redis 큐 → inference(ONNX/YOLOv8n)
   │                          얼굴 블러 → 위반 판정 → 업로드
   ▼
ZONE 3  Supabase              violations 테이블 + Storage(공개/비공개 분리)
                              web 대시보드에서 조회
```

사람이 없는 프레임에서 나온 위반은 오탐으로 보고 버리며, 같은 위반이 반복되면 한 건만 남긴다.

## 구성

| 경로 | 역할 |
|---|---|
| [`esp32/`](esp32/) | XIAO ESP32S3 펌웨어 (Arduino, Edge Impulse FOMO) |
| [`raspberry-pi/receiver/`](raspberry-pi/receiver/) | 사진 수신 → Redis 큐. 인증·레이트리밋·백프레셔 |
| [`raspberry-pi/inference/`](raspberry-pi/inference/) | ONNX 추론, 얼굴 블러, Supabase 적재 |
| [`web/`](web/) | 관제 대시보드 (로그인 필요) |
| [`docs/`](docs/) | 설계 결정, API 스펙, 개발 워크플로 |

Pi에서 `receiver` / `inference` / `web` / `redis` 컨테이너 4개로 돈다.
Python 쓰레드는 GIL 때문에 CPU 바운드인 추론을 병렬화하지 못하므로 프로세스 단위로 나눴다.

## 문서

- [`docs/HANDOFF.md`](docs/HANDOFF.md) — **이어받는 사람은 여기부터**
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — 설계 결정과 그 이유, 실측값, 알려진 한계
- [`docs/API.md`](docs/API.md) — 엔드포인트 스펙
- [`docs/DEV_WORKFLOW.md`](docs/DEV_WORKFLOW.md) — 재빌드 없이 코드 반영하는 법
- [`docs/ESP32_SETUP.md`](docs/ESP32_SETUP.md) — Arduino IDE 설정, Edge Impulse 라이브러리 패치
- [`docs/supabase_schema.sql`](docs/supabase_schema.sql) — 테이블·권한·RLS

## 실행

**1. Supabase** — [`docs/supabase_schema.sql`](docs/supabase_schema.sql) 실행,
Storage 버킷 2개 생성 (`violations` 공개 / `violations-original` 비공개).

**2. 설정**
```bash
cd raspberry-pi
cp .env.example .env   # 키와 비밀번호를 채운다
```

**3. 모델 배치** — `raspberry-pi/inference/models/` 에 두 파일이 필요하다 (용량 때문에 git 제외).
```
best.onnx                          PPE 탐지 (YOLOv8n → ONNX 변환)
face_detection_yunet_2023mar.onnx  얼굴 블러용 (OpenCV Zoo)
```

**4. 기동**
```bash
docker compose up -d
```
대시보드 `:8080`, receiver `:8000`.

**5. ESP32** — [`docs/ESP32_SETUP.md`](docs/ESP32_SETUP.md) 참고.
`esp32/Esp32_camera_/secrets_example.h` 를 `secrets.h` 로 복사해 값을 채운다.

## 외부 공개

Cloudflare Tunnel로 내보낸다. 공유기 포트를 열지 않아도 되고 통신사 CGNAT와 무관하다.
```bash
cloudflared tunnel --url http://localhost:8080   # 대시보드
cloudflared tunnel --url http://localhost:8000   # receiver (ESP32를 외부에 둘 때만)
```
ESP32를 외부망에서 쓰려면 `.ino` 의 `USE_TLS` 를 `1` 로 바꾸고 `TUNNEL_HOST` 를 채운다.
터널은 HTTPS만 받으므로 평문으로는 붙지 않는다.

## 보안

저장소가 public이므로 비밀값은 전부 git 밖에 둔다 (`raspberry-pi/.env`, `esp32/**/secrets.h`).

| 대상 | 방식 |
|---|---|
| receiver | `X-API-Key` 상수 시간 비교, IP당 분당 40회 제한 |
| 대시보드 | 비밀번호 로그인, 세션 쿠키(HttpOnly), 로그인 시도 제한 |
| 위반 사진 | 얼굴 블러본만 공개 버킷. 원본은 비공개 버킷 + `ADMIN_KEY` + 수명 5분 signed URL |
| Supabase | 백엔드만 `service_role`. 대시보드는 `anon` + RLS, `reviewed` 컬럼만 UPDATE 허용 |

## 담당

- ESP32: 팀원
- Raspberry Pi / Web: ddiw
