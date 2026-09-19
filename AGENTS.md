# Project Agent Instructions

## Collaboration and ownership

- For all future development work and file modifications, delegate implementation to a subagent whenever subagent execution is available. The root agent is the high-level technical lead: it owns planning, task decomposition, coordination, review, integration, verification, and status reporting.
- The root agent may perform read-only inspection and the necessary Git integration steps, including staging, committing, and pushing changes. Implementation changes should be made by the delegated subagent, unless a tool limitation makes that impossible and the user explicitly approves an exception.
- Prefer delegating implementation to `gpt-5.6-luna` with `max` reasoning when that model/reasoning combination is available. Do not use the Terra model for this project.
- Keep delegated work scoped, independently verifiable, and reported back to the root agent before integration. Do not claim completion without running appropriate validation.

## Codebase discovery

This project uses codebase-memory-mcp to maintain a knowledge graph of the codebase. Prefer MCP graph tools over grep/glob/file-search for code discovery.

Priority order:

1. `search_graph` — find functions, classes, routes, and variables by pattern
2. `trace_path` — trace who calls a function or what it calls
3. `get_code_snippet` — read specific function/class source code
4. `query_graph` — run Cypher queries for complex patterns
5. `get_architecture` — high-level project summary

Fall back to `rg`/glob for string literals, error messages, configuration values, non-code files, or when graph tools are insufficient. Run `index_repository` first if the project is not indexed.

## Communication

The project owner has ADHD. Keep plans, progress updates, blockers, and handoffs concise, concrete, and easy to scan.
