# Slack Pets — Architecture

This document describes the architecture of the **Slack Pets** framework: a system
for running long-lived, configurable, transparent AI agents ("pets") that augment
your Slack experience. It complements [pets-framework.md](pets-framework.md) (the
user/author guide) with the *why* and *how it fits together*.

---

## 1. The core idea in one sentence

> A **pet** is a headless `claude -p` agent whose **behavior tree** is a `.claude/skills/`
> directory + a `CLAUDE.md` persona, with a **configurable grant** of memory, MCP servers,
> and Slack tools — and the framework is the supervisor, spec format, lifecycle, and safety
> rails around running these agents in response to Slack events.

The key inversion from the original clandestine bot: behavior is **not** baked into a shell
script. It lives in declarative specs + skill trees the model routes through. The framework
just decides *which* pet wakes up, runs it safely, and records everything.

---

## 2. Layered component view

```mermaid
flowchart TB
    subgraph slack["Slack (Enterprise Grid)"]
        WS["Real-time WebSocket<br/>(client.getWebSocketURL)"]
        API["Web API<br/>(chat.update / postMessage / delete)"]
    end

    subgraph supervisor["Supervisor daemon — ONE process"]
        CONN["WS connect loop<br/>(cookie+origin spoof, backoff)"]
        ENGINE["RulesEngine<br/>legacy rules + compiled pet rules"]
        POOL["actions ThreadPool<br/>(isolates slow runs)"]
        ARCH["archive worker<br/>(SQLite, every message)"]
    end

    subgraph petsys["Pets subsystem (pets/)"]
        SPEC["spec.py<br/>load + validate bot.yml"]
        REG["registry.py<br/>compile triggers + status"]
        RUN["runner.py<br/>mcp-config + allowedTools + prompt"]
        AUD["audit.py<br/>snapshot, record, undo"]
    end

    subgraph pet["A pet invocation (isolated subprocess)"]
        CLAUDE["claude -p<br/>cwd = pet dir"]
        SKILLS[".claude/skills + CLAUDE.md<br/>(the decision tree)"]
        MEM["memory/ (read / read_append)"]
    end

    subgraph mcps["Granted MCP servers (subset of ~/.claude.json)"]
        SB["slack-bridge (this server)"]
        OTHER["any MCP servers you grant<br/>(prometheus, opensearch, glean, ...)"]
    end

    subgraph control["Operator surface (tools/pets.py)"]
        CTL["slack_pet_list / status / enable / disable<br/>/ dry_run / logs / undo"]
    end

    WS --> CONN --> ENGINE
    CONN -.->|every message| ARCH
    ENGINE -->|matched rule kind:pet| POOL
    POOL --> RUN
    REG -->|compiled rules| ENGINE
    SPEC --> REG
    RUN --> CLAUDE
    CLAUDE --- SKILLS
    CLAUDE --- MEM
    CLAUDE -->|tool calls| SB
    CLAUDE -->|tool calls| OTHER
    SB -->|mutations via guard| AUD
    SB --> API
    CTL --> REG
    CTL --> AUD
```

**Reading it:** the supervisor owns the single WebSocket and the rule engine. A pet is just a
rule whose action is `kind: pet`. When it matches, the runner launches an isolated
`claude -p` whose skills decide what to do; its Slack mutations route back through the
**audit guard** before hitting the API.

---

## 3. Event lifecycle — from alert to action

```mermaid
sequenceDiagram
    autonumber
    participant S as Slack WS
    participant D as Supervisor daemon
    participant E as RulesEngine
    participant R as pets.runner
    participant C as claude -p (pet)
    participant G as Mutation guard
    participant A as Slack Web API
    participant J as audit/actions.jsonl

    S->>D: message event ("[FIRING] ServiceDown")
    D->>D: archive event to SQLite (always)
    D->>E: match(event)
    E-->>D: [pet:alert-responder]
    D->>R: run(spec, ctx) on thread pool
    R->>R: build .runtime/mcp.json (granted servers only)
    R->>R: compute allowedTools, prompt, env
    R->>C: spawn claude -p (cwd = pet dir)
    Note over C: loads CLAUDE.md + skills<br/>routes triage to investigate to escalate
    C->>C: read memory, query Prometheus/OpenSearch (read-only)
    C->>G: slack_post_message(channel, thread_ts, text)
    alt dry_run = true
        G->>J: append would_have
        G-->>C: ok, dry_run true (NOTHING sent)
    else live
        G->>A: chat.postMessage (real)
        G->>J: append tool + result_ts
        G-->>C: real result
    end
    C-->>R: exits
    R->>J: log fire marker + rc/duration
```

