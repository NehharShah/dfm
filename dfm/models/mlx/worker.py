"""MLX inference worker: `python -m dfm.models.mlx.worker` (one model per process).

Speaks JSON lines on stdin/stdout so the parent can survive an OOM kill or a
crash. Runs with HF_HUB_OFFLINE=1 (set by the parent) and loads weights only
from a local path; it never downloads anything.

Requests:  {"id": n, "op": "ping" | "load" | "generate" | "unload", ...}
Responses: {"id": n, "ok": true, ...} or {"id": n, "ok": false, "kind": "...", "error": "..."}
Streaming generate sends {"id": n, "chunk": "..."} lines before the final response.
Cancel:    {"op": "cancel", "target": n} stops request n after its current token; it then
           answers with what it had (`stop_reason: "cancelled"`). stdin is read on a thread so
           the cancel is seen while the main thread is generating.

Generation spends as few joules as it can without changing what the model would answer:
- Sampling follows the model's own generation_config.json unless the request sets a
  temperature. Greedy decoding drives reasoning models into repetition loops. A request `seed`
  makes sampling reproducible.
- A reply that has started repeating itself is cut off (`stop_reason: "loop"`) instead of
  running to max_tokens.
- Under thermal pressure at or above the request's `thermal_limit`, generation rests between
  chunks of tokens (half duty at the limit, less above it), within `rest_budget_s`, so a hot
  laptop cools while it works.
- The KV state at the end of each message but the last is kept (at most MAX_SNAPSHOTS, together
  no longer than the context); the next request reuses the longest one its prompt starts with.
  Agent steps share their task, context and history; when the history window slides, the task
  and context still match.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import itertools
import json
import os
import queue
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from dfm.models.thermal import monitor, pause_for

# Hugging Face's GenerationConfig defaults, used when do_sample is set without a value
_HF_SAMPLING = {"temperature": 1.0, "top_p": 1.0, "top_k": 50}
LOOP_NGRAM = 40  # tokens
LOOP_REPEATS = 5  # the same stretch this many times in a row: the reply is going in circles
# Measured 2026-10-31 on an M5 Pro, Qwen3.8-27B 4-bit, 6k-token prompt: 4096-token chunks were
# no faster (417-472 vs 428-478 tok/s) and peaked 2.3 GB higher. Memory is the scarce resource.
PREFILL_STEP = 2048
SNAPSHOT_BACKOFF = 4  # tokens
MAX_SNAPSHOTS = 2
PACE_TOKENS = 32  # check thermal pressure this often
REST_SLICE_S = 0.5  # a thermal rest sleeps in slices this long, so a cancel is seen promptly
_PROBE = "\n⁣probe"
_PROBE_MEMO = 32
_THINK_CLOSE = "\n\nI have thought enough; time to answer.\n</think>\n\n"

Messages = list[dict[str, str]]


def sampling_defaults(model_dir: Path) -> dict[str, float]:
    """The model's recommended sampling, from generation_config.json; greedy if it has none."""
    try:
        config = json.loads((model_dir / "generation_config.json").read_text())
    except (OSError, ValueError):
        return {"temperature": 0.0, "top_p": 0.0, "top_k": 0}
    if not isinstance(config, dict) or not cast(dict[str, Any], config).get("do_sample"):
        return {"temperature": 0.0, "top_p": 0.0, "top_k": 0}
    values = cast(dict[str, Any], config)
    out: dict[str, float] = {}
    for key, default in _HF_SAMPLING.items():
        value = values.get(key, default)
        out[key] = float(value) if isinstance(value, int | float) else default
    return out


