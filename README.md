# 🖼️ Imprint

**Leave a mark of every image you find.**

Imprint is a command-line tool that scans a web page for images, figures out
the largest / full-size version of each one it can find, and downloads them
into neatly organized subfolders — all kept inside Imprint's own working
directory, right next to the script. It supports single-page downloads,
batch downloads from a list of URLs, concurrent fetching, duplicate
detection, a searchable history, and a plain interactive menu.

## ☄️ How to Install (Easy Way)
1. [Download the latest release.](https://github.com/ErfanNamira/Imprint/releases/latest)
2. Run `imprint.exe`
   
## 🌟 Features

- **Smart image detection** — reads `<img>` `srcset`/`data-src` attributes,
  `<picture><source>` elements, CSS `background-image`, `og:image` /
  `twitter:image` meta tags, and direct image links, always preferring the
  largest available version.
- **Self-contained downloads** — everything (config, history database, and
  every image) lives in subfolders under Imprint's own directory:
  ```
  imprint.py
  config.json
  history.db
  downloads/
    example.com/
      some-page/
        photo1.jpg
        photo2.png
  ```
- **Concurrent downloads** with a configurable worker pool.
- **Real dimension filtering** — uses Pillow (if installed) to check actual
  image width/height and skip small icons/thumbnails, instead of guessing
  from file size alone.
- **Retry with backoff** on flaky network requests.
- **Duplicate detection** by content hash — the same image is never saved
  twice, even if it turns up on different pages.
- **Batch mode** — feed it a text file with one URL per line.
- **History & stats** — every attempt is logged to a local SQLite database.
- **Both interactive and CLI modes.**

## 💻 Installation

```bash
pip install -r requirements.txt
```

Pillow is optional but recommended — without it, Imprint falls back to a
simpler size heuristic instead of checking real image dimensions.

## 🧙‍♂️ Usage

### Interactive menu

```bash
python3 imprint.py
```

Walks you through downloading from a single page, running a batch job,
viewing history/stats, and adjusting settings.

### ⚡ Command line

```bash
# Download every image from a single page
python3 imprint.py https://example.com/gallery

# Batch download from a list of URLs (one per line)
python3 imprint.py --batch urls.txt

# Override the output folder and worker count for this run
python3 imprint.py https://example.com/gallery -o ./my-photos -w 10

# Only keep images at least 1000x1000
python3 imprint.py https://example.com/gallery --min-width 1000 --min-height 1000

# Print recent history / stats without downloading anything
python3 imprint.py --history
python3 imprint.py --stats
```

Run `python3 imprint.py --help` for the full list of flags.

## 🛠️ Configuration

Settings are stored in `config.json` next to the script and can be edited
either through the interactive **Settings** menu or by hand:

| Key                       | Description                                             |
|---------------------------|----------------------------------------------------------|
| `download_dir`             | Root folder for downloads (defaults to `./downloads`)    |
| `min_width` / `min_height` | Minimum image dimensions to keep                          |
| `enforce_real_dimensions`  | Use Pillow to check actual decoded image size             |
| `max_workers`              | Concurrent downloads per page                             |
| `max_retries` / `retry_backoff` | Retry behavior for flaky requests                    |
| `organize_by`              | `domain_and_page` \| `domain` \| `flat` \| `date`         |
| `skip_existing`            | Don't re-download files that already exist                |
| `dedupe_by_hash`           | Skip images identical to ones already downloaded           |
| `allowed_extensions`       | Which image file extensions to accept                     |

## 📄 License

MIT — see [`LICENSE`](LICENSE).
## ⚠️ Disclaimer

Only download images you have the right to use, and always respect a
website's terms of service and `robots.txt`.