The pet **posts/edits Slack itself** — the runner never forwards model text to Slack (that
was the original footgun). The model's "output" is the side-effecting tool calls it makes.

---

## 4. Anatomy of a pet (a directory)

```mermaid
flowchart TB
    ROOT["~/.slack-bridge-mcp/bots/NAME/"]
    ROOT --> Y["bot.yml — trigger, capabilities, enabled, dry_run"]
    ROOT --> CM["CLAUDE.md — persona + routing (tree root)"]
    ROOT --> SK[".claude/skills/"]
    ROOT --> M["memory/ — read, or read_append (journal)"]
    ROOT --> AU["audit/actions.jsonl — mutations + original snapshot"]
    ROOT --> L["logs/run.log — claude output + fire markers"]
    ROOT --> RT[".runtime/mcp.json — generated per run"]

    SK --> S1["triage/SKILL.md"]
    SK --> S2["investigate-known/SKILL.md"]
    SK --> S3["escalate/SKILL.md"]
```

`bot.yml` is the only thing the framework parses. `CLAUDE.md` + `.claude/skills/` are loaded
by Claude Code automatically because the pet runs with `cwd` set to this directory — so the
"tree of skills that routes behavior" *is* the native skills mechanism.

---

## 5. How a trigger becomes a live rule

```mermaid
flowchart LR
    A["bot.yml<br/>trigger + enabled + ignore_self<br/>+ rate_limit_per_min"]
    A -->|spec.parse_spec| B["BotSpec (validated)"]
    B -->|registry.compile_rule| C["rule dict<br/>name: pet:NAME<br/>match: trigger<br/>actions: kind=pet"]
    C -->|RulesEngine.maybe_reload| D["merged rule set<br/>legacy + enabled pet rules"]
    D -->|match(event)| E["fire to kind:pet action to runner.run"]
    F["watcher-rules.yml<br/>(legacy shell/webhook rules)"] -->|_load_legacy| D
```

`maybe_reload()` reloads when **either** source changes: the legacy file's mtime, or the max
mtime across all `bot.yml` files (`registry.signature()`). So `slack_pet_enable/disable`
(which patches `enabled` in `bot.yml`) is picked up automatically — typically within ~50
events.

---

## 6. The safety model — mutation guard + dry-run + undo

Every Slack mutation funnels through one chokepoint: `tools.dispatch`. This is what makes
"auto-edit in place" safe and pets transparent.

```mermaid
flowchart TD
    START["pet calls a slack_* tool"] --> Q1{"inside a pet?<br/>(PET env set)"}
    Q1 -->|no| RAW["normal dispatch to Slack API"]
    Q1 -->|yes| Q2{"mutating tool?<br/>(post/update/delete/react)"}
    Q2 -->|no, read-only| RAW
    Q2 -->|yes| SNAP{"edit or delete?"}
    SNAP -->|yes| FETCH["fetch + snapshot ORIGINAL text"]
    SNAP -->|no| REC0["build audit record"]
    FETCH --> REC0
    REC0 --> Q3{"dry_run?"}
    Q3 -->|yes| DRY["append would_have<br/>return ok, NOTHING sent"]
    Q3 -->|no| DO["execute via Slack API<br/>append result_ts"]
    DO --> DONE["done"]
    DRY --> DONE
    UNDO["slack_pet_undo NAME TS"] -->|replay audit backward| REV["restore edited /<br/>repost deleted /<br/>delete posted"]
```

Guarantees this gives you:

- **Reversible**: edits/deletes snapshot the original first → `slack_pet_undo` restores it.
- **Vettable**: `dry_run` records exactly what a pet *would* do without touching Slack.
- **Transparent**: `audit/actions.jsonl` is the full, per-pet record; `slack_pet_logs` tails it.
- **Scoped**: a pet only gets the MCP servers + tools its `bot.yml` grants — nothing else.
- **Isolated**: each invocation is its own subprocess; a hang/crash can't take down the fleet.

