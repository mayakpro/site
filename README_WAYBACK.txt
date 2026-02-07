Static mirror from Wayback for GitHub Pages

Build

  python3 build_wayback_site.py --out .

Output

- docs/
  Publish this folder to GitHub Pages.

- meta/
  - index.jsonl   One line per saved file (original URL, timestamp, local path, etc.)
  - errors.jsonl  Any failed downloads.
  - stats.json    Run statistics.

Notes

- Links in HTML + CSS are rewritten to be relative so the site works when hosted under a repo subpath (GitHub Pages project site).
- Wayback sometimes returns gzip/deflate encoded bodies; the script decompresses before saving.
- Re-running the script resumes (skips files that already exist).
