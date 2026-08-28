import hmac
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

import redis
from flask import (
    Flask, Response, jsonify, redirect, render_template, request, session, url_for
)
from supabase import create_client

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
QUEUE_KEY = "violation_jobs"
# receiver가 업로드 때마다 기록하는 보드 정보. 키 이름을 receiver와 맞춰야 한다.
DEVICES_KEY = "devices"
DEVICE_UPLOADS_KEY = "device_uploads"

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]

# 원본(블러 없는) 사진은 비공개 버킷에 있어 service_role이 있어야 열람 링크를 만들 수 있다.
# 이 키는 signed URL 발급에만 쓰고, 해당 엔드포인트는 ADMIN_KEY로 따로 막는다.
# 둘 다 없으면 원본 열람 기능만 꺼지고 나머지는 정상 동작한다.
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
BUCKET_ORIGINAL = os.environ.get("SUPABASE_BUCKET_ORIGINAL", "violations-original")
SIGNED_URL_TTL = int(os.environ.get("SIGNED_URL_TTL", 300))

DEFAULT_DEVICE_ID = os.environ.get("DEFAULT_DEVICE_ID", "esp32-01")

# 대시보드 로그인 비밀번호. 인터넷에 노출되므로 비워두면 안 된다.
# 비어 있으면 인증 없이 열리므로, 그 경우 기동을 거부한다.
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
# 세션 서명용. 지정하지 않으면 매 기동마다 새로 만들어 기존 세션이 모두 풀린다.
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", 7))
LOGIN_RATE_PER_MIN = int(os.environ.get("LOGIN_RATE_PER_MIN", 10))

if not DASHBOARD_PASSWORD:
    raise SystemExit(
        "DASHBOARD_PASSWORD가 비어 있습니다. 대시보드가 인증 없이 공개되므로 기동하지 않습니다."
    )

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.permanent_session_lifetime = timedelta(days=SESSION_DAYS)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # 터널이 HTTPS를 종단하므로 쿠키를 HTTPS 전용으로 둘 수 있다.
    # 내부망에서 http로 접속해 테스트할 때는 SESSION_COOKIE_SECURE=0 으로 끈다.
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "1") == "1",
)
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT)
supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
admin_supabase = (
    create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    if SUPABASE_SERVICE_KEY and ADMIN_KEY
    else None
)


