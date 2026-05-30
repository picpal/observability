#!/usr/bin/env python3
"""
message-gate local load generator (검수 보조).

초당 [--min, --max] 건 무작위로 POST /api/v1/messages 호출 → Prometheus/Grafana 실시간 확인.

usage:
    python3 loadgen.py                  # 무한 (Ctrl+C 또는 kill 로 종료)
    python3 loadgen.py --secs 60        # 60초만
    python3 loadgen.py --min 5 --max 50 # rps 범위 조정

의존성: 표준 라이브러리만 사용 (urllib, concurrent.futures).
인증: H2 시드 계정 testuser / password123 (local profile 전용).
"""
import argparse
import base64
import json
import random
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib import error, request

URL = "http://localhost:7700/api/v1/messages"
AUTH = base64.b64encode(b"testuser:password123").decode()
HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Basic {AUTH}",
}

_lock = threading.Lock()
_ok = 0
_err = 0


def send_one() -> None:
    global _ok, _err
    tid = uuid.uuid4().hex[:32]
    body = {
        "tid": tid,
        "uid": "loadgen",
        "messageType": "SMS",
        "payload": {
            "to": f"010{random.randint(10000000, 99999999)}",
            "from": "01000000000",
            "message": f"loadgen {tid[:8]}",
        },
    }
    req = request.Request(
        URL,
        data=json.dumps(body).encode(),
        headers=HEADERS,
        method="POST",
    )
    ok = False
    try:
        with request.urlopen(req, timeout=5) as resp:
            ok = 200 <= resp.status < 300
    except (error.HTTPError, error.URLError, TimeoutError, OSError):
        ok = False
    with _lock:
        if ok:
            _ok += 1
        else:
            _err += 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--secs", type=int, default=0, help="total seconds (0 = infinite)")
    p.add_argument("--min", type=int, default=10, help="min rps")
    p.add_argument("--max", type=int, default=40, help="max rps")
    p.add_argument("--workers", type=int, default=64)
    args = p.parse_args()

    pool = ThreadPoolExecutor(max_workers=args.workers)
    deadline = time.time() + args.secs if args.secs > 0 else None

    print(
        f"loadgen start — url={URL} rps={args.min}~{args.max} "
        f"workers={args.workers} duration={'inf' if args.secs == 0 else f'{args.secs}s'}",
        flush=True,
    )

    try:
        while True:
            now = time.time()
            if deadline and now >= deadline:
                break
            rps = random.randint(args.min, args.max)
            interval = 1.0 / rps
            sec_end = now + 1.0
            while time.time() < sec_end:
                pool.submit(send_one)
                time.sleep(interval)
            with _lock:
                print(
                    f"[{time.strftime('%H:%M:%S')}] rps={rps:>2d}  "
                    f"total ok={_ok}  err={_err}",
                    flush=True,
                )
    except KeyboardInterrupt:
        print("\ninterrupted", flush=True)
    finally:
        pool.shutdown(wait=False)
        with _lock:
            print(f"final: ok={_ok}, err={_err}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
