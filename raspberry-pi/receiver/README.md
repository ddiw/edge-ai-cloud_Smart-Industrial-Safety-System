# receiver

ESP32의 HTTP POST(사진) 수신 후 Redis 큐로 전달하는 컨테이너. 스펙은 [`../../docs/API.md`](../../docs/API.md) 참고.

## 로컬 테스트
```bash
docker compose up receiver redis
curl -X POST http://localhost:8000/upload \
  -H "X-API-Key: $API_KEY" \
  -F "device_id=esp32-01" \
  -F "image=@test.jpg"
```
