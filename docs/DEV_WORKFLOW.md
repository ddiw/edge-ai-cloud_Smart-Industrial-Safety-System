# 개발 워크플로

Pi에서 컨테이너를 돌리면서 코드를 고칠 때, **매번 `docker compose build`를 하지 않는다.**
빌드는 몇 분씩 걸리는데 코드 한 줄 고칠 때마다 반복할 이유가 없다.

## 원칙

| 무엇을 고쳤나 | 필요한 작업 |
|---|---|
| `app.py` (파이썬 코드) | `receiver`/`web`은 **아무것도 안 해도 됨** (gunicorn `--reload`가 감지). `inference`는 `docker compose restart inference` |
| `.env` | `docker compose up -d` (컨테이너 재생성) |
| `docker-compose.yml` | `docker compose up -d` |
| `requirements.txt` / `Dockerfile` | 이때만 `docker compose build <서비스>` 후 `up -d` |
| 모델 파일(`best.onnx`) | `docker compose restart inference` (models 디렉터리가 마운트돼 있음) |

`docker-compose.override.yml`이 소스를 바인드 마운트하기 때문에 가능한 구조다.
override는 `docker compose` 실행 시 자동 병합된다.

> **파일 하나가 아니라 디렉터리를 마운트해야 한다.**
> `rsync`, `sed -i`, 대부분의 에디터는 파일을 새로 쓰고 rename 하기 때문에 inode가 바뀐다.
> 단일 파일 바인드 마운트는 원래 inode에 묶여 있어서 수정해도 컨테이너에 반영되지 않는다.
> (실제로 `./receiver/app.py:/app/app.py`로 했다가 반영이 안 돼서 `./receiver:/app`으로 고쳤음)

## PC → Pi 코드 동기화

PC에서 코드를 고치고 Pi로 보낼 때:

```bash
rsync -az --delete --exclude '.git/' --exclude '.claude/' --exclude '__pycache__/' \
  ~/raspberrypi_project/ pi@raspberrypi.local:~/project/
```

`.env`와 `best.onnx`는 `.gitignore` 대상이지만 rsync는 그대로 전송하므로 별도 복사가 필요 없다.

## 자주 쓰는 명령

Pi에서 `~/project/raspberry-pi` 기준:

```bash
docker compose ps                      # 상태 확인
docker compose logs -f inference       # 로그 추적
docker compose restart inference       # inference만 재시작
docker compose down                    # 전체 중지
```

## 프로덕션 실행

바인드 마운트 없이 이미지에 구운 코드로 돌리려면 override를 제외한다:

```bash
docker compose -f docker-compose.yml up -d
```
