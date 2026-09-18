"""Small transactional SQLite store with validated JSON and indexed relationships."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import TypeVar
from uuid import UUID, uuid4, uuid5

from pydantic import Field

from argos import models as m
from argos.common import EntityReference, Model
from argos.protocols import CriticReview

T = TypeVar("T", bound=m.Entity)
TYPES = {
    "project": m.Project,
    "subproblem": m.Subproblem,
    "research_branch": m.ResearchBranch,
    "hypothesis": m.Hypothesis,
    "experiment": m.Experiment,
    "run": m.Run,
    "observation": m.Observation,
    "claim": m.Claim,
    "decision": m.Decision,
    "task": m.Task,
    "evidence": m.Evidence,
    "baseline": m.Baseline,
}
KINDS = {cls: name for name, cls in TYPES.items()}


class StateError(ValueError):
    """A mutation would violate persistent research invariants."""


class Provenance(Model):
    claim: m.Claim
    evidence: m.Evidence
    observation: m.Observation
    run: m.Run
    experiment: m.Experiment
    hypothesis: m.Hypothesis


class StoredReview(Model):
    id: UUID
    review: CriticReview
    claim: m.Claim


class StateSnapshot(Model):
    """Bounded, project-scoped query; not an RA context or full agent briefing."""

    project: m.Project
    main_research_question: str | None
    active_subproblems: list[m.Subproblem]
    active_branches: list[m.ResearchBranch]
    hypotheses: list[m.Hypothesis]
    recent_experiments: list[m.Experiment]
    recent_observations: list[m.Observation]
    candidate_claims: list[m.Claim]
    failed_directions: list[m.Hypothesis]
    abandoned_branches: list[m.ResearchBranch]
    rejected_claims: list[m.Claim]
    failed_runs: list[m.Run]
    tasks: list[m.Task]
    decisions: list[m.Decision]
    reviews: list[StoredReview]
    truncated: list[str] = Field(default_factory=list)


class StateStore:
    """Single-thread connection. Open one store per thread/process.

    Human methods are trusted application entry points, never agent tools.
    Transactions serialize writers; snapshots use a single read transaction.
    No deletion API: revisions and failed records remain available indefinitely.
    """

    def __init__(self, path: str | Path):
        self._db = sqlite3.connect(str(path), isolation_level=None, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA synchronous = FULL")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.close()
            raise StateError(f"Unsupported state schema version: {version}")
        self._db.executescript(files("argos.state").joinpath("schema.sql").read_text())
        self._depth = 0

    def runtime_get(self, project_id: UUID, key: str) -> dict | None:
        """Host-only cursor/outcome storage; never exposed as an agent tool."""
        self.get(m.Project, project_id)
        row = self._db.execute(
            "SELECT payload FROM runtime_records WHERE project_id=? AND key=?",
            (str(project_id), key),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def runtime_put(self, project_id: UUID, key: str, payload: dict) -> None:
        with self.transaction():
            self.get(m.Project, project_id)
            encoded = json.dumps(payload, allow_nan=False)
            self._db.execute(
                "INSERT INTO runtime_records VALUES (?, ?, ?) "
                "ON CONFLICT(project_id, key) DO UPDATE SET payload=excluded.payload",
                (str(project_id), key, encoded),
            )
            self._db.execute(
                "INSERT INTO runtime_history(project_id, key, payload) VALUES (?, ?, ?)",
                (str(project_id), key, encoded),
            )

    def runtime_history(self, project_id: UUID) -> list[dict]:
        return [
            dict(row)
            for row in self._db.execute(
                "SELECT sequence, key, payload FROM runtime_history WHERE project_id=? "
                "ORDER BY sequence",
                (str(project_id),),
            )
        ]

    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def transaction(self):
        """Atomic multi-entity mutation; nested calls use rollback savepoints."""
        name = f"state_{self._depth}"
        nested = self._depth > 0
        self._db.execute(f"SAVEPOINT {name}" if nested else "BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield self
            self._db.execute(f"RELEASE {name}" if nested else "COMMIT")
        except BaseException:
            if nested:
                self._db.execute(f"ROLLBACK TO {name}")
                self._db.execute(f"RELEASE {name}")
            else:
                self._db.execute("ROLLBACK")
            raise
        finally:
            self._depth -= 1

    @contextmanager
    def _read(self):
        if self._depth:
            yield
            return
        self._db.execute("BEGIN")
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1
            self._db.execute("ROLLBACK")

    def get(self, cls: type[T], entity_id: UUID | str) -> T:
        row = self._db.execute(
            "SELECT payload FROM entities WHERE id=? AND kind=?",
            (str(UUID(str(entity_id))), KINDS[cls]),
        ).fetchone()
        if row is None:
            raise KeyError(f"Missing {cls.__name__}: {entity_id}")
        return cls.model_validate_json(row[0])

    def list(
        self,
        cls: type[T],
        *,
        project_id: UUID | None = None,
        status: str | list[str] | None = None,
        parent_id: UUID | None = None,
        role: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[T]:
        """Latest persisted change first; parent matches any outgoing relationship.

        `role` narrows the relationship, e.g. hypothesis_id, branch_id or run_id.
        Use project_id for project membership and pagination for full history.
        """
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("limit must be positive")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be nonnegative")
        if role is not None and parent_id is None:
            raise ValueError("role requires parent_id")
        clauses, args = ["e.kind=?"], [KINDS[cls]]
        if project_id is not None:
            clauses.append("e.project_id=?")
            args.append(str(project_id))
        if status is not None:
            statuses = [status] if isinstance(status, str) else list(status)
            if not statuses:
                return []
            clauses.append(f"e.status IN ({','.join('?' for _ in statuses)})")
            args.extend(statuses)
        if parent_id is not None:
            condition = "l.source_id=e.id AND l.target_id=?"
            args.append(str(parent_id))
            if role is not None:
                condition += " AND l.role=?"
                args.append(role)
            clauses.append(f"EXISTS (SELECT 1 FROM links l WHERE {condition})")
        query = "SELECT e.payload FROM entities e WHERE " + " AND ".join(clauses)
        query += " ORDER BY e.sequence DESC LIMIT ? OFFSET ?"
        args.extend([limit if limit is not None else -1, offset])
        return [cls.model_validate_json(r[0]) for r in self._db.execute(query, args)]

    def history(self, cls: type[T], entity_id: UUID) -> list[T]:
        self.get(cls, entity_id)
        return [
            cls.model_validate_json(r[0])
            for r in self._db.execute(
                "SELECT payload FROM revisions WHERE entity_id=? ORDER BY sequence",
                (str(entity_id),),
            )
        ]

    def create(self, entity: T) -> T:
        with self.transaction():
            if isinstance(entity, m.Baseline):
                raise StateError("Use approve_baseline from the trusted human interface")
            return self._write(entity, create=True)

    def update(self, entity: T) -> T:
        with self.transaction():
            return self._write(entity, create=False)

    def _write(self, entity: T, *, create: bool, human: bool = False) -> T:
        cls = type(entity)
        if cls not in KINDS:
            raise TypeError("Expected a shared persistent entity")
        entity = cls.model_validate_json(entity.model_dump_json())
        old = None if create else self.get(cls, entity.id)
        if (
            create
            and self._db.execute("SELECT 1 FROM entities WHERE id=?", (str(entity.id),)).fetchone()
        ):
            raise StateError(f"Duplicate entity ID: {entity.id}")
        if old:
            self._check_update(old, entity, human)
        elif isinstance(entity, m.Project) and (
            entity.main_question_approved or entity.baseline_id
        ):
            raise StateError("Initialize draft project, then explicitly approve question/baseline")
        project_id, links = self._validate_links(entity)
        payload = entity.model_dump_json()
        sequence = self._db.execute(
            "INSERT INTO revisions(entity_id,payload) VALUES (?,?)", (str(entity.id), payload)
        ).lastrowid
        values = (KINDS[cls], str(project_id), getattr(entity, "status", None), payload, sequence)
        if create:
            self._db.execute(
                "INSERT INTO entities(kind,project_id,status,payload,sequence,id) "
                "VALUES (?,?,?,?,?,?)",
                (*values, str(entity.id)),
            )
        else:
            self._db.execute(
                "UPDATE entities SET kind=?,project_id=?,status=?,payload=?,sequence=? WHERE id=?",
                (*values, str(entity.id)),
            )
            self._db.execute("DELETE FROM links WHERE source_id=?", (str(entity.id),))
        self._db.executemany(
            "INSERT INTO links(source_id,role,target_id) VALUES (?,?,?)",
            [(str(entity.id), role, str(target)) for role, target in set(links)],
        )
        if isinstance(entity, m.Claim):
            for relation, ids in self._claim_ids(entity):
                for observation_id in ids:
                    existing = self.list(m.Evidence, parent_id=entity.id, role="claim_id")
                    if any(e.observation_id == observation_id for e in existing):
                        continue
                    observation = self.get(m.Observation, observation_id)
                    self._write(
                        m.Evidence(
                            id=uuid5(entity.id, str(observation_id)),
                            created_at=entity.created_at,
                            claim_id=entity.id,
                            observation_id=observation_id,
                            run_id=observation.run_id,
                            relation=relation,
                        ),
                        create=True,
                    )
        return entity

    @staticmethod
    def _claim_ids(claim):
        return (
            ("supports", claim.evidence.supporting_observation_ids),
            ("contradicts", claim.evidence.contradicting_observation_ids),
        )

    def _check_update(self, old, new, human):
        if old.created_at != new.created_at:
            raise StateError("created_at is immutable")
        if isinstance(old, (m.Decision, m.Baseline, m.Evidence, m.Observation)) and old != new:
            raise StateError("Historical evidence and decisions are append-only")
        for field in ("project_id", "subproblem_id", "parent_id", "branch_id", "experiment_id"):
            if hasattr(old, field) and getattr(old, field) != getattr(new, field):
                raise StateError(f"{field} is immutable; create a new entity")
        if isinstance(old, m.Project) and not human:
            if (
                old.config != new.config
                or old.baseline_id != new.baseline_id
                or old.main_question_approved != new.main_question_approved
            ):
                raise StateError("Canonical project settings require a trusted human operation")
        if isinstance(old, m.Hypothesis) and old.statement != new.statement:
            if self.list(m.Experiment, parent_id=old.id, role="hypothesis_id", limit=1):
                raise StateError(
                    "A tested hypothesis statement is immutable; create a new hypothesis"
                )
        if isinstance(old, m.Experiment):
            if old.spec != new.spec:
                raise StateError("Experiment specifications are immutable; create a new experiment")
            transitions = {
                "proposed": {"selected", "planned", "cancelled"},
                "selected": {"planned", "implementing", "cancelled"},
                "planned": {"implementing", "cancelled"},
                "implementing": {"implemented", "implementation_failed", "timeout", "cancelled"},
                "implemented": {"testing", "running", "cancelled"},
                "testing": {
                    "running",
                    "implementation_failed",
                    "test_failed",
                    "timeout",
                    "cancelled",
                },
                "running": {"evaluating", "run_failed", "timeout", "cancelled"},
                "evaluating": {"completed", "invalid_result", "timeout", "cancelled"},
            }
            if old.status != new.status and new.status not in transitions.get(old.status, set()):
                raise StateError("Illegal experiment transition; retries use new experiments")
        if isinstance(old, m.Task):
            for field in ("kind", "resource_class", "references"):
                if getattr(old, field) != getattr(new, field):
                    raise StateError(f"Task {field} is immutable; create a new task")
            if new.repair_attempts < old.repair_attempts:
                raise StateError("Task repair_attempts cannot decrease")
            if old.started_at is not None and new.started_at != old.started_at:
                raise StateError("Task started_at is immutable once recorded")
        if isinstance(old, (m.Run, m.Task)):
            terminals = {"succeeded", "failed", "completed", "timeout", "cancelled"}
            if old.status in terminals:
                # Evaluation is attached once after deterministic execution exits.
                before, after = old.model_dump(), new.model_dump()
                if isinstance(old, m.Run) and old.status == "succeeded" and old.evaluation is None:
                    before.pop("evaluation")
                    after.pop("evaluation")
                if before != after:
                    raise StateError("Terminal attempts are immutable; create a new attempt")
            elif old.status == "running" and new.status == "pending":
                raise StateError("Running attempts cannot return to pending")
        if isinstance(old, m.Claim):
            for (_, previous), (_, current) in zip(self._claim_ids(old), self._claim_ids(new)):
                if not set(previous) <= set(current):
                    raise StateError("Claim evidence is append-only; revise with a new claim")

    def _validate_links(self, entity):
        links = []
        owners = []

        def ref(cls, ident, role):
            if ident is None:
                return None
            try:
                value = self.get(cls, ident)
            except KeyError as exc:
                raise StateError(str(exc)) from exc
            owner = self._db.execute(
                "SELECT project_id FROM entities WHERE id=?", (str(ident),)
            ).fetchone()[0]
            owners.append(UUID(owner))
            links.append((role, ident))
            return value

        if isinstance(entity, m.Project):
            owner = entity.id
            ref(m.Baseline, entity.baseline_id, "baseline_id")
        elif hasattr(entity, "project_id"):
            owner = ref(m.Project, entity.project_id, "project_id").id
        else:
            owner = None
        if isinstance(entity, m.Subproblem):
            parent = ref(m.Subproblem, entity.parent_id, "parent_id")
            if parent and parent.id == entity.id:
                raise StateError("Subproblem cannot parent itself")
            # Parents are immutable and must already exist, so longer cycles cannot form.
        if isinstance(entity, (m.ResearchBranch, m.Hypothesis)):
            ref(m.Subproblem, entity.subproblem_id, "subproblem_id")
        if isinstance(entity, m.Hypothesis):
            branch = ref(m.ResearchBranch, entity.branch_id, "branch_id")
            if branch and branch.subproblem_id != entity.subproblem_id:
                raise StateError("Hypothesis branch belongs to another subproblem")
        if isinstance(entity, m.Experiment):
            hypothesis = ref(m.Hypothesis, entity.spec.hypothesis_id, "hypothesis_id")
            if entity.spec.hypothesis_statement != hypothesis.statement:
                raise StateError("Experiment must preserve the referenced hypothesis statement")
            branch = ref(m.ResearchBranch, entity.branch_id, "branch_id")
            if branch and (
                branch.subproblem_id != hypothesis.subproblem_id
                or (hypothesis.branch_id and branch.id != hypothesis.branch_id)
            ):
                raise StateError("Experiment branch does not match hypothesis")
            ref(m.Baseline, entity.spec.baseline_id, "baseline_id")
        if isinstance(entity, m.Run):
            experiment = ref(m.Experiment, entity.experiment_id, "experiment_id")
            if entity.evaluation:
                if entity.evaluation.baseline_id != experiment.spec.baseline_id:
                    raise StateError("Evaluation must use the experiment's pinned baseline")
                if entity.evaluation.protocol_name != experiment.spec.evaluation_protocol.name:
                    raise StateError("Evaluation protocol does not match experiment")
        if isinstance(entity, (m.Observation, m.Baseline)):
            run = ref(m.Run, entity.run_id, "run_id")
            if run.status != "succeeded" or run.evaluation != entity.evaluation:
                raise StateError("Evaluation must equal the successful run's authoritative result")
        if isinstance(entity, m.Claim):
            for relation, ids in self._claim_ids(entity):
                for ident in ids:
                    observation = ref(m.Observation, ident, relation)
                    if observation.relation == "invalid" or observation.evaluation.status != "ok":
                        raise StateError("Invalid observation cannot be scientific evidence")
        if isinstance(entity, m.Evidence):
            claim = ref(m.Claim, entity.claim_id, "claim_id")
            observation = ref(m.Observation, entity.observation_id, "observation_id")
            ref(m.Run, entity.run_id, "run_id")
            expected = dict(self._claim_ids(claim)).get(entity.relation, [])
            if entity.run_id != observation.run_id or entity.observation_id not in expected:
                raise StateError("Evidence must agree with claim and observation")
            for existing in self.list(m.Evidence, parent_id=entity.claim_id, role="claim_id"):
                if existing.observation_id == entity.observation_id and existing.id != entity.id:
                    raise StateError("Duplicate claim/observation evidence")
        if isinstance(entity, (m.Decision, m.Task)):
            for item in entity.references:
                ref(TYPES[item.entity_type], item.entity_id, "references")
            if isinstance(entity, m.Task):
                for item in entity.output_references:
                    ref(TYPES[item.entity_type], item.entity_id, "output_references")
        if isinstance(entity, m.Baseline):
            previous = ref(m.Baseline, entity.previous_baseline_id, "previous_baseline_id")
            decision = ref(m.Decision, entity.approval_decision_id, "approval_decision_id")
            if decision.actor != "human":
                raise StateError("Baseline requires a human decision")
            if previous and previous.id == entity.id:
                raise StateError("Baseline cannot replace itself")
            if (
                run.result.source_commit != entity.source_commit
                or run.result.resulting_commit != entity.source_commit
            ):
                raise StateError("Baseline must record unchanged source commit")
        owner = owner or owners[0]
        if any(project != owner for project in owners):
            raise StateError("Cross-project relationships are forbidden")
        if isinstance(entity, m.Experiment):
            project = self.get(m.Project, owner)
            if entity.spec.evaluation_protocol != project.config.evaluation_protocol:
                raise StateError("Experiment cannot replace the project's protected evaluator")
        return owner, links

    def approve_main_question(
        self, project_id: UUID, question: str, *, rationale: str, cycle: int = 0
    ) -> m.Project:
        """Trusted human entry point; exact question must match the persisted proposal/draft."""
        with self.transaction():
            project = self.get(m.Project, project_id)
            expected = project.proposed_main_research_question
            if expected is None and not project.main_question_approved:
                expected = project.config.main_research_question
            if question != expected:
                raise StateError("Approval must match the current proposal or initial draft")
            self.create(
                m.Decision(
                    id=uuid4(),
                    created_at=datetime.now(UTC),
                    project_id=project_id,
                    cycle=cycle,
                    decision_type="main_question_approved",
                    actor="human",
                    summary=question,
                    rationale=rationale,
                    references=[EntityReference(entity_type="project", entity_id=project_id)],
                )
            )
            data = project.model_dump()
            data["config"]["main_research_question"] = question
            data.update(main_question_approved=True, proposed_main_research_question=None)
            return self._write(m.Project.model_validate(data), create=False, human=True)

    def approve_baseline(
        self, baseline: m.Baseline, *, rationale: str, cycle: int = 0
    ) -> m.Baseline:
        """Trusted human init/refresh; executor must separately verify a clean checkout."""
        baseline = m.Baseline.model_validate_json(baseline.model_dump_json())
        with self.transaction():
            project = self.get(m.Project, baseline.project_id)
            if baseline.previous_baseline_id != project.baseline_id:
                raise StateError("Baseline refresh must link to current baseline")
            self.create(
                m.Decision(
                    id=baseline.approval_decision_id,
                    created_at=datetime.now(UTC),
                    project_id=project.id,
                    cycle=cycle,
                    actor="human",
                    decision_type="baseline_refresh"
                    if project.baseline_id
                    else "baseline_initialization",
                    summary=f"Approve baseline {baseline.id} from run {baseline.run_id}",
                    rationale=rationale,
                    references=[EntityReference(entity_type="run", entity_id=baseline.run_id)],
                )
            )
            result = self._write(baseline, create=True)
            project.baseline_id = baseline.id
            self._write(project, create=False, human=True)
            return result

    def record_review(self, review: CriticReview, *, reviewed_claim: m.Claim) -> UUID:
        """Persist the reviewed version; reject a stale review after claim revision."""
        review = CriticReview.model_validate_json(review.model_dump_json())
        reviewed_claim = m.Claim.model_validate_json(reviewed_claim.model_dump_json())
        with self.transaction():
            claim = self.get(m.Claim, review.claim_id)
            if claim != reviewed_claim:
                raise StateError("Claim changed since review input was prepared")
            ident = uuid4()
            self._db.execute(
                "INSERT INTO reviews(id,claim_id,project_id,payload,claim_payload) "
                "VALUES (?,?,?,?,?)",
                (
                    str(ident),
                    str(claim.id),
                    str(claim.project_id),
                    review.model_dump_json(),
                    claim.model_dump_json(),
                ),
            )
            return ident

    def reviews(
        self, project_id: UUID, *, claim_id: UUID | None = None, limit: int = 20
    ) -> list[StoredReview]:
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be positive")
        rows = self._db.execute(
            "SELECT id,payload,claim_payload FROM reviews WHERE project_id=? "
            "AND (? IS NULL OR claim_id=?) ORDER BY sequence DESC LIMIT ?",
            (
                str(project_id),
                str(claim_id) if claim_id else None,
                str(claim_id) if claim_id else None,
                limit,
            ),
        )
        return [
            StoredReview(
                id=r[0],
                review=CriticReview.model_validate_json(r[1]),
                claim=m.Claim.model_validate_json(r[2]),
            )
            for r in rows
        ]

    def claim_provenance(self, claim_id: UUID) -> list[Provenance]:
        with self._read():
            claim = self.get(m.Claim, claim_id)
            result = []
            for evidence in self.list(m.Evidence, parent_id=claim_id, role="claim_id"):
                observation = self.get(m.Observation, evidence.observation_id)
                run = self.get(m.Run, evidence.run_id)
                experiment = self.get(m.Experiment, run.experiment_id)
                result.append(
                    Provenance(
                        claim=claim,
                        evidence=evidence,
                        observation=observation,
                        run=run,
                        experiment=experiment,
                        hypothesis=self.get(m.Hypothesis, experiment.spec.hypothesis_id),
                    )
                )
            return result

    def snapshot(self, project_id: UUID, *, limit: int = 20) -> StateSnapshot:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("snapshot limit must be between 1 and 1000 per section")
        sections = {
            "active_subproblems": (m.Subproblem, ["open", "active", "blocked"]),
            "active_branches": (m.ResearchBranch, ["active", "paused"]),
            "hypotheses": (m.Hypothesis, ["proposed", "testing", "supported", "inconclusive"]),
            "recent_experiments": (m.Experiment, None),
            "recent_observations": (m.Observation, None),
            "candidate_claims": (m.Claim, ["active"]),
            "failed_directions": (m.Hypothesis, ["rejected", "contradicted"]),
            "abandoned_branches": (m.ResearchBranch, ["rejected", "archived"]),
            "rejected_claims": (m.Claim, ["rejected"]),
            "failed_runs": (m.Run, ["failed"]),
            "tasks": (m.Task, None),
            "decisions": (m.Decision, None),
        }
        with self._read():
            project = self.get(m.Project, project_id)
            data, truncated = {}, []
            for name, (cls, status) in sections.items():
                rows = self.list(cls, project_id=project_id, status=status, limit=limit + 1)
                data[name] = rows[:limit]
                if len(rows) > limit:
                    truncated.append(name)
            reviews = self.reviews(project_id, limit=limit + 1)
            if len(reviews) > limit:
                truncated.append("reviews")
            return StateSnapshot(
                project=project,
                main_research_question=(
                    project.config.main_research_question
                    if project.main_question_approved
                    else None
                ),
                reviews=reviews[:limit],
                truncated=truncated,
                **data,
            )
