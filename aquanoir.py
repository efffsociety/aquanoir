#!/usr/bin/env python3
"""
Aqua Noir — Miami Dolphins Intelligence Digest
Posts to @aquanoirr.bsky.social via AT Protocol
Runs daily at noon via cron
"""

import os
import json
import time
import hashlib
import requests
from datetime import datetime, timezone
from anthropic import Anthropic

# ── Config ────────────────────────────────────────────────────────────────────

BSKY_HANDLE    = "aquanoirr.bsky.social"
BSKY_PASSWORD  = os.environ["BSKY_APP_PASSWORD"]    # set in environment
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]    # set in environment
WEATHER_KEY    = os.environ.get("WEATHER_API_KEY")  # openweathermap.org free tier
SPORTSDB_KEY   = os.environ.get("SPORTSDB_KEY", "3")  # thesportsdb.com — 3 is free

POSTED_LOG     = "/data/aquanoir_posted.json"       # persistent log
BSKY_API       = "https://bsky.social/xrpc"

# Dolphins team ID on TheSportsDB
DOLPHINS_TEAM_ID = "134919"

# ── Approved sources ──────────────────────────────────────────────────────────

APPROVED_SOURCES = [
    # Official
    "miamidolphins.com",
    "nfl.com",
    # Beat reporters / outlets
    "si.com",              # Alain Poupart, Jacob Westendorf
    "miamiherald.com",     # Isaiah Smalls, Barry Jackson, Omar Kelly
    "sun-sentinel.com",    # Chris Perkins, David Furones
    "palmbeachpost.com",   # Joe Schad
    "theathletic.com",     # Dolphins beat
    "espn.com",            # Marcel Louis-Jacques, Darlington, Schefter
    "nflnetwork.com",      # Rapoport, Pelissero, Rich Eisen
    "the33rdteam.com",     # Ari Meirov
    "thedraftnetwork.com", # Kyle Crabbs
    "profootballtalk.com", # Mike Florio — facts and transactions only
    "profootballreference.com",
    "nbcsports.com",       # Chris Simms — confirmed news only
    "patmcafeeshow.com",   # Pat McAfee — confirmed news/story breaks only
]

# Secondary sources — credible but skew toward analysis.
# Claude should only pull confirmed facts and story breaks from these, never takes or opinions.
SECONDARY_SOURCES = [
    "profootballtalk.com", # Mike Florio
    "nbcsports.com",       # Chris Simms
    "patmcafeeshow.com",   # Pat McAfee
    "nflnetwork.com",      # Rich Eisen
]

SEARCH_QUERIES = [
    "Miami Dolphins",
]

# Official outlets only — used to detect new beat writers covering the team
BEAT_WRITER_OUTLETS = [
    "espn.com",
    "miamiherald.com",
    "sun-sentinel.com",
    "palmbeachpost.com",
    "si.com",
    "theathletic.com",
    "nfl.com",
    "profootballtalk.com",
]

