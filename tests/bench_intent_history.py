"""Manual retained-history benchmark for IntentStore whole-list JSON storage.

This file is not collected by pytest (it matches no test_*.py pattern) and it
refuses to run unless CROSS_AGENT_CHAT_BENCH_HISTORY=1 is set, so it can never
run in CI or slow `pytest -q`.

Run from the repository root:

    CROSS_AGENT_CHAT_BENCH_HISTORY=1 python tests/bench_intent_history.py

It builds synthetic BODY-FREE intent rows in a temporary state root at ~600,
10,000, and 100,000 rows and reports min/median/max seconds over >=5 samples
for load, single append, status lookup by event id, and the state-lock hold
time of a read-modify-write, plus a 4-thread concurrent-append run.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from uuid import uuid4

from cross_agent_chat.core import (
    SCHEMA_VERSION,
    IntentStatus,
    IntentStore,
    atomic_json,
    state_lock,
    utc_now,
)

ENV_FLAG = "CROSS_AGENT_CHAT_BENCH_HISTORY"
ROW_COUNTS = (600, 10_000, 100_000)
SAMPLES = 5
APPEND_THREADS = 4
APPENDS_PER_THREAD = 5
STATUSES: tuple[IntentStatus, ...] = (
    "PENDING",
    "REMOTE_AUTHORIZED",
    "PRE_EFFECT_REJECTED",
    "TRANSPORT_ACCEPTED",
    "UNKNOWN_DELIVERY",
    "RESOLVED_BY_OWNER",
)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _row(index: int) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid4()),
        "source_key": _digest(f"bench-source-{index}"),
        "source_generation": str(uuid4()),
        "source_alias": f"bench@localhost:row-{index}",
        "target_key": _digest(f"bench-target-{index}"),
        "target_generation": str(uuid4()),
        "payload_digest": _digest(f"bench-payload-{index}"),
        "status": STATUSES[index % len(STATUSES)],
        "timestamp": utc_now(),
    }


def _seed_store(root: Path, count: int) -> IntentStore:
    store = IntentStore(root)
    started = time.perf_counter()
    rows = [_row(index) for index in range(count)]
    built = time.perf_counter()
    atomic_json(store.path, rows)
    written = time.perf_counter()
    print(f"  seeded {count} rows (build {built - started:.3f}s, write {written - built:.3f}s)")
    return store


def _sampled(label: str, samples: list[float]) -> None:
    ordered = sorted(samples)
    median = statistics.median(ordered)
    print(
        f"  {label:<28} min={ordered[0]:.4f}s  median={median:.4f}s  max={ordered[-1]:.4f}s"
        f"  n={len(samples)}"
    )


def _hardware_note() -> str:
    memory = "unknown"
    if platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=True
            )
            memory = f"{int(result.stdout.strip()) // (1 << 30)} GiB"
        except (OSError, subprocess.CalledProcessError, ValueError):
            pass
    return (
        f"{platform.machine()} {platform.system()} {platform.release()}, "
        f"{os.cpu_count()} logical CPUs, {memory} RAM, Python {platform.python_version()}"
    )


def _hold_probe(store: IntentStore) -> float:
    """Time the read-modify-write critical section every mutation holds the lock for."""
    with state_lock(store.root, "intents"):
        started = time.perf_counter()
        existing = store.intents()
        atomic_json(store.path, [item.to_dict() for item in existing])
        return time.perf_counter() - started


def _bench_count(root: Path, count: int) -> None:
    print(f"rows={count}")
    store = _seed_store(root, count)
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert isinstance(raw, list)
    last = raw[-1]
    assert isinstance(last, dict)
    event_id = str(last["event_id"])
    source_key = str(last["source_key"])
    source_generation = str(last["source_generation"])

    _sampled("load intents()", [(_time(store.intents)) for _ in range(SAMPLES)])

    def append_once(sample: int) -> str:
        return store.begin_identity(
            source_key=_digest(f"append-source-{count}-{sample}"),
            source_generation=str(uuid4()),
            source_alias="bench@localhost:append",
            target_key=_digest(f"append-target-{count}-{sample}"),
            target_generation=str(uuid4()),
            payload_digest=_digest(f"append-payload-{count}-{sample}"),
        )

    appends = [_time(partial(append_once, sample)) for sample in range(SAMPLES)]
    _sampled("single append begin_identity", appends)

    lookups = [
        _time(
            lambda: store.intent_for_source(
                event_id=event_id,
                source_key=source_key,
                source_generation=source_generation,
            )
        )
        for _ in range(SAMPLES)
    ]
    _sampled("lookup intent_for_source", lookups)

    _sampled("state-lock hold (read+write)", [_hold_probe(store) for _ in range(SAMPLES)])

    thread_times: list[float] = []
    barrier = threading.Barrier(APPEND_THREADS)

    def worker(worker_index: int) -> None:
        barrier.wait()
        started = time.perf_counter()
        for sample in range(APPENDS_PER_THREAD):
            store.begin_identity(
                source_key=_digest(f"mt-source-{count}-{worker_index}-{sample}"),
                source_generation=str(uuid4()),
                source_alias="bench@localhost:append-mt",
                target_key=_digest(f"mt-target-{count}-{worker_index}-{sample}"),
                target_generation=str(uuid4()),
                payload_digest=_digest(f"mt-payload-{count}-{worker_index}-{sample}"),
            )
        thread_times.append(time.perf_counter() - started)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(APPEND_THREADS)]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall = time.perf_counter() - started
    total_appends = APPEND_THREADS * APPENDS_PER_THREAD
    _sampled(
        f"{APPEND_THREADS}-thread x{APPENDS_PER_THREAD} appends (per thread)",
        thread_times,
    )
    print(
        f"  concurrent wall={wall:.4f}s  ({total_appends} appends, "
        f"{wall / total_appends:.4f}s/append serialized by the lock)"
    )


def _time(operation: Callable[[], object]) -> float:
    started = time.perf_counter()
    operation()
    return time.perf_counter() - started


def main() -> int:
    if os.environ.get(ENV_FLAG) != "1":
        print(f"benchmark disabled; set {ENV_FLAG}=1 to run")
        return 0
    print(f"hardware: {_hardware_note()}")
    for count in ROW_COUNTS:
        with tempfile.TemporaryDirectory(prefix="cac-bench-intents-") as root:
            _bench_count(Path(root), count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
