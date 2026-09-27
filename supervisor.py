#!/usr/bin/env python3
"""Container supervisor: helix-server -> uvicorn.

Order: start helix-server, wait for /healthz, then exec uvicorn. The catalog
load runs in a background thread inside the API (app.py) so health checks
answer within seconds while the graph loads; /health reports "loading" until
the counts verify. Forwards SIGTERM/SIGINT to the child so Fly stops drain
cleanly.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.request

HELIX_BIN = "/usr/local/bin/helix-server"
HELIX_PORT = int(os.environ.get("HELIX_INTERNAL_PORT", "8080"))
HELIX_URL = f"http://127.0.0.1:{HELIX_PORT}"
API_PORT = int(os.environ.get("PORT", "8000"))
DATA_DIR = os.environ.get("HELIX_DATA_DIR", "/data")


def wait_for_healthz(timeout_s: int = 120) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(HELIX_URL + "/healthz", timeout=5) as r:
                if r.status == 200:
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("helix-server did not become ready in time")


def main() -> int:
    os.makedirs(DATA_DIR, exist_ok=True)
    env = dict(os.environ, HELIX_DATA_DIR=DATA_DIR)

    helix = subprocess.Popen([HELIX_BIN], env=env)
    print(f"supervisor: helix-server pid={helix.pid}", flush=True)

    def _term(signum, frame):
        print("supervisor: stopping", flush=True)
        helix.terminate()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    try:
        wait_for_healthz()
        print("supervisor: helix ready, starting api (load runs in background)",
              flush=True)
        os.execvpe("uvicorn",
                   ["uvicorn", "app:app", "--host", "0.0.0.0",
                    "--port", str(API_PORT)],
                   {**os.environ, "HELIX_URL": HELIX_URL})
    finally:
        helix.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
