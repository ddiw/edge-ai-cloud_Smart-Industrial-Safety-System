# Raspberry Pi 5 (엣지 게이트웨이)

Docker Compose로 컨테이너 2개 운영:

- [`receiver/`](receiver/) — ESP32의 HTTP POST 수신, 사진을 큐로 전달
- [`inference/`](inference/) — `best.onnx`(ONNX Runtime)로 정밀 위반 분석, 바운딩 박스 생성 후 클라우드 업로드

## TODO
- Docker / Docker Compose 설치
- receiver 서비스 구현 (Flask/FastAPI 등)
- inference 서비스 구현 (onnxruntime)
- 큐잉 방식 결정 (Redis, 파일 기반 등)
- 클라우드 업로드 연동 (Storage + DB)
