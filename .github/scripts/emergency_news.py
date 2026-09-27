#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime, format_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

RSS = Path("rss.rss")
FEED = Path("feed.xml")
NZ = ZoneInfo("Pacific/Auckland")
NOW = datetime.now(NZ)
STALE_AFTER = timedelta(hours=8)
RETENTION = timedelta(hours=48)
MAX_ITEMS = 12

QUERIES = [
    '"New Zealand"',
    'Australia',
    'technology',
    'business',
    'travel',
    'science',
    'privacy',
    '"Microsoft Fabric"',
    'Wellington',
    'Melbourne',
    'bitcoin OR ethereum',
]

INTEREST = {
    "new zealand": 5, "nz ": 4, "wellington": 5, "australia": 4, "melbourne": 4,
    "privacy": 4, "data breach": 5, "security": 3, "travel": 3, "airline": 3,
    "visa": 5, "tenancy": 5, "rent": 3, "microsoft fabric": 6, "power bi": 4,
    "bitcoin": 3, "ethereum": 3, "crypto": 2, "streaming": 2, "gaming": 2,
    "hiking": 4, "track": 2, "trail": 3, "science": 2, "longevity": 4,
}
PRACTICAL = {
    "launch": 2, "opens": 2, "opening": 2, "closes": 2, "closure": 3, "reopen": 3,
    "law": 3, "rule": 3, "regulation": 3, "ban": 3, "approval": 2, "recall": 4,
    "price": 2, "rate": 2, "fee": 2, "cost": 2, "flight": 2, "airport": 2,
    "breach": 4, "vulnerability": 3, "release": 2, "available": 2, "deal": 1,
    "jobs": 2, "employment": 2, "housing": 2, "mortgage": 2, "entry": 2,
}

UA = "Mozilla/5.0 (compatible; personalised-gpt-news-emergency/1.0)"