SYSTEM_PROMPT = """You are Aqua Noir — a terse, precise Miami Dolphins intelligence account on Bluesky.
Been watching since Marino retired. January 26, 2000.

Given a list of Dolphins news items, generate posts. Each post follows this exact format:

POST:
🐬 [fact from the article. One sentence max. No speculation. Under 180 characters.]

#NFL #MiamiDolphins #FinsUp
SOURCE_URL: [the article url]

Rules:
- Facts only. Transactions, signings, injuries, official statements. No analysis, no implications, no "this means" commentary.
- No opinions, no analysis beyond what is in the article.
- No emojis except 🐬 at the start.
- Prioritize breaking news and transactions from the last 6 hours above all else. Beat writer features and analysis are acceptable if from the last 48 hours. Anything older should be skipped.
- Skip pure opinion pieces, rankings and hot takes.
- Only use these approved sources: miamidolphins.com, nfl.com, si.com, miamiherald.com, sun-sentinel.com, palmbeachpost.com, theathletic.com, espn.com, nflnetwork.com, the33rdteam.com, thedraftnetwork.com, profootballtalk.com, profootballreference.com, nbcsports.com, patmcafeeshow.com. Reject anything from heavy.com, bleacherreport.com, fansided.com, or any fan/aggregator site.
- RELEVANCE FILTER — only post a story if it meets one of these criteria:
  1. Involves a current Dolphins player, coach or front office member
  2. Alumni milestone only — Hall of Fame, death, or major award. Skip all other alumni news.
  3. Legal or lawsuit story — only if there is an official NFL ruling or penalty that directly affects the current or future team (draft picks, salary cap, ownership). Lawsuit updates with no ruling or consequence should be skipped.
  4. Off-field conduct — only post if the NFL or Miami Dolphins organization issues an official ruling, penalty, suspension or roster action as a direct result. Never post the underlying incident such as an arrest, allegation or personal behavior. Only the official organizational response.
  5. Trade rumors — only post if two or more approved beat reporters have independently reported the same rumor. Single source speculation should be skipped.
- If a story does not clearly meet one of the above criteria, skip it.
- For secondary sources (PFT/Mike Florio, NBC Sports/Chris Simms, Pat McAfee Show, Rich Eisen/NFL Network) only post if the content is a confirmed story break or transaction. Never post their analysis or opinions.
- Generate one POST per distinct news item.
- Max 3 posts per run.
- If no new worthy news, output: NO_NEWS.
- Never use hyphens or em dashes.
- Never use Oxford commas.
- Keep sentence structure direct and clean.
- If an article covers the same underlying event or news as something already in the posted log — even from a different outlet — skip it. The test is whether the underlying fact is new, not whether the URL or outlet is new. For example if Manny Fernandez passing away was already posted from miamidolphins.com, do not post the same news from sun-sentinel.com.
- When posting a meaningful follow-up, start the post with "Update:" instead of 🐬. No emoji on updates.

Output only the POST blocks. No other text."""

# ── Bluesky auth ──────────────────────────────────────────────────────────────

def bsky_login():
    res = requests.post(f"{BSKY_API}/com.atproto.server.createSession", json={
        "identifier": BSKY_HANDLE,
        "password": BSKY_PASSWORD
    })
    res.raise_for_status()
    data = res.json()
    return data["accessJwt"], data["did"]


def fetch_link_card(url):
    """Fetch Open Graph metadata for a URL to build a Bluesky link card embed."""
    try:
        res = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if not res.ok:
            return None
        from html.parser import HTMLParser

        class OGParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.og = {}
            def handle_starttag(self, tag, attrs):
                if tag == "meta":
                    d = dict(attrs)
                    prop = d.get("property", d.get("name", ""))
                    content = d.get("content", "")
                    if prop in ("og:title", "og:description", "og:image", "og:url"):
                        self.og[prop] = content

        parser = OGParser()
        parser.feed(res.text[:20000])
        og = parser.og

        title = og.get("og:title", "")
        description = og.get("og:description", "")
        thumb_url = og.get("og:image", "")
        canonical = og.get("og:url", url)

        if not title:
            return None

        card = {
            "$type": "app.bsky.embed.external",
            "external": {
                "uri": canonical,
                "title": title[:300],
                "description": description[:1000],
            }
        }

        return card
    except Exception as e:
        print(f"Link card fetch error: {e}")
        return None


def truncate_post(text, limit=280):
    """Truncate post to fit Bluesky 300 grapheme limit."""
    if len(text) <= limit:
        return text
    return text[:limit - 3] + "..."


