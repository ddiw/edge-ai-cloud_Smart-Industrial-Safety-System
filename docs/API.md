# API 스펙

## receiver (포트 8000)

### `POST /upload`
ESP32 → Pi. FOMO가 사람을 감지했을 때만 호출한다 (상시 스트리밍 아님).

**요청**
```
Content-Type: image/jpeg          ← raw 본문. multipart 아님
Body: JPEG 바이트 그대로

헤더:
  X-API-Key         (필수) receiver .env의 API_KEY와 일치해야 함
  X-Device-Id       (필수) 예: "esp32-01"
  X-Captured-At     UTC epoch 초. NTP 미동기 시 0을 보내면 수신 시각으로 대체됨
  X-Person-Count    FOMO가 센 사람 수
  X-Fomo-Confidence FOMO 최대 confidence
```

임베디드에서 multipart를 조립하면 코드와 RAM만 늘어나므로 raw 본문을 쓴다.
`curl` 수동 테스트를 위해 `multipart/form-data`(`image` 파일 + `device_id` 필드)도 받아준다.

**응답**

| 코드 | 의미 | ESP32 동작 |
|---|---|---|
| `202` | 큐 적재 성공 | 정상 |
| `400` | 본문 또는 `X-Device-Id` 누락 | 재시도 무의미, 프레임 폐기 |
| `401` | API 키 불일치 | 재시도 무의미, 폐기 |
| `413` | 페이로드가 `MAX_UPLOAD_BYTES`(기본 512KB) 초과 | JPEG 품질을 낮춰 다음 프레임부터 축소 |
| `503` | 큐 깊이가 `MAX_QUEUE_DEPTH`(기본 50) 이상 | 지수 백오프 후 재시도 |

`503`은 백프레셔다. 계속 받아두기만 하면 Redis 메모리는 불어나는데 추론이 못 따라간다.

### `GET /healthz`
`{"status": "ok", "queue_depth": N}`

## web (포트 8080)

### `GET /stream?device_id=esp32-01`
MJPEG 실시간 스트림. 브라우저에서 `<img src="/stream">`로 바로 사용.
프레임이 바뀔 때만 전송한다 (이벤트 트리거라 갱신 간격이 길어서, 매번 보내면 같은 이미지를 반복 전송하게 됨).

### `GET /violations?limit=50&offset=0&device_id=...`
Supabase `violations` 테이블 조회 (최신순).
사진 한 장에서 위반이 여러 건 나오면 행도 여러 개다. 화면에서는 `image_url` 기준으로 묶을 것.

### `GET /devices`
붙어 있는 보드 목록.
```json
[{"device_id": "esp32-01", "last_seen": "2026-08-28T06:02:44+00:00",
  "uploads": 1, "streaming": false}]
```
ESP32는 사람이 감지될 때만 전송해서 간격이 몇 분씩 벌어질 수 있다. 온라인 여부는
`last_seen` 경과 시간으로 화면에서 판단할 것. `streaming`은 Redis에 최신 프레임이
살아있는지(TTL 30초) 여부다.

### `GET /healthz`
`{"status": "ok", "queue_depth": N}`

## 검증된 동작 (2026-08-28)

펌웨어와 동일한 형태로 실제 요청해 확인:

| 케이스 | 결과 |
|---|---|
| raw image/jpeg + 전체 헤더 | 202 |
| API 키 없음 | 401 |
| `X-Device-Id` 누락 | 400 |
| 700KB 페이로드 | 413 |
| multipart 하위호환 | 202 |
| `X-Captured-At` 전파 | epoch → DB `timestamp`에 정확히 반영됨 |
