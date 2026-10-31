import json
from pathlib import Path

from dfm.models.mlx.worker import (
    LOOP_NGRAM,
    LoopDetector,
    common_prefix,
    merge_turns,
    sampling_defaults,
    shared_prefix,
    stop_index,
)


def test_sampling_follows_the_models_generation_config(tmp_path: Path) -> None:
    qwen = tmp_path / "qwen"
    qwen.mkdir()
    (qwen / "generation_config.json").write_text(
        json.dumps({"do_sample": True, "temperature": 1.0, "top_k": 20, "top_p": 0.95})
    )
    assert sampling_defaults(qwen) == {"temperature": 1.0, "top_p": 0.95, "top_k": 20.0}
    gpt_oss = tmp_path / "gpt-oss"  # do_sample without values: Hugging Face's defaults
    gpt_oss.mkdir()
    (gpt_oss / "generation_config.json").write_text(json.dumps({"do_sample": True}))
    assert sampling_defaults(gpt_oss) == {"temperature": 1.0, "top_p": 1.0, "top_k": 50.0}
    greedy = tmp_path / "greedy"
    greedy.mkdir()
    (greedy / "generation_config.json").write_text(json.dumps({"do_sample": False}))
    assert sampling_defaults(greedy)["temperature"] == 0.0
    assert sampling_defaults(tmp_path / "missing")["temperature"] == 0.0
    (greedy / "generation_config.json").write_text("not json")
    assert sampling_defaults(greedy)["temperature"] == 0.0


def test_loop_detector_cuts_repetition_early() -> None:
    # "Ok.\n\n" over and over (3 tokens a cycle), as gpt-oss-20b did under greedy decoding
    detector = LoopDetector()
    stopped_at = next(i for i in range(4096) if detector.add([11, 13, 17][i % 3]))
    assert stopped_at < 100
    # a 200-token paragraph repeated: caught in the fifth copy, not at the 4,096-token limit
    paragraph = list(range(1000, 1200))
    detector = LoopDetector()
    stopped_at = next(i for i in range(4096) if detector.add(paragraph[i % 200]))
    assert stopped_at < 5 * 200
    # varied text with short repeated phrases is never cut
    detector = LoopDetector()
    text = [i % 7 if i % 50 < 5 else 100 + i for i in range(4096)]
    assert not any(detector.add(t) for t in text)
    assert LOOP_NGRAM >= 30  # long enough that code with similar lines is not a loop


def test_shared_prefix_only_when_the_prompt_extends_the_cache() -> None:
    assert shared_prefix([1, 2, 3], [1, 2, 3, 4, 5]) == 3
    assert shared_prefix([1, 2, 3], [1, 2, 9, 4]) == 0  # diverged: rebuild
    assert shared_prefix([1, 2, 3], [1, 2, 3]) == 0  # nothing left to feed the model
    assert shared_prefix([], [1, 2]) == 0


def test_common_prefix() -> None:
    assert common_prefix([1, 2, 3, 4], [1, 2, 9]) == 2
    assert common_prefix([1, 2], [1, 2, 3]) == 2
    assert common_prefix([], [1]) == 0


def test_repeated_lines_that_are_not_a_loop_are_kept() -> None:
    # the same 40+ token header five times, with different code between: legitimate output
    header = list(range(500, 545))
    detector = LoopDetector()
    stream: list[int] = []
    for block in range(6):
        stream += header + [2000 + block * 10 + i for i in range(3 + block)]
    assert not any(detector.add(t) for t in stream)


def test_merge_turns_for_templates_that_require_alternation() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "task"},
        {"role": "user", "content": "so far"},
        {"role": "user", "content": "step"},
    ]
    assert merge_turns(messages) == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "task\n\nso far\n\nstep"},
    ]
    assert messages[1]["content"] == "task"  # the input is not modified


def test_stop_index_looks_only_where_a_new_chunk_can_complete_a_stop() -> None:
    assert stop_index("hello</answer>tail", ["</answer>"], since=10) == 5
    assert stop_index("hello </ans", ["</answer>"], since=6) is None
    assert stop_index("a</x>b</x>", ["</x>"], since=9) == 6  # earlier stops were already checked


def test_a_template_that_rejects_a_prefix_only_skips_that_snapshot() -> None:
    from dfm.models.mlx.worker import Worker

    class StrictTemplate:  # like Qwen3.8: "No user query found in messages."
        bos_token = None

        def apply_chat_template(self, messages, add_generation_prompt, tokenize):  # type: ignore[no-untyped-def]
            if not any(m["role"] == "user" for m in messages):
                raise ValueError("No user query found in messages.")
            return "".join(f"<{m['role']}>{m['content']}" for m in messages)

        def encode(self, text, add_special_tokens):  # type: ignore[no-untyped-def]
            return [ord(c) for c in text]

    worker = Worker()
    worker.tokenizer = StrictTemplate()
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]
    tokens = worker._encode(messages, generation_prompt=True)  # pyright: ignore[reportPrivateUsage]
    # the probe is retried with a placeholder user turn: the system prompt's end is still found
    assert worker._content_end(messages, 0, tokens) == len("<system>rules") - 4  # pyright: ignore[reportPrivateUsage]


def test_reasoning_levels_map_to_the_templates_own_names() -> None:
    from dfm.models.mlx.worker import Worker

    worker = Worker()
    worker.template = (
        "{% if reasoning_effort %}xhigh medium low{% endif %}{% if tools %}{% endif %}"
    )
    assert worker._template_kwargs({"reasoning": "high"}) == {"reasoning_effort": "xhigh"}  # pyright: ignore[reportPrivateUsage]
    tools = [{"type": "function", "function": {"name": "read"}}]
    assert worker._template_kwargs({"reasoning": "low", "tools": tools}) == {  # pyright: ignore[reportPrivateUsage]
        "reasoning_effort": "low",
        "tools": tools,
    }
    worker.template = "{{ messages }}"  # no reasoning effort, no tools: nothing is passed
    assert worker._template_kwargs({"reasoning": "high", "tools": tools}) == {}  # pyright: ignore[reportPrivateUsage]


def test_detects_a_prompt_that_opens_a_thinking_block() -> None:
    from dfm.models.mlx.worker import Worker

    class Chars:
        def decode(self, tokens):  # type: ignore[no-untyped-def]
            return "".join(chr(t) for t in tokens)

    worker = Worker()
    worker.tokenizer = Chars()
    assert worker._opens_thinking([ord(c) for c in "ant\n<think>\n"])  # pyright: ignore[reportPrivateUsage]
    assert not worker._opens_thinking([ord(c) for c in "<think>\n\n</think>\n\n"])  # pyright: ignore[reportPrivateUsage]