def build_facets(text):
    """Build Bluesky facets for clickable hashtags and URLs."""
    import re
    facets = []

    # Hashtags
    for match in re.finditer(r'#[\w]+', text):
        start = len(text[:match.start()].encode("utf-8"))
        end = len(text[:match.end()].encode("utf-8"))
        facets.append({
            "index": {"byteStart": start, "byteEnd": end},
            "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": match.group()[1:]}]
        })

    # URLs
    for match in re.finditer(r'https?://[^\s]+', text):
        url = match.group().rstrip('/.,;:)')
        start = len(text[:match.start()].encode("utf-8"))
        end = start + len(url.encode("utf-8"))
        facets.append({
            "index": {"byteStart": start, "byteEnd": end},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}]
        })

    return facets


def bsky_post(jwt, did, text, reply_to=None, embed=None):
    """Post a skeet. If reply_to is set, post as a thread reply."""
    text = truncate_post(text)
    facets = build_facets(text)
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.now(timezone.utc).isoformat()
    }

    if facets:
        record["facets"] = facets

    if embed:
        record["embed"] = embed

    if reply_to:
        record["reply"] = {
            "root": reply_to["root"],
            "parent": reply_to["parent"]
        }

    res = requests.post(
        f"{BSKY_API}/com.atproto.repo.createRecord",
        headers={"Authorization": f"Bearer {jwt}"},
        json={
            "repo": did,
            "collection": "app.bsky.feed.post",
            "record": record
        }
    )
    if not res.ok:
        print(f"Bluesky error {res.status_code}: {res.text}")
        res.raise_for_status()
    return res.json()

# ── Posted log ────────────────────────────────────────────────────────────────