class LoopDetector:
    """Flags a reply whose tail is one stretch of tokens repeated LOOP_REPEATS times in a row.

    A repeated n-gram alone is not enough: code and tests legitimately repeat headers and
    lines. The occurrences must be evenly spaced and everything between them identical.
    """

    def __init__(self, n: int = LOOP_NGRAM, repeats: int = LOOP_REPEATS) -> None:
        self.n, self.repeats = n, repeats
        self.tokens: list[int] = []
        self.starts: dict[tuple[int, ...], list[int]] = {}

    def add(self, token: int) -> bool:
        self.tokens.append(token)
        if len(self.tokens) < self.n:
            return False
        start = len(self.tokens) - self.n
        seen = self.starts.setdefault(tuple(self.tokens[start:]), [])
        seen.append(start)
        del seen[: -self.repeats]
        if len(seen) < self.repeats:
            return False
        period = seen[1] - seen[0]
        if any(b - a != period for a, b in itertools.pairwise(seen)):
            return False
        tail = self.tokens[seen[0] :]
        return all(tail[i] == tail[i - period] for i in range(period, len(tail)))


def common_prefix(a: Sequence[int], b: Sequence[int]) -> int:
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n


def shared_prefix(cached: Sequence[int], tokens: Sequence[int]) -> int:
    """Length of `cached` if `tokens` extends it (the cache is reusable), else 0."""
    n = len(cached)
    return n if 0 < n < len(tokens) and list(tokens[:n]) == list(cached) else 0


def merge_turns(messages: Messages) -> Messages:
    """Consecutive messages of one role joined: for chat templates that require alternation."""
    merged: Messages = []
    for m in messages:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1] = {**merged[-1], "content": merged[-1]["content"] + "\n\n" + m["content"]}
        else:
            merged.append(dict(m))
    return merged


def stop_index(text: str, stops: Sequence[str], since: int) -> int | None:
    """Where the first stop sequence starts, looking only at text a new chunk could complete."""
    longest = max((len(s) for s in stops), default=0)
    window = max(0, since - longest + 1)
    found = [i for s in stops if (i := text.find(s, window)) != -1]
    return min(found) if found else None


def _lib(name: str) -> Any:
    """mlx / mlx-lm are imported dynamically: an untyped boundary, and optional at install time."""
    return importlib.import_module(name)


def _send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


