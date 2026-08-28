# 프로젝트 개요

ESP32 엣지 단말(FOMO 기반 1차 사람 감지) → Raspberry Pi 5 게이트웨이(Docker, ONNX Runtime 기반 2차 위반 분석) → Supabase(Storage + DB) 로 이어지는 실시간 위반 감지 시스템.

## 구성

- [`esp32/`](esp32/) — ESP32 펌웨어 (ESP-IDF 기반, 카메라 캡처 및 HTTP POST 전송)
- [`raspberry-pi/`](raspberry-pi/) — Pi 5 게이트웨이. Docker 컨테이너 4개(receiver, inference, redis, web)로 프로세스 분리
- [`web/`](web/) — 웹 대시보드, Pi 위에서 별도 컨테이너로 실행

## 담당

- ESP32: TBD
- Raspberry Pi: TBD
- Web: TBD
