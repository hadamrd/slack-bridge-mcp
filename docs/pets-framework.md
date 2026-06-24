# Slack Pets — programmable AI agents that augment your Slack

A **pet** is a long-running, configurable, transparent AI agent attached to Slack.
It ingests events you funnel to it, walks a *tree of skills* to decide what the
situation calls for, then acts: reply, stay silent, edit your own messages, run an
investigation, DM you, call an MCP, journal a note — whatever its skills prescribe.

Under the hood each pet is a **headless `claude -p` agent**: its behavior tree is a
`.claude/skills/` directory, its persona is a `CLAUDE.md`, and its capabilities
(memory, MCP servers, Slack mutation tools) are a *configurable grant* in `bot.yml`.
A single supervisor daemon funnels matching Slack events to the right pet, which runs
as an isolated subprocess and acts through its granted tools. Everything is auditable,
reversible, listable, and killable.

## A pet is a directory

```
$SLACK_BRIDGE_BOTS_DIR/<name>/        # default: ~/.slack-bridge-mcp/bots/<name>/
  bot.yml                             # framework config (below)
  CLAUDE.md                           # persona + top-level routing (the decision tree root)
  .claude/skills/<skill>/SKILL.md     # decision-tree branches (answer / defer / edit / …)
  memory/                             # pet reads here; appends when memory: read_append
  audit/actions.jsonl                 # every Slack mutation, with original snapshot (undo source)
  logs/run.log                        # per-pet claude -p output + fire markers
  .runtime/mcp.json                   # generated per run; the pet's scoped MCP config
```

Because `cwd` is the pet directory, Claude Code auto-loads its `CLAUDE.md` and
`.claude/skills/` — so the "tree of skills that routes behavior" is literally the
native skills mechanism.

## `bot.yml` reference

```yaml
name: intern-helper              # defaults to the directory name
enabled: true                    # false = inert (the "kill" switch); hot-reloaded
dry_run: false                   # true = decide + record intended actions, never mutate Slack
archetype: responder             # responder | augmenter | observer (labeling/defaults)
description: "one line shown in slack_pet_list"
trigger:                         # which events wake this pet (rule-engine match schema)
  channel_id: D0123456789        # channel_id | channel_name | from_user | from_bot |
  from_user: U0123456789         # subtype | text_contains | text_regex | is_thread_reply
ignore_self: true                # responders: true; augmenters acting on your own msgs: false
rate_limit_per_min: 4            # cap firings/min (sliding window)
runtime:
  model: claude-opus-4-8         # optional; omit to inherit default
  timeout_s: 180                 # kill the invocation after N seconds
  grounding_dirs:                # extra read-only dirs (--add-dir): repos, memory, catalogs
    - /path/to/some-repo
capabilities:
  memory: read_append            # none | read | read_append (grants Write/Edit scoped to the pet)
  mcp_servers: [slack-bridge, glean, sourcegraph]   # subset of ~/.claude.json wired in
  slack_tools: [post_message, channel_history, react, post_dm]   # which slack-bridge tools
  extra_tools: [Read, Grep, Glob, Skill, WebSearch] # native Claude Code tools
```

`slack_tools` are short names; the runner expands them to
`mcp__slack-bridge__slack_<name>`. Granting a non-`slack-bridge` MCP server unlocks all
its tools (`mcp__<server>__*`). Unknown tool names are rejected at load time.

### Capability → action space

What a pet *can* do is the union of its granted tools. Mix them to get behaviors:

| Want the pet to…            | Grant                                                        |
|-----------------------------|-------------------------------------------------------------|
| reply in a thread           | `slack_tools: [post_message, channel_history]`              |
| edit your own messages      | `slack_tools: [update_message]` (+ `ignore_self: false`)    |
| delete & repost cleaner      | `slack_tools: [delete_message, post_message]`               |
| DM you privately            | `slack_tools: [post_dm]`                                     |
| investigate infra           | `mcp_servers: [prometheus, opensearch, jenkins-agents, …]`  |
| learn over time             | `memory: read_append`                                        |
| read company knowledge      | `mcp_servers: [glean, sourcegraph]`                          |

## Safety model (transparent + reversible)

- **Dry-run** (`dry_run: true`): the pet investigates and decides fully, but every Slack
  mutation is recorded to `audit/actions.jsonl` as `would_have` and **not executed**.
  Vet any new or edited pet in dry-run, read the audit, then go live.