def load_log():
    try:
        with open(POSTED_LOG, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_log(log):
    os.makedirs(os.path.dirname(POSTED_LOG), exist_ok=True)
    with open(POSTED_LOG, "w") as f:
        json.dump(log[-500:], f, indent=2)  # keep last 500


def story_id(url, text):
    """Fingerprint a story by URL first, fall back to text hash."""
    if url:
        return hashlib.md5(url.strip().encode()).hexdigest()
    return hashlib.md5(text.strip()[:120].encode()).hexdigest()


def already_posted(log, text, url=""):
    sid = story_id(url, text)
    return any(entry.get("id") == sid for entry in log)


def mark_posted(log, text, url):
    log.append({
        "id": story_id(url, text),
        "text": text[:200],
        "url": url,
        "date": datetime.now(timezone.utc).isoformat()
    })

# ── News fetch ────────────────────────────────────────────────────────────────

def fetch_news():
    """
    Fetch recent Dolphins news via NewsAPI.
    Falls back to a curated RSS approach if NewsAPI key not set.
    Set NEWS_API_KEY in environment for live use.
    """
    api_key = os.environ.get("NEWS_API_KEY")
    articles = []

    if api_key:
        for query in SEARCH_QUERIES:
            try:
                res = requests.get("https://newsapi.org/v2/everything", params={
                    "q": query,
                    "language": "en",
                    "sortBy": "publishedAt",
                    "pageSize": 10,
                    "apiKey": api_key
                }, timeout=10)
                res.raise_for_status()
                for a in res.json().get("articles", []):
                    url = a.get("url", "")
                    # Only approved sources
                    if any(src in url for src in APPROVED_SOURCES):
                        articles.append({
                            "title": a.get("title", ""),
                            "description": a.get("description", ""),
                            "url": url,
                            "published": a.get("publishedAt", "")
                        })
            except Exception as e:
                print(f"NewsAPI error for '{query}': {e}")
    # Always run RSS regardless of NewsAPI — parallel source
    print("Starting RSS fetch...")
    import xml.etree.ElementTree as ET
    rss_feeds = [
        "https://www.miamidolphins.com/rss/news",
        "https://www.profootballtalk.com/feed/",
        "https://www.espn.com/espn/rss/nfl/news",
        "https://www.nfl.com/rss/rsslanding?searchString=miami+dolphins",
    ]
    for feed_url in rss_feeds:
        try:
            res = requests.get(feed_url, timeout=10, headers={"User-Agent": "Mozilla/5.0 (compatible; AquaNoirBot/1.0)"})
            print(f"RSS {feed_url.split('/')[2]}: status {res.status_code}")
            root = ET.fromstring(res.content)
            items = root.findall(".//item")
            print(f"RSS {feed_url.split('/')[2]}: {len(items)} items")
            for item in items[:10]:
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                desc = item.findtext("description", "")
                # Fix smart quote encoding issues
                def fix_quotes(s):
                    return s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("â", "'").replace(" ", " ")
                title = fix_quotes(title)
                desc = fix_quotes(desc)
                if title and link:
                    articles.append({
                        "title": title,
                        "description": desc,
                        "url": link,
                        "published": item.findtext("pubDate", "")
                    })
        except Exception as e:
            print(f"RSS error for {feed_url.split('/')[2]}: {e}")

    # Deduplicate by URL
    seen = set()
    unique = []
    for a in articles:
        if a["url"] not in seen:
            seen.add(a["url"])
            unique.append(a)

    # Filter by recency — drop anything older than 48 hours
    cutoff = datetime.now(timezone.utc).timestamp() - (48 * 3600)
    fresh = []
    for a in unique:
        pub = a.get("published", "")
        if not pub:
            fresh.append(a)  # No date — include it, let Claude judge
            continue
        try:
            from email.utils import parsedate_to_datetime
            import dateutil.parser
            try:
                dt = dateutil.parser.parse(pub)
            except Exception:
                dt = parsedate_to_datetime(pub)
            if dt.timestamp() >= cutoff:
                fresh.append(a)
        except Exception:
            fresh.append(a)  # Can't parse date — include it

    print(f"Articles after 48hr freshness filter: {len(fresh)} of {len(unique)}")
    return fresh[:20]  # Cap at 20 for Claude context

# ── Claude digest generation ──────────────────────────────────────────────────

def should_run_web_search():
    """Only run web search every 6 hours to save costs."""
    web_search_log = "/data/aquanoir_websearch.json"
    try:
        with open(web_search_log, "r") as f:
            data = json.load(f)
        last_run = datetime.fromisoformat(data.get("last_run", "2000-01-01T00:00:00+00:00"))
        if last_run.tzinfo is None:
            last_run = last_run.replace(tzinfo=timezone.utc)
        hours_since = (datetime.now(timezone.utc) - last_run).total_seconds() / 3600
        hours_until = max(0, 6 - hours_since)
        print(f"Hours since last web search: {hours_since:.1f} — next in {hours_until:.1f}hrs")
        return hours_since >= 6
    except Exception:
        return True


def mark_web_search_ran():
    """Record that web search just ran."""
    web_search_log = "/data/aquanoir_websearch.json"
    os.makedirs(os.path.dirname(web_search_log), exist_ok=True)
    with open(web_search_log, "w") as f:
        json.dump({"last_run": datetime.now(timezone.utc).isoformat()}, f)


def generate_posts(articles, log):
    client = Anthropic(api_key=ANTHROPIC_KEY)

    # Build context from NewsAPI articles
    news_text = ""
    if articles:
        for a in articles:
            news_text += f"TITLE: {a['title']}\n"
            news_text += f"URL: {a['url']}\n"
            if a.get("description"):
                news_text += f"DESCRIPTION: {a['description']}\n"
            news_text += "\n"
    else:
        news_text = "No NewsAPI articles available.\n"

    # Add already-posted context to avoid repeats
    if log:
        recent = log[-50:]
        already = "\n".join(f"- {e['text']}" for e in recent)
        news_text += f"\nALREADY POSTED — do not repeat:\n{already}\n"

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    use_web_search = should_run_web_search()
    print(f"Web search due: {use_web_search}")

    if use_web_search:
        print("Running web search cycle (6hr)...")
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}]
        # Get today and yesterday for specific date search
        from datetime import timedelta
        today = datetime.now(timezone.utc).strftime("%B %d %Y")
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%B %d %Y")

        user_prompt = f"""Current time: {now_str}

Search for Miami Dolphins news published on {today} or {yesterday} only. Use a date-specific query like "Miami Dolphins {today}" to find the most recent articles. Focus on si.com, miamiherald.com, sun-sentinel.com, theathletic.com and palmbeachpost.com. Ignore anything older than 48 hours.

NewsAPI articles:
{news_text}

Generate posts now. Max 3 posts."""
    else:
        print("Running NewsAPI/RSS cycle (30min)...")
        tools = []
        user_prompt = f"""Current time: {now_str}

Using only the NewsAPI articles below, generate posts for any new Miami Dolphins news not already posted. Max 3 posts.

NewsAPI articles:
{news_text}"""

    kwargs = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}]
    }
    if tools:
        kwargs["tools"] = tools

    try:
        message = client.messages.create(**kwargs)
    except Exception as e:
        print(f"Claude API error: {e}")
        return []

    if use_web_search:
        mark_web_search_ran()

    # Extract text from response
    print(f"Response blocks: {[(b.type if hasattr(b, 'type') else str(b)) for b in message.content]}")
    raw = ""
    for block in message.content:
        if hasattr(block, "type") and block.type == "text":
            raw += block.text

    raw = raw.strip()
    print(f"Claude raw output: {raw[:300]}")

    if not raw or raw == "NO_NEWS":
        print("Claude found no new worthy news.")
        return []

    # Parse POST blocks
    posts = []
    blocks = raw.split("POST:")[1:]
    for block in blocks:
        lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
        fact_lines = []
        url = ""
        for line in lines:
            if line.startswith("SOURCE_URL:"):
                url = line.replace("SOURCE_URL:", "").strip()
            else:
                fact_lines.append(line)

        fact_text = "\n".join(fact_lines).strip()

        if fact_text and url:
            if not already_posted(log, fact_text, url):
                posts.append({"fact": fact_text, "url": url})

    return posts[:3]

