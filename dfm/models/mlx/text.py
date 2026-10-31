"""Turn raw local-model output into the answer (reasoning stripped).

Reasoning models emit their thinking before the answer, in model-specific
markup. DFM keeps only the final answer:
- gpt-oss "harmony": `<|channel|>analysis<|message|>…<|end|>…<|channel|>final<|message|>ANSWER`
- Gemma 4 thinking: `…thoughts…<channel|>ANSWER`
- Qwen / DeepSeek style: `<think>…</think>ANSWER`

Some models answer with an action in their own syntax instead of DFM's JSON; the
translators below turn those into `{"action": …}` JSON (or None when unsure).
"""

from __future__ import annotations

import html
import json
import re
import shlex
from typing import Any, cast

# gpt-oss may add a constraint marker: `<|channel|>final <|constrain|>JSON<|message|>`,
# and sometimes leaves out `<|message|>`: `<|channel|>final <|constrain|>{"action": …}`
_HARMONY_FINAL = re.compile(
    r"<\|channel\|>\s*final[^<]*(?:<\|constrain\|>[^<{]*)?(?:<\|message\|>)?(.*?)(?:<\|return\|>|<\|end\|>|$)",
    re.S,
)
_THINK = re.compile(r"<think>.*?</think>", re.S)
_SPECIAL = re.compile(r"<\|[a-z_]+\|>")


def final_text(raw: str) -> str:
    match = _HARMONY_FINAL.search(raw)
    if match:
        return match.group(1).strip()
    if "<|channel|>" in raw:  # harmony without a final channel: no answer was produced
        return ""
    if "<channel|>" in raw:
        raw = raw.rsplit("<channel|>", 1)[1]
    raw = _THINK.sub("", raw)
    if "</think>" in raw:  # opening tag was in the prompt template
        raw = raw.rsplit("</think>", 1)[1]
    return _SPECIAL.sub("", raw).strip()


# gpt-oss acts through tool calls even when told not to:
#   <|channel|>commentary to=repo_browser.patch <|constrain|>json<|message|>{"path": …}
_HARMONY_CALL = re.compile(
    r"<\|channel\|>\s*(?:commentary|analysis)\s+to=([^<]*)(?:<\|constrain\|>[^<]*)?<\|message\|>(.*?)(?:<\|call\|>|<\|end\|>|$)",
    re.S,
)
_HARMONY_ANALYSIS = re.compile(
    r"<\|channel\|>\s*analysis\s*<\|message\|>(.*?)(?:<\|end\|>|$)", re.S
)

ACTION_SYNONYMS = {
    "open": "read",
    "open_file": "read",
    "read_file": "read",
    "cat": "read",
    "view": "read",
    "grep": "search",
    "find": "search",
    "search_text": "search",
    "apply_patch": "patch",
    "edit": "patch",
    "replace": "patch",
    "patch_file": "patch",
    "write": "create",
    "create_file": "create",
    "write_file": "create",
    "exec": "run",
    "shell": "run",
    "run_command": "run",
    "bash": "run",
    "run_tests": "test",
    "tests": "test",
    "pytest": "test",
    "finish": "done",
    "submit": "done",
    "final": "done",  # gpt-oss: `commentary to=final <|constrain|>json<|message|>{}`
}
ARG_SYNONYMS = {
    "query": "pattern",
    "regex": "pattern",
    "cmd": "command",
    "file": "path",
    "file_path": "path",
    "filename": "path",
    "old": "search",
    "new": "replace",
    "text": "content",
    "line_start": "start",
    "line_end": "end",
}
# directory listings (gpt-oss: repo_browser.print_tree) have no DFM action: a read-only `run`
_LISTING_TOOLS = {"print_tree", "list_files", "list_dir", "list_directory", "ls", "tree"}


_HEADER_NOISE = {"code", "json", "to"}


def _tool(header: str, allowed: list[str]) -> str:
    """The tool a call names, however gpt-oss spells it.

    `repo_browser.print_tree`, `repo-search`, `assistant:read`, `assistant read code`,
    `reposearch` → print_tree, search, read, read, search.
    """
    words = [w for w in re.split(r"[\s.:/\-]+", header.lower()) if w and w not in _HEADER_NOISE]
    known = {*_LISTING_TOOLS, *ACTION_SYNONYMS, *allowed}
    for word in reversed(words):
        if word in known:
            return word
    for word in reversed(words):  # namespace and tool run together
        for name in sorted(known, key=len, reverse=True):
            if len(name) >= 4 and word.endswith(name):
                return name
    return words[-1] if words else ""


