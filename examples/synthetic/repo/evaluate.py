import json
import time
import tracemalloc

from algorithm import count_distinct
from test_algorithm import check

check()
values = list(range(400)) * 2
tracemalloc.start()
started = time.perf_counter()
count, comparisons = count_distinct(values)
elapsed = time.perf_counter() - started
_, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
print(
    json.dumps(
        {
            "status": "ok",
            "metrics": {
                "comparisons": {"value": comparisons, "direction": "minimize"},
                "runtime": {"value": elapsed, "unit": "second", "direction": "minimize"},
                "peak_memory": {"value": peak, "unit": "byte", "direction": "minimize"},
            },
            "constraints": {"correctness": count == len(set(values))},
            "artifacts": [],
        }
    )
)
