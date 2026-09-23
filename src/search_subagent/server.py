from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

mcp = MCPServer("local-code-search")

FINAL_ANSWER_ATTEMPTS = 3
SPAN_KEYS = {"file", "start_line", "end_line"}

REPO_TOOL = {
    "type": "function",
    "function": {
        "name": "repo_command",
        "description": (
            "Run one safe, read-only repository operation: glob lists files, "
            "grep searches text, and read returns a line range."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["glob", "grep", "read"],
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Optional file glob for glob, required regex for grep, "
                        "or required repository-relative path for read."
                    ),
                },
                "file_glob": {
                    "type": "string",
                    "description": "Optional file filter for grep, such as **/*.py.",
                },
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    },
}


class SearchError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3:4b"
    max_steps: int = 8
    max_results: int = 100
    max_output_chars: int = 30_000
    max_file_bytes: int = 1_000_000
    request_timeout_seconds: int = 120

    @classmethod
    def load(cls, path: Path) -> Config:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise SearchError(f"Configuration file does not exist: {path}") from error
        except json.JSONDecodeError as error:
            raise SearchError(f"Configuration file is invalid JSON: {error}") from error
        if not isinstance(data, dict):
            raise SearchError("Configuration must be a JSON object")

        unknown = set(data) - {field.name for field in fields(cls)}
        if unknown:
            raise SearchError(f"Unknown configuration fields: {', '.join(sorted(unknown))}")
        config = cls(**data)
        if not isinstance(config.ollama_url, str) or not config.ollama_url.startswith(
            ("http://", "https://")
        ):
            raise SearchError("ollama_url must use http:// or https://")
        if not isinstance(config.model, str) or not config.model.strip():
            raise SearchError("model must not be empty")
        for name in (
            "max_steps",
            "max_results",
            "max_output_chars",
            "max_file_bytes",
            "request_timeout_seconds",
        ):
            value = getattr(config, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise SearchError(f"{name} must be a positive integer")
        return config


class Repository:
    def __init__(self, root: str, config: Config) -> None:
        candidate = Path(root).expanduser()
        if not candidate.is_absolute():
            raise SearchError("workspace_root must be an absolute path")
        try:
            self.root = candidate.resolve(strict=True)
        except FileNotFoundError as error:
            raise SearchError(f"workspace_root does not exist: {candidate}") from error
        if not self.root.is_dir():
            raise SearchError(f"workspace_root is not a directory: {self.root}")
        if shutil.which("rg") is None:
            raise SearchError("ripgrep (rg) is required but was not found on PATH")
        self.config = config

    def call(self, name: str, arguments: Any) -> str:
        if name != "repo_command":
            raise SearchError(f"Unknown tool: {name}")
        if not isinstance(arguments, dict):
            raise SearchError("Tool arguments must be an object")

        operation = self._string(arguments, "operation")
        shapes = {
            "glob": ({"operation"}, {"operation", "query"}),
            "grep": (
                {"operation", "query"},
                {"operation", "query", "file_glob"},
            ),
            "read": (
                {"operation", "query"},
                {"operation", "query", "start_line", "end_line"},
            ),
        }
        if operation not in shapes:
            raise SearchError(f"Unknown repo_command operation: {operation}")
        required, allowed = shapes[operation]
        missing = required - arguments.keys()
        unknown = arguments.keys() - allowed
        if missing:
            raise SearchError(f"Missing arguments: {', '.join(sorted(missing))}")
        if unknown:
            raise SearchError(f"Unknown arguments: {', '.join(sorted(unknown))}")

        if operation == "glob":
            return self._glob(self._string(arguments, "query", required=False))
        if operation == "grep":
            return self._grep(
                self._string(arguments, "query"),
                self._string(arguments, "file_glob", required=False),
            )
        return self._read(
            self._string(arguments, "query"),
            self._positive_int(arguments, "start_line", 1),
            self._positive_int(arguments, "end_line", 200),
        )

    def _glob(self, pattern: str | None) -> str:
        command = self._rg_base() + ["--files"]
        if pattern:
            command += ["--glob", pattern]
        return self._limit_lines(self._run_rg(command, no_matches_ok=True).stdout)

    def _grep(self, pattern: str, file_glob: str | None) -> str:
        command = self._rg_base() + [
            "--line-number",
            "--column",
            "--no-heading",
            "--color",
            "never",
        ]
        if file_glob:
            command += ["--glob", file_glob]
        command += ["--", pattern, "."]
        return self._limit_lines(self._run_rg(command, no_matches_ok=True).stdout)

    def _read(self, path: str, start_line: int, end_line: int) -> str:
        if end_line < start_line:
            raise SearchError("Line range must satisfy start_line <= end_line")
        file_path = self._resolve_file(path)
        if file_path.stat().st_size > self.config.max_file_bytes:
            raise SearchError(f"File exceeds {self.config.max_file_bytes} bytes: {path}")
        data = file_path.read_bytes()
        if b"\x00" in data:
            raise SearchError(f"Refusing to read binary file: {path}")
        lines = data.decode("utf-8", errors="replace").splitlines()
        selected = lines[start_line - 1 : end_line]
        output = "\n".join(
            f"{number}:{line}" for number, line in enumerate(selected, start=start_line)
        )
        return self._limit_chars(output) or "(requested range is empty)"

    def _run_rg(
        self,
        command: list[str],
        *,
        no_matches_ok: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                command,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise SearchError("Search exceeded the 30 second timeout") from error
        except OSError as error:
            raise SearchError(f"Could not run ripgrep: {error}") from error
        if result.returncode not in ({0, 1} if no_matches_ok else {0}):
            raise SearchError(result.stderr.strip() or f"ripgrep exited {result.returncode}")
        return result

    def _resolve_file(self, path: str) -> Path:
        candidate = Path(path)
        if candidate.is_absolute():
            raise SearchError("read path must be relative to workspace_root")
        try:
            resolved = (self.root / candidate).resolve(strict=True)
        except FileNotFoundError as error:
            raise SearchError(f"File does not exist: {path}") from error
        if not resolved.is_relative_to(self.root) or not resolved.is_file():
            raise SearchError(f"Path is not a file inside workspace_root: {path}")
        return resolved

    def _limit_lines(self, text: str) -> str:
        lines = text.splitlines()
        output = "\n".join(lines[: self.config.max_results])
        if len(lines) > self.config.max_results:
            output += f"\n... truncated after {self.config.max_results} results"
        return self._limit_chars(output) or "(no matches)"

    def _limit_chars(self, text: str) -> str:
        if len(text) <= self.config.max_output_chars:
            return text
        return text[: self.config.max_output_chars] + "\n... output truncated"

    @staticmethod
    def _rg_base() -> list[str]:
        return ["rg", "--hidden", "--no-require-git", "--glob", "!.git/**"]

    @staticmethod
    def _string(
        arguments: dict[str, Any],
        name: str,
        *,
        required: bool = True,
    ) -> str | None:
        value = arguments.get(name)
        if value is None and not required:
            return None
        if not isinstance(value, str) or not value:
            raise SearchError(f"{name} must be a non-empty string")
        return value

    @staticmethod
    def _positive_int(arguments: dict[str, Any], name: str, default: int) -> int:
        value = arguments.get(name, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise SearchError(f"{name} must be a positive integer")
        return value


def _load_system_prompt(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise SearchError(f"Agent definition does not exist: {path}") from error
    parts = text.split("---", 2)
    if len(parts) != 3 or parts[0].strip():
        raise SearchError("Agent definition must start with YAML frontmatter")
    allowed_tools = [
        line.lstrip()[2:].strip()
        for line in parts[1].splitlines()
        if line.lstrip().startswith("- ")
    ]
    if allowed_tools != ["repo_command"]:
        raise SearchError("Agent definition must allow only repo_command")
    prompt = parts[2].strip()
    if not prompt:
        raise SearchError("Agent definition system prompt must not be empty")
    return prompt


def _chat_sync(
    config: Config,
    messages: list[dict[str, Any]],
    *,
    use_tools: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "stream": False,
        "think": True,
    }
    if use_tools:
        payload["tools"] = [REPO_TOOL]
    request = urllib.request.Request(
        f"{config.ollama_url.rstrip('/')}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - URL is explicit configuration
            request,
            timeout=config.request_timeout_seconds,
        ) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        if error.code == 404:
            raise SearchError(
                f"Ollama does not have model {config.model!r}; run: ollama pull {config.model}"
            ) from error
        raise SearchError(f"Ollama returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise SearchError(f"Cannot connect to Ollama at {config.ollama_url}") from error
    except TimeoutError as error:
        raise SearchError(
            f"Ollama request timed out after {config.request_timeout_seconds} seconds"
        ) from error

    try:
        message = data["message"]
    except (KeyError, TypeError) as error:
        raise SearchError("Ollama returned a malformed chat response") from error
    if not isinstance(message, dict):
        raise SearchError("Ollama returned a malformed message")
    message.pop("thinking", None)
    return message


async def _chat(
    config: Config,
    messages: list[dict[str, Any]],
    *,
    use_tools: bool,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _chat_sync,
        config,
        messages,
        use_tools=use_tools,
    )


def _final_answer(content: str) -> str:
    if "</think>" in content:
        content = content.rsplit("</think>", 1)[1]
    if "<think>" in content:
        content = content.split("<think>", 1)[0]
    return content.strip()


def _validated_final_answer(content: str, repository: Repository) -> str:
    answer = _final_answer(content)
    if not answer:
        raise SearchError("Final answer is empty")
    try:
        value = json.loads(answer)
    except json.JSONDecodeError as error:
        raise SearchError(f"Final answer is not valid JSON: {error.msg}") from error
    if not isinstance(value, list):
        raise SearchError("Final answer must be a JSON array")

    spans: list[dict[str, str | int]] = []
    for index, span in enumerate(value):
        location = f"span {index}"
        if not isinstance(span, dict):
            raise SearchError(f"{location} must be an object")
        keys = set(span)
        if keys != SPAN_KEYS:
            missing = SPAN_KEYS - keys
            unknown = keys - SPAN_KEYS
            details = []
            if missing:
                details.append(f"missing {', '.join(sorted(missing))}")
            if unknown:
                details.append(f"unknown {', '.join(sorted(unknown))}")
            raise SearchError(f"{location} has invalid fields: {'; '.join(details)}")

        file = span["file"]
        start_line = span["start_line"]
        end_line = span["end_line"]
        if not isinstance(file, str) or not file:
            raise SearchError(f"{location}.file must be a non-empty string")
        candidate = Path(file)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise SearchError(f"{location}.file must be a repository-relative path")
        repository._resolve_file(file)
        for name, number in (("start_line", start_line), ("end_line", end_line)):
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                raise SearchError(f"{location}.{name} must be a positive integer")
        if end_line < start_line:
            raise SearchError(f"{location} must satisfy start_line <= end_line")
        spans.append(
            {
                "file": file,
                "start_line": start_line,
                "end_line": end_line,
            }
        )
    return json.dumps(spans, ensure_ascii=False, separators=(",", ":"))


def _format_correction(error: SearchError) -> str:
    return (
        f"Your final response was invalid: {error}. "
        "Return ONLY a valid JSON array of objects with exactly these fields: "
        '"file" (repository-relative path), "start_line" (positive integer), '
        'and "end_line" (positive integer not smaller than start_line). '
        "Do not include prose or a Markdown fence."
    )


async def _search(
    config: Config,
    system_prompt: str,
    query: str,
    workspace_root: str,
    max_steps: int | None,
) -> str:
    if not query.strip():
        raise SearchError("query must not be empty")
    repository = Repository(workspace_root, config)
    step_limit = config.max_steps if max_steps is None else max_steps
    if not isinstance(step_limit, int) or isinstance(step_limit, bool) or step_limit < 1:
        raise SearchError("max_steps must be a positive integer")
    step_limit = min(step_limit, 20)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"Repository root: {repository.root}\n"
                f"Question: {query.strip()}\n"
                "Investigate with tools, then answer from evidence."
            ),
        },
    ]
    used_tool = False
    for _ in range(step_limit):
        message = await _chat(config, messages, use_tools=True)
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            if not used_tool:
                messages.append(
                    {
                        "role": "user",
                        "content": "Call repo_command before answering.",
                    }
                )
                continue
            content = message.get("content")
            try:
                if not isinstance(content, str):
                    raise SearchError("The model returned non-text final content")
                return _validated_final_answer(content, repository)
            except SearchError as error:
                messages.append({"role": "user", "content": _format_correction(error)})
                continue
        if not isinstance(calls, list):
            raise SearchError("The model returned malformed tool_calls")

        for call in calls:
            name = ""
            try:
                function = call["function"]
                name = function["name"]
                arguments = function.get("arguments", {})
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                result = repository.call(name, arguments)
            except (KeyError, TypeError, json.JSONDecodeError, SearchError) as error:
                result = f"Tool error: {error}"
            messages.append(
                {
                    "role": "tool",
                    "tool_name": name or "unknown",
                    "content": result,
                }
            )
        used_tool = True

    if not used_tool:
        raise SearchError("The model did not inspect the repository")
    messages.append(
        {
            "role": "user",
            "content": (
                "Give the final answer using only the collected evidence. "
                "Return only the required JSON array of source spans."
            ),
        }
    )
    last_error = SearchError("The model did not produce a final answer")
    for _ in range(FINAL_ANSWER_ATTEMPTS):
        message = await _chat(config, messages, use_tools=False)
        messages.append(message)
        content = message.get("content")
        if not isinstance(content, str):
            last_error = SearchError("The model returned non-text final content")
        else:
            try:
                return _validated_final_answer(content, repository)
            except SearchError as error:
                last_error = error
        messages.append({"role": "user", "content": _format_correction(last_error)})
    raise SearchError(
        f"The model did not produce a valid final answer after "
        f"{FINAL_ANSWER_ATTEMPTS} attempts: {last_error}"
    )


_config: Config | None = None
_system_prompt: str | None = None


@mcp.tool(
    title="Local Code Search Subagent",
    description=(
        "Delegate broad, iterative repository investigation to a read-only local Qwen subagent. "
        "Returns a JSON array of repository-relative source spans."
    ),
)
async def search_codebase(
    query: str,
    workspace_root: str,
    max_steps: int | None = None,
) -> str:
    """Investigate a local codebase with a lightweight read-only subagent.

    Args:
        query: What to find or explain.
        workspace_root: Absolute path to the repository root.
        max_steps: Optional tool-call round limit (1-20).
    """
    try:
        config, system_prompt = _state()
        return await _search(
            config,
            system_prompt,
            query,
            workspace_root,
            max_steps,
        )
    except ValueError as error:
        raise ToolError(str(error)) from error


def _state() -> tuple[Config, str]:
    if _config is None or _system_prompt is None:
        raise SearchError("Server configuration has not been loaded")
    return _config, _system_prompt


def main() -> None:
    parser = argparse.ArgumentParser(description="Local code-search MCP server")
    default_path = Path(os.environ.get("PLUGIN_ROOT", Path.cwd())) / "config.json"
    parser.add_argument("--config", type=Path, default=default_path)
    parser.add_argument("--agent-definition", type=Path)
    args = parser.parse_args()

    definition_path = (
        args.agent_definition
        if args.agent_definition is not None
        else args.config.resolve().parent / "agents" / "search_subagent.md"
    )

    global _config, _system_prompt
    _config = Config.load(args.config)
    _system_prompt = _load_system_prompt(definition_path)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
