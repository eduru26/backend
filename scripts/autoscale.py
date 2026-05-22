#!/usr/bin/env python3
"""Lightweight metrics-driven autoscaler for the Eduru backend.

Polls the backend `/metrics` endpoint (through the nginx gateway) and
scales the `backend` service between MIN and MAX replicas based on load.

This is deliberately a small host-run script rather than a full
orchestrator: docker-compose has no native metric-based autoscaling, and
standing up Kubernetes for an MVP is overkill. It covers both targets:

    SCALER_MODE=compose   docker compose up -d --no-recreate --scale backend=N
    SCALER_MODE=swarm     docker service scale <STACK>_backend=N

Scaling signal (any one trips a scale-up):
  * queue_depth        — arq jobs waiting in Redis (global, exact)
  * requests_last_60s  — recent request rate on the sampled replica
  * p95_latency_ms     — tail latency on the sampled replica

A cooldown prevents flapping. Scale-down is conservative: every signal
must be quiet.

Usage:
    python scripts/autoscale.py
Env:
    SCALER_MODE        compose | swarm           (default: compose)
    METRICS_URL        default: http://localhost:8080/metrics
    STACK_NAME         swarm stack name          (default: ai-mentor)
    MIN_REPLICAS       default: 2
    MAX_REPLICAS       default: 6
    POLL_SEC           default: 20
    COOLDOWN_SEC       default: 60
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

MODE = os.environ.get("SCALER_MODE", "compose")
METRICS_URL = os.environ.get("METRICS_URL", "http://localhost:8080/metrics")
STACK_NAME = os.environ.get("STACK_NAME", "ai-mentor")
MIN_REPLICAS = int(os.environ.get("MIN_REPLICAS", "2"))
MAX_REPLICAS = int(os.environ.get("MAX_REPLICAS", "6"))
POLL_SEC = int(os.environ.get("POLL_SEC", "20"))
COOLDOWN_SEC = int(os.environ.get("COOLDOWN_SEC", "60"))

# Thresholds — tuned for a small deployment; adjust to taste.
QUEUE_UP = 10            # arq jobs waiting → need more workers/backends
RATE_UP = 100            # requests in last 60s on the sampled replica
LATENCY_UP_MS = 4000     # p95 latency ceiling
RATE_DOWN = 20           # below this (and queue empty) → scale down


def fetch_metrics() -> dict:
    with urllib.request.urlopen(METRICS_URL, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def desired_replicas(current: int, m: dict) -> int:
    queue = int(m.get("queue_depth", 0))
    rate = int(m.get("requests_last_60s", 0))
    p95 = float(m.get("p95_latency_ms", 0))

    scale_up = queue > QUEUE_UP or rate > RATE_UP or p95 > LATENCY_UP_MS
    scale_down = queue == 0 and rate < RATE_DOWN and p95 < LATENCY_UP_MS / 2

    if scale_up:
        return min(current + 1, MAX_REPLICAS)
    if scale_down:
        return max(current - 1, MIN_REPLICAS)
    return current


def apply_scale(replicas: int) -> None:
    if MODE == "swarm":
        cmd = ["docker", "service", "scale", f"{STACK_NAME}_backend={replicas}"]
    else:
        cmd = [
            "docker", "compose", "up", "-d", "--no-recreate",
            "--scale", f"backend={replicas}",
        ]
    print(f"[autoscale] applying: {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True)


def main() -> int:
    current = MIN_REPLICAS
    last_action = 0.0
    print(f"[autoscale] mode={MODE} range=[{MIN_REPLICAS},{MAX_REPLICAS}] "
          f"poll={POLL_SEC}s cooldown={COOLDOWN_SEC}s", flush=True)

    while True:
        try:
            m = fetch_metrics()
        except Exception as exc:  # noqa: BLE001
            print(f"[autoscale] metrics fetch failed: {exc}", flush=True)
            time.sleep(POLL_SEC)
            continue

        target = desired_replicas(current, m)
        now = time.time()
        if target != current and (now - last_action) >= COOLDOWN_SEC:
            try:
                apply_scale(target)
                print(f"[autoscale] {current} → {target} replicas "
                      f"(queue={m.get('queue_depth')}, "
                      f"rate={m.get('requests_last_60s')}, "
                      f"p95={m.get('p95_latency_ms')}ms)", flush=True)
                current = target
                last_action = now
            except subprocess.CalledProcessError as exc:
                print(f"[autoscale] scale command failed: {exc}", flush=True)
        else:
            print(f"[autoscale] steady at {current} "
                  f"(queue={m.get('queue_depth')}, "
                  f"rate={m.get('requests_last_60s')}, "
                  f"p95={m.get('p95_latency_ms')}ms)", flush=True)

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[autoscale] stopped", flush=True)
        sys.exit(0)
