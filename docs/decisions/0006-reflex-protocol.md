# 0006 — DFM is a Reflex source via a versioned JSONL trace

Status: accepted (2026-10-25)

Decision: DFM writes one JSONL file per session under `~/.local/share/dfm/sessions/`:
first record is the header (`schema_version`, `session_id`, `source: "dfm"`,
`repo_hash`, `repo_name`, `started_at`, `request`), then one event per line
(`user_request`, `route_decision`, `command_started`, ... `task_completed`).
Everything is redacted before writing. Reflex gains `"dfm"` in `SourceId` and a
`DfmAdapter` that maps these to its `SessionEvent` kinds. Reflex never scrapes
terminal output. DFM reads back practices and skills through `reflex ... --json`.
