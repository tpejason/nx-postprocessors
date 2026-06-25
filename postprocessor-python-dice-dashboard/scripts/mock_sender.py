#!/usr/bin/env python3
"""
Mock dice-detection sender for local development.

Simulates the NX AI Manager by sending randomised dice detection frames
to the postprocessor socket at a steady rate.

Usage:
    python3 scripts/mock_sender.py [SOCKET_PATH]

Environment:
    TRIPLE_PROB       Probability of a Triple roll per frame  (default 0.15)
    WRONG_COUNT_PROB  Probability of sending != 3 dice        (default 0.05)
    FRAME_INTERVAL    Seconds between frames                  (default 0.5)
"""
import os
import random
import struct
import sys
import time

_here    = os.path.dirname(os.path.abspath(__file__))
_sdk_root = os.path.dirname(_here)
sys.path.insert(0, os.path.join(_sdk_root, "../nxai-utilities/python-utilities"))

import msgpack                          # noqa: E402 (installed via SDK requirements)
import nxai_communication_utils        # noqa: E402


SOCK           = sys.argv[1] if len(sys.argv) > 1 else "/tmp/python-dice-dashboard-postprocessor.sock"
TRIPLE_PROB    = float(os.environ.get("TRIPLE_PROB",       "0.15"))
WRONG_PROB     = float(os.environ.get("WRONG_COUNT_PROB",  "0.05"))
INTERVAL       = float(os.environ.get("FRAME_INTERVAL",    "0.5"))
W, H           = 640, 480


def _pack_bboxes(detections):
    """Build BBoxes_xyxy wire format: {label: packed_float32_bytes}."""
    raw: dict[str, list] = {}
    for d in detections:
        k = str(d["value"])
        raw.setdefault(k, []).extend(d["bbox"])
    return {k: struct.pack(f"{len(v)}f", *v) for k, v in raw.items()}


def _make_frame(frame_id: int) -> bytes:
    """Return a msgpack-encoded inference-results message with random dice."""
    # Decide dice count
    if random.random() < WRONG_PROB:
        n = random.choice([0, 1, 2, 4])
    else:
        n = 3

    # Decide values
    if n == 3 and random.random() < TRIPLE_PROB:
        v = random.randint(1, 6)
        values = [v, v, v]
    else:
        values = [random.randint(1, 6) for _ in range(n)]

    # Spread bboxes evenly across the frame width
    detections = []
    if n > 0:
        step = W // (n + 1)
        for i, val in enumerate(values):
            x1 = float(step * (i + 1) - 30)
            y1 = float(H // 2 - 30)
            detections.append({"value": val, "bbox": [x1, y1, x1 + 60.0, y1 + 60.0]})

    msg: dict = {
        "DeviceID":   "mock-device",
        "DeviceName": "Mock Camera",
        "Timestamp":  frame_id,
        "Width":      W,
        "Height":     H,
    }
    if detections:
        msg["BBoxes_xyxy"] = _pack_bboxes(detections)
        msg["ObjectsMetaData"] = {
            str(d["value"]): {"Confidences": [round(0.7 + random.random() * 0.3, 2)]}
            for d in detections
        }

    return msgpack.packb(msg, use_bin_type=True)


def main():
    print(f"Mock sender → {SOCK}")
    print(f"Triple prob: {TRIPLE_PROB:.0%}  |  Wrong-count prob: {WRONG_PROB:.0%}  |  Interval: {INTERVAL}s")
    print("Press Ctrl+C to stop.\n")

    frame = 0
    while True:
        frame += 1
        data = _make_frame(frame)
        try:
            nxai_communication_utils.send_receive_message(SOCK, data)
            print(f"\rSent frame {frame:>6}", end="", flush=True)
        except ConnectionRefusedError:
            print(f"\r[frame {frame}] Connection refused — is the postprocessor running?", end="")
        except Exception as e:
            print(f"\r[frame {frame}] Error: {e}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