class Worker:
    def __init__(self) -> None:
        self.model: Any = None
        self.tokenizer: Any = None
        self.path: str | None = None
        self.sampling: dict[str, float] = {}
        self.merge_turns = False  # the chat template rejected consecutive same-role messages
        # (tokens, KV state after prefilling them), shortest first: one per message boundary
        self.snapshots: list[tuple[list[int], Any]] = []
        self.probes: dict[str, int] = {}  # message-prefix JSON -> where its content ends
        self.thermal = monitor()
        self.template = ""  # the chat template's source: what it supports
        self.kv_file: Path | None = None  # where the shared prefix snapshot persists
        self.template_kwargs: dict[str, Any] = {}  # this request's reasoning effort, tools
        self.cancelled: set[Any] = set()  # request ids the parent asked to stop (reader thread)
        self.restored_in_use = False  # the first snapshot came from disk and is unproven

    def ping(self, req: dict[str, Any]) -> dict[str, Any]:
        import importlib.metadata as md

        mx = _lib("mlx.core")
        return {
            "mlx": md.version("mlx"),
            "mlx_lm": md.version("mlx-lm"),
            "device": str(mx.default_device()),
            "loaded": self.path,
            "offline": _lib("os").environ.get("HF_HUB_OFFLINE") == "1",
        }

    def _reset(self) -> None:
        self.merge_turns = False
        self.snapshots, self.probes = [], {}

    def load(self, req: dict[str, Any]) -> dict[str, Any]:
        mx = _lib("mlx.core")
        load = _lib("mlx_lm").load
        path = str(req["path"])
        if not Path(path).is_dir():
            return {"ok": False, "kind": "missing", "error": f"model directory not found: {path}"}
        started = time.monotonic()
        self.model, self.tokenizer = load(path)
        self.path = path
        self.sampling = sampling_defaults(Path(path))
        template = getattr(self.tokenizer, "chat_template", None)
        self.template = template if isinstance(template, str) else ""
        self._reset()
        kv_dir = req.get("kv_dir")
        self.kv_file = (
            Path(str(kv_dir)) / (hashlib.sha256(path.encode()).hexdigest()[:16] + ".safetensors")
            if kv_dir
            else None
        )
        restored = self._restore()
        return {
            "restored_tokens": restored,
            "load_s": round(time.monotonic() - started, 2),
            "peak_gb": round(mx.get_peak_memory() / 1e9, 2),
            "sampling": self.sampling,
            "native_tools": "tools" in self.template,
            "reasoning_effort": "reasoning_effort" in self.template,
        }

    def _weights_fingerprint(self) -> str:
        """What the snapshot was computed with: model files by name, size and mtime, plus the
        mlx-lm version (its cache classes). A changed file at the same path is a different
        model, and its old snapshot would be silently wrong."""
        import importlib.metadata as md

        root = Path(self.path or "")
        parts = [md.version("mlx-lm")]
        for file in sorted(root.glob("*")):
            if file.suffix in {".safetensors", ".json"} and file.is_file():
                st = file.stat()
                parts.append(f"{file.name}:{st.st_size}:{int(st.st_mtime)}")
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()

    def _persist(self) -> None:
        """Save the shortest snapshot (system prompt and tool definitions: the same for every
        task) so the next worker, a new `dfm` command or a resumed session, skips its prefill."""
        if self.kv_file is None or not self.snapshots or self.path is None:
            return
        if self.restored_in_use:
            return  # it came from this file; rewriting 250 MB to say the same thing is waste
        tokens, state = self.snapshots[0]
        save = _lib("mlx_lm.models.cache").save_prompt_cache
        self.kv_file.parent.mkdir(parents=True, exist_ok=True)
        # a private temp name: two workers persisting at once must not clobber each other's
        fd, name = tempfile.mkstemp(dir=self.kv_file.parent, suffix=".tmp.safetensors")
        tmp = Path(name)
        os.close(fd)
        try:
            tokens_json = json.dumps(tokens)
            save(
                str(tmp),
                state,
                {
                    "model": self.path,
                    "tokens": tokens_json,
                    "tokens_sha256": hashlib.sha256(tokens_json.encode()).hexdigest(),
                    "weights": self._weights_fingerprint(),
                },
            )
            tmp.chmod(0o600)  # repository text (already redacted) is in there: user-only
            tmp.replace(self.kv_file)
        except Exception:  # a cache that cannot be saved costs a prefill, nothing else
            tmp.unlink(missing_ok=True)

    def _restore(self) -> int:
        """The persisted snapshot, if it belongs to this model and these weights; tokens
        restored (0 if none). A file that does not match is deleted, not merely ignored."""
        if self.kv_file is None or not self.kv_file.is_file():
            return 0
        load = _lib("mlx_lm.models.cache").load_prompt_cache
        try:
            state, meta = load(str(self.kv_file), return_metadata=True)
            tokens_json = str(meta["tokens"])
            fits = (
                meta.get("model") == self.path
                and meta.get("weights") == self._weights_fingerprint()
                and meta.get("tokens_sha256") == hashlib.sha256(tokens_json.encode()).hexdigest()
            )
            if not fits:
                self._drop_persisted()
                return 0
            tokens = [int(t) for t in json.loads(tokens_json)]
        except Exception:  # stale or unreadable: it is only a cache
            self._drop_persisted()
            return 0
        self.snapshots = [(tokens, state)]
        self.restored_in_use = True
        return len(tokens)

    def _drop_persisted(self) -> None:
        if self.kv_file is not None:
            self.kv_file.unlink(missing_ok=True)

    def restored_snapshot_failed(self) -> None:
        """Generation raised while a snapshot read from disk was in play: it is the prime
        suspect. Forget it and delete the file so the next worker starts from a prefill
        instead of inheriting the same failure."""
        if self.restored_in_use:
            self.restored_in_use = False
            self.snapshots = []
            self._drop_persisted()

    def _template_kwargs(self, req: dict[str, Any]) -> dict[str, Any]:
        """Chat-template variables for this request: reasoning effort and native tools."""
        kwargs: dict[str, Any] = {}
        level = req.get("reasoning")
        if level in ("low", "medium", "high") and "reasoning_effort" in self.template:
            # Qwen3.8 names its top level "xhigh"; gpt-oss and others "high"
            kwargs["reasoning_effort"] = (
                "xhigh" if level == "high" and "xhigh" in self.template else level
            )
        tools = req.get("tools")
        if isinstance(tools, list) and tools and "tools" in self.template:
            kwargs["tools"] = tools
        return kwargs

    def _opens_thinking(self, tokens: list[int]) -> bool:
        """The prompt ends inside a thinking block (Qwen's template opens `<think>`)."""
        tail = str(self.tokenizer.decode(tokens[-8:]))
        return "<think>" in tail and "</think>" not in tail.split("<think>")[-1]

    def unload(self, req: dict[str, Any]) -> dict[str, Any]:
        import gc

        mx = _lib("mlx.core")
        self._persist()
        self.model = self.tokenizer = self.path = None
        self._reset()
        self.restored_in_use = False
        gc.collect()
        mx.clear_cache()
        return {}

    def _rest(self, seconds: float, request_id: Any) -> float:
        """Sleep `seconds` in slices, stopping early if the request is cancelled; slept time."""
        slept = 0.0
        while slept < seconds and request_id not in self.cancelled:
            step = min(REST_SLICE_S, seconds - slept)
            time.sleep(step)
            slept += step
        return slept

    def _encode(self, messages: Messages, *, generation_prompt: bool) -> list[int]:
        text = self.tokenizer.apply_chat_template(
            messages,
            add_generation_prompt=generation_prompt,
            tokenize=False,
            **self.template_kwargs,
        )
        if not isinstance(text, str):
            return [int(t) for t in text]
        bos = getattr(self.tokenizer, "bos_token", None)  # as stream_generate encodes a str prompt
        special = bos is None or not text.startswith(bos)
        return [int(t) for t in self.tokenizer.encode(text, add_special_tokens=special)]

    def _prompt(self, messages: Messages) -> tuple[Messages, list[int]]:
        """The messages as the template accepts them, and their tokens."""
        if not self.merge_turns:
            try:
                return messages, self._encode(messages, generation_prompt=True)
            except Exception:  # e.g. "Conversation roles must alternate user/assistant/..."
                merged = merge_turns(messages)
                if merged == messages:
                    raise
                self.merge_turns = True
        merged = merge_turns(messages)
        return merged, self._encode(merged, generation_prompt=True)

    def _prefill(self, cache: Any, tokens: list[int]) -> None:
        mx = _lib("mlx.core")
        for start in range(0, len(tokens), PREFILL_STEP):
            self.model(mx.array(tokens[start : start + PREFILL_STEP])[None], cache=cache)
            mx.eval([c.state for c in cache])

    def _content_end(self, messages: Messages, i: int, tokens: list[int]) -> int:
        """Where message i's *content* ends in `tokens` (memoized per message prefix).

        The next request appends to a message ahead of the template's end-of-message tokens, so
        the content end is found by appending text to it and comparing the encodings.
        """
        key = json.dumps([self.template_kwargs, messages[: i + 1]], default=str)
        if key not in self.probes:
            probe = [*messages[:i], {**messages[i], "content": messages[i]["content"] + _PROBE}]
            encoded: list[int] = []
            # some templates reject a prefix (Qwen3.8: no user message yet); a placeholder user
            # turn after it leaves the prefix itself unchanged
            for attempt in (probe, [*probe, {"role": "user", "content": "."}]):
                try:
                    encoded = self._encode(attempt, generation_prompt=False)
                    break
                except Exception:  # noqa: S112  (no snapshot here; the request is unaffected)
                    continue
            end = common_prefix(tokens, encoded)
            if len(self.probes) >= _PROBE_MEMO:
                self.probes.pop(next(iter(self.probes)))
            self.probes[key] = max(0, end - SNAPSHOT_BACKOFF)  # tokens can merge across the seam
        return self.probes[key]

    def _cache_for(
        self, messages: Messages, tokens: list[int], context_limit: int
    ) -> tuple[Any, int, int]:
        """(KV cache holding tokens[:n], n, how many of those n came from an earlier request).

        Starts from the longest snapshot the prompt extends and snapshots each new message end.
        """
        make_prompt_cache = _lib("mlx_lm.models.cache").make_prompt_cache
        kept = [(t, state) for t, state in self.snapshots if shared_prefix(t, tokens)]
        reused = len(kept[-1][0]) if kept else 0
        cache = copy.deepcopy(kept[-1][1]) if kept else make_prompt_cache(self.model)
        ready = reused
        for i in range(len(messages) - 1):
            end = self._content_end(messages, i, tokens)
            if ready < end < len(tokens):
                self._prefill(cache, tokens[ready:end])
                ready = end
                kept.append((tokens[:end], copy.deepcopy(cache)))
        kept = kept[-MAX_SNAPSHOTS:]
        # snapshots together hold no more than one context's worth of KV (the memory estimate)
        while context_limit and len(kept) > 1 and sum(len(t) for t, _ in kept) > context_limit:
            kept.pop(0)
        self.snapshots = kept
        return cache, ready, reused

    def generate(self, req: dict[str, Any]) -> dict[str, Any]:
        mx = _lib("mlx.core")
        stream_generate = _lib("mlx_lm").stream_generate
        make_sampler = _lib("mlx_lm.sample_utils").make_sampler
        if self.model is None:
            return {"ok": False, "kind": "not_loaded", "error": "no model loaded"}
        self.template_kwargs = self._template_kwargs(req)
        messages, tokens = self._prompt(cast(Messages, req["messages"]))
        context_limit = int(req.get("context_limit") or 0)
        max_tokens = int(req.get("max_tokens") or 2048)
        if context_limit and len(tokens) + max_tokens > context_limit:
            return {
                "ok": False,
                "kind": "context",
                "error": f"prompt is {len(tokens)} tokens; limit {context_limit} minus {max_tokens} for the answer",
            }
        sampling = dict(self.sampling)
        if req.get("temperature") is not None:  # the caller's choice wins; 0 is greedy
            sampling["temperature"] = float(req["temperature"])
        sampler = make_sampler(
            temp=sampling.get("temperature", 0.0),
            top_p=sampling.get("top_p", 0.0) if sampling.get("temperature") else 0.0,
            top_k=int(sampling.get("top_k", 0)) if sampling.get("temperature") else 0,
        )
        if isinstance(req.get("seed"), int):
            mx.random.seed(int(req["seed"]))
        stops = [str(s) for s in cast(list[Any], req.get("stop") or []) if s]
        holdback = max((len(s) for s in stops), default=1) - 1  # a stop may span two chunks
        stream = bool(req.get("stream"))
        thermal_limit = req.get("thermal_limit")
        thermal_limit = thermal_limit if isinstance(thermal_limit, int) else None
        rest_budget = float(req.get("rest_budget_s") or 0.0)
        started = time.monotonic()
        request_id = req.get("id")
        self.cancelled.discard(request_id)  # a cancel for an earlier request with the same id
        cache, ready, reused = self._cache_for(messages, tokens, context_limit)
        # from here on the restored snapshot has been used in anger: a later failure is not its
        # fault only if this request completes
        rested = 0.0
        chunk_started = time.monotonic()
        loops = LoopDetector()
        stop_reason = "length"
        text = ""
        sent = 0
        generated = 0
        thinking_budget = int(req.get("thinking_budget") or 0)
        thinking = thinking_budget > 0 and self._opens_thinking(tokens)
        thought = 0  # tokens generated inside the thinking block
        forced = False
        first_token_at: float | None = None
        prompt = tokens[ready:]
        while True:
            budget_spent: int | None = None
            for response in stream_generate(
                self.model,
                self.tokenizer,
                prompt=prompt,
                max_tokens=max_tokens - generated,
                sampler=sampler,
                prompt_cache=cache,
            ):
                generated += 1
                if first_token_at is None:
                    first_token_at = time.monotonic()
                before = len(text)
                text += response.text
                if stops and (at := stop_index(text, stops, before)) is not None:
                    text = text[:at]
                    stop_reason = "stop"
                    break
                if stream and len(text) - holdback > sent:
                    _send({"id": req["id"], "chunk": text[sent : len(text) - holdback]})
                    sent = len(text) - holdback
                if loops.add(int(response.token)):
                    stop_reason = "loop"
                    break
                if request_id in self.cancelled:
                    stop_reason = "cancelled"
                    break
                if getattr(response, "finish_reason", None) == "stop":
                    stop_reason = "stop"
                if thinking:
                    if "</think>" in text[max(0, before - 8) :]:
                        thinking = False
                    else:
                        thought += 1
                        if thought >= thinking_budget:
                            budget_spent = int(response.token)
                            break
                if thermal_limit is not None and generated % PACE_TOKENS == 0:
                    busy = time.monotonic() - chunk_started
                    rest = min(
                        pause_for(self.thermal.level(), thermal_limit, busy), rest_budget - rested
                    )
                    if rest > 0:
                        rested += self._rest(rest, request_id)
                    chunk_started = time.monotonic()
            if budget_spent is None or generated >= max_tokens or stop_reason == "cancelled":
                break
            # Budget forcing: close the thinking block and let the model answer. The last token
            # was sampled but not yet fed to the model, so it leads the continuation.
            forced, thinking = True, False
            text += _THINK_CLOSE
            prompt = [
                budget_spent,
                *self.tokenizer.encode(_THINK_CLOSE, add_special_tokens=False),
            ]
        if stream and len(text) > sent:
            _send({"id": req["id"], "chunk": text[sent:]})
        self.cancelled.discard(request_id)
        if stop_reason != "cancelled":
            self.restored_in_use = False  # a full request ran on it: the snapshot is sound
        return {
            "text": text,
            "prompt_tokens": len(tokens),
            "cached_tokens": reused,  # prefill skipped thanks to an earlier request
            "generation_tokens": generated,
            "generation_tps": round(
                (generated - 1) / max(1e-6, time.monotonic() - first_token_at - rested), 1
            )
            if first_token_at is not None and generated > 1
            else 0.0,
            "thinking_tokens": thought,
            "thinking_cut": forced,
            "stop_reason": stop_reason,
            "thermal_pause_s": round(rested, 2),
            "thermal_level": self.thermal.level(),
            "seconds": round(time.monotonic() - started, 2),
            "peak_gb": round(mx.get_peak_memory() / 1e9, 2),
        }


