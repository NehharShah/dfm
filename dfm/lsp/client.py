"""A small async LSP client: symbol-level truth after RepoKG's module-level navigation.

JSON-RPC 2.0 over stdio with Content-Length framing. Results are normalized to
repo-relative paths and 1-based lines/columns. Rename returns a preview only
(the WorkspaceEdit is never applied here; edits go through FsTools and policy).
Server crashes and timeouts raise `LspError`; callers fall back to text search.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse

from dfm.execution import ExecutionEnvironment
from dfm.tools.languages.base import LspServerSpec
from dfm.tools.results import Diagnostic

SYMBOL_KINDS = {
    1: "file",
    2: "module",
    3: "namespace",
    4: "package",
    5: "class",
    6: "method",
    7: "property",
    8: "field",
    9: "constructor",
    10: "enum",
    11: "interface",
    12: "function",
    13: "variable",
    14: "constant",
    15: "string",
    16: "number",
    17: "boolean",
    18: "array",
    19: "object",
    20: "key",
    21: "null",
    22: "enum_member",
    23: "struct",
    24: "event",
    25: "operator",
    26: "type_parameter",
}


class LspError(Exception):
    pass


@dataclass(frozen=True)
class Location:
    path: str
    line: int  # 1-based
    column: int  # 1-based
    end_line: int
    end_column: int

    def __str__(self) -> str:
        return f"{self.path}:{self.line}:{self.column}"


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    location: Location
    container: str | None = None
    children: list[Symbol] = field(default_factory=lambda: [])


@dataclass(frozen=True)
class CallItem:
    name: str
    kind: str
    location: Location


@dataclass(frozen=True)
class RenamePreview:
    edits: dict[str, list[tuple[Location, str]]]  # path -> [(range, new text)]

    @property
    def files(self) -> list[str]:
        return sorted(self.edits)


def _hover_part(part: Any) -> str:
    if isinstance(part, dict):
        return str(cast(dict[str, Any], part).get("value", ""))
    return str(part)


class LspClient:
    def __init__(
        self,
        spec: LspServerSpec,
        env: ExecutionEnvironment,
        *,
        root: Path | None = None,
        request_timeout: float = 30.0,
        wrap: Sequence[str] = (),
    ) -> None:
        self.spec = spec
        self.env = env
        self.root = (root or env.repo_root).resolve()
        self.timeout = request_timeout
        self.wrap = list(wrap)
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._diagnostics: dict[str, list[dict[str, Any]]] = {}
        self._diag_events: dict[str, asyncio.Event] = {}
        self._versions: dict[str, int] = {}
        self.capabilities: dict[str, Any] = {}
        self.stderr_tail: list[str] = []

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        child_env = self.env.child_env()
        program = shutil.which(self.spec.argv[0], path=child_env.get("PATH")) or self.spec.argv[0]
        argv = [*self.wrap, program, *self.spec.argv[1:]]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.root,
                env=child_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise LspError(f"{self.spec.argv[0]} not found") from exc
        self._reader = asyncio.create_task(self._read_loop())
        asyncio.create_task(self._drain_stderr())  # noqa: RUF006
        result = await self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": self.root.as_uri(),
                "rootPath": str(self.root),
                "workspaceFolders": [{"uri": self.root.as_uri(), "name": self.root.name}],
                "initializationOptions": self.spec.init_options,
                "capabilities": {
                    "textDocument": {
                        "synchronization": {"didSave": False},
                        "definition": {"linkSupport": True},
                        "references": {},
                        "hover": {"contentFormat": ["plaintext", "markdown"]},
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                        "implementation": {"linkSupport": True},
                        "callHierarchy": {},
                        "rename": {"prepareSupport": True},
                        "publishDiagnostics": {"relatedInformation": False},
                    },
                    "workspace": {"workspaceFolders": True, "symbol": {}, "configuration": True},
                    "window": {"workDoneProgress": False},
                },
            },
        )
        init: dict[str, Any] = cast(dict[str, Any], result) if isinstance(result, dict) else {}
        self.capabilities = cast(dict[str, Any], init.get("capabilities", {}))
        await self.notify("initialized", {})

    async def close(self) -> None:
        if self._proc is None:
            return
        with contextlib.suppress(LspError, asyncio.TimeoutError, ConnectionError):
            await asyncio.wait_for(self.request("shutdown", None), 5)
            await self.notify("exit", None)
        if self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self._proc.pid, signal.SIGTERM)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._proc.wait(), 5)
            if self._proc.returncode is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self._proc.pid, signal.SIGKILL)
        if self._reader is not None:
            self._reader.cancel()
        for future in self._pending.values():
            future.cancel()  # nobody is waiting on these any more
        self._pending.clear()
        self._proc = None

    async def __aenter__(self) -> LspClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ---------------------------------------------------------------- transport

    async def _send(self, message: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None or self._proc.returncode is not None:
            raise LspError(f"{self.spec.argv[0]} is not running")
        body = json.dumps(message).encode()
        self._proc.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        try:
            await self._proc.stdin.drain()
        except (ConnectionError, BrokenPipeError) as exc:
            raise LspError(f"{self.spec.argv[0]} closed its input") from exc

    async def request(self, method: str, params: Any, *, timeout: float | None = None) -> Any:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout or self.timeout)
        except TimeoutError as exc:
            raise LspError(f"{method} timed out") from exc
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Any) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None  # noqa: S101
        stdout = self._proc.stdout
        try:
            while True:
                length = None
                while True:
                    line = await stdout.readline()
                    if not line:
                        raise LspError(f"{self.spec.argv[0]} exited")
                    text = line.decode(errors="replace").strip()
                    if not text:
                        break
                    if text.lower().startswith("content-length:"):
                        length = int(text.split(":", 1)[1])
                if length is None:
                    continue
                message = cast(dict[str, Any], json.loads(await stdout.readexactly(length)))
                await self._dispatch(message)
        except (LspError, asyncio.IncompleteReadError, ValueError) as exc:
            self._fail_pending(
                exc if isinstance(exc, LspError) else LspError(f"protocol error: {exc}")
            )

    def _fail_pending(self, exc: LspError) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)

    async def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        while line := await self._proc.stderr.readline():
            self.stderr_tail = [*self.stderr_tail[-20:], line.decode(errors="replace").rstrip()]

    async def _dispatch(self, message: dict[str, Any]) -> None:
        if (
            "id" in message
            and ("result" in message or "error" in message)
            and "method" not in message
        ):
            future = self._pending.get(int(message["id"]))
            if future is not None and not future.done():
                if "error" in message:
                    err = cast(dict[str, Any], message["error"])
                    future.set_exception(LspError(f"{err.get('code')}: {err.get('message')}"))
                else:
                    future.set_result(message.get("result"))
            return
        method = message.get("method")
        if method == "textDocument/publishDiagnostics":
            params = cast(dict[str, Any], message.get("params", {}))
            uri = str(params.get("uri"))
            self._diagnostics[uri] = cast(list[dict[str, Any]], params.get("diagnostics", []))
            self._diag_events.setdefault(uri, asyncio.Event()).set()
        elif "id" in message:  # a request from the server: answer conservatively
            result: Any = None
            if method == "workspace/configuration":
                items = cast(
                    list[Any], cast(dict[str, Any], message.get("params", {})).get("items", [])
                )
                result = [None for _ in items]
            elif method == "workspace/workspaceFolders":
                result = [{"uri": self.root.as_uri(), "name": self.root.name}]
            await self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})

    # ---------------------------------------------------------------- documents

    def _uri(self, rel: str) -> str:
        path = self.env.resolve(rel)
        return path.as_uri()

    def _rel(self, uri: str) -> str:
        path = Path(unquote(urlparse(uri).path))
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    async def open(self, rel: str) -> str:
        uri = self._uri(rel)
        text = self.env.resolve(rel).read_text(errors="replace")
        version = self._versions.get(uri, 0) + 1
        self._versions[uri] = version
        if version == 1:
            await self.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": self.spec.language_id,
                        "version": version,
                        "text": text,
                    }
                },
            )
        else:
            await self.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": text}],
                },
            )
        return uri

    def _position(self, uri: str, line: int, column: int) -> dict[str, Any]:
        return {
            "textDocument": {"uri": uri},
            "position": {"line": line - 1, "character": column - 1},
        }

    def _location(self, item: dict[str, Any]) -> Location:
        uri = str(item.get("uri") or item.get("targetUri"))
        rng = cast(
            dict[str, Any],
            item.get("range") or item.get("targetSelectionRange") or item.get("targetRange"),
        )
        start, end = cast(dict[str, int], rng["start"]), cast(dict[str, int], rng["end"])
        return Location(
            self._rel(uri),
            start["line"] + 1,
            start["character"] + 1,
            end["line"] + 1,
            end["character"] + 1,
        )

    def _locations(self, result: Any) -> list[Location]:
        if result is None:
            return []
        items = cast(list[dict[str, Any]], result if isinstance(result, list) else [result])
        return [self._location(i) for i in items]

    # ---------------------------------------------------------------- operations

    async def definition(self, rel: str, line: int, column: int) -> list[Location]:
        uri = await self.open(rel)
        return self._locations(
            await self.request("textDocument/definition", self._position(uri, line, column))
        )

    async def implementation(self, rel: str, line: int, column: int) -> list[Location]:
        uri = await self.open(rel)
        return self._locations(
            await self.request("textDocument/implementation", self._position(uri, line, column))
        )

    async def references(
        self, rel: str, line: int, column: int, *, include_declaration: bool = True
    ) -> list[Location]:
        uri = await self.open(rel)
        params = {
            **self._position(uri, line, column),
            "context": {"includeDeclaration": include_declaration},
        }
        return self._locations(await self.request("textDocument/references", params))

    async def hover(self, rel: str, line: int, column: int) -> str:
        uri = await self.open(rel)
        result = await self.request("textDocument/hover", self._position(uri, line, column))
        if not result:
            return ""
        contents: Any = cast(dict[str, Any], result).get("contents")
        if isinstance(contents, dict):
            return str(cast(dict[str, Any], contents).get("value", ""))
        if isinstance(contents, list):
            return "\n".join(_hover_part(c) for c in cast(list[Any], contents))
        return str(contents or "")

    def _symbol(self, item: dict[str, Any], uri: str, container: str | None) -> Symbol:
        if "location" in item:
            location = self._location(cast(dict[str, Any], item["location"]))
        else:
            location = self._location(
                {"uri": uri, "range": item.get("selectionRange") or item["range"]}
            )
        children = [
            self._symbol(c, uri, str(item["name"]))
            for c in cast(list[dict[str, Any]], item.get("children", []))
        ]
        return Symbol(
            str(item["name"]),
            SYMBOL_KINDS.get(int(item.get("kind", 0)), "unknown"),
            location,
            cast(str | None, item.get("containerName")) or container,
            children,
        )

    async def document_symbols(self, rel: str) -> list[Symbol]:
        uri = await self.open(rel)
        result = await self.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        return [self._symbol(i, uri, None) for i in cast(list[dict[str, Any]], result or [])]

    async def workspace_symbols(self, query: str) -> list[Symbol]:
        result = await self.request("workspace/symbol", {"query": query})
        return [
            self._symbol(i, str(cast(dict[str, Any], i.get("location", {})).get("uri", "")), None)
            for i in cast(list[dict[str, Any]], result or [])
        ]

    async def _call_items(self, rel: str, line: int, column: int) -> list[dict[str, Any]]:
        uri = await self.open(rel)
        return cast(
            list[dict[str, Any]],
            await self.request(
                "textDocument/prepareCallHierarchy", self._position(uri, line, column)
            )
            or [],
        )

    async def incoming_calls(self, rel: str, line: int, column: int) -> list[CallItem]:
        out: list[CallItem] = []
        for item in await self._call_items(rel, line, column):
            for call in cast(
                list[dict[str, Any]],
                await self.request("callHierarchy/incomingCalls", {"item": item}) or [],
            ):
                src = cast(dict[str, Any], call["from"])
                out.append(
                    CallItem(
                        str(src["name"]),
                        SYMBOL_KINDS.get(int(src.get("kind", 0)), "unknown"),
                        self._location({"uri": src["uri"], "range": src["selectionRange"]}),
                    )
                )
        return out

    async def outgoing_calls(self, rel: str, line: int, column: int) -> list[CallItem]:
        out: list[CallItem] = []
        for item in await self._call_items(rel, line, column):
            for call in cast(
                list[dict[str, Any]],
                await self.request("callHierarchy/outgoingCalls", {"item": item}) or [],
            ):
                dst = cast(dict[str, Any], call["to"])
                out.append(
                    CallItem(
                        str(dst["name"]),
                        SYMBOL_KINDS.get(int(dst.get("kind", 0)), "unknown"),
                        self._location({"uri": dst["uri"], "range": dst["selectionRange"]}),
                    )
                )
        return out

    async def rename_preview(
        self, rel: str, line: int, column: int, new_name: str
    ) -> RenamePreview:
        uri = await self.open(rel)
        result = cast(
            dict[str, Any] | None,
            await self.request(
                "textDocument/rename", {**self._position(uri, line, column), "newName": new_name}
            ),
        )
        edits: dict[str, list[tuple[Location, str]]] = {}
        if result:
            for doc_uri, changes in cast(
                dict[str, list[dict[str, Any]]], result.get("changes", {}) or {}
            ).items():
                for change in changes:
                    loc = self._location({"uri": doc_uri, "range": change["range"]})
                    edits.setdefault(loc.path, []).append((loc, str(change["newText"])))
            for doc_change in cast(list[dict[str, Any]], result.get("documentChanges", []) or []):
                doc = cast(dict[str, Any], doc_change.get("textDocument", {}))
                for change in cast(list[dict[str, Any]], doc_change.get("edits", [])):
                    loc = self._location({"uri": doc.get("uri"), "range": change["range"]})
                    edits.setdefault(loc.path, []).append((loc, str(change["newText"])))
        return RenamePreview(edits)

    async def diagnostics(self, rel: str, *, wait: float = 10.0) -> list[Diagnostic]:
        """Diagnostics the server publishes for a file (pushed after open/change)."""
        uri = self._uri(rel)
        event = self._diag_events.setdefault(uri, asyncio.Event())
        event.clear()
        await self.open(rel)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), wait)
        severities = {1: "error", 2: "warning", 3: "note", 4: "note"}
        out: list[Diagnostic] = []
        for d in self._diagnostics.get(uri, []):
            start = cast(dict[str, int], cast(dict[str, Any], d["range"])["start"])
            code = d.get("code")
            out.append(
                Diagnostic(
                    severities.get(int(d.get("severity", 1)), "error"),  # type: ignore[arg-type]
                    str(d.get("message", "")),
                    self._rel(uri),
                    start["line"] + 1,
                    start["character"] + 1,
                    str(code) if code is not None else None,
                    self.spec.argv[0],
                )
            )
        return out