def harmony_tool_call(raw: str) -> tuple[str, str, str] | None:
    """(tool name, JSON arguments, analysis text) for a harmony tool call without a final answer."""
    if _HARMONY_FINAL.search(raw):
        return None
    match = _HARMONY_CALL.search(raw)
    if not match:
        return None
    analysis = _HARMONY_ANALYSIS.search(raw)
    return match.group(1), match.group(2).strip(), analysis.group(1).strip() if analysis else ""


def tool_call_as_action(raw: str, allowed: list[str]) -> str | None:
    """Translate a harmony tool call into DFM's `{"action": …}` JSON, or None."""
    call = harmony_tool_call(raw)
    if call is None:
        return None
    name, args_text, analysis = call
    try:
        parsed: Any = json.loads(args_text) if args_text else {}
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    args = cast(dict[str, Any], parsed)
    tool = _tool(name, allowed)
    if isinstance(
        args.get("action"), str
    ):  # the message already is an action, addressed to someone
        action = ACTION_SYNONYMS.get(str(args["action"]), str(args["action"]))
    elif tool in _LISTING_TOOLS:
        path = str(args.get("path") or "").strip().strip("/")
        command = "git ls-files" + (f" -- {shlex.quote(path)}" if path not in {"", "."} else "")
        action, args = "run", {"command": command}
    else:
        action = ACTION_SYNONYMS.get(tool, tool)
    if action not in allowed:
        return None
    normalized: dict[str, Any] = {ARG_SYNONYMS.get(k, k): v for k, v in args.items()}
    normalized.setdefault("reason", (analysis.splitlines() or [action])[-1][:200])
    normalized["action"] = action
    return json.dumps(normalized)


# Qwen answers with markup instead of JSON at times:
#   <done summary="Fixed it.">                       (attributes)
#   <tool_call><function=read><parameter=path>a.py</parameter></function></tool_call>
_TAG_ACTION = re.compile(r"^\s*<([a-z_]+)((?:\s+[a-z_]+\s*=\s*\"[^\"]*\")*)\s*/?>", re.S)
_TAG_ATTR = re.compile(r"([a-z_]+)\s*=\s*\"([^\"]*)\"")
_XML_CALL = re.compile(r"<function=([\w.\-]+)>(.*?)(?:</function>|$)", re.S)
_XML_PARAM = re.compile(r"<parameter=([\w\-]+)>\n?(.*?)\n?</parameter>", re.S)


def tag_action(text: str, allowed: list[str]) -> str | None:
    """Translate a markup action (`<done summary=…>`, Qwen XML tool call) into DFM's JSON, or None."""
    call = _XML_CALL.search(text)
    if call is not None:
        name = call.group(1)
        args = {str(k): str(v) for k, v in _XML_PARAM.findall(call.group(2))}
    else:
        tag = _TAG_ACTION.match(text)
        if tag is None:
            return None
        name = tag.group(1)
        args = {str(k): html.unescape(str(v)) for k, v in _TAG_ATTR.findall(tag.group(2))}
    action = ACTION_SYNONYMS.get(_tool(name, allowed), _tool(name, allowed))
    if action not in allowed:
        return None
    normalized: dict[str, Any] = {ARG_SYNONYMS.get(k, k): v for k, v in args.items()}
    for key in ("start", "end"):
        value = normalized.get(key)
        if isinstance(value, str):
            normalized[key] = int(value) if value.strip().isdigit() else None
    normalized.setdefault("reason", action)
    normalized["action"] = action
    return json.dumps(normalized)


def named_call_action(value: dict[str, Any], allowed: list[str]) -> dict[str, Any] | None:
    """`{"name": "read", "arguments": {...}}` (a JSON tool call) as DFM's action, or None."""
    name, args = value.get("name"), value.get("arguments")
    if "action" in value or not isinstance(name, str) or not isinstance(args, dict):
        return None
    action = ACTION_SYNONYMS.get(_tool(name, allowed), _tool(name, allowed))
    if action not in allowed:
        return None
    normalized: dict[str, Any] = {
        ARG_SYNONYMS.get(str(k), str(k)): v for k, v in cast(dict[Any, Any], args).items()
    }
    normalized.setdefault("reason", action)
    normalized["action"] = action
    return normalized