def _read_requests(worker: Worker, requests: queue.Queue[dict[str, Any] | None]) -> None:
    """stdin → request queue, on its own thread. `cancel` is handled here, so it takes effect
    while the main thread is inside a generation; everything else waits its turn."""
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = cast(dict[str, Any], json.loads(line))
        except json.JSONDecodeError:
            continue
        if req.get("op") == "cancel":
            worker.cancelled.add(req.get("target"))
            continue
        requests.put(req)
    requests.put(None)  # EOF: the parent is gone


def main() -> None:
    worker = Worker()
    requests: queue.Queue[dict[str, Any] | None] = queue.Queue()
    threading.Thread(target=_read_requests, args=(worker, requests), daemon=True).start()
    while (req := requests.get()) is not None:
        op = str(req.get("op"))
        handler = {
            "ping": worker.ping,
            "load": worker.load,
            "generate": worker.generate,
            "unload": worker.unload,
        }.get(op)
        if handler is None:
            _send(
                {"id": req.get("id"), "ok": False, "kind": "protocol", "error": f"unknown op {op}"}
            )
            continue
        try:
            result = handler(req)
        except MemoryError as exc:
            _send({"id": req.get("id"), "ok": False, "kind": "oom", "error": str(exc)})
            continue
        except Exception as exc:  # report everything; the parent decides what to do
            if op == "generate":
                worker.restored_snapshot_failed()
            kind = "oom" if "memory" in str(exc).lower() else "error"
            _send(
                {
                    "id": req.get("id"),
                    "ok": False,
                    "kind": kind,
                    "error": f"{type(exc).__name__}: {exc}",
                    "trace": traceback.format_exc()[-1500:],
                }
            )
            continue
        result.setdefault("ok", True)
        result["id"] = req.get("id")
        _send(result)
        if op == "unload" and req.get("exit"):
            break


if __name__ == "__main__":
    main()
