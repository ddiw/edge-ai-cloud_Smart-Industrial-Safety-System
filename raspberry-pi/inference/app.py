import ast
import os
import time
import uuid
from datetime import datetime, timezone

import cv2
import numpy as np
import onnxruntime as ort
import redis
from supabase import create_client

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
QUEUE_KEY = "violation_jobs"
DEAD_LETTER_KEY = "violation_jobs:failed"
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", 3))

MODEL_PATH = os.environ.get("MODEL_PATH", "/models/best.onnx")
CONF_THRESHOLD = float(os.environ.get("CONF_THRESHOLD", 0.5))
IOU_THRESHOLD = float(os.environ.get("IOU_THRESHOLD", 0.45))
INPUT_SIZE = int(os.environ.get("INPUT_SIZE", 640))

# 사람이 없는 프레임의 위반은 오탐으로 보고 버린다. 끄려면 REQUIRE_PERSON=0.
REQUIRE_PERSON = os.environ.get("REQUIRE_PERSON", "1") == "1"
PERSON_OVERLAP_MIN = float(os.environ.get("PERSON_OVERLAP_MIN", 0.5))

# ESP32 쿨다운이 짧아(4초) 같은 사람이 서 있기만 해도 동일한 위반이 계속 올라온다.
# 감지 반응성은 그대로 두고, 같은 보드에서 같은 위반 조합이 이 시간 안에 다시 오면
# 저장(Storage 업로드 + DB insert)만 건너뛴다. 스트리밍 프레임은 계속 갱신된다.
DEDUPE_WINDOW_S = int(os.environ.get("DEDUPE_WINDOW_S", 60))

FACE_MODEL_PATH = os.environ.get("FACE_MODEL_PATH", "/models/face_detection_yunet_2023mar.onnx")
FACE_CONF_THRESHOLD = float(os.environ.get("FACE_CONF_THRESHOLD", 0.6))
# 얼굴 검출이 실패해도 머리 영역이 가려지도록, PPE 모델이 뱉는 머리/얼굴 부위 클래스도
# 함께 블러 대상으로 삼는다.
HEAD_CLASSES = {"hardhat", "no-hardhat", "mask", "no-mask"}

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
# 블러 버전(공개)과 원본(비공개)은 반드시 다른 버킷에 저장한다. 원본에는 얼굴이 그대로 남으므로
# 공개 버킷에 섞이면 URL만 알면 누구나 접근 가능해진다.
BUCKET_BLURRED = os.environ.get("SUPABASE_BUCKET", "violations")
BUCKET_ORIGINAL = os.environ.get("SUPABASE_BUCKET_ORIGINAL", "violations-original")

STREAM_WIDTH = int(os.environ.get("STREAM_WIDTH", 640))
STREAM_HEIGHT = int(os.environ.get("STREAM_HEIGHT", 480))
STREAM_JPEG_QUALITY = int(os.environ.get("STREAM_JPEG_QUALITY", 80))
LATEST_FRAME_TTL = int(os.environ.get("LATEST_FRAME_TTL", 30))

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT)
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

session = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])
input_name = session.get_inputs()[0].name

face_detector = cv2.FaceDetectorYN.create(
    FACE_MODEL_PATH, "", (320, 320), FACE_CONF_THRESHOLD
)


def load_class_names():
    names_raw = session.get_modelmeta().custom_metadata_map.get("names")
    if not names_raw:
        return None
    try:
        names_dict = ast.literal_eval(names_raw)
        return [names_dict[i] for i in sorted(names_dict)]
    except (ValueError, SyntaxError, KeyError):
        return None


CLASS_NAMES = load_class_names()


