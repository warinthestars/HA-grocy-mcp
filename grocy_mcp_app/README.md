# Grocy MCP Server (Home Assistant app)

Connect AI assistants (Claude, Home Assistant Assist, Claude Code and any MCP
client) to your [Grocy](https://grocy.info) instance. They can check and
update stock, manage shopping lists, recipes, meal plans, chores, tasks,
batteries and equipment.

- Works with Grocy running as another app on the same Home Assistant, or anywhere on your network
- Streamable HTTP endpoint on port 8100 (`/mcp`) with bearer-token protection
- Web UI in the Home Assistant sidebar: connection status, copy-ready client setup, per-tool on/off switches, library counts, and a list of expired, due and below-minimum stock and overdue chores, tasks and batteries
- amd64 and aarch64

Open the **Documentation** tab for step-by-step setup, including connecting
Claude Desktop and Claude custom connectors.
