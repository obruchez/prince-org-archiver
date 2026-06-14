# Prince.org archive — coverage summary

Snapshot of what this archive contains, how it's laid out, and where the
remaining gaps are. Numbers were captured on 2026-06-14, after the
Wayback recovery and integration passes completed.

## Headline

- **474,135** distinct thread IDs probed (range 1–475,000)
  - **422,243** complete (text content fully captured from the live site)
  - **46,053** in permanently closed forums (no content on the live site)
  - **6,704** never existed / deleted IDs (terminal `not_found`)
- **8,135** of the 46,053 closed-forum threads recovered via the
  Wayback Machine (17.7%) and now folded into the main schema
  alongside the live-crawl content. Index pages for all 10 closed
  forums also recovered.
- **21,133** "gated" stubs remain unresolvable — moderator-removed
  threads with no Wayback capture; the only known content is the 3.5 KB
  "Thread missing or not yet approved" error page the live site serves.
- **First archived post:** 1999-03-16. **Most recent:** 2026-04-22.
  ~27 years of continuous forum history.
- **639,694** thread pages stored on disk (631,562 live + 8,132 wayback).
- **21 GB** live HTML + **339 MB** Wayback HTML + **164 MB** logs.

## Coverage by forum

| forum_id | threads | complete | closed | recovered via Wayback |
| --- | ---: | ---: | ---: | ---: |
| 100 | 118,859 | 118,859 | 0 | 242 |
| 7 | 109,204 | 109,204 | 0 | 397 |
| 8 | 107,899 | 107,898 | 1 | 74 |
| 105 (politics/religion, closed ~2023) | 34,671 | 0 | 34,671 | **7,268** |
| 12 | 13,554 | 13,554 | 0 | 23 |
| 5 | 12,890 | 12,890 | 0 | 49 |
| 5001 (closed) | 10,006 | 10,006 | 0 | 8 |
| 15 | 8,878 | 8,878 | 0 | 10 |
| 9 | 6,719 | 6,719 | 0 | 28 |
| 300 (closed) | 6,203 | 0 | 6,203 | 8 |
| 3 | 4,714 | 4,712 | 2 | 6 |
| 13 | 4,681 | 4,681 | 0 | 9 |
| 10 (closed) | 3,902 | 0 | 3,902 | 7 |
| 2 | 3,709 | 3,709 | 0 | 3 |
| 110, 11, 101, 5000, 5002, 99 (closed) | 1,274 | 0 | 1,274 | 0 |
| **gated** (`forum_id IS NULL`) | 21,133 | 21,133 | 0 | 0 |

`closed` threads are those in a forum that was already shut down by the
time we crawled — the live site returns a "this forum is currently
closed" banner with no post content. `complete` threads have at least
page 1 captured from the live site; multi-page threads also have all
their additional pages downloaded. `gated` threads are status=`complete`
with a 3.5 KB error stub instead of real content (moderator-removed).

## File layout

```
data/
├── archive.db                 SQLite, 178 MB — the canonical index
├── html/
│   ├── threads/<bucket>/<thread_id>/page_<N>.html
│   │                         631,562 live-crawl HTMLs, ~21 GB
│   └── events/<year>/<MM>.html   (not crawled — see "Not done")
├── wayback/
│   ├── threads/<bucket>/<thread_id>/<wayback_ts>.html
│   │                         8,132 archive.org recoveries, ~339 MB
│   └── index/<forum_id>/<wayback_ts>.html
│                             49 closed-forum index snapshots
├── media/
│   ├── avatars/, emoticons/, post_images/, gallery/
│                             empty — see "Media")
└── logs/archiver.log         164 MB
```

`<bucket>` is the thread ID's first three digits, zero-padded
(`thread_id // 1000`). It keeps any one directory under ~1,000 entries.

## Database schema (1.5 lines per table)

- **`threads`** — one row per thread_id; `status` ∈ {`complete`,
  `closed`, `not_found`}, plus `forum_id`, `title`, `author`,
  `page_count`, `first_post_date`, `last_post_date`.
- **`thread_pages`** — one row per (thread_id, page_num); `source` ∈
  {`live`, `wayback`} with `snapshot_ts` when wayback. Points at the
  HTML file via `html_path`.
- **`wayback`** — one row per (target_type, target_key) = recovery
  worklist + audit trail. `status` ∈ {`pending`, `recovered`,
  `no_capture`, `error`, `no_forum`, `skipped`}.
- **`media`** — one row per asset URL discovered in any captured HTML
  (live or wayback). `status` mostly `pending` (no media run yet).
- **`forums`** — empty. Was meant to hold forum names but the
  enumeration step was never run; forum IDs above were derived from
  thread join.
- **`events`** — empty. The events-calendar crawler (`crawl events`)
  was never run.
- **`crawl_state`** — internal key/value (resume checkpoints).

