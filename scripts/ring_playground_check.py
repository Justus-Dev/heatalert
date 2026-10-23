"""Call the REAL Ring Partner API with a Playground token, using the same client as HeatWatch.

Get a token (valid ~30 minutes) from the Ring developer Playground, then:

    .venv/bin/python scripts/ring_playground_check.py --token <PLAYGROUND_TOKEN>
    .venv/bin/python scripts/ring_playground_check.py --token <TOKEN> --record fixtures/

`--record` saves the real responses as fixtures so the emulator can replay your real
device list:  ring-sandbox serve --port 8787 --fixtures fixtures/
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ring_sandbox import PRODUCTION_BASE_URL, RingAPIError, RingClient


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--token", required=True, help="Ring Playground / OAuth bearer token")
    ap.add_argument("--base-url", default=PRODUCTION_BASE_URL)
    ap.add_argument("--record", metavar="DIR", help="also save the responses as emulator fixtures")
    args = ap.parse_args()

    print(f"Ring Partner API: {args.base_url}\n")
    with RingClient(args.token, base_url=args.base_url) as ring:
        try:
            me = ring.me()
            print(f"GET /v1/users/me            -> account {me.id}")
        except RingAPIError as exc:
            print(f"GET /v1/users/me            -> HTTP {exc.status_code} {exc.code}")

        try:
            devices = ring.devices(include=["status", "capabilities"])
        except RingAPIError as exc:
            print(f"GET /v1/devices             -> HTTP {exc.status_code} {exc.code}")
            print("\nThe token may have expired (Playground tokens last ~30 minutes).")
            return 1
        print(f"GET /v1/devices?include=... -> {len(devices)} device(s)")
        for d in devices:
            caps = d.capabilities
            kind = "camera" if caps and caps.is_camera else "chime" if caps and caps.is_chime else "device"
            print(f"   - {d.name:<28} {kind:<7} online={d.online}  id={d.id}")

        since = datetime.now(tz=UTC) - timedelta(days=1)
        for d in devices:
            if not (d.capabilities and d.capabilities.is_camera):
                continue
            try:
                events = list(ring.events(d.id, since=since, max_pages=1))
            except RingAPIError as exc:
                print(f"GET /v1/history/devices/{d.id}/events -> HTTP {exc.status_code} {exc.code}")
                continue
            print(f"GET /v1/history/.../events  -> {len(events)} event(s) in 24 h for {d.name}")
            for ev in events[:5]:
                at = datetime.fromtimestamp(ev.attributes.start / 1000, tz=UTC).isoformat(timespec="seconds")
                print(f"   - {at}  {ev.attributes.event_type}  {getattr(ev.attributes, "sub_type", None) or ''}")

    if args.record:
        out = Path(args.record)
        print(f"\nRecording fixtures into {out}/ ...")
        cmd = [sys.executable, "-m", "ring_sandbox.cli", "record", "--token", args.token, "--out", str(out)]
        if args.base_url != PRODUCTION_BASE_URL:
            cmd += ["--base-url", args.base_url]
        subprocess.run(cmd, check=False)
        print(json.dumps(sorted(p.name for p in out.glob("*.json")), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
