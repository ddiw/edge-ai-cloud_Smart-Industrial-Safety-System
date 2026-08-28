# 인수인계

이어서 작업할 사람이 가장 먼저 읽을 문서. 지금 어디까지 되어 있고, 무엇이 안 되어 있고,
어디서부터 손대면 되는지 정리했다.

## 지금 상태 한 줄

ESP32 → Pi → Supabase → 대시보드까지 **실기기로 전 구간 동작 확인 완료**.
보드가 외부(Cloudflare Tunnel, HTTPS)로 붙어서 사진을 올리고, 위반이 DB에 쌓이고,
로그인한 사람만 대시보드에서 볼 수 있다.

## 접속 정보

| 대상 | 주소 |
|---|---|
| 대시보드 | 터널 URL (아래 "터널 주소" 참고) 또는 내부망 `http://raspberrypi.local:8080` |
| receiver | 터널 URL 또는 `http://raspberrypi.local:8000` |
| Pi SSH | `ssh pi@raspberrypi.local` |

비밀번호·키는 전부 `raspberry-pi/.env` 와 `esp32/Esp32_camera_/secrets.h` 에 있다.
**둘 다 git에 없다.** 저장소가 public이므로 절대 커밋하지 말 것.
새로 세팅한다면 `.env.example`, `secrets_example.h` 를 복사해서 채운다.

## 실측값 (추측 아님, 직접 재본 값)

| 항목 | 값 |
|---|---|
| Pi 5 ONNX 추론 | 183ms/frame, 약 5.5 FPS (640x640, CPU) |
| ESP32 FOMO 추론 | 약 1075ms/frame |
| ESP32 여유 메모리 | heap 201KB, PSRAM 7.2MB |
| 업로드 프레임 크기 | VGA q12 기준 12~13KB |
| PPE 모델 | mAP@0.5 0.744 / precision 0.831 / recall 0.685 |

## 아직 안 된 것

1. **터널 URL이 고정이 아니다.** `cloudflared tunnel --url` 은 재시작할 때마다 주소가 바뀐다.
   ESP32 펌웨어에 `TUNNEL_HOST` 가 하드코딩돼 있어서, 주소가 바뀌면 **보드를 다시 구워야 한다.**
   해결하려면 아래 "터널 주소" 참고.
2. **터널이 systemd 서비스가 아니다.** `nohup` 으로 띄워둬서 Pi를 재부팅하면 사라진다.
3. **검토(reviewed) 버튼이 화면에 없다.** API(`POST /violations/<id>/review`)는 되지만 UI가 없다.
   Supabase에서 아래 SQL을 실행해야 동작한다:
   ```sql
   grant update (reviewed) on violations to anon;
   create policy "allow update reviewed for anon" on violations
       for update using (true) with check (true);
   ```
4. **원본 열람 버튼이 화면에 없다.** API(`GET /violations/<id>/original`, `X-Admin-Key` 헤더)는 된다.
5. **테스트 데이터가 섞여 있다.** `esp32-test`, `esp32-dedup`, `esp32-sec`, `esp32-tunnel`,
   `route-internal`, `route-external` 은 개발 중 만든 가짜 보드다. 실제 보드는 `esp32-01`, `esp32-02`.
   화면에서 필터로 걸러 보거나 정리할 것.
6. **오래된 기록 일부에 얼굴이 안 가려져 있다.** 얼굴 블러를 YuNet으로 고치기 *전에* 저장된
   건들이다. 공개 버킷에 있으므로 시연 전에 지우는 게 낫다.

## 터널 주소를 고정하려면

`trycloudflare` 임시 터널은 URL이 매번 바뀐다. 고정하는 방법:

- **Cloudflare 계정 + 본인 도메인** → named tunnel. 가장 정석이지만 도메인이 있어야 한다.
- **Tailscale Funnel** → 도메인 없이도 `*.ts.net` 고정 주소를 무료로 받는다. 계정만 있으면 된다.
- **ngrok** → 무료 플랜에 고정 도메인 1개가 포함된다.

무엇을 고르든 ESP32의 `TUNNEL_HOST` 를 그 주소로 바꾸고 다시 구워야 한다.

터널을 재부팅에도 살리려면 systemd 서비스로 만들 것. 지금은 이렇게 떠 있다:
```bash
nohup cloudflared tunnel --url http://localhost:8000 > ~/tunnel/receiver.log 2>&1 &
nohup cloudflared tunnel --url http://localhost:8080 > ~/tunnel/web.log 2>&1 &
# 현재 주소 확인
grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" ~/tunnel/receiver.log | head -1
```

