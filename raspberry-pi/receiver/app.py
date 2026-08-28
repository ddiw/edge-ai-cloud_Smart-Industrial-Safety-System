import hmac
import os
import uuid
from datetime import datetime, timezone

import redis
from flask import Flask, jsonify, request

API_KEY = os.environ["API_KEY"]
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
QUEUE_KEY = "violation_jobs"
# 어떤 보드가 붙어 있는지 대시보드에 보여주기 위한 기록. TTL을 두지 않고 마지막
# 수신 시각만 남긴다. ESP32는 사람이 감지될 때만 보내서 간격이 몇 분씩 벌어질 수
# 있으므로, 온라인 여부 판정은 화면에서 경과 시간으로 한다.
DEVICES_KEY = "devices"
DEVICE_UPLOADS_KEY = "device_uploads"

# ESP32는 413을 받으면 JPEG 품질을 낮춰 다음 프레임부터 페이로드를 줄인다.
# VGA q12 실측 30~50KB이므로 여유는 두되 무한정 받아주지는 않는다.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 512 * 1024))
# 큐가 밀리면 503으로 밀어내 ESP32가 백오프하게 한다. 계속 받아두기만 하면
# Redis 메모리는 불어나는데 추론이 따라가지 못한다.
MAX_QUEUE_DEPTH = int(os.environ.get("MAX_QUEUE_DEPTH", 50))
# 인터넷에 노출하면 아무나 무한정 때릴 수 있다. 출발지 IP 기준으로 분당 요청을 제한한다.
# ESP32는 쿨다운 4초라 정상 동작에서는 분당 15건을 넘지 않는다.
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", 40))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT)


@app.errorhandler(413)
def payload_too_large(_):
    return jsonify(error="payload too large"), 413


def read_image():
    """ESP32는 raw image/jpeg 본문으로 보낸다. 임베디드에서 multipart를 직접
    조립하면 코드와 RAM만 늘어난다. curl로 수동 테스트할 때를 위해 multipart도 받는다."""
    if request.content_type and request.content_type.startswith("multipart/"):
        f = request.files.get("image")
        return f.read() if f else None
    return request.get_data() or None


def read_captured_at():
    """ESP32는 NTP 동기 전이면 X-Captured-At을 0으로 보낸다. 그 경우 수신 시각으로 대체."""
    try:
        epoch = int(request.headers.get("X-Captured-At", "0"))
    except ValueError:
        epoch = 0
    if epoch > 0:
        return datetime.fromtimestamp(epoch, tz=timezone.utc)
    return datetime.now(timezone.utc)


def client_ip():
    """Cloudflare Tunnel 뒤에 있으면 remote_addr은 항상 터널 프로세스가 된다.
    프록시가 붙여주는 원래 IP를 우선 본다."""
    for header in ("CF-Connecting-IP", "X-Forwarded-For"):
        value = request.headers.get(header)
        if value:
            return value.split(",")[0].strip()
    return request.remote_addr or "unknown"


def rate_limited(ip):
    if RATE_LIMIT_PER_MIN <= 0:
        return False
    key = f"ratelimit:{ip}:{datetime.now(timezone.utc):%Y%m%d%H%M}"
    count = r.incr(key)
    if count == 1:
        r.expire(key, 120)
    return count > RATE_LIMIT_PER_MIN


@app.post("/upload")
def upload():
    ip = client_ip()
    if rate_limited(ip):
        return jsonify(error="rate limit exceeded"), 429

    # 단순 != 는 앞에서부터 비교하다 다르면 멈춰서 응답 시간으로 키를 한 글자씩
    # 추측할 수 있다. 인터넷에 노출하는 이상 상수 시간 비교를 써야 한다.
    if not hmac.compare_digest(request.headers.get("X-API-Key", ""), API_KEY):
        return jsonify(error="unauthorized"), 401

    image = read_image()
    device_id = request.headers.get("X-Device-Id") or request.form.get("device_id")
    if not image or not device_id:
        return jsonify(error="image body and X-Device-Id are required"), 400

    if r.llen(QUEUE_KEY) >= MAX_QUEUE_DEPTH:
        return jsonify(error="queue full"), 503

    job_id = str(uuid.uuid4())
    r.hset(
        f"job:{job_id}",
        mapping={
            "device_id": device_id,
            "image": image,
            "captured_at": read_captured_at().isoformat(),
            # ESP32의 FOMO 1차 판정 결과. Pi의 2차 추론 결과와 비교하면 오탐 분석에 쓸 수 있다.
            "person_count": request.headers.get("X-Person-Count", ""),
            "fomo_confidence": request.headers.get("X-Fomo-Confidence", ""),
        },
    )
    r.rpush(QUEUE_KEY, job_id)

    r.hset(DEVICES_KEY, device_id, datetime.now(timezone.utc).isoformat())
    r.hincrby(DEVICE_UPLOADS_KEY, device_id, 1)
    # ESP32가 업로드에 얹어 보내는 텔레메트리. 별도 하트비트 엔드포인트를 두지 않는다.
    r.hset(
        f"device_info:{device_id}",
        mapping={
            "ip": request.headers.get("X-Device-Ip", ""),
            "rssi": request.headers.get("X-Rssi", ""),
            "uptime_s": request.headers.get("X-Uptime-S", ""),
            "free_heap": request.headers.get("X-Free-Heap", ""),
            "free_psram": request.headers.get("X-Free-Psram", ""),
            "inference_ms": request.headers.get("X-Inference-Ms", ""),
            "person_count": request.headers.get("X-Person-Count", ""),
            "fomo_confidence": request.headers.get("X-Fomo-Confidence", ""),
            "last_image_bytes": str(len(image)),
        },
    )

    return "", 202


@app.get("/healthz")
def healthz():
    return jsonify(status="ok", queue_depth=r.llen(QUEUE_KEY)), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
