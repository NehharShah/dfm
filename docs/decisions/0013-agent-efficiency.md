# 0013 — Agent efficiency: reasoning effort, budgets, compaction, native tools, persistent cache

Status: accepted (2026-10-31)

Context: Draw Things' Local Code runs the same model (Qwen3.8 27B) with per-model tuning, a
thinking budget, a task token limit, compaction and an on-disk context cache. DFM ran Qwen3.8 at
its template default, reasoning effort `xhigh`, on every step.

Decision:

- **Reasoning effort per step** (`models.reasoning`, default `auto`): `medium`, and `high` for
  the step after a failed edit, patch conflict, error or refusal. The level is passed to the chat
  template (`high` maps to `xhigh` where the template uses that name).
- **Thinking budget per level:** 512, 1,536 and 3,072 tokens. At the budget the worker closes the
  thinking block and continues from the same cache. In a live test this produced the same
  correct answer in 35 output tokens instead of 410.
- **Task token budget** (`limits.task_output_tokens`, default 50,000) across all steps and
  retries. When exhausted, the task stops as `failed` with "token budget used up".
- **History compaction** replaces the last-8-steps window. All steps are kept up to 16,000
  characters; then the oldest are reduced to one line each, down to 8,000. History changes only
  at compaction, so the prefix cache keeps matching.
- **Native tool calls.** Actions are sent as function schemas (`ACTION_TOOLS`). Templates that
  support tools receive them instead of JSON-schema instructions, and the model's native format
  (Qwen: `<tool_call><function=…>`) is translated back to an action. Other models keep the JSON
  schema.
- **Persistent prefix cache.** The shortest KV snapshot (system prompt and tools) is saved to
  `~/.cache/dfm/kv/` (mode 0600) when the worker unloads and restored by the next worker for the
  same model. In a live test the new worker reused 1,283 tokens on its first request.
- **Prefill chunk stays at 2,048 tokens.** 4,096 was no faster on an M5 Pro and used 2.3 GB
  more memory at peak.

Not adopted: MTP speculative decoding (mlx-lm 0.32.2 drops the MTP layers), Local Code's engine
and quantization formats, and App Sandbox (an App Store mechanism).

Consequences: `medium` effort trades some quality (Artificial Analysis Intelligence Index for
Qwen3.8 27B: 20 at `xhigh`, 17 at `medium`) for far fewer tokens. Set `models.reasoning = "high"`
to restore the previous behaviour. The cache file contains redacted repository text from
prompts; it is readable only by the user and can be deleted. Task success and energy per task
across the benchmark have not yet been measured with these settings.
