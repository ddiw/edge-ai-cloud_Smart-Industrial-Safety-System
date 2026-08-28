# Raspberry Pi 5 (엣지 게이트웨이)

Docker Compose로 컨테이너 4개 운영 (프로세스 격리를 위해 웹도 별도 컨테이너로 분리, CPU 바운드인 inference와 GIL 경합 안 나게):

- [`receiver/`](receiver/) — ESP32의 HTTP POST(사진) 수신, Redis 큐에 push 후 즉시 응답 (ESP32가 추론 끝날 때까지 안 기다리게)
- [`inference/`](inference/) — Redis 큐 polling, `best.onnx`(ONNX Runtime, 강사님 제공 학습 모델)로 정밀 위반 분석, 바운딩 박스 생성 후 Supabase Storage/DB 업로드
- [`../web/`](../web/) — 실시간 스트리밍 + 위반 이력/갤러리 대시보드, Pi 위에서 별도 컨테이너로 실행
- `redis` — receiver ↔ inference 간 비동기 큐

## TODO
- Docker / Docker Compose 설치
- receiver 서비스 구현 (Flask)
- inference 서비스 구현 (onnxruntime, Redis consumer)
- Supabase 연동 (Storage 업로드 + DB insert, 재시도 로직 포함 — 와이파이 불안정 대비)
- 얼굴 블러 처리 로직
- 외부 접근 필요 시 Cloudflare Tunnel 연결
