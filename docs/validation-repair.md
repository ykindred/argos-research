# Real-model validation repairs

These changes address generic failures observed with two independent model providers.
They do not constitute acceptance of an autonomous scientific result.

## Contracts

`ExperimentSpec.required_artifacts` accepts `{path, producer}` entries. Producers are
`host`, `coding`, and `experiment`. Host paths are `code.diff`, `stdout.log`,
`stderr.log`, and `revision.json` in the trusted evidence directory. Coding artifacts
must be within the editable scope or `.argos-coding/`, and never protected paths.
Only coding-owned requirements enter `CodingTask`. Experiment commands produce their
own outputs. The host checks and preserves required files; missing files remain failures.
Old string requirements can still be read as experiment-owned paths, but new manager
plans must name the producer explicitly.

New retries use a fresh experiment ID, `retry_of`, and `recovery_rationale`. A retry
links a terminal failed/interrupted experiment of the same hypothesis. State persistence
checks that reference, including project isolation. The manager's action validator
rejects a second execution of a planned experiment in the same plan and any execution
of a non-planned experiment before applying actions. The execution host remains the
final authority. Only one structured repair is allowed.

New Critic responses must state `measurement_comparability`, `main_question_support`,
and `assessment_rationale`. Old responses remain readable as `not_assessed`; no
migration promotes historical evidence. A limited factual claim can be accepted
without establishing the main question. Incomparable measurements cannot support the
main goal. Runtime `completed` means the loop ended, not that research succeeded.

## Independent measurement fixture

`examples/synthetic` retains protocol v1 for historical compatibility. New acceptance
runs use `examples/synthetic-v2`, a new project and baseline. Protected instrumentation
counts equality and hashing operations on equality/hash keys independently of the
candidate's second return value. A fabricated zero therefore cannot reduce measured
comparisons. Correctness cases include colliding hashes. Hash work is reported separately;
fewer equality operations is not automatically less total work or scientific novelty.
This fixture is a bounded measurement contract, not a sandbox for adversarial Python.
Its rules are not special-cased in platform code.

## Prompt iteration and validation

The primary developer authored the contract changes and prompt candidate. Gemini 3.8
Flash High reviewed the supplied prompts in a fresh, tool-free request. Accepted edits
clarify coding command boundaries, untrusted task data and narrow-claim acceptance.
The suggestion to duplicate all RA boundaries inside the falsification suffix was not
adopted because it is always appended to the full RA prompt. The suggestion to promise
no partial writes on every failure was narrowed to checking scope before editing and
preserving diagnostics if a later failure occurs.

Behavior probes cover artifacts, immutable retry IDs, invented constraints, metric
proxy failures, narrow claims, and valid negative evidence. Separate acceptance probes
use precomputation outside a timer, a changed recovery sequence and a valid positive
result. These short policy probes do not replace full-schema role or end-to-end tests.
Raw requests, responses, prompt hashes, provider confirmation and scored results are
retained outside the source checkout in the validation evidence directory.

The external AGY adapter retries an explicitly transient, side-effect-free role call
once, with a two-second delay and a shared deadline. It never automatically replays
coding calls or retries authentication errors. Provider routing remains outside ARGOS.

## Release gate

Run deterministic regressions, cross-model probes and independent full-model projects
on a fixed revision. Human inspection must check actual measurements and claim scope,
not just task counts. Preserve every failed trial. Two consecutive accepted validation
rounds and a fresh eight-hour scripted soak on the final candidate are required before
release. A correct negative result or explicitly unresolved conclusion is acceptable;
fabricated or incomparable scientific success is not. Validation in progress is not
acceptance, and an earlier revision's soak does not validate this revision.


## Follow-up: evidence availability

The first repaired real-model batch recorded correct independent measurements but one
manager paused to request existing protocol and source records. A scoped host projection
now reads immutable Git blobs for protected and editable files at the recorded revision,
with a 6,000-byte text budget and 16-file limit. Larger, binary and symlink blobs expose
identities and explicit omission markers only. No role receives arbitrary filesystem
access. Execution results persist this projection; historical results default to an
empty projection rather than being reconstructed from current source.

Manager briefings include up to three recent successful run projections and command
exit codes. Critic evidence also includes the pinned baseline execution and evaluation.
This allows comparing protected blob identities and reading available measurement code
without leaking RM deliberation. Identical hashes prove identity, not metric adequacy;
omissions remain uncertainty. Prompt updates instruct roles to use evidence already
supplied instead of requesting its duplicate from a human.