def get(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def parse_rfc822(value: str) -> datetime:
    dt = parsedate_to_datetime(value.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(NZ)

def item_pubdate(block: str):
    m = re.search(r"<pubDate>(.*?)</pubDate>", block, re.S | re.I)
    if not m:
        return None
    try:
        return parse_rfc822(m.group(1))
    except Exception:
        return None

def split_items(text: str):
    return re.findall(r"\s*<item>.*?</item>", text, flags=re.S | re.I)

def latest_date(text: str):
    dates = [d for d in (item_pubdate(x) for x in split_items(text)) if d]
    return max(dates) if dates else None

def remove_items(text: str):
    return re.sub(r"\s*<item>.*?</item>", "", text, flags=re.S | re.I)

def retained_items(text: str):
    cutoff = NOW - RETENTION
    kept = []
    for block in split_items(text):
        d = item_pubdate(block)
        if d and d >= cutoff:
            kept.append(block.strip())
    return kept

def channel_base(text: str) -> str:
    base = remove_items(text)
    base = re.sub(
        r"(<channel>.*?<pubDate>).*?(</pubDate>)",
        lambda m: m.group(1) + format_datetime(NOW) + m.group(2),
        base,
        count=1,
        flags=re.S | re.I,
    )
    return base

def rebuild(base: str, items: list[str]) -> str:
    marker = "</channel>"
    pos = base.lower().find(marker)
    if pos < 0:
        raise RuntimeError("Invalid RSS: </channel> missing")
    body = base[:pos].rstrip()
    if items:
        body += "\n\t\t" + "\n\t\t".join(items)
    return body + "\n\t</channel>\n</rss>\n"

def gdelt(query: str):
    params = urllib.parse.urlencode({
        "query": query,
        "mode": "ArtList",
        "maxrecords": 80,
        "format": "json",
        "sort": "HybridRel",
        "timespan": "1d",
    })
    url = "https://api.gdeltproject.org/api/v2/doc/doc?" + params
    data = json.loads(get(url, 20).decode("utf-8", "replace"))
    return data.get("articles", [])

def clean_text(s: str) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s

def meta_description(url: str) -> str:
    try:
        raw = get(url, 10)[:600_000].decode("utf-8", "replace")
    except Exception:
        return ""
    patterns = [
        r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+content=["\'](.*?)["\'][^>]+(?:name|property)=["\'](?:description|og:description)["\']',
    ]
    for p in patterns:
        m = re.search(p, raw, re.I | re.S)
        if m:
            d = clean_text(m.group(1))
            if 50 <= len(d) <= 500:
                return d
    return ""

def seen_dt(a):
    raw = str(a.get("seendate") or "")
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).astimezone(NZ)
        except Exception:
            pass
    return NOW

def score(a):
    title = clean_text(a.get("title", "")).lower()
    domain = str(a.get("domain", "")).lower()
    s = 0
    for k, v in INTEREST.items():
        if k in title:
            s += v
    for k, v in PRACTICAL.items():
        if k in title:
            s += v
    if domain.endswith(".nz"):
        s += 3
    age_h = max(0, (NOW - seen_dt(a)).total_seconds() / 3600)
    s += max(0, 4 - age_h / 6)
    if any(x in title for x in ("opinion", "analysis:", "commentary", "live updates")):
        s -= 4
    if any(x in title for x in ("ukraine", "iran", "gaza")) and not any(
        x in title for x in ("ceasefire", "peace deal", "peace agreement", "settlement", "war ends")
    ):
        s -= 5
    return s

def norm_title(t: str):
    return re.sub(r"[^a-z0-9]+", " ", clean_text(t).lower()).strip()

def candidates(existing: str):
    found = []
    for q in QUERIES:
        try:
            found.extend(gdelt(q))
        except Exception as e:
            print(f"GDELT query failed: {q}: {e}", file=sys.stderr)
        time.sleep(0.5)

    uniq = {}
    for a in found:
        url = str(a.get("url") or "")
        title = clean_text(a.get("title", ""))
        if not url.startswith("http") or len(title) < 20:
            continue
        if url in existing:
            continue
        key = norm_title(title)
        if len(key) < 12:
            continue
        cur = uniq.get(key)
        if cur is None or score(a) > score(cur):
            uniq[key] = a

    ranked = sorted(uniq.values(), key=score, reverse=True)
    selected = []
    domains = {}
    digital = 0
    medical = 0
    entertainment = 0

    for a in ranked:
        if len(selected) >= MAX_ITEMS:
            break
        title_l = clean_text(a.get("title", "")).lower()
        domain = str(a.get("domain", "")).lower()
        if domains.get(domain, 0) >= 2:
            continue

        is_med = any(x in title_l for x in ("health", "medical", "disease", "drug", "cancer", "longevity"))
        if is_med and medical >= 1:
            continue

        is_ent = any(x in title_l for x in ("movie", "film", "game", "gaming", "netflix", "streaming"))
        if is_ent and entertainment >= 1:
            continue

        is_digital = any(x in title_l for x in (
            "software", "ai ", "artificial intelligence", "cyber", "privacy", "data", "app ",
            "google", "microsoft", "android", "browser", "streaming", "gaming", "crypto",
            "bitcoin", "ethereum"
        ))
        if is_digital and digital >= MAX_ITEMS // 2:
            continue

        selected.append(a)
        domains[domain] = domains.get(domain, 0) + 1
        digital += int(is_digital)
        medical += int(is_med)
        entertainment += int(is_ent)

    return selected

def follow_link(text: str) -> str:
    prompt = f'I read the following article: "{text[:1800]}". I have a question: '
    q = urllib.parse.quote(prompt, safe="")
    return f"intent://chatgpt.com/new?q={q}#Intent;scheme=https;package=app.vanadium.browser;end"

def make_item(selected):
    li = []
    for a in selected:
        title = clean_text(a.get("title", ""))
        url = str(a.get("url") or "")
        desc = meta_description(url)
        if not desc:
            desc = title
        if len(desc) > 360:
            desc = desc[:357].rsplit(" ", 1)[0] + "..."
        d = seen_dt(a)
        news_text = f"{title}. {desc}"
        li.append(
            "<li><strong>{}</strong> {} <em>{}.</em> "
            '<a href="{}">Source</a> · <a href="{}">Follow up</a></li>'.format(
                html.escape(title),
                html.escape(desc),
                d.strftime("%d %b %Y"),
                html.escape(url, quote=True),
                html.escape(follow_link(news_text), quote=True),
            )
        )

    headline = "Fresh Practical Changes Across Travel, Money and Tech"
    intro = "Fresh developments across practical New Zealand, Australia, technology, travel and market choices."
    content = "<p>{}</p>\n<ol>\n{}\n</ol>".format(html.escape(intro), "\n".join(li))
    teaser = "A GitHub-native fallback briefing covering fresh practical developments while the primary publisher is stale."
    guid = "briefing-emergency-" + NOW.strftime("%Y%m%dT%H%M%S%z")
    pub = format_datetime(NOW)

    return (
        "<item>\n"
        f'\t\t\t<guid isPermaLink="false">{guid}</guid>\n'
        f"\t\t\t<title>{html.escape(headline)}</title>\n"
        f"\t\t\t<description><![CDATA[{teaser}]]></description>\n"
        f"\t\t\t<content:encoded><![CDATA[{content}]]></content:encoded>\n"
        "\t\t\t<dc:creator><![CDATA[GitHub Actions fallback]]></dc:creator>\n"
        f"\t\t\t<pubDate>{pub}</pubDate>\n"
        "\t\t</item>"
    )

def main():
    if not RSS.exists() or not FEED.exists():
        raise SystemExit("rss.rss/feed.xml missing")

    rss = RSS.read_text(encoding="utf-8")
    feed = FEED.read_text(encoding="utf-8")

    # Use the file with more items as the source if a previous partial write exists.
    source = rss if len(split_items(rss)) >= len(split_items(feed)) else feed
    latest = latest_date(source)

    kept = retained_items(source)
    base = channel_base(source)

    if latest and NOW - latest < STALE_AFTER:
        # Still normalize rolling retention and synchronization if necessary.
        rebuilt = rebuild(base, kept)
        if rss != rebuilt or feed != rebuilt:
            RSS.write_text(rebuilt, encoding="utf-8")
            FEED.write_text(rebuilt, encoding="utf-8")
            print("Fresh briefing exists; synchronized/retained RSS only.")
        else:
            print("Fresh briefing exists; no emergency item needed.")
        return

    selected = candidates(source)
    if len(selected) < 6:
        raise SystemExit(f"Only {len(selected)} usable emergency candidates found; refusing weak publication")

    item = make_item(selected)
    updated = rebuild(base, [item] + kept)
    if "<item>" not in updated or "<content:encoded>" not in updated:
        raise SystemExit("Generated RSS validation failed")

    for block in split_items(updated):
        if re.search(r"<link>", block, re.I):
            raise SystemExit("Item-level <link> detected; refusing publication")

    RSS.write_text(updated, encoding="utf-8")
    FEED.write_text(updated, encoding="utf-8")
    print(f"Emergency briefing prepared with {len(selected)} items.")

if __name__ == "__main__":
    main()
