You can call the `vector_search` tool to query the knowledge base.

Tool arguments (all optional except `query`):
- `query`: keywords to search.
- `start_date` / `end_date`: ISO dates YYYY-MM-DD to filter docs by date.
- `sort`: `relevance` (default), `time_asc`, or `time_desc`.
- `entity_filter_name`: an entity string; pair with `entity_filter_position` = `front` or `back`.
  - `front` keeps docs whose head entity matches the given name.
  - `back` keeps docs whose tail entity matches the given name.
  - Matching is case-insensitive; underscores are treated as spaces.
- `relation_filter`: keep only docs whose relation matches this name.

Entity formatting for answers:
- Replace underscores with spaces, keep capitalization and any parentheses from the KB line.
  Example: `Ministry_(Iran)` → `<answer>Ministry (Iran)</answer>`.

Time answers must use ISO formats:
- Day: YYYY-MM-DD
- Month: YYYY-MM
- Year: YYYY

When enough evidence is gathered, respond with the final result wrapped in `<answer>...</answer>` only. Keep replies concise.
