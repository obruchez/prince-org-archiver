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