## What was done — chronological

1. **Live crawl** of thread IDs 1–475,000 against prince.org, ~400K real
   threads pulled, all multi-page threads fully paginated, all status
   classifications recorded. (Pre-2026-05-17.)
2. **Closed-forum forum_id backfill** — for the 46,053 closed threads
   the live site refused to identify, re-fetched the redirect to learn
   their `forum_id`. All 46,053 resolved.
3. **Gated-stub forum_id extraction** — parsed the 3.5 KB "error" stubs
   for embedded forum hints, recovered 21,970 of 21,982 stub forum_ids
   from the HTML chrome itself.
4. **Wayback recovery** — for each closed/gated thread plus each closed
   forum's index page, queried the CDX API and downloaded the
   `id_`-replay snapshot. Multiple passes refined the client over
   several weeks (adaptive pacing, throttle/no_capture disambiguation,
   tolerant Content-Encoding decoding). Final: **8,135** thread HTMLs
   recovered.
5. **Wayback integration** — folded the recovered HTMLs into the main
   `threads` and `thread_pages` schema via `parse_thread_page`, filled
   in titles/authors/page_counts (8,132 threads enriched), and
   registered 87,591 media URL references discovered in the recovered
   HTML.
6. **Media worklist normalization** — rewrote the dead
   `http://prince.org:81/...` avatar/image URLs to their working
   `https://prince.org:444/...` equivalents, marked the :81 rows as
   `skipped` with audit message. No HTTP traffic, just a SQL pass.

Each step is documented in `git log` with the substantive design
decisions explained in the commit body.

## What was NOT done — open work

1. **Media download.** All 11,003 viable media URLs (10,054 avatars,
   806 emoticons, 137 post images, 6 gallery items) are sitting
   `pending`. Live probes confirm they're reachable; run is
   `prince-org-archiver crawl media` at ~0.5 req/s = ~6 h. Expected
   yield ~75-80% on avatars (the rest are deleted accounts), ~100% on
   emoticons and post images.
2. **Events calendar.** The `crawl events` command exists and the
   schema is in place but it was never run. ~27 years × 12 months ≈
   324 monthly event pages, mostly tiny. Probably not high-value.
3. **Forum metadata enumeration.** The `forums` table is empty. Forum
   names would have to be scraped from the top-level forum index;
   nothing in the code currently does this. The IDs are derivable from
   thread joins (above table), so it's mostly cosmetic.
4. **The 3 unparseable Wayback HTMLs.** Files
   `data/wayback/threads/075/75340/...`,
   `data/wayback/threads/104/104337/...`, and
   `data/wayback/threads/349/349090/...` contain real thread content
   but their captured chrome includes the "this forum is currently
   closed" banner, which `classify_response` matches before the
   msg-body markers, returning `FORUM_CLOSED`. 0.04% loss; the files
   are on disk and human-readable.
5. **The 21,133 gated threads with no Wayback capture.** Most likely
   never archived (archive.org rarely crawled private-by-default
   threads, and these were moderator-removed before its bots noticed
   them). The 3.5 KB error-page stubs are still saved at
   `data/html/threads/<bucket>/<thread_id>/page_1.html`.
6. **The 59,900 Wayback no-capture rows.** Threads in closed/gated
   buckets where archive.org honestly has nothing. No action available.

## How to query

The schema is normal SQL. Some recipes:

```sql
-- All posts by user X across the whole archive
SELECT thread_id, title, page_count, status, p.source
FROM threads t JOIN thread_pages p USING (thread_id)
WHERE author = 'someone' ORDER BY first_post_date;

-- All closed-forum threads we recovered text for, with snapshot date
SELECT thread_id, forum_id, title, snapshot_ts, html_path
FROM threads t JOIN thread_pages p USING (thread_id)
WHERE p.source = 'wayback' AND p.snapshot_ts IS NOT NULL
ORDER BY snapshot_ts DESC LIMIT 50;

-- Activity by year on the live (non-closed) forums
SELECT substr(first_post_date, 1, 4) AS year, COUNT(*)
FROM threads WHERE status = 'complete'
GROUP BY year ORDER BY year;

-- Pages of a given thread, in order, with their on-disk HTML paths
SELECT page_num, source, snapshot_ts, html_path, post_count
FROM thread_pages WHERE thread_id = 280342 ORDER BY page_num;
```

## Notes for future runs

- `archive.db` carries WAL; close all readers before backing it up or
  use `sqlite3 archive.db ".backup target.db"` to get a consistent
  copy.
- Anywhere the live site is hit, the default rate is conservative
  (0.5 req/s) on the assumption prince.org is unstable. The Wayback
  default is even slower (0.2 req/s = 1 req / 5s) to stay under
  archive.org's undocumented CDX rate cap.
- All long-running jobs are resumable from the DB. There are no
  in-memory queues — interrupting and re-running is always safe.