- **Audit + snapshot**: every real mutation appends a record; edits/deletes snapshot the
  **original text first**. This is the transparency surface (`slack_pet_logs`).
- **Undo**: `slack_pet_undo <name> <ts>` reverses the most recent reversible action on a
  message — restores edited text, reposts deleted text, or deletes a posted message.
- **Rate limits + isolation**: per-pet `rate_limit_per_min`; each invocation is its own
  subprocess (a crash or hang can't take down the supervisor or other pets).
- **Scoped capability**: a pet only gets the MCP servers + tools it's granted — nothing else.

## Lifecycle / operating the fleet

Via the slack-bridge MCP tools:

| Tool                              | Does                                                      |
|-----------------------------------|----------------------------------------------------------|
| `slack_pet_list`                  | every pet: enabled, dry_run, trigger, caps, fire stats   |
| `slack_pet_status [name]`         | detail incl. resolved allowed-tools                      |
| `slack_pet_enable` / `_disable`   | flip `enabled` (the kill switch); hot-reloaded           |
| `slack_pet_dry_run {name, on}`    | toggle dry-run                                            |
| `slack_pet_logs {name}`           | tail run log + audit trail                               |
| `slack_pet_undo {name, ts}`       | reverse a mutation                                        |

The **supervisor** is the existing watcher daemon — it now loads both the legacy
`watcher-rules.yml` and all pet specs into one rule engine over one WebSocket. Run it:

```
.venv/bin/python -m slack_bridge_mcp.watcher        # foreground
# or persist via the existing launchd plist (it already runs this entrypoint):
#   launchd/com.example.slack-bridge-watcher.plist  → now supervises pets too
```

Do **not** run a second copy — that would open a second WS and double-fire. One supervisor
handles every pet. "Kill all pets" = stop the supervisor (`pkill -f slack_bridge_mcp.watcher`).
"Kill one pet" = `slack_pet_disable <name>`.

## Write your own pet in 5 minutes

1. `mkdir -p ~/.slack-bridge-mcp/bots/my-pet/.claude/skills/do-the-thing`
2. Write `bot.yml` (copy one from `bots/`); set `trigger`, `capabilities`, keep
   `dry_run: true` to start.
3. Write `CLAUDE.md` — the persona + a routing section that classifies the event and
   picks a skill.
4. Write `.claude/skills/do-the-thing/SKILL.md` — the steps and which tools to call.
5. `slack_pet_list` to confirm it loaded; watch a dry-run via `slack_pet_logs`; then
   `slack_pet_dry_run my-pet off` + `slack_pet_enable my-pet`.

### Example: an `observer` pet (after-hours timesheet notes)

Shows the third archetype — it watches, never replies, just journals:

```yaml
name: timesheet-notes
enabled: false
archetype: observer
description: "When I'm active in work channels outside hours, jot what I was doing."
trigger:
  from_user: U0123456789
  channel_name: your-team-channel
ignore_self: false
rate_limit_per_min: 2
capabilities:
  memory: read_append
  mcp_servers: [slack-bridge]
  slack_tools: [channel_history]     # read-only; no posting
  extra_tools: [Read, Grep, Glob, Skill]
```
Its single skill reads the recent context and appends a dated line to `memory/timesheet.md`
when the event is outside working hours — no Slack mutation at all. Later you (or another
pet) turn that journal into a timesheet.

## Example pet patterns

Pets live in `$SLACK_BRIDGE_BOTS_DIR` (default `~/.slack-bridge-mcp/bots/`), not in
this repo — they're your runtime data. Some patterns worth copying:

- **greeter** — responder that watches a DM or channel and replies in-thread. The
  "hello world" pet.
- **qa-helper** — responder that answers a teammate's technical questions and defers
  hard calls back to you via DM.
- **message-editor** — augmenter that fixes typos/clarity in your *own* messages in
  place (reversible via the audit snapshot). Run it disabled + dry-run first.
- **alert-responder** — responder that watches an alerts channel, runs a skill catalog
  to triage/investigate, fixes known issues where safe, and escalates novel ones to you.
  Always vet in dry-run before enabling.

Start any new pet disabled + `dry_run: true`, read its audit, then go live.