def preprocess(frame):
    """YOLOv8은 letterbox(종횡비 유지 + 패딩)로 학습됐으므로 동일하게 맞춘다.
    그냥 resize하면 종횡비가 찌그러져 정확도가 떨어진다."""
    h, w = frame.shape[:2]
    scale = min(INPUT_SIZE / w, INPUT_SIZE / h)
    new_w, new_h = int(round(w * scale)), int(round(h * scale))
    pad_x, pad_y = (INPUT_SIZE - new_w) // 2, (INPUT_SIZE - new_h) // 2

    canvas = np.full((INPUT_SIZE, INPUT_SIZE, 3), 114, dtype=np.uint8)
    canvas[pad_y : pad_y + new_h, pad_x : pad_x + new_w] = cv2.resize(frame, (new_w, new_h))

    blob = canvas[:, :, ::-1].astype(np.float32) / 255.0
    blob = blob.transpose(2, 0, 1)[None, ...]
    return blob, scale, pad_x, pad_y


def postprocess(output, scale, pad_x, pad_y, frame_shape):
    # ultralytics YOLOv8 ONNX(NMS 미포함) 출력 (1, 4+num_classes, num_boxes)
    preds = output[0][0].T
    scores_all = preds[:, 4:]
    class_ids = scores_all.argmax(axis=1)
    confidences = scores_all.max(axis=1)

    keep = confidences >= CONF_THRESHOLD
    preds, class_ids, confidences = preds[keep], class_ids[keep], confidences[keep]

    h, w = frame_shape[:2]
    boxes = []
    for pred, class_id, conf in zip(preds, class_ids, confidences):
        cx, cy, bw, bh = pred[:4]
        x1 = (cx - bw / 2 - pad_x) / scale
        y1 = (cy - bh / 2 - pad_y) / scale
        x2 = (cx + bw / 2 - pad_x) / scale
        y2 = (cy + bh / 2 - pad_y) / scale
        boxes.append(
            {
                "class_name": (
                    CLASS_NAMES[class_id]
                    if CLASS_NAMES and class_id < len(CLASS_NAMES)
                    else str(class_id)
                ),
                "confidence": float(conf),
                "bbox": [
                    max(0.0, float(x1)),
                    max(0.0, float(y1)),
                    min(float(w), float(x2)),
                    min(float(h), float(y2)),
                ],
            }
        )
    return non_max_suppression(boxes)


def non_max_suppression(boxes):
    """클래스별로 따로 NMS를 건다. 클래스를 섞어서 억제하면 같은 사람 위에 겹치는
    Person / NO-Safety Vest 처럼 서로 다른 의미의 박스가 지워진다."""
    kept = []
    for class_name in {b["class_name"] for b in boxes}:
        candidates = sorted(
            (b for b in boxes if b["class_name"] == class_name),
            key=lambda b: b["confidence"],
            reverse=True,
        )
        while candidates:
            best = candidates.pop(0)
            kept.append(best)
            candidates = [b for b in candidates if iou(best["bbox"], b["bbox"]) < IOU_THRESHOLD]
    return kept


def iou(box_a, box_b):
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0


def is_violation(class_name):
    return class_name.lower().startswith(("no-", "no_"))


def is_duplicate(device_id, violations):
    """같은 보드에서 같은 위반 조합이 DEDUPE_WINDOW_S 안에 또 오면 저장을 생략한다.

    ESP32 쿨다운이 4초라 사람이 그대로 서 있기만 해도 동일 위반이 계속 올라온다.
    감지 반응성은 유지해야 하므로 쿨다운을 늘리는 대신 여기서 걸러낸다.
    스트리밍 프레임 갱신은 이 함수 호출 전에 이미 끝나 있어 영향받지 않는다."""
    if DEDUPE_WINDOW_S <= 0:
        return False
    signature = ",".join(sorted(v["class_name"] for v in violations))
    # SET NX는 키가 없을 때만 성공한다. 성공 = 이 창에서 처음 본 조합.
    first_time = r.set(f"dedupe:{device_id}:{signature}", "1",
                       ex=DEDUPE_WINDOW_S, nx=True)
    return not first_time