# ── Game day post ────────────────────────────────────────────────────────────

# Stadium coordinates for weather lookup
STADIUM_COORDS = {
    "Hard Rock Stadium": (25.9579, -80.2389),
    "Highmark Stadium": (42.7738, -78.7868),
    "Gillette Stadium": (42.0909, -71.2643),
    "MetLife Stadium": (40.8128, -74.0742),
    "Allegiant Stadium": (36.0909, -115.1833),
    "Levi's Stadium": (37.4033, -121.9694),
    "Arrowhead Stadium": (39.0489, -94.4839),
    "SoFi Stadium": (33.9535, -118.3392),
    "AT&T Stadium": (32.7480, -97.0929),
    "Lincoln Financial Field": (39.9008, -75.1675),
    "Bank of America Stadium": (35.2258, -80.8528),
    "Mercedes-Benz Stadium": (33.7554, -84.4008),
    "Soldier Field": (41.8623, -87.6167),
    "Ford Field": (42.3400, -83.0456),
    "US Bank Stadium": (44.9736, -93.2575),
    "Lambeau Field": (44.5013, -88.0622),
    "Paycor Stadium": (39.0954, -84.5160),
    "FirstEnergy Stadium": (41.5061, -81.6995),
    "Acrisure Stadium": (40.4468, -80.0158),
    "M&T Bank Stadium": (39.2780, -76.6227),
    "Raymond James Stadium": (27.9759, -82.5033),
    "EverBank Stadium": (30.3239, -81.6373),
    "Nissan Stadium": (36.1665, -86.7713),
    "Lucas Oil Stadium": (39.7601, -86.1639),
    "NRG Stadium": (29.6847, -95.4107),
    "State Farm Stadium": (33.5276, -112.2626),
    "Empower Field": (39.7439, -105.0200),
    "Lumen Field": (47.5952, -122.3316),
    "Caesars Superdome": (29.9511, -90.0812),
    "Huntington Bank Field": (41.5061, -81.6995),
    "Commanders Field": (38.9078, -76.8645),
}

