#!/usr/bin/env python3
"""Send a synthetic /api/frames POST, without needing the ESP32 hardware.

Use this FIRST to confirm mock_receiver.py itself works and your PC's
firewall isn't blocking the port, before involving the ESP32 at all.
Stdlib only (urllib) -- mirrors exactly what HTTPClient.h sends on the
ESP32 side (see uploadFrame() in Esp32 camera.ino).

Usage:
    python send_test_frame.py --host 127.0.0.1 --port 8080
    python send_test_frame.py --host 172.30.1.50 --file my_photo.jpg
    python send_test_frame.py --host 172.30.1.50 --repeat 5 --delay 1
"""
import argparse
import base64
import time
import urllib.error
import urllib.request

# 64x64 solid-color JPEG, used when --file is not given.
_FALLBACK_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAYEBQYFBAYGBQYHBwYIChAKCgkJChQODwwQFxQYGBcU"
    "FhYaHSUfGhsjHBYWICwgIyYnKSopGR8tMC0oMCUoKSj/2wBDAQcHBwoIChMKChMoGhYaKCgoKCgo"
    "KCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCj/wAARCABAAEADASIA"
    "AhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQA"
    "AAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3"
    "ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWm"
    "p6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA"
    "AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSEx"
    "BhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElK"
    "U1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3"
    "uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDt6KKK"
    "+hPngooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKK"
    "KKACiiigD//Z"
)


def main():
    parser = argparse.ArgumentParser(description="Send a test frame to the mock (or real) receiver")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--path", default="/api/frames")
    parser.add_argument("--file", help="path to a .jpg to send instead of the built-in test image")
    parser.add_argument("--device-id", default="esp32-cam-01")
    parser.add_argument("--person-count", type=int, default=1)
    parser.add_argument("--confidence", type=float, default=0.87)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between repeats")
    args = parser.parse_args()

    if args.file:
        with open(args.file, "rb") as f:
            jpeg_bytes = f.read()
    else:
        jpeg_bytes = base64.b64decode(_FALLBACK_JPEG_B64)

    url = f"http://{args.host}:{args.port}{args.path}"

    for attempt in range(1, args.repeat + 1):
        headers = {
            "Content-Type": "image/jpeg",
            "X-Device-Id": args.device_id,
            "X-Captured-At": str(int(time.time())),
            "X-Person-Count": str(args.person_count),
            "X-Fomo-Confidence": f"{args.confidence:.2f}",
        }
        req = urllib.request.Request(url, data=jpeg_bytes, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                print(f"[{attempt}/{args.repeat}] {resp.status} {resp.reason} "
                      f"({len(jpeg_bytes)} bytes sent)")
        except urllib.error.HTTPError as e:
            print(f"[{attempt}/{args.repeat}] HTTP {e.code} {e.reason}")
        except urllib.error.URLError as e:
            print(f"[{attempt}/{args.repeat}] connection failed: {e.reason}")
            print("  -> check: mock_receiver.py running? correct --host/--port? firewall blocking?")

        if attempt < args.repeat:
            time.sleep(args.delay)


if __name__ == "__main__":
    main()
