# Local Code Search

A lightweight demonstration of using [Agent Plugins v1](https://agent-plugins.org/specification)
to package a search subagent for easy maintenance, sharing across teams, and use across agent
harnesses.

## How it works

1. Codex calls `search_codebase` with a question and an absolute workspace path.
2. The MCP server asks Qwen to investigate the workspace.
3. Qwen can call only `repo_command`, which supports:
   - `glob` to list files with `rg --files`;
   - `grep` to search text with `rg`;
   - `read` to return a bounded section of a text file.
4. The server validates Qwen's final answer and returns repository-relative source spans.

The subagent has no shell or write tool. Paths are resolved against the supplied workspace
root, reads cannot escape that root, and file reads and returned tool output are bounded.

## Requirements

- Codex 0.147.0 or newer
- [uv](https://docs.astral.sh/uv/)
- [Ollama](https://ollama.com/)
- [ripgrep](https://github.com/BurntSushi/ripgrep)

On macOS:

```sh
brew install ollama ripgrep uv
ollama serve
```

Keep `ollama serve` running. In another terminal, download the configured model:

```sh
ollama pull qwen3:4b
```

## Install in Codex

The repository includes a local development marketplace at
`.agents/plugins/marketplace.json`. Register it from the repository root, then install the
plugin:

```sh
codex plugin marketplace add "$(pwd)"
codex plugin add local-code-search@local-code-search-dev
```

Start a new Codex session in the repository you want to search and open `/mcp`.
`local-code-search` and its `search_codebase` tool should be listed.

To uninstall it:

```sh
codex plugin remove local-code-search@local-code-search-dev
codex plugin marketplace remove local-code-search-dev
```

## Tool contract

`search_codebase` accepts:

- `query`: the code question to investigate;
- `workspace_root`: an absolute path to the repository;
- `max_steps`: an optional tool-round budget from 1 to 20.

It returns a JSON array of source spans:

```json
[{"file":"src/example.py","start_line":10,"end_line":25}]
```

The server rejects invalid paths and malformed model output. It asks the model to correct an
invalid answer and returns an MCP tool error if no valid answer is produced.

## Configuration

Edit `config.json` to change the Ollama endpoint, model, tool-round budget, result and file
limits, or request timeout. Changes take effect the next time the MCP server starts.

The system prompt and tool allowlist are in `agents/search_subagent.md`. This is internal MCP
server configuration: Agent Plugins v1 does not define a portable custom-agent component.

The MCP definition stores uv's environment and download cache under the host-provided
`PLUGIN_DATA` directory. It does not download the Ollama model.

## Repository layout

- `.agents/plugins/marketplace.json`: local Codex marketplace used for development
- `agents/search_subagent.md`: Qwen system prompt and tool allowlist, an internal extension
  inspired by Devin rather than a portable Agent Plugins v1 component
- `config.json`: Ollama MCP server configuration
- `mcp.json`: local stdio MCP server registration (part of the Agent Plugins v1 specification)
- `plugin.json`: Agent Plugins v1 metadata (part of the Agent Plugins v1 specification)
- `src/search_subagent/`: MCP server implementation

The `_search()` loop in `src/search_subagent/server.py` is similar to JetBrains'
[AgenticSearchRunner.kt](https://github.com/JetBrains/jetbrains-ai-platform/blob/main/indexing/indexing-cli/src/main/kotlin/ai/grazie/indexing/code/cli/agentic/loop/AgenticSearchRunner.kt).