TV_NETWORKS = {
    "CBS": "CBS · Paramount+",
    "FOX": "FOX · NFL+",
    "NBC": "NBC · Peacock",
    "ESPN": "ESPN · ESPN+",
    "ABC": "ABC · ESPN+",
    "NFL Network": "NFL Network · NFL+",
    "Amazon": "Prime Video",
    "Peacock": "Peacock",
}

def get_dolphins_game_today():
    """Check if Dolphins play today and return game info."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        res = requests.get(
            f"https://www.thesportsdb.com/api/v1/json/{SPORTSDB_KEY}/eventsnext.php",
            params={"id": DOLPHINS_TEAM_ID},
            timeout=10
        )
        res.raise_for_status()
        events = res.json().get("events", [])
        for event in events:
            event_date = event.get("dateEvent", "")
            if event_date == today:
                return event
    except Exception as e:
        print(f"Schedule fetch error: {e}")
    return None


def get_weather(lat, lon):
    """Fetch current weather for stadium location."""
    if not WEATHER_KEY:
        return None
    try:
        res = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={
                "lat": lat,
                "lon": lon,
                "appid": WEATHER_KEY,
                "units": "imperial"
            },
            timeout=10
        )
        res.raise_for_status()
        data = res.json()
        temp = round(data["main"]["temp"])
        desc = data["weather"][0]["description"].capitalize()
        wind = round(data["wind"]["speed"])
        wind_deg = data["wind"].get("deg", 0)
        directions = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
        wind_dir = directions[round(wind_deg / 45) % 8]
        return f"{temp}°F · {desc} · Wind {wind} mph {wind_dir}"
    except Exception as e:
        print(f"Weather fetch error: {e}")
        return None


def get_weather_emoji(description):
    desc = description.lower()
    if any(w in desc for w in ["thunder", "storm"]):
        return "⛈"
    if any(w in desc for w in ["rain", "drizzle", "shower"]):
        return "🌧"
    if "snow" in desc:
        return "🌨"
    if "cloud" in desc:
        return "⛅"
    if "clear" in desc or "sun" in desc:
        return "☀️"
    return "🌤"


def build_kickoff_post(event):
    """Build the Today's Kickoff post from game data."""
    home_team = event.get("strHomeTeam", "")
    away_team = event.get("strAwayTeam", "")
    venue = event.get("strVenue", "")
    city = event.get("strCity", "")
    state = event.get("strCountry", "")
    time_str = event.get("strTime", "")
    network = event.get("strTVStation", "")

    # Format date and time
    date_str = event.get("dateEvent", "")
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
        formatted_time = dt.strftime("%A %b %-d · %-I:%M %p ET")
    except Exception:
        formatted_time = f"{date_str} · {time_str} ET"

    # Format matchup
    matchup = f"{away_team} @ {home_team}"

    # Location
    location = f"{venue} · {city} {state}".strip(" ·")

    # Weather
    weather_line = ""
    coords = STADIUM_COORDS.get(venue)
    if coords:
        weather_raw = get_weather(coords[0], coords[1])
        if weather_raw:
            emoji = get_weather_emoji(weather_raw)
            weather_line = f"{emoji} {weather_raw}"

    # TV
    tv_display = TV_NETWORKS.get(network, network)

    # Build post
    lines = [
        "Today's Kickoff",
        "",
        matchup,
        location,
        "",
        formatted_time,
    ]
    if weather_line:
        lines.append(weather_line)
    lines.extend(["", tv_display])

    return "\n".join(lines)


