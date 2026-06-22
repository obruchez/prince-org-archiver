# Notes for Claude / agents

## What this project is

A CLI tool that has *already populated* a comprehensive archive of
prince.org (the independent Prince fan forum, founded 1998). The
archival side is functionally done — 422K threads with full content
plus 8K wayback recoveries, ~640K HTML pages, 20 GB of forum HTML,
51 MB of media, and the events calendar (348 monthly pages). See
[COVERAGE.md](COVERAGE.md) for the canonical state — read it before
making claims about counts, scope, or what's missing.

## If a user asks you to consume the archive

(e.g. "find threads about X", "what did user Y post about Z")

The "Using the archived data" section in [README.md](README.md) has
the rules:

- Thread metadata is in `archive.db` (SQLite); **post text is only
  in HTML files on disk**, NOT in the DB.
- Path rule: `data/html/threads/<bucket>/<thread_id>/page_<N>.html`
  with `<bucket> = f"{thread_id // 1000:03d}"`. Wayback HTMLs follow
  the same bucket rule under `data/wayback/threads/`. The
  `thread_pages.html_path` column always has the resolved path.
- Real CSS selectors for post extraction: `[class^="msgbody"]` for
  bodies, `.msgauth` for authors, `.msgsubj` for subjects. (The
  `parse_thread_page` parser in `src/archiver/parsers/thread_page.py`
  only extracts thread-level metadata — not individual posts. Its
  fallback selector list does NOT match the real HTML; use the
  README's verified selectors.)
- No full-text search index exists. For grep over 20 GB use
  `ripgrep`; for repeated queries consider building a one-shot SQLite
  FTS5 index over the HTML.

The README also has a worked Python example combining SQL lookup +
BeautifulSoup extraction.

## If a user asks you to crawl more

- Long-running crawls (anything over a few minutes) are run by the
  user themselves. Do NOT auto-launch full crawls unless they
  explicitly say so in the same turn.
- Before recommending `crawl threads --end-id N`, see
  `~/.claude/projects/.../memory/feedback_discovery_range.md` —
  `crawl_state.last_enumerated_id` records the `--end-id` you passed,
  not the highest real ID found. Sample the live `/msg/<forum>/`
  index pages first to learn the true ceiling.
- For a partial refresh of existing threads:
  `crawl threads --refresh-since 30d` (or `60d`, etc.). Re-fetches
  page 1 + previously-last page + any newly-created pages.

## Conventions

- Commits go directly to `master` with a
  `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>` trailer.
- Read-only DB access during status checks (use `-readonly` or take
  a `cp` snapshot to `/tmp` first if the writer is active).
- When status checks fail with sqlite "unable to open database file"
  in this sandbox, `cp archive.db /tmp/snapshot.db` and read from
  there.
