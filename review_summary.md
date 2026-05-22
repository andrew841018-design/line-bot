STATUS: ok

## Critical / Important
*   **`calendar_db.py` - Migration Safety**: The `PRAGMA pre-check + conditional ALTER` for adding `event_type` column is well-designed to mitigate race conditions across multiple processes (`uvicorn` workers and `event_reminder.py` launchd job). The handling of `sqlite3.OperationalError` for "duplicate column" and "database is locked" is robust. The final `assert "event_type" in cols` ensures the migration's success.
*   **`main.py` - Calendar Query Determinism**: The calendar query path (`_is_calendar_query` and `_handle_calendar_query`) is correctly implemented as a deterministic path that bypasses general LLM invocation, respecting the GP2 feedback and ensuring functionality even under Gemini quota exhaustion.
*   **`calendar_extractor.py` - Event Type Whitelisting**: The `_normalize` function effectively whitelist-validates `event_type` from the LLM response, defaulting to `family_gathering` for invalid types. This prevents malformed data from reaching the database, addressing a critical feedback point.

## Code quality nits
*   **`calendar_db.py` - `search_by_keyword` ordering**: While functional and correct, the concatenation of two separate SQL queries in Python to achieve "future ASC then past DESC" ordering could potentially be refactored into a single `UNION ALL` query with a more complex `ORDER BY` clause for a purely SQL-centric approach. However, given SQLite's characteristics and the specific sorting needs, the current approach is clear and acceptable.
*   **`calendar_regex.py` - `_TYPE_PATTERNS` structure**: The use of `tuple[tuple[str, re.Pattern], ...]` is idiomatic and clearly conveys the ordered priority for event type classification. It's a clean and effective solution for this specific problem.

## Better alternatives
*   None significant. The chosen implementations are generally solid and address prior feedback well.

## Verified safe areas
*   **`calendar_db.py`**:
    *   `EVENT_TYPES` constant and `_validate_event_type` ensure type safety.
    *   `_escape_like` correctly handles SQL LIKE wildcards.
    *   `insert_event` correctly handles `event_type` and leverages the unique index for deduplication.
    *   `list_past` and `search_by_keyword` adhere to the specified ordering requirements.
*   **`calendar_regex.py`**:
    *   `_TYPE_PATTERNS` and `_classify_type` correctly implement the priority-based event type classification.
    *   Regex patterns (`_FAMILY_KW`, `_DATE_TIME_TITLE`, etc.) are well-defined for their respective extraction tasks.
*   **`calendar_extractor.py`**:
    *   The `_PROMPT` effectively guides the LLM for event type extraction.
    *   The model fallback to regex mechanism ensures robustness.
    *   `_normalize` provides essential data validation and sanitization.
*   **`main.py`**:
    *   `_CALENDAR_QUERY_RE` and `_QUERY_NOUN_KEYWORDS` enable accurate calendar query detection.
    *   `_resolve_relative_date` handles various relative date inputs correctly.
    *   `_format_calendar_event` applies emojis based on `event_type` for enhanced UX.
    *   `_maybe_capture_calendar_event` correctly integrates event extraction and persistence.
    *   No new coupling to `lite_reply.py` for the calendar feature.