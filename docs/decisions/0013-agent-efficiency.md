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

Addendum (2026-10-31): a task also has a wall-clock limit (`limits.task_minutes`, default 15),
checked between steps, so a long step can overrun it. In a live run where `pytest` was not
installed, the coder spent about ten minutes looking for it, with one step taking three
minutes, and stayed within its token budget. A missing test tool is now recognised inside the
sandbox (exit 71, `execvp() … failed`), reported as "not installed here", and the coder is told
not to look for it or install it.

Addendum (2026-10-31): the persisted snapshot is keyed on more than the model path. Its metadata
carries a fingerprint of the weight and config files (name, size, mtime) plus the mlx-lm
version, and a hash of its token list; a file that does not match is deleted, and a snapshot
read from disk that a generation then fails on is forgotten and deleted rather than re-persisted
on unload. The file lives under `$XDG_CACHE_HOME/dfm/kv` and is write-denied to repository code
(ADR 0010). Cancellation is cooperative: a `cancel` message on the worker's stdin (read on a
thread) stops generation after the current token, so Ctrl-C and timeouts keep the loaded model
instead of killing the worker; it is killed only if it does not answer within five seconds.

Addendum (2026-11-11, first use of 0.0.6): with about 18 GB free, admission loaded a 27B coder
at an 8k context (it halved down to a 4k floor), and a question failed at step 6 with
`prompt is 4627 tokens; limit 8192 minus 4096 for the answer`. Nothing said the context was
reduced. Changes:

- **A 16k floor** (`MIN_CODER_CONTEXT`). The agent loop's repository context, history,
  instructions and tools, plus the tokens kept for the answer, need about that much. Admission
  reduces no further: it picks dfm-small at 16k if that fits, or refuses with the memory the
  model needs at 16k and the memory available. A context the developer configured below 16k is
  still honoured.
- **A reduced context is shown**: the runtime records it as a `note` event, which the
  interactive session prints.
- **One retry on overflow.** The pool raises `ContextLimitExceeded` when every coder rejected the
  prompt as too long. The agent loop then folds every earlier step to one line, halves the
  repository context and retries the step once. If that fails too, the task ends with what to do.
- The session prints a loop failure once (the `failure` event of the `act` and `answer` stages
  is the outcome it already prints) and says when the coder fell back to dfm-small.
