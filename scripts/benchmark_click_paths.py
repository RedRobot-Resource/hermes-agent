"""
Benchmark: Current main (3 separate WS connections) vs optimized (1 connection).

Compares the two CDP coordinate click implementations against a real
Lightpanda WebSocket at ws://127.0.0.1:63372/.

  - Baseline (current main style): 3 separate _cdp_call() invocations, each
    opening a fresh WS connection (Target.getTargets, mousePressed, mouseReleased)
  - Optimized (this PR): single WS connection with all 4 messages pipelined
    (getTargets + attachToTarget + mousePressed+mouseReleased in one burst)

Also measures the agent-browser HTTP IPC round-trip as a reference point
for how fast the existing ref-based click path is.

Usage:
    python scripts/benchmark_click_paths.py
    python scripts/benchmark_click_paths.py --iterations 300 --warmup 20
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from statistics import mean, median, stdev
from typing import List, Dict, Optional, Tuple

sys.path.insert(0, "/private/tmp/hermes-coord-click")

LIGHTPANDA_WS = "ws://127.0.0.1:63372/"
AGENT_BROWSER_PORT = 63371


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stats(times_s: List[float]) -> Dict:
    ms = [t * 1000 for t in times_s]
    return {
        "mean_ms":   mean(ms),
        "median_ms": median(ms),
        "min_ms":    min(ms),
        "max_ms":    max(ms),
        "stdev_ms":  stdev(ms) if len(ms) > 1 else 0.0,
        "p95_ms":    sorted(ms)[int(len(ms) * 0.95)],
    }


def _bench(fn, warmup: int, n: int) -> Tuple[List[float], int]:
    for _ in range(warmup):
        fn()
    times, errors = [], 0
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            result = fn()
            elapsed = time.perf_counter() - t0
            if isinstance(result, str):
                d = json.loads(result)
                if not d.get("success"):
                    errors += 1
        except Exception:
            elapsed = time.perf_counter() - t0
            errors += 1
        times.append(elapsed)
    return times, errors


def _row(label: str, stats: Dict, col_w: int = 9) -> None:
    print(
        f"  {label:<46}  "
        f"{stats['mean_ms']:>{col_w}.2f}  "
        f"{stats['median_ms']:>{col_w}.2f}  "
        f"{stats['min_ms']:>{col_w}.2f}  "
        f"{stats['p95_ms']:>{col_w}.2f}  "
        f"{stats['max_ms']:>{col_w}.2f}  ms"
    )


# ---------------------------------------------------------------------------
# The "current main" approach — 3 separate _cdp_call() connections
# ---------------------------------------------------------------------------

def _baseline_cdp_click(endpoint: str, x: int, y: int, button: str = "left") -> str:
    """Replicate the previous 3-connection approach from the original PR."""
    from tools.browser_cdp_tool import _cdp_call, _run_async

    try:
        targets_result = _run_async(_cdp_call(endpoint, "Target.getTargets", {}, None, 10.0))
        page_target = None
        for t in targets_result.get("targetInfos", []):
            if t.get("type") == "page" and t.get("attached", True):
                page_target = t["targetId"]
                break
    except Exception:
        page_target = None

    mouse_params = {"type": "", "x": x, "y": y, "button": button, "clickCount": 1}
    try:
        _run_async(_cdp_call(endpoint, "Input.dispatchMouseEvent",
                             {**mouse_params, "type": "mousePressed"}, page_target, 10.0))
        _run_async(_cdp_call(endpoint, "Input.dispatchMouseEvent",
                             {**mouse_params, "type": "mouseReleased"}, page_target, 10.0))
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})
    return json.dumps({"success": True, "clicked_at": {"x": x, "y": y}, "method": "baseline"})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_benchmark(iterations: int = 300, warmup: int = 20) -> None:
    print(f"\n{'=' * 78}")
    print(f"  browser_click Coordinate Click: Current Main vs Optimized (1-conn)")
    print(f"  Real Lightpanda WS: {LIGHTPANDA_WS}")
    print(f"{'=' * 78}")
    print(f"  Iterations: {iterations}  |  Warmup: {warmup}")

    # pre-flight
    try:
        with urllib.request.urlopen("http://127.0.0.1:63372/json/version", timeout=2) as r:
            info = json.loads(r.read())
            assert "webSocketDebuggerUrl" in info
        print(f"  ✓ Lightpanda CDP: {info.get('webSocketDebuggerUrl')}")
    except Exception as e:
        print(f"  ✗ Lightpanda not reachable: {e}")
        return

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{AGENT_BROWSER_PORT}/api/sessions", timeout=2) as r:
            sessions = json.loads(r.read())
        print(f"  ✓ agent-browser: {len(sessions)} session(s)")
        ab_ok = True
    except Exception:
        print(f"  ⚠  agent-browser not reachable — ref-click IPC baseline skipped")
        ab_ok = False

    import importlib
    import tools.browser_tool as bt
    import tools.browser_cdp_tool as cdp_mod
    importlib.reload(cdp_mod)
    importlib.reload(bt)
    bt._is_camofox_mode = lambda: False
    _orig_resolve = cdp_mod._resolve_cdp_endpoint

    # -----------------------------------------------------------------------
    # 1. Baseline: current-main 3-connection approach
    # -----------------------------------------------------------------------
    print(f"\n  [1/4] Baseline (current main — 3 separate WS connections per click)")
    print(f"        Warmup {warmup}, then {iterations} iterations...")

    base_times, base_err = _bench(
        lambda: _baseline_cdp_click(LIGHTPANDA_WS, 150, 200),
        warmup, iterations,
    )
    base_stats = _stats(base_times)
    print(f"        Done — {base_err} errors, mean={base_stats['mean_ms']:.2f}ms")

    # -----------------------------------------------------------------------
    # 2. Optimized: single-connection — cold cache (session resolve included)
    # -----------------------------------------------------------------------
    print(f"\n  [2/4] Optimized — cold cache (1 WS conn, includes getTargets+attachToTarget)")
    print(f"        {iterations} iterations, cache cleared before each...")

    def _cold_click():
        bt._CDP_SESSION_CACHE.clear()
        return bt.browser_click(x=150.0, y=200.0, task_id="bench")

    cdp_mod._resolve_cdp_endpoint = lambda: LIGHTPANDA_WS
    # Temporarily null out supervisor registry so this test isolates path 2
    import tools.browser_supervisor as sup_mod
    _orig_registry_get = sup_mod.SUPERVISOR_REGISTRY.get
    sup_mod.SUPERVISOR_REGISTRY.get = lambda tid: None
    cold_times, cold_err = _bench(_cold_click, warmup=0, n=iterations)
    cold_stats = _stats(cold_times)
    print(f"        Done — {cold_err} errors, mean={cold_stats['mean_ms']:.2f}ms")

    # -----------------------------------------------------------------------
    # 3. Optimized: warm cache (session cached — skips getTargets+attachToTarget)
    # -----------------------------------------------------------------------
    print(f"\n  [3/4] Optimized — warm cache (1 WS conn, skips getTargets+attachToTarget)")
    print(f"        Warmup {warmup} (fills cache), then {iterations} iterations...")

    bt._CDP_SESSION_CACHE.clear()
    opt_times, opt_err = _bench(
        lambda: bt.browser_click(x=150.0, y=200.0, task_id="bench"),
        warmup, iterations,
    )
    sup_mod.SUPERVISOR_REGISTRY.get = _orig_registry_get
    cdp_mod._resolve_cdp_endpoint = _orig_resolve
    opt_stats = _stats(opt_times)
    print(f"        Done — {opt_err} errors, mean={opt_stats['mean_ms']:.2f}ms")

    # -----------------------------------------------------------------------
    # 4. Supervisor path: real CDPSupervisor with persistent WS
    # -----------------------------------------------------------------------
    print(f"\n  [4/4] Supervisor path (persistent WS — zero per-click connection cost)")
    print(f"        Starting supervisor → {LIGHTPANDA_WS}...")
    sup_stats = None
    sup_err_count = 0
    try:
        supervisor = sup_mod.CDPSupervisor.__new__(sup_mod.CDPSupervisor)
        # minimal init — we only need _loop, _ws, _page_session_id, _state_lock,
        # _pending_calls, _next_call_id, _active, _stop_requested
        # Use SUPERVISOR_REGISTRY.get_or_start for a fully initialized supervisor
        TASK_ID = "bench-supervisor"
        real_sup = sup_mod.SUPERVISOR_REGISTRY.get_or_start(TASK_ID, LIGHTPANDA_WS)
        import time as _time
        # Give supervisor time to connect and attach
        for _ in range(20):
            snap = real_sup.snapshot()
            if snap.active:
                break
            _time.sleep(0.1)

        if not real_sup.snapshot().active:
            print(f"        ⚠  Supervisor did not become active — skipping")
        else:
            print(f"        ✓ Supervisor active, warmup {warmup}...")
            def _sup_click():
                real_sup.dispatch_mouse_click(150, 200)
                return json.dumps({"success": True})

            for _ in range(warmup):
                _sup_click()
            print(f"        Running {iterations} iterations...")
            sup_times, sup_err_count = _bench(_sup_click, warmup=0, n=iterations)
            sup_stats = _stats(sup_times)
            print(f"        Done — {sup_err_count} errors, mean={sup_stats['mean_ms']:.2f}ms")
            sup_mod.SUPERVISOR_REGISTRY.stop(TASK_ID)
    except Exception as e:
        print(f"        ⚠  Supervisor benchmark failed: {e}")

    # -----------------------------------------------------------------------
    # Ref baseline
    # -----------------------------------------------------------------------
    if ab_ok:
        print(f"\n  [ref] agent-browser HTTP IPC (ref-click latency baseline)")
        ab_times = []
        for _ in range(warmup):
            urllib.request.urlopen(f"http://127.0.0.1:{AGENT_BROWSER_PORT}/api/sessions", timeout=5).read()
        for _ in range(iterations):
            t0 = time.perf_counter()
            urllib.request.urlopen(f"http://127.0.0.1:{AGENT_BROWSER_PORT}/api/sessions", timeout=5).read()
            ab_times.append(time.perf_counter() - t0)
        ab_stats = _stats(ab_times)
        print(f"        Done — mean={ab_stats['mean_ms']:.2f}ms")

    # -----------------------------------------------------------------------
    # Results
    # -----------------------------------------------------------------------
    col_w = 9
    print(f"\n{'─' * 82}")
    print(f"  {'Approach':<50}  {'Mean':>{col_w}}  {'Median':>{col_w}}  {'Min':>{col_w}}  {'p95':>{col_w}}")
    print(f"{'─' * 82}")
    _row("Baseline  (3 WS connections, sequential)         ", base_stats, col_w)
    _row("Optimized — cold cache (1 conn + negotiate)      ", cold_stats, col_w)
    _row("Optimized — warm cache (1 conn, skip resolve)    ", opt_stats,  col_w)
    if sup_stats:
        _row("Supervisor (persistent WS, zero conn cost)       ", sup_stats,  col_w)
    if ab_ok:
        _row("Ref-click IPC baseline (1 HTTP req)              ", ab_stats,  col_w)
    print(f"{'─' * 82}")

    print(f"\n  Speedups (mean vs baseline):")
    print(f"    Cold cache:   {base_stats['mean_ms'] / cold_stats['mean_ms']:.2f}x  ({base_stats['mean_ms'] - cold_stats['mean_ms']:.2f} ms saved)")
    print(f"    Warm cache:   {base_stats['mean_ms'] / opt_stats['mean_ms']:.2f}x  ({base_stats['mean_ms'] - opt_stats['mean_ms']:.2f} ms saved)")
    if sup_stats:
        print(f"    Supervisor:   {base_stats['mean_ms'] / sup_stats['mean_ms']:.2f}x  ({base_stats['mean_ms'] - sup_stats['mean_ms']:.2f} ms saved)")
        print(f"    Warm→Supervisor additional gain: {opt_stats['mean_ms'] - sup_stats['mean_ms']:.2f} ms  (WS conn eliminated)")
    if ab_ok and sup_stats:
        print(f"    Supervisor vs ref-click: {sup_stats['mean_ms'] / ab_stats['mean_ms']:.1f}x  (+{sup_stats['mean_ms'] - ab_stats['mean_ms']:.2f} ms)")

    print(f"\n  Optimization tiers in this PR:")
    print(f"    1. Single WS connection       — eliminates 2 TCP+WS handshakes")
    print(f"    2. mouseReleased-only wait     — skips redundant press ack (Playwright)")
    print(f"    3. Session ID cache            — skips getTargets+attachToTarget")
    print(f"    4. Supervisor reuse (new)      — eliminates the WS open entirely")
    print(f"       Active after browser_navigate; falls back to warm-cache path if absent.")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=300)
    parser.add_argument("--warmup", type=int, default=20)
    args = parser.parse_args()
    run_benchmark(iterations=args.iterations, warmup=args.warmup)
