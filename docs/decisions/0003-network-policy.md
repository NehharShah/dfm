# 0003 — Model networking ≠ engineering networking

Status: accepted (2026-10-25)

Decision: two independent axes.
- Model networking: `remote_inference = "deny"` by default. No hosted-model SDK may
  appear in runtime dependencies (enforced by test). No Apple PCC.
- Engineering networking: per-category allow/ask/deny (`github`, `git_remote`,
  `package_registries`, `documentation`, `container_registries`,
  `blockchain_rpc_read`, `blockchain_rpc_write`, `kubernetes`, `other_network`).
- Cluster writes (`kubernetes`) always ask with a hard confirmation naming the
  context/namespace, whatever the network rule says; an unknown or production-looking context
  is denied unless `security.allow_production_cluster_writes` (user config only) is true.
- `--offline` = deny all network. `--local-models` = deny remote inference, the rest follows policy (the default).
- `ask` with no interactive user = deny.

Consequences: `cargo fetch` can be allowed while remote inference stays impossible.
