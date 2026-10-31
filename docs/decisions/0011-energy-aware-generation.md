# 0011 — Energy-aware local generation

Status: accepted (2026-10-30)

Context: benchmark runs on a 48 GB M5 Pro overheated the laptop, mostly on wasted work.
gpt-oss-20b produced 16 times the output tokens per solved task of Qwen3.6-27B. The causes were
repetition loops under greedy decoding (`request.temperature or 0.0` made every unset
temperature greedy), tool calls in gpt-oss's own syntax that DFM discarded, and re-encoding the
full prompt at every step.

Decision (in `dfm/models/mlx/worker.py` unless noted):

- **Sampling.** Without a caller temperature, use the model's `generation_config.json`
  (Hugging Face defaults if it sets `do_sample` without values; greedy if it sets nothing). The
  structured-output retry keeps the caller's sampling.
- **Loop cutoff.** A reply in which one 40-token n-gram occurs 5 times stops with
  `stop_reason: "loop"`. `structured()` reports the loop to the model and retries once.
- **Prefix cache.** The worker snapshots the KV cache at the end of everything before the last
  message and reuses it when the next prompt shares that prefix. The agent loop
  (`dfm/orchestration/agent.py`) sends per-step state as the last message so the prefix only
  grows. Each snapshot costs one copy of the prefix's KV cache.
- **gpt-oss tool calls** are translated regardless of header spelling (`dfm/models/mlx/text.py`).
- **Benchmark cool-down.** `dfm benchmark --cooldown` (default 30 s) pauses after each run that
  used a model.
- **Thermal pacing** (`dfm/models/thermal.py`, `models.thermal_limit`, default `moderate`). Every
  32 tokens the worker reads macOS thermal pressure through the notify API (no privileges;
  works in the sandbox). At or above the limit it rests for as long as it just worked, a third
  longer above it. Before a call, `MLXModel` waits up to 120 s for the device to cool. Nothing
  changes while the machine is cool. Rest time is reported as `model.thermal_rest_s`. `off`
  disables pacing.

Consequences: generation is no longer deterministic by default; callers that need determinism
pass `temperature=0`. Cache reuse was verified on gpt-oss-20b (sliding-window cache) and
Qwen3.6-27B (hybrid linear-attention cache): greedy output was identical with and without the
cache. The snapshot boundary backs off 4 tokens so tokenizer merges at the seam do not break
reuse. Energy was not measured; savings are reported in tokens and wall time only.
