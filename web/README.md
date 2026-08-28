# web

현장 실시간 스트리밍 및 위반 이력/갤러리 대시보드.
Raspberry Pi 5 위에서 별도 Docker 컨테이너로 실행 (`../raspberry-pi/docker-compose.yml` 참고).
Redis 큐 기반으로 receiver/inference와 프로세스 분리되어 있어, 추론 부하가 걸려도 스트리밍이 안 밀림.

DB/Storage는 Supabase 사용.

## TODO
- 대시보드 프레임워크 결정
- Supabase 클라이언트 연동 (violations 테이블 조회)
- 실시간 스트리밍 구현