---

## 7. Pet lifecycle states

```mermaid
stateDiagram-v2
    [*] --> Disabled: pet directory created
    Disabled --> DryRun: enable + dry_run on
    DryRun --> Live: dry_run off
    Live --> DryRun: dry_run on
    Live --> Disabled: disable
    DryRun --> Disabled: disable
    Disabled --> Live: enable (dry_run off)

    note right of DryRun
        Decides + investigates fully,
        records would_have, sends nothing.
        Recommended first state.
    end note
    note right of Live
        Acts for real. Every mutation
        is audited + reversible.
    end note
```

Recommended onboarding for a new/mutating pet: **Disabled → DryRun → (read the audit) → Live.**

---

## 8. Example pet patterns

Pets are your own runtime data (in `$SLACK_BRIDGE_BOTS_DIR`), not part of this repo.
These patterns map cleanly onto the three archetypes:

```mermaid
flowchart LR
    subgraph responders["archetype: responder"]
        GRE["greeter<br/>DM to reply in-thread"]
        QA["qa-helper<br/>teammate DM to answer<br/>or defer back to you"]
        DBG["alert-responder<br/>alerts channel to<br/>triage / investigate / escalate"]
    end
    subgraph augmenters["archetype: augmenter"]
        EDIT["message-editor<br/>your own msgs to<br/>fix typos in place"]
    end
    subgraph observers["archetype: observer"]
        OBS["timesheet-notes<br/>watch to journal only"]
    end
```

| Pet | Archetype | Trigger | Acts |
|-----|-----------|---------|------|
| `greeter` | responder | a DM or channel | reply in-thread |
| `qa-helper` | responder | a teammate's DM | answer / defer back to you |
| `message-editor` | augmenter | your own msgs in a channel | edit in place (reversible) |
| `alert-responder` | responder | an alerts channel (`FIRING`) | investigate / fix-known / escalate |
| `timesheet-notes` | observer | your after-hours activity | journal only (no Slack write) |

Start every pet disabled + dry-run; vet the audit before going live.

---

## 9. Design decisions & their rationale

| Decision | Why |
|----------|-----|
| One supervisor, many pets (not one daemon per pet) | One WS connection; cheap; pets are configs, not processes. "Running" = enabled spec. |
| Pet = isolated `claude -p` subprocess per fire | Crash-safe, no shared state, natural timeout; the proven model from the original bot. |
| Behavior tree = `.claude/skills/` + `CLAUDE.md` | Reuses Claude Code's native skill routing — no bespoke DSL. The pet *is* a mini Claude Code project. |
| Capabilities are an explicit grant in `bot.yml` | Least privilege: a pet can only reach the MCPs/tools it's given. |
| Single mutation chokepoint in `tools.dispatch` | One place to enforce audit + dry-run for **every** Slack write, including `post_dm` in another module. |
| Snapshot-before-mutate | Makes auto-edit-in-place reversible; without it, undo is impossible. |
| Specs reloaded fresh at fire time | `dry_run`/capability edits take effect immediately, no restart. |
| Supervisor = the existing watcher entrypoint | No second daemon to run; legacy rules + pets coexist over one socket. |

---

## 10. Where things live (source map)

| Path | Role |
|------|------|
| `src/slack_bridge_mcp/pets/spec.py` | `BotSpec` + validation; tool-name resolution |
| `src/slack_bridge_mcp/pets/registry.py` | discover pets, compile rules, status, enable/disable |
| `src/slack_bridge_mcp/pets/runner.py` | build mcp-config + allowedTools + prompt; run `claude -p` |
| `src/slack_bridge_mcp/pets/audit.py` | snapshot, record, undo |
| `src/slack_bridge_mcp/tools/pets.py` | MCP control tools (`slack_pet_*`) |
| `src/slack_bridge_mcp/tools/__init__.py` | central mutation guard + registry wiring |
| `src/slack_bridge_mcp/watcher/daemon.py`, `rules.py`, `actions.py` | supervisor: WS, rule engine (+pets), `kind:pet` action |
| `src/slack_bridge_mcp/config.py` | `bots_dir`, `pets_log_dir` settings |
| `~/.slack-bridge-mcp/bots/NAME/` | the pets themselves (runtime data, not in the repo) |
```
