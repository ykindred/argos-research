CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES entities(id),
    status TEXT,
    payload TEXT NOT NULL,
    sequence INTEGER NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS entity_frontier ON entities(project_id, kind, status, sequence);
CREATE TABLE IF NOT EXISTS links (
    source_id TEXT NOT NULL REFERENCES entities(id),
    role TEXT NOT NULL,
    target_id TEXT NOT NULL REFERENCES entities(id),
    PRIMARY KEY (source_id, role, target_id)
);
CREATE INDEX IF NOT EXISTS incoming_links ON links(target_id, role);
CREATE TABLE IF NOT EXISTS revisions (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id TEXT NOT NULL REFERENCES entities(id) DEFERRABLE INITIALLY DEFERRED,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    claim_id TEXT NOT NULL REFERENCES entities(id),
    project_id TEXT NOT NULL REFERENCES entities(id),
    payload TEXT NOT NULL,
    claim_payload TEXT NOT NULL
);
PRAGMA user_version = 1;
