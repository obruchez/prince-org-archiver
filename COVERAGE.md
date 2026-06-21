# Prince.org archive — coverage summary

Snapshot of what this archive contains, how it's laid out, and where the
remaining gaps are. Numbers were captured on 2026-06-21, after the
Wayback recovery, integration, media download, and a 60-day partial
refresh (which also repaired the forum-redirect bug) all completed.

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
- **First archived post:** 1999-03-16. **Most recent:** 2026-04-27.
  ~27 years of continuous forum history.
- **639,700** thread pages stored on disk (631,568 live + 8,132 wayback).
- **7,944** media assets downloaded (7,041 avatars + 793 emoticons +
  104 post images + 6 gallery items), **51 MB** on disk. Another 3,016
  returned legitimate 404/400/403 from the server and 4,406 lived on
  dead hosts/ports that were skipped without a request.
- **20 GB** live HTML + **339 MB** Wayback HTML + **51 MB** media +
  **181 MB** logs.

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
├── archive.db                 SQLite, 162 MB — the canonical index
├── html/
│   ├── threads/<bucket>/<thread_id>/page_<N>.html
│   │                         631,568 live-crawl HTMLs, ~20 GB
│   └── events/<year>/<MM>.html   (not crawled — see "Not done")
├── wayback/
│   ├── threads/<bucket>/<thread_id>/<wayback_ts>.html
│   │                         8,132 archive.org recoveries, ~339 MB
│   └── index/<forum_id>/<wayback_ts>.html
│                             49 closed-forum index snapshots
├── media/
│   ├── avatars/             39 MB, 7,041 files
│   ├── emoticons/           1.6 MB, 793 files
│   ├── post_images/         10 MB, 104 files
│   └── gallery/             24 KB, 6 files
└── logs/archiver.log         181 MB
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
  (live or wayback). `status` ∈ {`downloaded`, `error`, `skipped`}
  with `pending` cleared. 51.7% of all known URLs landed on disk;
  the rest are real server-side 4xx or URLs that point at dead
  hosts/ports we never bothered to request.
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
7. **Media download** — **7,944** assets pulled to `data/media/` over
   four restarts (commits `f956796`, `3488b3a`, `19c489b`, `601a1a3`).
   Each restart fixed a real bug: `httpx.RemoteProtocolError` wasn't in
   the retry catch list, the transient-error auto-re-pend filter was
   too loose and dragged dead external hosts back into the worklist,
   and the tightened `%prince.org%` filter still matched the dead
   `img.prince.org` subdomain. Final filter pins the re-pend to the
   four canonical-host URL prefixes (https/http × `prince.org/` /
   `prince.org:`). 99.97% of remaining errors are real HTTP 4xx.
8. **Partial refresh + forum-redirect bug fix** (commits `c461d03`,
   `aba7564`). Added `crawl threads --refresh-since DURATION` to re-fetch
   page 1 + previously-last page + any newly-created pages for threads
   whose `last_post_date` is on/after the cutoff. Test run with `60d`
   surfaced a silent pre-existing bug: `client.thread_url()` hardcoded
   `/msg/7/<id>` for every forum; the server 301-redirects to the real
   `/msg/<forum>/<id>` but **strips the `?pg=N` query parameter** in
   the process, so pages 2+ of any non-forum-7 thread silently saved
   page 1's content under `page_N.html`. Scope: 141,469 pages across
   62,240 threads (≈22% of all multi-page live-crawl content). Fixed
   by threading `forum_id` through to `thread_url()` plus a defensive
   `pg=N must be in final_url` sanity check before save. All 141,469
   corrupted pages re-queued and re-downloaded successfully (12 hit
   transient `ConnectError`s during a brief connectivity blip but
   self-healed via the per-page re-pend path).

Each step is documented in `git log` with the substantive design
decisions explained in the commit body.

## What was NOT done — open work

1. **New threads since 2026-04-27.** The discovery checkpoint
   (`crawl_state.last_enumerated_id`) is still at 475,000. The 60-day
   refresh picked up new replies on existing threads (`last_post_date`
   advanced from 2026-04-22 to 2026-04-27) but didn't bump the
   discovery ceiling. Re-run with `crawl threads --end-id <higher>`
   (or another `--refresh-since`) to pick up genuinely new threads
   posted after 2026-04-22.
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
7. **3,016 media URLs returned real HTTP 4xx** — deleted avatars,
   malformed URLs in old post HTML, the occasional 403. Not
   recoverable without server-side cooperation.

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
