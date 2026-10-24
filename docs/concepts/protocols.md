# Protocols

ln-agents speaks three protocols. Each has one job, and the orchestrator in `src/agentic_pipeline` stays authoritative: no protocol lets a caller take a graph transition, sign a decision or change policy.

| Protocol | Between | Job | Where |
|---|---|---|---|
| ACP (Agent Client Protocol) | an editor and `ln-agents acp` | Slash commands in the editor; see [Editor commands](../reference/editor-commands.md). | `ln-agents acp` |
| A2A (Agent2Agent) | `ln-agents` and the orchestrator | Start, advance and read runs; see [A2A skills](../reference/a2a-skills.md). | `serve a2a` |
| MCP (Model Context Protocol) | an agent's runtime and a tool server | Give each agent a specific set of tools and data; see [MCP server](../reference/mcp.md). | `serve mcp` |

MCP is where the policy shows most. ln-agents is the MCP **host**: for each agent session it picks the agent's tool set from `policy/tools.yaml` and starts the local project server with only those tools. The server enforces the set on every call, so a model's own tool list or request grants nothing: a tool that is not in the profile does not exist for the caller, repository content is held back from a destination the data classes do not allow, and every call is logged.

What exists today: the server and its policy, the registry fields that name an operation's profile and the artifacts it may fetch (`tool_profile`, `tool_reads`, checked by `validate`), and path rules that give the files of the repository their own classes. No operation uses them yet, and none of the agents' runtimes is given a tool set.
