#!/usr/bin/env python3
"""Build a static mirror of dodmayak.org from the Wayback Machine.

Goal: produce a folder you can publish to GitHub Pages.

Key behaviors (defaults):
- CDX API: pick the latest successful snapshot per archived URL (reverse-sorted, collapse=urlkey).
- Download ALL resources for the domain (HTML, CSS, JS, images, fonts, etc.).
- Save files into ./docs/ preserving the original URL path.
- HTML pages are written as directories with index.html when the URL path is a "pretty" URL.
- Rewrite internal links in HTML and CSS to be *relative* (works on GitHub Pages project sites).
- Strip the Age Gate (18+) overlay code from saved HTML.
- Add docs/.nojekyll for safer asset serving.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import time
import zlib
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen


CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
WAYBACK_BASE = "https://web.archive.org/web"


RE_SKIP_SCHEMES = re.compile(r"^(?:mailto:|tel:|sms:|javascript:|data:|blob:|about:)", re.IGNORECASE)


def is_age_gate_resource(original_url: str) -> bool:
    """Return True if URL belongs to the Age Gate plugin or its wp-json endpoints."""
    try:
        path = (urlsplit(original_url).path or "").lower()
    except Exception:
        return False
    return "/wp-content/plugins/age-gate/" in path or "/wp-json/age-gate/" in path


_AG_QUOTE = r"['\"]"
_AG_RE_TEMPLATE = re.compile(r'<template\s+id="tmpl-age-gate"\s*>.*?</template>\s*', re.IGNORECASE | re.DOTALL)
_AG_RE_STYLE_ID = re.compile(
    rf'<style\b[^>]*\bid={_AG_QUOTE}age-gate-[^>]*?{_AG_QUOTE}[^>]*>.*?</style>\s*',
    re.IGNORECASE | re.DOTALL,
)
_AG_RE_LINK_ID = re.compile(rf'<link\b[^>]*\bid={_AG_QUOTE}age-gate-css{_AG_QUOTE}[^>]*>\s*', re.IGNORECASE)
_AG_RE_SCRIPT_ID = re.compile(
    rf'<script\b[^>]*\bid={_AG_QUOTE}age-gate-[^>]*?{_AG_QUOTE}[^>]*>.*?</script>\s*',
    re.IGNORECASE | re.DOTALL,
)
_AG_RE_ANY_SCRIPT_ASSET = re.compile(
    r'<script\b[^>]*\bsrc=["\"][^"\"]*wp-content/plugins/age-gate/[^"\"]*["\"][^>]*>.*?</script>\s*',
    re.IGNORECASE | re.DOTALL,
)
_AG_RE_ANY_LINK_ASSET = re.compile(
    r'<link\b[^>]*\bhref=["\"][^"\"]*wp-content/plugins/age-gate/[^"\"]*["\"][^>]*>\s*',
    re.IGNORECASE,
)
_AG_RE_CUSTOM_CSS_BLOCK = re.compile(
    r'\s*\.age-gate-submit-no\s*,\s*\.age-gate-submit-yes\s*\{.*?\}\s*',
    re.IGNORECASE | re.DOTALL,
)


def strip_age_gate_html(html: str) -> str:
    """Remove Age Gate overlay code from WordPress-rendered HTML."""
    out = html
    out = _AG_RE_TEMPLATE.sub("", out)
    out = _AG_RE_STYLE_ID.sub("", out)
    out = _AG_RE_LINK_ID.sub("", out)
    out = _AG_RE_SCRIPT_ID.sub("", out)
    out = _AG_RE_ANY_SCRIPT_ASSET.sub("", out)
    out = _AG_RE_ANY_LINK_ASSET.sub("", out)
    out = _AG_RE_CUSTOM_CSS_BLOCK.sub("\n", out)
    out = re.sub(r"\n{4,}", "\n\n\n", out)
    return out


def _norm_mime(m: str) -> str:
    return (m or "").split(";", 1)[0].strip().lower()


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="strict")).hexdigest()


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _write_bytes(path: str, data: bytes) -> None:
    parent = os.path.dirname(path)
    if parent:
        _ensure_dir(parent)
    with open(path, "wb") as f:
        f.write(data)


def _write_text(path: str, text: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        _ensure_dir(parent)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _detect_charset_from_headers(content_type: str) -> Optional[str]:
    if not content_type:
        return None
    m = re.search(r"charset=([^;\s]+)", content_type, re.IGNORECASE)
    if not m:
        return None
    charset = m.group(1).strip().strip('"').strip("'")
    return charset or None


def _decode_text(data: bytes, content_type: str) -> str:
    charset = _detect_charset_from_headers(content_type) or "utf-8"
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _decompress_if_needed(data: bytes, content_encoding: str) -> bytes:
    enc = (content_encoding or "").strip().lower()
    if not enc or enc == "identity":
        return data
    if enc == "gzip":
        return gzip.decompress(data)
    if enc == "deflate":
        # zlib wrapper or raw deflate
        try:
            return zlib.decompress(data)
        except zlib.error:
            return zlib.decompress(data, -zlib.MAX_WBITS)
    if enc == "br":
        # Optional: brotli module (not in stdlib)
        try:
            import brotli  # type: ignore

            return brotli.decompress(data)
        except Exception:
            # Keep as-is; caller will likely fail to decode/rewrite.
            return data
    return data


@dataclass
class CdxRecord:
    timestamp: str
    original: str
    mimetype: str
    statuscode: str


def fetch_cdx(domain: str) -> List[CdxRecord]:
    params = [
        ("url", f"{domain}/*"),
        ("output", "txt"),
        ("fl", "timestamp,original,mimetype,statuscode"),
        ("filter", "statuscode:200"),
        ("collapse", "urlkey"),
        ("sort", "reverse"),
    ]
    url = CDX_ENDPOINT + "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "OpenCode/1.0"})
    with urlopen(req, timeout=60) as r:
        body = r.read().decode("utf-8", errors="replace")

    out: List[CdxRecord] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        ts, original, mimetype, status = parts[0], parts[1], parts[2], parts[3]
        out.append(CdxRecord(timestamp=ts, original=original, mimetype=mimetype, statuscode=status))
    return out


def fetch_wayback(archived_url: str, timeout: int = 60) -> Tuple[int, Dict[str, str], bytes]:
    req = Request(
        archived_url,
        headers={
            "User-Agent": "OpenCode/1.0",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        },
    )
    with urlopen(req, timeout=timeout) as r:
        status = getattr(r, "status", 200)
        headers = {k.lower(): v for k, v in (r.headers.items() if r.headers else [])}
        data = r.read()
    return status, headers, data


def _posix_relpath(from_dir: str, to_dir: str) -> str:
    rel = os.path.relpath(to_dir, from_dir)
    if rel == ".":
        return ""
    return rel.replace(os.sep, "/").rstrip("/") + "/"


def _extract_original_from_wayback(url: str) -> Optional[str]:
    """If URL is a web.archive.org wrapper, return the embedded original URL."""
    try:
        u = urlsplit(url)
    except Exception:
        return None
    if u.netloc.lower() not in {"web.archive.org", "www.web.archive.org"}:
        return None
    # Typical: /web/<timestamp><flags>/<original>
    m = re.match(r"^/web/\d+(?:[a-z_]+)?/(https?://.+)$", u.path, re.IGNORECASE)
    if not m:
        return None
    return m.group(1)


def _rewrite_one_url_value(value: str, domain_set: Set[str], rel_prefix: str) -> str:
    v = (value or "").strip()
    if not v or v.startswith("#"):
        return value
    if RE_SKIP_SCHEMES.match(v):
        return value

    # Wayback wrapper -> original
    embedded = _extract_original_from_wayback(v)
    if embedded:
        v = embedded

    # Protocol-relative
    if v.startswith("//"):
        v = "https:" + v

    # Absolute URLs
    if v.startswith("http://") or v.startswith("https://"):
        try:
            u = urlsplit(v)
        except Exception:
            return value
        host = (u.netloc or "").lower()
        if host in domain_set:
            path = u.path or "/"
            if path.startswith("/"):
                path = path[1:]
            rebuilt = rel_prefix + path
            if u.query:
                rebuilt += "?" + u.query
            if u.fragment:
                rebuilt += "#" + u.fragment
            return rebuilt
        return value

    # Root-relative
    if v.startswith("/"):
        return rel_prefix + v.lstrip("/")

    # Some WP themes use paths that are intended to be root-relative.
    for maybe_root in (
        "wp-content/",
        "wp-includes/",
        "wp-json/",
        "cdn-cgi/",
    ):
        if v.startswith(maybe_root):
            return rel_prefix + v

    return value


def rewrite_html(html: str, page_dir: str, site_root: str, domain_set: Set[str]) -> str:
    rel_prefix = _posix_relpath(page_dir, site_root)

    # Rewrite srcset separately.
    def repl_srcset(m: re.Match) -> str:
        quote = m.group(1)
        raw = m.group(2)
        parts = [p.strip() for p in raw.split(",")]
        out_parts: List[str] = []
        for p in parts:
            if not p:
                continue
            # "url 2x" or "url 640w"
            toks = p.split()
            if not toks:
                continue
            url = toks[0]
            rest = " ".join(toks[1:])
            new_url = _rewrite_one_url_value(url, domain_set, rel_prefix)
            out_parts.append((new_url + (" " + rest if rest else "")).strip())
        return f"srcset={quote}{', '.join(out_parts)}{quote}"

    html = re.sub(r"(?i)\bsrcset\s*=\s*([\"'])(.*?)(\1)", repl_srcset, html, flags=re.DOTALL)

    # Rewrite common URL-bearing attributes.
    attrs = [
        "href",
        "src",
        "poster",
        "action",
        "data-src",
        "data-href",
        "data-url",
        "content",
    ]
    attr_re = re.compile(r"(?i)\b(" + "|".join(attrs) + r")\s*=\s*([\"'])(.*?)(\2)", re.DOTALL)

    def repl_attr(m: re.Match) -> str:
        name = m.group(1)
        quote = m.group(2)
        val = m.group(3)
        new_val = _rewrite_one_url_value(val, domain_set, rel_prefix)
        return f"{name}={quote}{new_val}{quote}"

    html = attr_re.sub(repl_attr, html)

    # Rewrite url(...) inside style attributes / inline CSS.
    html = rewrite_css_urls_in_text(html, page_dir=page_dir, site_root=site_root, domain_set=domain_set)
    return html


def rewrite_css_urls_in_text(css_text: str, page_dir: str, site_root: str, domain_set: Set[str]) -> str:
    rel_prefix = _posix_relpath(page_dir, site_root)

    # url( ... )
    url_re = re.compile(r"(?i)url\(\s*(?P<q>[\"']?)(?P<u>[^\"')]+)(?P=q)\s*\)")

    def repl_url(m: re.Match) -> str:
        u = m.group("u")
        q = m.group("q") or ""
        new_u = _rewrite_one_url_value(u, domain_set, rel_prefix)
        return f"url({q}{new_u}{q})"

    css_text = url_re.sub(repl_url, css_text)

    # @import "..." or @import url(...)
    imp_re = re.compile(r"(?i)@import\s+(?:url\()?\s*(?P<q>[\"'])(?P<u>.*?)(?P=q)\s*\)?\s*;", re.DOTALL)

    def repl_imp(m: re.Match) -> str:
        q = m.group("q")
        u = m.group("u")
        new_u = _rewrite_one_url_value(u, domain_set, rel_prefix)
        return f"@import {q}{new_u}{q};"

    css_text = imp_re.sub(repl_imp, css_text)
    return css_text


def _local_path_for_original(original_url: str, mimetype: str) -> str:
    """Return a filesystem path relative to the site root (docs/)."""
    u = urlsplit(original_url)
    path = unquote(u.path or "/")
    if not path.startswith("/"):
        path = "/" + path
    # Normalize duplicate slashes
    path = re.sub(r"/{2,}", "/", path)

    mime = _norm_mime(mimetype)
    has_ext = bool(re.search(r"\.[a-z0-9]{1,8}$", path, re.IGNORECASE))

    if mime == "text/html" and (path.endswith("/") or not has_ext):
        if not path.endswith("/"):
            path = path + "/"
        # / -> index.html, /foo/ -> foo/index.html
        return path.lstrip("/") + "index.html"

    if path.endswith("/"):
        # Non-HTML with trailing slash: store as index.<ext> based on mime.
        ext = ".bin"
        if mime.startswith("image/"):
            ext = "." + mime.split("/", 1)[1]
        elif mime == "text/css":
            ext = ".css"
        elif mime.endswith("javascript"):
            ext = ".js"
        elif "json" in mime:
            ext = ".json"
        elif mime.endswith("xml"):
            ext = ".xml"
        return path.lstrip("/") + "index" + ext

    return path.lstrip("/")


def _ext_for_mime(mimetype: str) -> str:
    m = _norm_mime(mimetype)
    if not m:
        return ".bin"
    if m == "text/plain":
        return ".txt"
    if m == "text/css":
        return ".css"
    if m.endswith("javascript") or m in {"application/javascript", "text/javascript", "application/x-javascript"}:
        return ".js"
    if "json" in m:
        return ".json"
    if m.endswith("xml") or "xml" in m:
        return ".xml"
    if m.startswith("image/"):
        return "." + m.split("/", 1)[1]
    if m.startswith("font/"):
        return "." + m.split("/", 1)[1]
    return ".bin"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--domain", default="dodmayak.org")
    p.add_argument("--out", default=os.path.abspath(os.getcwd()))
    p.add_argument("--delay", type=float, default=0.2)
    p.add_argument("--max", type=int, default=0, help="0 = no limit")
    p.add_argument("--keep-age-gate", action="store_true", help="Keep the Age Gate (18+) overlay code")
    p.add_argument(
        "--include-age-gate-resources",
        action="store_true",
        help="Download Age Gate plugin assets and its wp-json endpoints",
    )
    p.add_argument("--no-rewrite", action="store_true", help="Do not rewrite HTML/CSS links")
    args = p.parse_args()

    out_root = os.path.abspath(args.out)
    docs_root = os.path.join(out_root, "docs")
    meta_root = os.path.join(out_root, "meta")
    _ensure_dir(docs_root)
    _ensure_dir(meta_root)

    # GitHub Pages: avoid Jekyll ignoring folders.
    _write_text(os.path.join(docs_root, ".nojekyll"), "")

    records = fetch_cdx(args.domain)
    if args.max and args.max > 0:
        records = records[: args.max]

    if not args.include_age_gate_resources:
        records = [r for r in records if not is_age_gate_resource(r.original)]

    domain_set = {args.domain.lower(), ("www." + args.domain).lower()}

    # Dedupe by local path (ignoring query strings) for simpler static hosting.
    chosen: List[Tuple[CdxRecord, str]] = []
    seen_paths: Set[str] = set()
    for r in records:
        lp = _local_path_for_original(r.original, r.mimetype)
        if lp in seen_paths:
            continue
        seen_paths.add(lp)
        chosen.append((r, lp))

    # Static filesystems cannot have both a file and a directory with the same name.
    # Example: /wp-json/oembed/1.0 and /wp-json/oembed/1.0/embed.
    dir_prefixes: Set[str] = set()
    for _, lp in chosen:
        parts = lp.split("/")
        for j in range(1, len(parts)):
            dir_prefixes.add("/".join(parts[:j]))
    if dir_prefixes:
        taken = {lp for _, lp in chosen}
        fixed: List[Tuple[CdxRecord, str]] = []
        for rec, lp in chosen:
            if lp in dir_prefixes:
                ext = _ext_for_mime(rec.mimetype)
                new_lp = lp + ext
                if new_lp in taken:
                    new_lp = lp + "__" + _sha1(rec.original)[:8] + ext
                taken.discard(lp)
                taken.add(new_lp)
                lp = new_lp
            fixed.append((rec, lp))
        chosen = fixed

    index_path = os.path.join(meta_root, "index.jsonl")
    errors_path = os.path.join(meta_root, "errors.jsonl")
    stats_path = os.path.join(meta_root, "stats.json")

    total = len(chosen)
    ok = 0
    skipped = 0
    failed = 0
    rewritten_html = 0
    rewritten_css = 0

    start = time.time()

    with open(index_path, "w", encoding="utf-8", newline="\n") as index_f, open(
        errors_path, "w", encoding="utf-8", newline="\n"
    ) as err_f:
        for i, (rec, local_rel) in enumerate(chosen, start=1):
            local_abs = os.path.join(docs_root, local_rel)
            archived_url = f"{WAYBACK_BASE}/{rec.timestamp}id_/{rec.original}"

            try:
                if os.path.exists(local_abs):
                    if os.path.isfile(local_abs):
                        skipped += 1
                        index_f.write(
                            json.dumps(
                                {
                                    "original": rec.original,
                                    "timestamp": rec.timestamp,
                                    "mimetype": _norm_mime(rec.mimetype),
                                    "statuscode": rec.statuscode,
                                    "archived_url": archived_url,
                                    "local_path": os.path.relpath(local_abs, out_root),
                                    "skipped": True,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                        continue
                    # Path exists but is not a file (likely a directory): treat as a conflict.
                    raise IsADirectoryError(local_abs)

                attempt = 0
                while True:
                    attempt += 1
                    try:
                        status, headers, data = fetch_wayback(archived_url, timeout=90)
                        break
                    except HTTPError as e:
                        if attempt < 4 and e.code in {429, 500, 502, 503, 504}:
                            time.sleep(1.0 * attempt)
                            continue
                        raise
                    except URLError:
                        if attempt < 4:
                            time.sleep(1.0 * attempt)
                            continue
                        raise

                data = _decompress_if_needed(data, headers.get("content-encoding", ""))

                mime = _norm_mime(headers.get("content-type", "")) or _norm_mime(rec.mimetype)

                did_rewrite = False
                if mime == "text/html":
                    if args.no_rewrite and args.keep_age_gate:
                        _write_bytes(local_abs, data)
                    else:
                        text = _decode_text(data, headers.get("content-type", ""))
                        if not args.keep_age_gate:
                            text = strip_age_gate_html(text)
                        if not args.no_rewrite:
                            page_dir = os.path.dirname(local_abs)
                            text = rewrite_html(text, page_dir=page_dir, site_root=docs_root, domain_set=domain_set)
                            rewritten_html += 1
                            did_rewrite = True
                        _write_text(local_abs, text)

                elif not args.no_rewrite and mime == "text/css":
                    text = _decode_text(data, headers.get("content-type", ""))
                    css_dir = os.path.dirname(local_abs)
                    rewritten = rewrite_css_urls_in_text(text, page_dir=css_dir, site_root=docs_root, domain_set=domain_set)
                    _write_text(local_abs, rewritten)
                    rewritten_css += 1
                    did_rewrite = True
                else:
                    # Save bytes as-is (after content-encoding decompression).
                    _write_bytes(local_abs, data)

                ok += 1
                index_f.write(
                    json.dumps(
                        {
                            "original": rec.original,
                            "timestamp": rec.timestamp,
                            "mimetype": mime,
                            "statuscode": rec.statuscode,
                            "archived_url": archived_url,
                            "local_path": os.path.relpath(local_abs, out_root),
                            "rewritten": did_rewrite,
                            "bytes": len(data),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

                if args.delay > 0:
                    time.sleep(args.delay)
                if i % 50 == 0:
                    elapsed = time.time() - start
                    print(
                        f"[{i}/{total}] ok={ok} skipped={skipped} failed={failed} "
                        f"html_rewrite={rewritten_html} css_rewrite={rewritten_css} elapsed={elapsed:.1f}s"
                    )

            except Exception as e:
                failed += 1
                err_f.write(
                    json.dumps(
                        {
                            "original": rec.original,
                            "timestamp": rec.timestamp,
                            "mimetype": _norm_mime(rec.mimetype),
                            "statuscode": rec.statuscode,
                            "archived_url": archived_url,
                            "local_path": os.path.relpath(local_abs, out_root),
                            "error": f"{type(e).__name__}: {e}",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                if args.delay > 0:
                    time.sleep(args.delay)

    stats = {
        "domain": args.domain,
        "out": out_root,
        "docs": os.path.relpath(docs_root, out_root),
        "total_records": len(records),
        "total_selected": total,
        "ok": ok,
        "skipped": skipped,
        "failed": failed,
        "rewritten_html": rewritten_html,
        "rewritten_css": rewritten_css,
        "seconds": round(time.time() - start, 3),
        "rewrite": not bool(args.no_rewrite),
        "keep_age_gate": bool(args.keep_age_gate),
        "include_age_gate_resources": bool(args.include_age_gate_resources),
    }
    _write_text(stats_path, json.dumps(stats, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(stats, ensure_ascii=False))
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
