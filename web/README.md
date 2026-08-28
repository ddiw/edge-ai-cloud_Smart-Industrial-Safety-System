# web

현장 실시간 스트리밍 및 위반 이력/갤러리 대시보드.
Raspberry Pi 5 위에서 별도 Docker 컨테이너로 실행 (`../raspberry-pi/docker-compose.yml` 참고).
Redis 기반으로 receiver/inference와 프로세스 분리되어 있어, 추론 부하가 걸려도 스트리밍이 안 밀림.

## 엔드포인트

- `GET /stream?device_id=esp32-01` — MJPEG 실시간 스트림. 브라우저에서 `<img src="/stream">`로 바로 사용
- `GET /violations?limit=50&offset=0&device_id=...` — Supabase `violations` 테이블 조회 (최신순)
- `GET /healthz` — 헬스체크

스펙 상세는 [`../docs/API.md`](../docs/API.md) 참고.

## 배포
Gunicorn + gevent 워커로 실행 (Flask 개발 서버는 스트림 하나가 워커를 붙잡아서 다른 요청이 막힘).

## TODO
- 프론트엔드 UI (현재는 JSON API + raw 스트림만)
- 외부 노출 시 인증 (Supabase Auth 검토)