def login_required(view):
    """로그인 안 된 접근은 화면이면 로그인 페이지로, API면 401 JSON으로 돌린다."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if session.get("auth"):
            return view(*args, **kwargs)
        if request.path.startswith(("/violations", "/devices", "/stats", "/stream")):
            return jsonify(error="unauthorized"), 401
        return redirect(url_for("login", next=request.path))
    return wrapper


@app.get("/login")
def login():
    if session.get("auth"):
        return redirect(url_for("index"))
    return render_template("login.html", error=None)


@app.post("/login")
def do_login():
    ip = request.headers.get("CF-Connecting-IP") or request.remote_addr or "unknown"
    key = f"loginrate:{ip}:{datetime.now(timezone.utc):%Y%m%d%H%M}"
    attempts = r.incr(key)
    if attempts == 1:
        r.expire(key, 120)
    if attempts > LOGIN_RATE_PER_MIN:
        return render_template("login.html", error="시도가 너무 잦습니다. 잠시 후 다시 시도하세요."), 429

    # 상수 시간 비교. 단순 == 는 응답 시간으로 비밀번호를 추측할 여지를 준다.
    if not hmac.compare_digest(request.form.get("password", ""), DASHBOARD_PASSWORD):
        return render_template("login.html", error="비밀번호가 올바르지 않습니다."), 401

    session.permanent = True
    session["auth"] = True
    nxt = request.args.get("next", "")
    return redirect(nxt if nxt.startswith("/") else url_for("index"))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def mjpeg_generator(device_id):
    """ESP32는 사람이 감지될 때만 전송하므로 프레임이 갱신되는 간격이 길다.
    같은 프레임을 반복해서 밀어내지 않도록 바뀔 때만 내보낸다."""
    last_frame = None
    while True:
        frame = r.get(f"latest_frame:{device_id}")
        if frame and frame != last_frame:
            last_frame = frame
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            )
        time.sleep(0.2)


@app.get("/")
@login_required
def index():
    return render_template("index.html", device_id=DEFAULT_DEVICE_ID)


@app.get("/stream")
@login_required
def stream():
    device_id = request.args.get("device_id", DEFAULT_DEVICE_ID)
    return Response(
        mjpeg_generator(device_id),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/violations")
@login_required
def violations():
    limit = int(request.args.get("limit", 50))
    offset = int(request.args.get("offset", 0))
    device_id = request.args.get("device_id")

    query = supabase.table("violations").select("*").order("timestamp", desc=True)
    if device_id:
        query = query.eq("device_id", device_id)
    res = query.range(offset, offset + limit - 1).execute()

    return jsonify(res.data)


@app.get("/devices")
@login_required
def devices():
    """붙어 있는 보드 목록. ESP32는 사람이 감지될 때만 전송해서 간격이 몇 분씩
    벌어질 수 있으므로, 온라인 여부는 last_seen 경과 시간으로 화면에서 판단한다."""
    seen = r.hgetall(DEVICES_KEY)
    uploads = r.hgetall(DEVICE_UPLOADS_KEY)

    result = []
    for device_id, last_seen in sorted(seen.items()):
        did = device_id.decode()
        info = {k.decode(): v.decode() for k, v in r.hgetall(f"device_info:{did}").items()}
        result.append({
            "device_id": did,
            "last_seen": last_seen.decode(),
            "uploads": int(uploads.get(device_id, 0)),
            "streaming": r.exists(f"latest_frame:{did}") == 1,
            **info,
        })
    return jsonify(result)


@app.get("/stats")
@login_required
def stats():
    """관제 화면 상단 지표. 최근 기록을 한 번만 읽어 클래스별·시간대별로 집계한다.
    Supabase 무료 플랜에서 집계 쿼리를 여러 번 날리는 것보다 이쪽이 싸다."""
    hours = int(request.args.get("hours", 24))
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    res = (
        supabase.table("violations")
        .select("class_label,device_id,timestamp,reviewed")
        .gte("timestamp", since.isoformat())
        .order("timestamp", desc=True)
        .limit(1000)
        .execute()
    )
    rows = res.data

    by_class, by_hour, by_device = {}, {}, {}
    unreviewed = 0
    for row in rows:
        by_class[row["class_label"]] = by_class.get(row["class_label"], 0) + 1
        by_device[row["device_id"]] = by_device.get(row["device_id"], 0) + 1
        hour = row["timestamp"][:13]  # YYYY-MM-DDTHH
        by_hour[hour] = by_hour.get(hour, 0) + 1
        if not row.get("reviewed"):
            unreviewed += 1

    return jsonify(
        window_hours=hours,
        total=len(rows),
        unreviewed=unreviewed,
        by_class=by_class,
        by_device=by_device,
        by_hour=[{"hour": h, "count": c} for h, c in sorted(by_hour.items())],
    )


@app.post("/violations/<violation_id>/review")
@login_required
def review(violation_id):
    """오탐 검토 표시. anon 키에 reviewed 컬럼만 UPDATE 권한을 줘서 처리한다."""
    reviewed = bool(request.json.get("reviewed", True)) if request.is_json else True
    res = (
        supabase.table("violations")
        .update({"reviewed": reviewed})
        .eq("id", violation_id)
        .execute()
    )
    if not res.data:
        return jsonify(error="not found"), 404
    return jsonify(id=violation_id, reviewed=reviewed)


@app.get("/violations/<violation_id>/original")
@login_required
def original(violation_id):
    """블러 없는 원본 열람 링크. 근로자 얼굴이 그대로 담겨 있으므로 ADMIN_KEY로 막고,
    수명이 짧은 signed URL만 발급한다."""
    if admin_supabase is None:
        return jsonify(error="원본 열람이 설정되지 않음 (SUPABASE_SERVICE_KEY/ADMIN_KEY 필요)"), 501
    if request.headers.get("X-Admin-Key") != ADMIN_KEY:
        return jsonify(error="unauthorized"), 401

    res = (
        supabase.table("violations")
        .select("image_path_original")
        .eq("id", violation_id)
        .execute()
    )
    if not res.data:
        return jsonify(error="not found"), 404

    path = res.data[0].get("image_path_original")
    if not path:
        return jsonify(error="원본 경로 없음"), 404

    signed = admin_supabase.storage.from_(BUCKET_ORIGINAL).create_signed_url(
        path, SIGNED_URL_TTL
    )
    return jsonify(url=signed["signedURL"], expires_in=SIGNED_URL_TTL)


@app.get("/healthz")
def healthz():
    return jsonify(status="ok", queue_depth=r.llen(QUEUE_KEY)), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
