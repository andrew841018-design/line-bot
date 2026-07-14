# Fix LINE bot no-reply regression

## Intent

Restore the inbound text path so a LINE webhook event can reach the Claude-first
reply pipeline instead of returning HTTP 200 after a SQLite open failure.

## Evidence to reproduce

- `line_bot_uvicorn.log` records `memory.log_raw_message` failing with
  `sqlite3.OperationalError: unable to open database file`.
- `SQLITE_PATH=line_bot.db` is relative and the service is launchd-managed.
- `line_bot/.venv/bin/python` points to a removed Homebrew Python 3.13 path.

## Scope

1. Resolve the default relative SQLite path from the bot module directory while
   preserving explicit absolute paths used by tests and deployments.
2. Add regression coverage for relative-path resolution and connection from a
   different working directory.
3. Restore a working launchd interpreter/runtime, restart the local service, and
   verify both the webhook handler path and `/health`.

## Acceptance criteria

- A relative `SQLITE_PATH` resolves to the bot directory, not the process cwd.
- Existing absolute test/database paths remain unchanged.
- Relevant tests pass.
- launchd runs a valid interpreter and a synthetic inbound text event reaches
  the reply path without the SQLite error.
