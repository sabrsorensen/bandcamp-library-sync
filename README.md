# Bandcamp Library Sync

`bandcamp-library-sync` is a Python CLI for pulling your purchased Bandcamp
collection to a local directory, defaulting to FLAC downloads.

The utility is built around three constraints:

1. Bandcamp's public developer API is not for fan-library downloads.
2. The fan collection and download flows are exposed through Bandcamp's web
   application and unofficial JSON endpoints.
3. Bandcamp's login flow may require CAPTCHA, so the safest automation path is
   browser-assisted login instead of password scraping.

## Current design

- `login` launches a Playwright-controlled browser so you can log in yourself.
- The tool saves authenticated session cookies and your collection page URL.
- `sync` indexes your purchased collection via Bandcamp's fan collection
  endpoint and downloads any missing releases.
- Downloaded releases are extracted to:

```text
<library-root>/
  <Artist>/
    <Album>/
      bandcamp_item_id.txt
      .bandcamp-release.json
      cover.jpg
      01 Track.flac
```

The `bandcamp_item_id.txt` and `.bandcamp-release.json` markers are used to
avoid redownloading releases that already exist locally. This layout also works
well as a staging area before handing files to Demlo or a similar organizer.

## Install

### With Nix

If you use `fish` or otherwise do not want to manage a Python virtualenv manually,
use the flake dev shell instead:

```bash
nix develop
python -m bandcamp_library_sync.cli --help
```

The shell already provides Python, `pip`, Playwright, and a Chromium browser
runtime through Nix, so you do not need to run `playwright install chromium`.
It also adds this repo's `src/` directory to `PYTHONPATH`, so you do not need
to run `pip install -e .` inside the dev shell.

### Without Nix

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

If you use `fish`, activate the virtualenv with:

```bash
source .venv/bin/activate.fish
```

## Usage

Authenticate once:

```bash
bandcamp-library-sync login
```

The browser opens. Log in to Bandcamp, navigate to your collection page, then
return to the terminal and press Enter.

List what the tool can see:

```bash
bandcamp-library-sync list
```

Sync your library as FLAC:

```bash
bandcamp-library-sync sync --output ~/Music/bandcamp-staging
```

Export a simple manifest for Demlo or another downstream organizer:

```bash
bandcamp-library-sync export-manifest --output ~/Music/bandcamp-staging
```

Other common options:

```bash
bandcamp-library-sync sync --output ~/Music/bandcamp-staging --format alac
bandcamp-library-sync sync --output ~/Music/bandcamp-staging --limit 25
bandcamp-library-sync sync --output ~/Music/bandcamp-staging --dry-run
```

The exported manifest is written by default to:

```text
~/Music/bandcamp-staging/.bandcamp-demlo-manifest.json
```

It contains one entry per staged release with the Bandcamp item id, source URL,
release directory, and extracted files. That gives you a stable handoff point if
you want a separate Demlo job to import and reorganize the downloads.

## Notes and assumptions

- The tool uses the unofficial `https://bandcamp.com/api/fancollection/1/collection_items`
  endpoint to enumerate your collection.
- Download links are resolved from the web download flow and embedded page data.
- Bandcamp can change their HTML or internal JSON shapes at any time. If that
  happens, the sync logic will need to be adjusted.
- This project is intended for downloading music you already purchased.