def maybe_post_kickoff(jwt, did, log, event):
    """Post Today's Kickoff if game is today and not yet posted."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    kickoff_key = f"kickoff_{today}"

    # Check if already posted today
    if any(e.get("id") == kickoff_key for e in log):
        return

    if not event:
        return

    # Check if we're within 3-4 hours of kickoff
    time_str = event.get("strTime", "")
    date_str = event.get("dateEvent", "")
    try:
        kickoff = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        hours_until = (kickoff - now).total_seconds() / 3600
        if not (2.5 <= hours_until <= 3.5):
            return
    except Exception:
        return

    post_text = build_kickoff_post(event)
    result = bsky_post(jwt, did, post_text)
    log.append({
        "id": kickoff_key,
        "text": "Today's Kickoff post",
        "uri": result.get("uri", ""),
        "cid": result.get("cid", ""),
        "date": datetime.now(timezone.utc).isoformat()
    })
    print(f"Posted kickoff post for {event.get('strHomeTeam')} vs {event.get('strAwayTeam')}")


# ── Game day inactives ───────────────────────────────────────────────────────

def get_inactives(team_name, event_id):
    """
    Fetch inactives and game day elevations for a team.
    Uses TheSportsDB lineup endpoint — falls back to NewsAPI search if unavailable.
    """
    try:
        res = requests.get(
            f"https://www.thesportsdb.com/api/v1/json/{SPORTSDB_KEY}/lookuplineupevent.php",
            params={"id": event_id},
            timeout=10
        )
        res.raise_for_status()
        data = res.json()
        lineup = data.get("lineup", [])
        inactives = [p["strPlayer"] for p in lineup if p.get("strStatus", "").lower() == "inactive"]
        elevations = [p["strPlayer"] for p in lineup if "elevation" in p.get("strStatus", "").lower()]
        return inactives, elevations
    except Exception as e:
        print(f"Inactives fetch error for {team_name}: {e}")
        return [], []


def build_inactives_post(team_name, inactives, elevations):
    """Build the inactives reply post for a team."""
    lines = [f"{team_name} Inactives"]
    if inactives:
        lines.extend(inactives)
    else:
        lines.append("Not yet available.")
    if elevations:
        lines.append("")
        lines.append("Game Day Elevations")
        lines.extend(elevations)
    return "\n".join(lines)


def maybe_post_inactives(jwt, did, log, event):
    """Post inactives thread as replies to the kickoff post — fires ~90 min before kickoff."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    inactives_key = f"inactives_{today}"

    # Check if already posted
    if any(e.get("id") == inactives_key for e in log):
        return

    # Check timing — fire between 80 and 100 minutes before kickoff
    time_str = event.get("strTime", "")
    date_str = event.get("dateEvent", "")
    try:
        kickoff = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        minutes_until = (kickoff - now).total_seconds() / 60
        if not (80 <= minutes_until <= 100):
            return
    except Exception:
        return

    event_id = event.get("idEvent", "")
    home_team = event.get("strHomeTeam", "")
    away_team = event.get("strAwayTeam", "")

    # Determine which is Dolphins and which is opponent
    dolphins_name = "Miami Dolphins"
    opponent_name = home_team if away_team == dolphins_name else away_team

    # Fetch inactives for both teams
    dolphins_inactives, dolphins_elevations = get_inactives(dolphins_name, event_id)
    opponent_inactives, opponent_elevations = get_inactives(opponent_name, event_id)

    # Find the kickoff post URI to reply to
    kickoff_entry = next((e for e in log if e.get("id") == f"kickoff_{today}"), None)
    if not kickoff_entry or not kickoff_entry.get("uri"):
        print("Kickoff post URI not found — cannot thread inactives.")
        return

    root_uri = kickoff_entry["uri"]
    root_cid = kickoff_entry["cid"]

    # Post Dolphins inactives as reply to kickoff
    dolphins_text = build_inactives_post("Dolphins", dolphins_inactives, dolphins_elevations)
    reply_ref = {
        "root": {"uri": root_uri, "cid": root_cid},
        "parent": {"uri": root_uri, "cid": root_cid}
    }
    result1 = bsky_post(jwt, did, dolphins_text, reply_to=reply_ref)
    time.sleep(2)

    # Post opponent inactives as reply to kickoff
    opponent_text = build_inactives_post(opponent_name, opponent_inactives, opponent_elevations)
    reply_ref2 = {
        "root": {"uri": root_uri, "cid": root_cid},
        "parent": {"uri": result1["uri"], "cid": result1["cid"]}
    }
    bsky_post(jwt, did, opponent_text, reply_to=reply_ref2)

    # Log it
    log.append({"id": inactives_key, "date": datetime.now(timezone.utc).isoformat()})
    print(f"Posted inactives for {dolphins_name} and {opponent_name}.")


