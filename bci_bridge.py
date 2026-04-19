"""
BCI bridge v4: calibration + validation + stress test + live detection.

Usage:
  python bci_bridge.py              # reuse saved calibration if it exists
  python bci_bridge.py --fresh      # force full re-calibration
  python bci_bridge.py --tune       # skip calibration, use saved config
  python bci_bridge.py --validate   # load saved, run only Phase 4+5
"""

import asyncio
import json
import os
import random
import ssl
import sys
import time
from collections import deque, Counter

import websockets
from websockets.server import serve as ws_serve

# ============================================================
# Credentials
# ============================================================
from creds import CLIENT_ID, CLIENT_SECRET

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 8765
CALIBRATION_FILE = "calibration.json"

REST_DURATION = 20
NUM_BLINK_TRIALS = 10
NUM_CLENCH_TRIALS = 10
CUE_DURATION = 1.5
INTER_TRIAL = 1.5

NUM_VALIDATION_TRIALS_PER_CLASS = 10
VALIDATION_PASS_THRESHOLD = 0.85
STRESS_TEST_DURATION = 30

DEFAULT_CONFIG = {
    "clench_labels": ["clench"],
    "clench_min_power": 0.05,
    "clench_required": 2,
    "clench_window": 4,
    "blink_required": 1,
    "blink_window": 3,
    "debounce": 0.6,
}


