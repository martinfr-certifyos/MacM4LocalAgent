"""Pull an Ollama model via the HTTP /api/pull endpoint with a clean,
log-friendly progress heartbeat. Designed to be run in the background:

    nohup python3 scripts/pull_with_heartbeat.py qwen2.5-coder:14b \
        > .logs/qwen-pull.log 2>&1 &

Writes one heartbeat line per --interval seconds (default 60) plus one
"DONE" line at the end. Status events from Ollama are streamed but
collapsed: we only print when the digest or status text changes.

Exits 0 on success, non-zero on error so launchd / supervisors can
detect failure.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import urllib.request


def fmt_bytes(n: int | None) -> str:
    if not n or n <= 0:
        return "0B"
    for unit, scale in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= scale:
            return f"{n / scale:.2f}{unit}"
    return f"{n}B"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    ap.add_argument("--interval", type=float, default=60.0,
                    help="seconds between heartbeat log lines")
    ap.add_argument("--max-retries", type=int, default=20,
                    help="retries on transient connection errors before giving up")
    ap.add_argument("--retry-delay", type=float, default=5.0,
                    help="seconds to wait between retries (linear back-off up to --retry-cap)")
    ap.add_argument("--retry-cap", type=float, default=60.0,
                    help="upper bound on retry delay (seconds)")
    args = ap.parse_args()

    body = json.dumps({"name": args.model, "stream": True}).encode()

    t_global = time.time()
    last_beat = [0.0]  # mutable so inner function can update
    last_digest = [""]
    last_status = [""]
    last_completed = [0]
    last_total = [0]

    def stream_once() -> int:
        """Returns: 0=success, 2=server-side error, 4=transient (retry), 1=fatal."""
        req = urllib.request.Request(
            f"{args.host}/api/pull",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                for raw_line in resp:
                    if not raw_line.strip():
                        continue
                    try:
                        ev = json.loads(raw_line.decode())
                    except Exception:
                        continue

                    status = ev.get("status", "")
                    digest = ev.get("digest", "")
                    completed = int(ev.get("completed") or 0)
                    total = int(ev.get("total") or 0)
                    err = ev.get("error")

                    if err:
                        # Treat connection-reset / timeout as transient.
                        msg = str(err)
                        if any(k in msg.lower() for k in (
                            "connection reset", "timeout", "max retries exceeded",
                            "broken pipe", "eof",
                        )):
                            print(f"[{time.strftime('%H:%M:%S')}] transient: {msg}", flush=True)
                            return 4
                        print(f"[{time.strftime('%H:%M:%S')}] ERROR: {msg}", flush=True)
                        return 2

                    now = time.time()
                    if now - last_beat[0] >= args.interval:
                        pct = (100 * completed / total) if total else 0
                        elapsed = int(now - t_global)
                        rate = (completed / elapsed) if elapsed > 0 else 0
                        print(
                            f"[{time.strftime('%H:%M:%S')}] +{elapsed:5d}s  "
                            f"status={status[:30]:30s}  "
                            f"digest={digest[:18]:18s}  "
                            f"{fmt_bytes(completed)}/{fmt_bytes(total)} ({pct:5.1f}%)  "
                            f"avg={fmt_bytes(int(rate))}/s",
                            flush=True,
                        )
                        last_beat[0] = now

                    if (digest, status) != (last_digest[0], last_status[0]):
                        if status and status != "downloading" or digest != last_digest[0]:
                            print(
                                f"[{time.strftime('%H:%M:%S')}] phase: status={status} "
                                f"digest={digest or '-'}",
                                flush=True,
                            )
                        last_digest[0], last_status[0] = digest, status

                    last_completed[0], last_total[0] = completed, total

                    if status == "success":
                        elapsed = int(time.time() - t_global)
                        print(
                            f"[{time.strftime('%H:%M:%S')}] DONE in {elapsed}s "
                            f"({fmt_bytes(last_completed[0])})",
                            flush=True,
                        )
                        return 0
        except Exception as e:
            print(
                f"[{time.strftime('%H:%M:%S')}] stream broke: {type(e).__name__}: {e}",
                flush=True,
            )
            return 4
        return 4  # stream ended without 'success' -> retry

    print(f"[{time.strftime('%H:%M:%S')}] starting pull: {args.model}", flush=True)

    attempt = 0
    while attempt <= args.max_retries:
        rc = stream_once()
        if rc == 0:
            return 0
        if rc != 4:
            return rc
        attempt += 1
        if attempt > args.max_retries:
            print(
                f"[{time.strftime('%H:%M:%S')}] giving up after {attempt} attempts; "
                f"last progress {fmt_bytes(last_completed[0])}/{fmt_bytes(last_total[0])}",
                flush=True,
            )
            return 5
        delay = min(args.retry_cap, args.retry_delay * attempt)
        print(
            f"[{time.strftime('%H:%M:%S')}] retry {attempt}/{args.max_retries} "
            f"in {delay:.0f}s (last: {fmt_bytes(last_completed[0])}/{fmt_bytes(last_total[0])})",
            flush=True,
        )
        time.sleep(delay)
    return 5


if __name__ == "__main__":
    sys.exit(main())
