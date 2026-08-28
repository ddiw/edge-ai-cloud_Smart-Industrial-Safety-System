#!/usr/bin/env python3
"""Mock Pi5 receiver for testing ESP32 -> HTTP POST connectivity.

Stands in for the not-yet-built `receiver` container so the ESP32 firmware
can be verified end-to-end before Zone 2 exists. Implements the interface
contract from README.md section 7.1 (POST /api/frames):

    Content-Type: image/jpeg
    X-Device-Id, X-Captured-At, X-Person-Count, X-Fomo-Confidence headers
    -> 202 Accepted / 400 Bad Request / 413 Payload Too Large / 503 Service Unavailable

Run on a PC connected to the SAME Wi-Fi network as the ESP32 (the network
named in WIFI_SSID inside Esp32 camera.ino), then point the firmware's
SERVER_IP at this PC's LAN IP address.

Usage:
    python mock_receiver.py                    # listen on 0.0.0.0:8080
    python mock_receiver.py --port 8080
    python mock_receiver.py --fail-first-n 2    # simulate 503 to test ESP32 retry/backoff
"""
import argparse
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "received")
MAX_BYTES = 512 * 1024  # PRD 7.1: 413 threshold

request_count = 0
fail_first_n = 0


class FrameHandler(BaseHTTPRequestHandler):
    server_version = "MockReceiver/0.1"

    def _respond(self, code, reason):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()
        print(f"  -> {code} {reason}")

    def do_GET(self):
        if self.path == "/":
            body = b"mock receiver alive\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        global request_count

        if self.path != "/api/frames":
            self._respond(404, "unknown path")
            return

        content_type = self.headers.get("Content-Type", "")
        content_length = self.headers.get("Content-Length")
        device_id = self.headers.get("X-Device-Id")
        captured_at = self.headers.get("X-Captured-At")
        person_count = self.headers.get("X-Person-Count")
        fomo_conf = self.headers.get("X-Fomo-Confidence")

        print(f"\n[{time.strftime('%H:%M:%S')}] POST /api/frames from {self.client_address[0]}")
        print(f"  Content-Type={content_type} Content-Length={content_length}")
        print(f"  X-Device-Id={device_id} X-Captured-At={captured_at} "
              f"X-Person-Count={person_count} X-Fomo-Confidence={fomo_conf}")

        if content_type != "image/jpeg" or not device_id or content_length is None:
            self._respond(400, "missing/invalid headers")
            return

        length = int(content_length)
        if length > MAX_BYTES:
            self.rfile.read(length)  # drain socket before responding
            self._respond(413, "payload too large")
            return

        body = self.rfile.read(length)

        if body[:2] != b"\xff\xd8":
            self._respond(400, "not a valid jpeg (bad magic bytes)")
            return

        request_count += 1
        if request_count <= fail_first_n:
            self._respond(503, f"simulated queue full ({request_count}/{fail_first_n})")
            return

        os.makedirs(SAVE_DIR, exist_ok=True)
        fname = f"{int(time.time() * 1000)}_{device_id}.jpg"
        fpath = os.path.join(SAVE_DIR, fname)
        with open(fpath, "wb") as f:
            f.write(body)

        print(f"  saved {len(body)} bytes -> {fpath}")
        self._respond(202, "accepted")

    def log_message(self, format, *args):
        pass  # replaced by the explicit prints above


def main():
    parser = argparse.ArgumentParser(description="Mock Pi5 receiver for ESP32 HTTP POST testing")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--fail-first-n", type=int, default=0,
                         help="respond 503 to the first N requests, to exercise ESP32 retry/backoff")
    args = parser.parse_args()

    global fail_first_n
    fail_first_n = args.fail_first_n

    os.makedirs(SAVE_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), FrameHandler)
    print(f"Mock receiver listening on 0.0.0.0:{args.port}")
    print(f"Saving accepted frames to: {SAVE_DIR}")
    if fail_first_n:
        print(f"Simulating 503 for the first {fail_first_n} request(s)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