class Hub:
    def __init__(self):
        self.clients: set = set()

    async def register(self, ws):
        self.clients.add(ws)
        try:
            await ws.send(json.dumps({"event": "status", "state": "connected"}))
            async for _ in ws:
                pass
        finally:
            self.clients.discard(ws)

    async def broadcast(self, payload: dict):
        if not self.clients:
            return
        msg = json.dumps(payload)
        dead = []
        for ws in self.clients:
            try:
                await ws.send(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


HUB = Hub()


async def send(ws, method, params=None, req_id=[0]):
    req_id[0] += 1
    msg = {"jsonrpc": "2.0", "method": method,
           "params": params or {}, "id": req_id[0]}
    await ws.send(json.dumps(msg))
    while True:
        resp = json.loads(await ws.recv())
        if resp.get("id") == req_id[0]:
            return resp


async def collect_samples(ws, duration_sec):
    samples = []
    end_time = time.time() + duration_sec
    while time.time() < end_time:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        data = json.loads(msg)
        if "fac" in data:
            samples.append(tuple(data["fac"]))
    return samples


def count_lower_labels(samples):
    counts = Counter()
    powers = {}
    for _, _, _, l_act, l_pow in samples:
        counts[l_act] += 1
        powers.setdefault(l_act, []).append(l_pow)
    return counts, powers


def distribution_dict(samples):
    counts, powers = count_lower_labels(samples)
    total = sum(counts.values())
    if total == 0:
        return {}
    return {
        lbl: {
            "frequency": cnt / total,
            "avg_power": sum(powers[lbl]) / len(powers[lbl]),
            "max_power": max(powers[lbl]),
            "count": cnt,
        }
        for lbl, cnt in counts.items()
    }


def pick_clench_signature(rest_samples, clench_samples):
    if not rest_samples or not clench_samples:
        return {"labels": ["clench"], "min_power": 0.05}
    rest_counts, rest_powers = count_lower_labels(rest_samples)
    clench_counts, clench_powers = count_lower_labels(clench_samples)
    rest_total = sum(rest_counts.values())
    clench_total = sum(clench_counts.values())

    print(f"\n  Rest samples: {rest_total}, Clench samples: {clench_total}")
    print(f"\n  REST label distribution:")
    for lbl, cnt in rest_counts.most_common(6):
        pct = cnt / rest_total * 100
        avg_pow = sum(rest_powers[lbl]) / len(rest_powers[lbl])
        print(f"    {lbl:15s} {pct:5.1f}%  avg_pow={avg_pow:.2f}")
    print(f"\n  CLENCH label distribution:")
    for lbl, cnt in clench_counts.most_common(6):
        pct = cnt / clench_total * 100
        avg_pow = sum(clench_powers[lbl]) / len(clench_powers[lbl])
        print(f"    {lbl:15s} {pct:5.1f}%  avg_pow={avg_pow:.2f}")

    candidates = []
    for lbl in set(rest_counts) | set(clench_counts):
        if lbl == "neutral":
            continue
        rest_freq = rest_counts[lbl] / rest_total if rest_total else 0
        clench_freq = clench_counts[lbl] / clench_total if clench_total else 0
        if clench_freq > 2 * rest_freq and clench_freq > 0.15:
            candidates.append((lbl, clench_freq, rest_freq))
    candidates.sort(key=lambda x: x[1] - x[2], reverse=True)

    if not candidates:
        for lbl, cnt in clench_counts.most_common(5):
            if lbl in ("neutral", "smirkLeft"):
                continue
            if cnt / clench_total > 0.10:
                candidates.append((lbl, cnt / clench_total, 0))
                break

    if not candidates:
        print("\n  WARNING: No clench signature found.")
        return {"labels": ["clench"], "min_power": 0.0}

    labels = [lbl for lbl, _, _ in candidates[:2]]
    min_pow = min(sum(clench_powers[lbl]) / len(clench_powers[lbl]) * 0.5
                  for lbl in labels)
    print(f"\n  Clench signature: {labels}, min_power={min_pow:.2f}")
    return {"labels": labels, "min_power": max(0.0, min_pow)}


def save_calibration(config, rest_samples=None, clench_samples=None,
                     validation=None, path=CALIBRATION_FILE):
    existing = load_calibration() or {}
    data = {**existing, **config, "calibrated_at": int(time.time())}
    if rest_samples is not None:
        data["rest_distribution"] = distribution_dict(rest_samples)
    if clench_samples is not None:
        data["clench_distribution"] = distribution_dict(clench_samples)
    if validation is not None:
        data["validation"] = validation
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\n  Saved calibration to {path}")


def load_calibration(path=CALIBRATION_FILE):
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            data.setdefault(k, v)
        return data
    except Exception as e:
        print(f"  Failed to load {path}: {e}")
        return None


def countdown(seconds, msg):
    for i in range(seconds, 0, -1):
        sys.stdout.write(f"\r{msg} in {i}...  ")
        sys.stdout.flush()
        time.sleep(1)
    print("\r" + " " * 60 + "\r", end="")


def big_print(text):
    print()
    print("=" * 60)
    print(f"  {text}")
    print("=" * 60)


class DetectionBuffer:
    def __init__(self, window, debounce):
        self.samples = deque(maxlen=window)
        self.debounce = debounce
        self.last_fired = 0.0

    def check(self, is_active, now, required=None):
        self.samples.append(is_active)
        if required is None:
            required = len(self.samples)
        if len(self.samples) < self.samples.maxlen:
            return False
        if sum(self.samples) < required:
            return False
        if now - self.last_fired < self.debounce:
            return False
        self.last_fired = now
        self.samples.clear()
        return True


def apply_detector(samples, config):
    blink_buf = DetectionBuffer(config["blink_window"], config["debounce"])
    clench_buf = DetectionBuffer(config["clench_window"], config["debounce"])
    allowed = set(config["clench_labels"])
    min_pow = config["clench_min_power"]
    fires = []
    now = 0.0
    for eye_act, _, _, l_act, l_pow in samples:
        now += 1.0 / 32
        if blink_buf.check(eye_act == "blink", now,
                           required=config["blink_required"]):
            fires.append((now, "blink"))
        clench_active = (l_act in allowed and l_pow >= min_pow)
        if clench_buf.check(clench_active, now,
                            required=config["clench_required"]):
            fires.append((now, "clench"))
    return fires


async def run_validation(ws, config):
    big_print("PHASE 4: VALIDATION")
    print(f"\n{NUM_VALIDATION_TRIALS_PER_CLASS} blinks + "
          f"{NUM_VALIDATION_TRIALS_PER_CLASS} clenches, randomized.")
    input("Press Enter when ready...")

    trials = (["blink"] * NUM_VALIDATION_TRIALS_PER_CLASS +
              ["clench"] * NUM_VALIDATION_TRIALS_PER_CLASS)
    random.shuffle(trials)

    results = {
        "blink": {"cued": 0, "correct": 0, "missed": 0, "wrong_class": 0},
        "clench": {"cued": 0, "correct": 0, "missed": 0, "wrong_class": 0},
        "false_positive_rest": 0,
    }

    for i, cue in enumerate(trials):
        print(f"\nTrial {i+1}/{len(trials)}: ", end="", flush=True)
        prep_samples = await collect_samples(ws, 1.5)
        prep_fires = apply_detector(prep_samples, config)
        results["false_positive_rest"] += len(prep_fires)

        print(f">>> {cue.upper()} NOW <<<", flush=True)
        action_samples = await collect_samples(ws, CUE_DURATION)
        action_fires = apply_detector(action_samples, config)
        fire_kinds = [k for _, k in action_fires]

        results[cue]["cued"] += 1
        if cue in fire_kinds:
            results[cue]["correct"] += 1
            print(f"  [OK]")
        elif fire_kinds:
            other = fire_kinds[0]
            results[cue]["wrong_class"] += 1
            print(f"  [WRONG: fired {other}]")
        else:
            results[cue]["missed"] += 1
            print(f"  [MISSED]")

        await asyncio.sleep(0.5)

    big_print("VALIDATION RESULTS")
    total = len(trials)
    correct = results["blink"]["correct"] + results["clench"]["correct"]
    acc = correct / total if total else 0

    for cls in ["blink", "clench"]:
        r = results[cls]
        cued = r["cued"]
        cls_acc = r["correct"] / cued if cued else 0
        print(f"  {cls.upper():8s}: {r['correct']}/{cued} ({cls_acc:.0%})  "
              f"missed={r['missed']}, wrong={r['wrong_class']}")
    print(f"\n  Overall: {correct}/{total} ({acc:.0%})")
    print(f"  Rest false positives: {results['false_positive_rest']}")

    results["overall_accuracy"] = acc
    results["passed"] = acc >= VALIDATION_PASS_THRESHOLD
    print(f"\n  {'PASSED' if results['passed'] else 'FAILED'} "
          f"(threshold {VALIDATION_PASS_THRESHOLD:.0%})")
    return results


async def run_stress_test(ws, config):
    big_print("PHASE 5: STRESS TEST")
    print(f"\nFor {STRESS_TEST_DURATION}s: talk, look around, small movements.")
    print("Do NOT intentionally blink hard or clench.")
    input("Press Enter when ready...")
    countdown(3, "Starting")

    samples = await collect_samples(ws, STRESS_TEST_DURATION)
    fires = apply_detector(samples, config)
    blink_fps = sum(1 for _, k in fires if k == "blink")
    clench_fps = sum(1 for _, k in fires if k == "clench")

    big_print("STRESS TEST RESULTS")
    print(f"  False blinks: {blink_fps}  "
          f"({blink_fps / STRESS_TEST_DURATION * 60:.1f}/min)")
    print(f"  False clenches: {clench_fps}  "
          f"({clench_fps / STRESS_TEST_DURATION * 60:.1f}/min)")

    return {
        "duration_sec": STRESS_TEST_DURATION,
        "false_blinks": blink_fps,
        "false_clenches": clench_fps,
    }


async def calibrate(ws):
    big_print("CALIBRATION")
    print(f"\n5 phases. Takes ~3 minutes.")
    input("\nPress Enter when ready...")

    big_print("PHASE 1: REST")
    print("Sit still. Neutral face.")
    countdown(3, "Starting")
    rest_samples = await collect_samples(ws, REST_DURATION)
    print(f"  Collected {len(rest_samples)} samples.")

    big_print("PHASE 2: BLINKS")
    print(f"Blink {NUM_BLINK_TRIALS} times on cue. Hard, deliberate.")
    input("Press Enter...")
    blink_samples = []
    for i in range(NUM_BLINK_TRIALS):
        countdown(2, f"Blink #{i+1}/{NUM_BLINK_TRIALS}")
        print(">>> BLINK NOW <<<")
        blink_samples.extend(await collect_samples(ws, CUE_DURATION))
        await asyncio.sleep(INTER_TRIAL)

    big_print("PHASE 3: CLENCHES")
    print(f"Clench {NUM_CLENCH_TRIALS} times on cue. Hard, hold 1+ second.")
    input("Press Enter...")
    clench_samples = []
    for i in range(NUM_CLENCH_TRIALS):
        countdown(2, f"Clench #{i+1}/{NUM_CLENCH_TRIALS}")
        print(">>> CLENCH HARD, HOLD <<<")
        clench_samples.extend(await collect_samples(ws, CUE_DURATION))
        await asyncio.sleep(INTER_TRIAL)

    big_print("ANALYZING")
    sig = pick_clench_signature(rest_samples, clench_samples)
    config = {**DEFAULT_CONFIG,
              "clench_labels": sig["labels"],
              "clench_min_power": sig["min_power"]}

    validation = await run_validation(ws, config)
    stress = await run_stress_test(ws, config)
    validation["stress_test"] = stress
    save_calibration(config, rest_samples, clench_samples, validation)

    if not validation["passed"]:
        print("\n  Validation failed.")
        ans = input("  [r]e-calibrate / [c]ontinue anyway: ").strip().lower()
        if ans == "r":
            return await calibrate(ws)

    return config


async def detect_loop(ws, config):
    big_print("LIVE DETECTION + BRIDGE ACTIVE")
    print(f"\nClench labels: {config['clench_labels']}")
    print(f"Clench min_power: {config['clench_min_power']:.2f}")
    print(f"Bridge: ws://{BRIDGE_HOST}:{BRIDGE_PORT}")
    print("\nBlink/clench to drive UI. Ctrl+C to quit.\n")

    blink_buf = DetectionBuffer(config["blink_window"], config["debounce"])
    clench_buf = DetectionBuffer(config["clench_window"], config["debounce"])
    await HUB.broadcast({"event": "status", "state": "ready"})

    allowed = set(config["clench_labels"])
    min_power = config["clench_min_power"]
    blink_req = config["blink_required"]
    clench_req = config["clench_required"]

    async for msg in ws:
        data = json.loads(msg)
        if "fac" not in data:
            continue
        eye_act, _, _, l_act, l_pow = data["fac"]
        now = data["time"]

        if blink_buf.check(eye_act == "blink", now, required=blink_req):
            print("BLINK")
            await HUB.broadcast({"event": "blink", "t": now})

        clench_active = (l_act in allowed and l_pow >= min_power)
        if clench_buf.check(clench_active, now, required=clench_req):
            print(f"CLENCH ({l_act}@{l_pow:.2f})")
            await HUB.broadcast({"event": "clench", "t": now})


async def cortex_pipeline(mode):
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    async with websockets.connect("wss://localhost:6868", ssl=ssl_ctx) as ws:
        r = await send(ws, "requestAccess",
                       {"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET})
        if r.get("result", {}).get("accessGranted") is False:
            print("Approve app in EMOTIV Launcher, then rerun.")
            return

        r = await send(ws, "authorize",
                       {"clientId": CLIENT_ID,
                        "clientSecret": CLIENT_SECRET, "debit": 1})
        if "error" in r:
            print(f"Auth error: {r['error']}")
            return
        token = r["result"]["cortexToken"]

        r = await send(ws, "queryHeadsets")
        if not r["result"]:
            print("No headset.")
            return

        r = await send(ws, "createSession",
                       {"cortexToken": token,
                        "headset": r["result"][0]["id"], "status": "active"})
        session_id = r["result"]["id"]
        await send(ws, "subscribe",
                   {"cortexToken": token, "session": session_id,
                    "streams": ["fac"]})

        if mode == "fresh":
            config = await calibrate(ws)
        elif mode == "tune":
            config = load_calibration()
            if config is None:
                print("No calibration.json.")
                return
        elif mode == "validate":
            config = load_calibration()
            if config is None:
                print("No calibration.json.")
                return
            v = await run_validation(ws, config)
            s = await run_stress_test(ws, config)
            v["stress_test"] = s
            save_calibration(config, validation=v)
            return
        else:
            saved = load_calibration()
            if saved is None:
                config = await calibrate(ws)
            else:
                print(f"\nFound calibration from "
                      f"{time.ctime(saved.get('calibrated_at', 0))}")
                v = saved.get("validation", {})
                if v:
                    print(f"  Last validation: {v.get('overall_accuracy', 0):.0%}")
                ans = input("\nReuse? [Y/n]: ").strip().lower()
                if ans in ("", "y", "yes"):
                    config = saved
                else:
                    config = await calibrate(ws)

        await detect_loop(ws, config)


async def main():
    mode = "default"
    if "--fresh" in sys.argv:
        mode = "fresh"
    elif "--tune" in sys.argv:
        mode = "tune"
    elif "--validate" in sys.argv:
        mode = "validate"

    print(f"Starting bridge on ws://{BRIDGE_HOST}:{BRIDGE_PORT}")
    server = await ws_serve(HUB.register, BRIDGE_HOST, BRIDGE_PORT)
    try:
        await cortex_pipeline(mode)
    finally:
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n--- Stopped ---")