# ── Beat writer check ────────────────────────────────────────────────────────

KNOWN_WRITERS_FILE = "/data/aquanoir_writers.json"

def load_known_writers():
    try:
        with open(KNOWN_WRITERS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_known_writers(writers):
    os.makedirs(os.path.dirname(KNOWN_WRITERS_FILE), exist_ok=True)
    with open(KNOWN_WRITERS_FILE, "w") as f:
        json.dump(writers, f, indent=2)

def check_for_new_writers(articles):
    """
    Scan article authors against known writers list.
    Flags any new official bylines for review.
    Only runs once per week.
    """
    known = load_known_writers()
    new_writers = []

    for article in articles:
        author = article.get("author", "")
        source = article.get("url", "")
        if not author:
            continue
        # Only flag authors from official outlets
        if not any(outlet in source for outlet in BEAT_WRITER_OUTLETS):
            continue
        if author not in known:
            new_writers.append({"name": author, "outlet": source, "first_seen": datetime.now(timezone.utc).isoformat()})
            known.append(author)

    if new_writers:
        print(f"New beat writers detected: {[w['name'] for w in new_writers]}")
        save_known_writers(known)

    return new_writers

# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    print(f"[{datetime.now().isoformat()}] Aqua Noir running...")

    log = load_log()
    articles = fetch_news()
    print(f"Fetched {len(articles)} articles from approved sources.")

    # Check for new beat writers weekly
    new_writers = check_for_new_writers(articles)
    if new_writers:
        for w in new_writers:
            print(f"[NEW WRITER] {w['name']} — {w['outlet']}")

    posts = generate_posts(articles, log)
    print(f"Generated {len(posts)} new posts.")

    if not posts:
        print("Nothing to post today.")
        return

    jwt, did = bsky_login()
    print("Logged into Bluesky.")

    # Game day kickoff post — fires 3 hours before kickoff
    event = get_dolphins_game_today()
    if event:
        maybe_post_kickoff(jwt, did, log, event)
        # Inactives thread — fires 90 min before kickoff
        maybe_post_inactives(jwt, did, log, event)

    for post in posts:
        try:
            # Post 1: fact — strip any hashtags Claude added, then append our own
            import re
            fact_text = post["fact"]
            fact_text = re.sub(r'#[\w]+', '', fact_text).strip()
            hashtags = "\n\n#NFL #MiamiDolphins #FinsUp"
            max_fact = 280 - len(hashtags)
            if len(fact_text) > max_fact:
                fact_text = fact_text[:max_fact - 3] + "..."
            full_text = fact_text + hashtags
            result = bsky_post(jwt, did, full_text)
            post_uri = result["uri"]
            post_cid = result["cid"]

            # Small delay between posts
            time.sleep(2)

            # Post 2: source link as reply with card preview
            reply_ref = {
                "root": {"uri": post_uri, "cid": post_cid},
                "parent": {"uri": post_uri, "cid": post_cid}
            }
            try:
                link_card = fetch_link_card(post['url'])
            except Exception as e:
                print(f"Link card error: {e}")
                link_card = None
            bsky_post(jwt, did, post['url'], reply_to=reply_ref, embed=link_card)

            # Log it
            mark_posted(log, post["fact"], post["url"])
            print(f"Posted: {post['fact'][:60]}...")

            time.sleep(3)  # Space out posts

        except Exception as e:
            import traceback
            print(f"Error posting: {e}")
            print(traceback.format_exc())
            continue

    save_log(log)
    print("Done. Log saved.")


if __name__ == "__main__":
    run()