## 손대기 전에 알아야 할 함정

여기 적힌 건 전부 **실제로 당해보고 고친 것**이다. 되돌리지 말 것.
자세한 이유는 [`DECISIONS.md`](DECISIONS.md) 에 있다.

| 함정 | 요약 |
|---|---|
| NMS를 클래스 구분 없이 걸면 | `Person`과 `NO-Safety Vest`가 IoU 0.743으로 겹쳐서 **위반이 통째로 사라진다** |
| 전처리에서 그냥 resize하면 | 종횡비가 찌그러져 정확도가 떨어진다. letterbox 필수 |
| 얼굴 블러를 Haar로 되돌리면 | 얼굴을 놓치고 엉뚱한 데를 가린다. **공개 버킷에 맨얼굴이 올라간다** |
| 블러를 bbox 뒤에 걸면 | 박스 선 위에 블러가 덮인다. 블러 → bbox 순서 |
| 원본을 공개 버킷에 두면 | URL만 알면 누구나 근로자 얼굴을 본다. 버킷 2개 분리 유지 |
| Edge Impulse 모델을 재-export하면 | 텐서 아레나 패치가 날아간다. [`ESP32_SETUP.md`](ESP32_SETUP.md) 참고 |
| 단일 파일을 bind mount하면 | rsync가 inode를 바꿔서 코드 수정이 반영되지 않는다. 디렉터리를 마운트할 것 |
| `DASHBOARD_PASSWORD` 를 비우면 | web 컨테이너가 아예 기동을 거부한다 (인증 없이 공개되는 것을 막기 위함) |

## 자주 쓰는 명령

개발 흐름은 [`DEV_WORKFLOW.md`](DEV_WORKFLOW.md) 에 정리돼 있다. 핵심만:

```bash
# PC에서 고친 코드를 Pi로
rsync -az --exclude '.git/' --exclude '__pycache__/' --exclude 'test_picture/' \
  ~/raspberrypi_project/ pi@raspberrypi.local:~/project/

# Pi에서 (~/project/raspberry-pi)
docker compose ps
docker compose logs -f inference
docker compose restart inference     # 파이썬 코드 고쳤을 때
docker compose up -d                 # .env 나 compose 고쳤을 때
docker compose build inference       # requirements.txt 나 Dockerfile 고쳤을 때만
```

`receiver` 와 `web` 은 gunicorn `--reload` 라서 코드만 고치면 재시작도 필요 없다.

## 튜닝 포인트

전부 `.env` 에 있고, 대부분 재빌드 없이 `docker compose up -d` 만으로 반영된다.

| 변수 | 현재 | 의미 |
|---|---|---|
| `CONF_THRESHOLD` | 0.3 | 낮추면 미탐↓ 오탐↑. 0.5에서는 실제 위반을 통째로 놓쳤다 |
| `REQUIRE_PERSON` | 1 | 사람 없는 프레임의 위반을 버린다. 오탐의 대부분이 이것이었다 |
| `DEDUPE_WINDOW_S` | 60 | 같은 위반 반복을 1건으로 묶는 창 |
| `SESSION_IDLE_S` | 300 | 이 시간 요청이 없으면 재로그인 |
| `RATE_LIMIT_PER_MIN` | 40 | receiver IP당 분당 요청 제한 |
| `COOLDOWN_MS` (.ino) | 4000 | ESP32 촬영 간격. 바꾸려면 재컴파일 필요 |

## 다음에 하면 좋을 것

우선순위 순.

1. **터널 URL 고정** — 지금은 재시작마다 보드를 다시 구워야 해서 가장 아프다.
2. **터널 systemd 등록** — 재부팅 후 자동 복구.
3. **검토/원본 열람 버튼** — API는 이미 있고 화면만 붙이면 된다.
4. **오래된 미블러 기록 정리** — 시연 전 필수.
5. **`setInsecure()` 걷어내기** — 지금 ESP32는 서버 인증서를 검증하지 않는다.
   통신은 암호화되지만 MITM은 막지 못한다. 고정 도메인으로 가면 `setCACert()` 로 바꿀 것.
6. **작업화(안전화) 탐지** — 현재 모델에는 클래스가 없다. 별도 모델이나 재학습 필요.
