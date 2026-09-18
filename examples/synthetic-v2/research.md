# Research Charter v2

Can membership equality comparisons be reduced while preserving exact distinct counts
for keys supporting equality and hashing? Only algorithm.py may change. Preserve its
count_distinct(values) -> (count, legacy_counter) interface. The second return value
is ignored by measurement: a protected evaluator counts equality and hash operations.
Keys must be processed through equality/hash operations; inspecting their internals
or coercing them to another representation is outside this protocol. Hash calls are
reported separately; fewer comparisons alone do not establish less total computation.
This is a bounded platform validation fixture, not a novelty claim. No hidden
memory or runtime budget is implied. All positive, negative and inconclusive results
must remain scoped to the measured workloads. Do not edit evaluator or tests.
