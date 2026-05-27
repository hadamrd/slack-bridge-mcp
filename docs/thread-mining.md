# Thread Mining

Thread mining is a workflow built on top of the local archive: search for a
topic, group matching messages by thread, and export readable Markdown for
later analysis.

Example query:

```text
slack_archive_search(query="release incident", since="2026-05-01", limit=50)
```

Example exported thread format:

```markdown
# Thread - #engineering - 2026-05-12 11:35:00

> permalink: https://your-workspace.slack.com/archives/C0123456789/p1778585700000000
> participants: Alice Example, build-bot
> messages: 14

**11:35:00 build-bot**
Release validation failed for service-api.

**11:38:14 Alice Example**
Investigating the failing health check.
```

The current codebase exposes the lower-level archive search and thread tools.
A higher-level export tool can be added without changing the archive schema.