def containment(inner, outer):
    """inner 박스가 outer 안에 얼마나 들어가 있는지(0~1).
    IoU는 크기 차가 크면 낮게 나오므로 포함 비율을 쓴다."""
    ix1, iy1 = max(inner[0], outer[0]), max(inner[1], outer[1])
    ix2, iy2 = min(inner[2], outer[2]), min(inner[3], outer[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    area = max(0, inner[2] - inner[0]) * max(0, inner[3] - inner[1])
    return inter / area if area > 0 else 0


def filter_violations(boxes):
    """사람이 없는 프레임에서 나온 위반은 버린다.

    실측에서 ESP32가 천장·조명을 찍은 프레임에 NO-Hardhat이 0.3~0.6으로 잡혀
    전체 위반의 60%가 오탐이었다. "사람이 안전모를 안 썼다"는 판정은 그 자리에
    사람이 있어야 성립하므로, 위반 박스가 Person 박스와 충분히 겹칠 때만 인정한다."""
    violations = [b for b in boxes if is_violation(b["class_name"])]
    if not REQUIRE_PERSON:
        return violations

    persons = [b["bbox"] for b in boxes if b["class_name"].lower() == "person"]
    if not persons:
        return []
    return [
        v for v in violations
        if any(containment(v["bbox"], p) >= PERSON_OVERLAP_MIN for p in persons)
    ]


def detect_faces(frame):
    h, w = frame.shape[:2]
    face_detector.setInputSize((w, h))
    _, faces = face_detector.detect(frame)
    if faces is None:
        return []
    return [tuple(f[:4]) for f in faces]


def blur_regions(frame, boxes):
    """얼굴 검출(YuNet) 결과와 PPE 모델의 머리/얼굴 부위 박스를 합쳐서 가린다.

    이전에 쓰던 Haar Cascade는 안전모를 쓰거나 비스듬한 얼굴을 거의 못 잡았다.
    실측에서 실제 얼굴은 놓치고 엉뚱한 옷 부분을 얼굴로 오탐해, 공개 버킷에
    얼굴이 그대로 올라갔다. YuNet만으로도 크게 나아지지만, 뒤통수만 보이거나
    가려진 경우를 대비해 Hardhat/Mask 계열 박스도 함께 블러 대상으로 둔다."""
    regions = list(detect_faces(frame))
    for b in boxes:
        if b["class_name"].lower() in HEAD_CLASSES:
            x1, y1, x2, y2 = b["bbox"]
            regions.append((x1, y1, x2 - x1, y2 - y1))

    h, w = frame.shape[:2]
    out = frame.copy()
    for (x, y, bw, bh) in regions:
        # 검출 박스가 얼굴을 빠듯하게 잡는 경우가 있어 약간 넓혀서 가린다.
        pad_x, pad_y = bw * 0.15, bh * 0.15
        x1 = max(0, int(x - pad_x))
        y1 = max(0, int(y - pad_y))
        x2 = min(w, int(x + bw + pad_x))
        y2 = min(h, int(y + bh + pad_y))
        if x2 <= x1 or y2 <= y1:
            continue
        roi = out[y1:y2, x1:x2]
        # 커널을 영역 크기에 비례시켜야 큰 얼굴도 확실히 뭉갠다. 고정 커널(51)은
        # 고해상도 이미지에서 얼굴이 크면 윤곽이 남는다.
        k = max(15, (min(x2 - x1, y2 - y1) // 4) | 1)
        out[y1:y2, x1:x2] = cv2.GaussianBlur(roi, (k, k), 0)
    return out


def draw_boxes(frame, boxes):
    out = frame.copy()
    for b in boxes:
        x1, y1, x2, y2 = map(int, b["bbox"])
        color = (0, 0, 255) if is_violation(b["class_name"]) else (0, 200, 0)
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        label = f'{b["class_name"]} {b["confidence"]:.2f}'
        cv2.putText(out, label, (x1, max(y1 - 8, 0)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
    return out


def upload_image(bucket, path, image_bgr):
    _, buf = cv2.imencode(".jpg", image_bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
    supabase.storage.from_(bucket).upload(path, buf.tobytes(), {"content-type": "image/jpeg"})
    return supabase.storage.from_(bucket).get_public_url(path)


def cache_latest_frame(device_id, frame):
    resized = cv2.resize(frame, (STREAM_WIDTH, STREAM_HEIGHT))
    _, buf = cv2.imencode(".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, STREAM_JPEG_QUALITY])
    r.set(f"latest_frame:{device_id}", buf.tobytes(), ex=LATEST_FRAME_TTL)


def save_violations(device_id, ts, blurred_url, original_path, violations):
    for v in violations:
        supabase.table("violations").insert(
            {
                "device_id": device_id,
                "timestamp": ts.isoformat(),
                "image_url": blurred_url,
                "image_path_original": original_path,
                "class_label": v["class_name"],
                "confidence": v["confidence"],
                "bbox": v["bbox"],
                "reviewed": False,
            }
        ).execute()


def handle_job(job_id):
    key = f"job:{job_id}"
    data = r.hgetall(key)
    if not data:
        return

    device_id = data[b"device_id"].decode()
    frame = cv2.imdecode(np.frombuffer(data[b"image"], dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        print(f"[inference] job {job_id}: 이미지 디코딩 실패, 폐기")
        r.delete(key)
        return

    blob, scale, pad_x, pad_y = preprocess(frame)
    output = session.run(None, {input_name: blob})
    boxes = postprocess(output, scale, pad_x, pad_y, frame.shape)
    violations = filter_violations(boxes)

    # 스트리밍/저장용 이미지 모두 원본에 블러를 먼저 입힌 뒤 박스를 그린다.
    # 순서가 반대면 블러가 박스 선 위에 덮인다.
    blurred_annotated = draw_boxes(blur_regions(frame, boxes), boxes)
    cache_latest_frame(device_id, blurred_annotated)

    if not violations:
        r.delete(key)
        return

    if is_duplicate(device_id, violations):
        r.delete(key)
        print(f"[inference] job {job_id}: 중복으로 저장 생략 ({device_id})")
        return

    # ESP32가 NTP로 맞춘 촬영 시각을 우선 쓴다. receiver가 이미 미동기(0) 케이스를
    # 수신 시각으로 대체해두므로 여기서는 파싱 실패만 방어하면 된다.
    ts = datetime.now(timezone.utc)
    raw_ts = data.get(b"captured_at")
    if raw_ts:
        try:
            ts = datetime.fromisoformat(raw_ts.decode())
        except ValueError:
            pass
    prefix = f"{device_id}/{ts.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4()}"
    original_path = f"{prefix}_original.jpg"

    blurred_url = upload_image(BUCKET_BLURRED, f"{prefix}_blurred.jpg", blurred_annotated)
    upload_image(BUCKET_ORIGINAL, original_path, frame)
    save_violations(device_id, ts, blurred_url, original_path, violations)

    r.delete(key)
    print(f"[inference] job {job_id}: 위반 {len(violations)}건 저장 ({device_id})")


def process_with_retry(job_id):
    """Supabase 업로드는 Pi 와이파이가 끊기면 실패한다. 성공할 때까지 job 데이터를
    Redis에 남겨두고 재시도하며, 한도를 넘기면 dead-letter로 옮겨 유실을 막는다."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            handle_job(job_id)
            return
        except Exception as e:
            print(f"[inference] job {job_id} 실패 ({attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)

    r.rpush(DEAD_LETTER_KEY, job_id)
    print(f"[inference] job {job_id}: 재시도 한도 초과, dead-letter로 이동")


def main():
    print(f"[inference] model={MODEL_PATH} classes={CLASS_NAMES}")
    while True:
        item = r.blpop(QUEUE_KEY, timeout=5)
        if item is None:
            continue
        process_with_retry(item[1].decode())


if __name__ == "__main__":
    main()
