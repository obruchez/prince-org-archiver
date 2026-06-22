# Prince.org Forum Archiver

A command-line tool to archive [prince.org](https://prince.org), an independent Prince fan community established in 1998. The site has become unstable in recent years, making preservation of its content important for research and historical purposes.

The archiver downloads forum threads, media (avatars, emoticons, post images), and events calendar pages in raw HTML format, with full resumability and respectful rate limiting.

## Features

- **Forum archival** - Downloads all ~400,000 threads across 10 forums (~9 million posts)
- **Media downloading** - Avatars, emoticons, and post-embedded images
- **Events calendar** - Monthly event pages from 1998 to present
- **Resumable** - All state tracked in SQLite; safely stop and resume anytime
- **Rate limited** - Conservative defaults (2s delay, 3 concurrent requests) with adaptive throttling
- **Graceful shutdown** - Ctrl+C saves progress before exiting
- **Progress tracking** - Rich terminal output and a `status` command for checking progress
- **File verification** - Checks that downloaded pages exist on disk

## How It Works

The archiver uses **thread ID enumeration** (IDs 1 through ~475,000) rather than crawling forum listing pages, which have unreliable pagination. Each thread is fetched, classified by its actual forum (via breadcrumb parsing), and saved as raw HTML with a JSON metadata sidecar.

### Crawl Phases

1. **Thread discovery** - Enumerate all thread IDs, download page 1 of each thread found
2. **Multi-page threads** - Download remaining pages for threads with more than one page, prioritized by forum
3. **Media** - Download avatars, emoticons, and post images extracted from archived HTML
4. **Events** - Archive monthly event calendar pages (1998-2026)

## Installation

Requires Python 3.12+.

```bash
git clone https://github.com/your-username/prince-org-archiver.git
cd prince-org-archiver
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Usage

### Start crawling threads

```bash
# Full crawl (will take ~10 days at default rate)
prince-org-archiver crawl threads

# Resume after interruption (automatically picks up where it left off)
prince-org-archiver crawl threads

# Crawl a specific ID range (useful for testing)
prince-org-archiver crawl threads --start-id 1 --end-id 1000

# Only archive threads from Forum 7 (Prince: Music and More)
prince-org-archiver crawl threads --forum 7

# Limit the session
prince-org-archiver crawl threads --max-requests 5000 --max-duration 8h
```

### Download media

```bash
# Download all pending media (avatars, emoticons, post images)
prince-org-archiver crawl media

# Download only avatars
prince-org-archiver crawl media --type avatar
```

### Archive events

```bash
prince-org-archiver crawl events
```

### Run all phases in sequence

```bash
prince-org-archiver crawl all
```

### Check progress

```bash
prince-org-archiver status
```

### Verify downloaded files

```bash
prince-org-archiver verify
```

### Common options

| Option | Default | Description |
|--------|---------|-------------|
| `--data-dir PATH` | `data` | Output directory |
| `--rate FLOAT` | `0.5` | Requests per second |
| `--concurrency INT` | `3` | Max concurrent requests |
| `--max-requests INT` | unlimited | Stop after N requests |
| `--max-duration TEXT` | unlimited | Stop after duration (e.g. `8h`, `30m`) |
| `--retry-errors` | off | Retry previously errored items |
| `-v, --verbose` | off | Verbose logging |

## Output Structure

```
data/
├── archive.db                          # SQLite state database
├── html/
│   ├── threads/
│   │   ├── 000/                        # Threads 0-999
│   │   │   └── 42/
│   │   │       ├── page_1.html         # Raw HTML
│   │   │       ├── page_2.html
│   │   │       └── metadata.json       # Thread metadata
│   │   ├── 001/                        # Threads 1000-1999
│   │   └── .../
│   └── events/
│       └── 2024/
│           ├── 01.html
│           └── ...
├── media/
│   ├── avatars/                        # User avatar images
│   ├── emoticons/                      # Forum emoticon GIFs
│   └── post_images/                    # Images embedded in posts
└── logs/
    └── archiver.log
```

### Thread metadata

Each thread includes a `metadata.json` file:

```json
{
  "thread_id": 471936,
  "forum_id": 7,
  "forum_name": "Prince: Music and More",
  "title": "Parade Expanded... it's coming",
  "author": "GiggityGoo",
  "page_count": 3,
  "media_urls": [
    "https://prince.org:444/avatars/1503.ava",
    "https://prince.org/i/s/icon_smile.gif"
  ]
}
```

## Using the archived data

This section is for *consumers* of the archive (you have the data on
disk, you want to find or extract content). For current archive state
and per-forum totals, see [COVERAGE.md](COVERAGE.md) — it's the
canonical reference.

### What lives where

| Question | Source |
| --- | --- |
| Thread metadata (id, forum, title, OP author, dates, page count) | `archive.db` (`threads` table) |
| Per-page metadata (which pages exist, on-disk path, source: live or wayback) | `archive.db` (`thread_pages` table) |
| **Post text, replies, individual authors, timestamps** | **Only in the HTML files** — NOT in the DB |
| Media (avatars, post images, emoticons) | `data/media/<type>/` |
| Forum/year activity counts | derived via SQL |

No full-text search is built. To search post bodies you either
`grep -r data/html/threads/` (slow on 20 GB) or build a SQLite FTS5
index post-hoc.

### Thread ID → file path

```
data/html/threads/<bucket>/<thread_id>/page_<N>.html
```

where `<bucket> = f"{thread_id // 1000:03d}"` (the first three digits
of the thread ID, zero-padded). Examples:

- thread 42 → `data/html/threads/000/42/page_1.html`
- thread 471936 → `data/html/threads/471/471936/page_1.html`

Wayback-recovered HTMLs (8,132 threads from permanently closed
forums) live under `data/wayback/threads/<bucket>/<thread_id>/<ts>.html`
with the same bucket rule. The `thread_pages.html_path` column always
points at the right file regardless of source.

### Extracting posts from a page

prince.org's class names are NOT what `parse_thread_page` documents
internally (that parser only pulls thread-level metadata; its
post-body selector list is a fallback that doesn't actually match the
live HTML — it falls back to counting reply links). The real class
names on the live forum HTML are:

| Selector | Content |
| --- | --- |
| `[class^="msgbody"]` (matches `msgbody0`, `msgbody1`) | Post body text |
| `.msgauth` | Author username for each post |
| `.msgsubj` | Per-post subject line |
| `.msgavatar` | Avatar image for each post |

There are typically ~30 posts per page on multi-page threads.

### Worked example — find threads about a topic and print posts

```python
import sqlite3
from pathlib import Path
from bs4 import BeautifulSoup

DATA = Path("data")
con = sqlite3.connect(DATA / "archive.db")
con.row_factory = sqlite3.Row

# 1. Find candidate threads by title (cheap: title is in the DB)
rows = con.execute("""
    SELECT thread_id, forum_id, page_count, title, author
    FROM threads
    WHERE status = 'complete' AND title LIKE ?
    ORDER BY first_post_date
""", ("%Hit N Run%",)).fetchall()

# 2. For each thread, walk its pages
for r in rows[:5]:
    print(f"\n=== {r['title']} (#{r['thread_id']}, forum {r['forum_id']}) ===")
    pages = con.execute("""
        SELECT page_num, html_path FROM thread_pages
        WHERE thread_id = ? ORDER BY page_num
    """, (r['thread_id'],)).fetchall()
    for p in pages:
        # html_path may be relative to data/; resolve as needed
        soup = BeautifulSoup(Path(p['html_path']).read_bytes(), 'lxml')
        bodies = soup.select('[class^="msgbody"]')
        authors = [a.get_text(strip=True) for a in soup.select('.msgauth')]
        for author, body in zip(authors, bodies):
            text = body.get_text(' ', strip=True)
            print(f"  [{author}] {text[:200]}")
```

For more SQL recipes (activity by year, all posts by a user, etc.)
see the "How to query" section of [COVERAGE.md](COVERAGE.md).

## Forums

| ID | Name | Threads | Replies |
|----|------|---------|---------|
| 7 | Prince: Music and More | 113,097 | 2,611,724 |
| 100 | General Discussion | 121,583 | 3,821,656 |
| 8 | Music: Non-Prince | 108,521 | 1,846,840 |
| 12 | Concerts | 14,079 | 188,407 |
| 5 | Associated artists & people | 13,144 | 232,896 |
| 15 | Art, Podcasts, & Fan Content | 9,089 | 62,452 |
| 9 | The Marketplace | 7,285 | 12,556 |
| 3 | prince.org site discussion | 5,105 | 73,275 |
| 13 | Past, Present, Future sites | 4,856 | 70,550 |
| 2 | Fan Gatherings | 4,020 | 38,727 |

## Rate Limiting

The archiver is designed to be respectful to the server:

- **2-second base delay** between requests (configurable with `--rate`)
- **3 concurrent requests** max (configurable with `--concurrency`)
- **Exponential backoff** on errors (5s, 10s, 20s, 40s, 80s)
- **Adaptive throttling** - automatically slows down if the server responds slowly
- **Error budget** - pauses after 50 consecutive errors to avoid hammering a struggling server
- **Honors HTTP 429** `Retry-After` headers

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

This tool is for archival and research purposes. Please respect prince.org's terms of service and the community's content.
