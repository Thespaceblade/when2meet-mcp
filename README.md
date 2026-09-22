# when2meet-mcp

MCP server that reads and fills [when2meet](https://www.when2meet.com/) scheduling polls over plain HTTP — no browser automation, no OAuth.

Built for use with Cursor, Claude Desktop, or any MCP client. Pair it with a calendar MCP to auto-mark your availability from Google Calendar, Outlook, or a weekly class schedule.

## What it does

Paste a when2meet link and ask your agent:

> Fill this poll from my calendar — skip mornings before 10

The server will:

1. **Load the poll** — slot grid, participants, local time range
2. **Compute free slots** from busy blocks (weekly schedule or ISO calendar intervals)
3. **Preview** (`dry_run=true` by default)
4. **Submit** a positional availability bitmask to when2meet

## How it works

when2meet has no public API, but the participant UI uses three simple POST endpoints:

| Endpoint | Purpose |
|----------|---------|
| `AvailabilityGrids.php` | Read slot grid + group availability |
| `ProcessLogin.php` | Sign in with name (+ optional password) → person ID |
| `SaveTimes.php` | Write full `0`/`1` availability string |

This MCP wraps those calls with validation, timezone handling, and safety defaults.

## Requirements

- **Python 3.11+**
- Network access to `when2meet.com`

## Install

```bash
git clone <repo-url>
cd when2meet-mcp
pip install -e .
```

Optional dev dependencies:

```bash
pip install pytest
pytest
```

## Cursor / Claude MCP config

Add to `~/.cursor/mcp.json` (or Claude Desktop config). Set `cwd` to wherever you cloned the repo:

```json
{
  "mcpServers": {
    "when2meet": {
      "command": "python3",
      "args": ["-m", "when2meet_mcp.server"],
      "cwd": "/Users/jasoncharwin/Personal Code Projects/MCPs/when2meet-mcp",
      "env": {
        "WHEN2MEET_AGENT_NAME": "Composer"
      }
    }
  }
}
```

`WHEN2MEET_AGENT_NAME` is the label used when submitting under an alternate name (e.g. `Jason Charwin (Composer)` when the main entry has a password). Set it to `Claude`, `GPT-4`, `Gemini`, etc. Defaults to `Cursor`.

See [`mcp.json.example`](mcp.json.example) for a copy-paste template.

## Claude Desktop (.mcpb)

Package for one-click install:

```bash
./scripts/pack-mcpb.sh
```

Then double-click `when2meet-mcp.mcpb` or drag it into Claude Desktop Settings. Uses the UV runtime — no manual `pip install` needed.

## Tools

| Tool | Description |
|------|-------------|
| `get_poll` | Poll metadata, participants, time range, inferred week start |
| `fill_from_weekly_schedule` | Fill from recurring `{day, start, end}` busy blocks (best for class schedules) |
| `compute_availability_from_busy_times` | Fill from ISO calendar busy intervals + optional all-day dates |
| `preview_availability` | Validate slot indices before submitting |
| `submit_availability` | Sign in and save availability |
| `find_common_availability` | Find overlapping free times across participants |

## Example workflows

### Weekly class schedule

No calendar API needed — match by weekday + local time from poll labels:

```
fill_from_weekly_schedule(
  url="https://www.when2meet.com/?12345678-AbCdE",
  name="Alex Kim",
  on_name_conflict="alternate_suffix",
  busy_blocks=[
    {"day": "Monday", "start": "09:00", "end": "10:15"},
    {"day": "Monday", "start": "10:30", "end": "15:00"},
    {"day": "Wednesday", "start": "09:00", "end": "10:15"},
  ],
  buffer_minutes=5,
  block_before="10:00",
  dry_run=true
)
```

### Calendar MCP integration

```
1. get_poll(url)
2. [calendar MCP] → busy_times as ISO intervals
3. compute_availability_from_busy_times(
     url=url,
     busy_times=[{"start": "2026-03-09T14:00:00-04:00", "end": "2026-03-09T15:00:00-04:00"}],
     all_day_dates=["2026-03-10"],
     buffer_minutes=5,
   )
4. submit_availability(url, name, slot_indices, dry_run=true)
5. submit_availability(..., dry_run=false, password="...")
```

## Edge cases handled

- **Wrong password on existing name** → `on_name_conflict="alternate_suffix"` submits as `Name (Agent)`
- **15-minute slot boundaries** → overlap-based matching
- **Buffer time** → `buffer_minutes` expands busy blocks
- **Daily limits** → `block_before` / `block_after`
- **All-day events** → `all_day_dates`
- **Accidental submit** → `dry_run=true` default on write tools
- **Invalid slot indices** → validated with warnings
- **Full positional bitmask** → always sends complete `0`/`1` string (required by when2meet)
- **Unmarked participants** → excluded from overlap by default

## Limitations

- when2meet's HTTP interface is **undocumented** and may change
- Cannot **delete** participant rows via API (only clear availability)
- Poll grid does not expose **absolute calendar dates** on the participant page
- Event times are stored in **UTC**; evening slots may be outside the grid in your local timezone
- Password-protected participant names require the user to provide the password

## Project layout

```
when2meet-mcp/
├── when2meet_mcp/
│   ├── client.py      # HTTP client + poll parsing
│   ├── scheduling.py  # Slot matching, validation, warnings
│   ├── auth.py        # Login + password fallback
│   ├── agent.py       # Agent name attribution
│   └── server.py      # MCP tool definitions
├── tests/
├── pyproject.toml
└── mcp.json.example
```

## Run the server manually

```bash
python3 -m when2meet_mcp.server
```

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

This project is not affiliated with when2meet. It uses reverse-engineered HTTP endpoints for personal automation. Use responsibly.
