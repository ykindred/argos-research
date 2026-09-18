import json
import time
import tracemalloc

from algorithm import count_distinct
from measurement import Meter
from test_algorithm import check

check()
meter = Meter()
values = meter.keys(list(range(400)) * 2)
tracemalloc.start()
started = time.perf_counter()
count, ignored_candidate_counter = count_distinct(values)
elapsed = time.perf_counter() - started
_, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
print(
    json.dumps(
        {
            "status": "ok",
            "metrics": {
                "hash_calls": {"value": meter.hash_calls, "direction": "minimize"},
                "comparisons": {"value": meter.comparisons, "direction": "minimize"},
                "runtime": {"value": elapsed, "unit": "second", "direction": "minimize"},
                "peak_memory": {"value": peak, "unit": "byte", "direction": "minimize"},
            },
            "constraints": {"correctness": count == 400},
            "artifacts": [],
        }
    )
)
