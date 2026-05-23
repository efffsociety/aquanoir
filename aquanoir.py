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
    "si.com",              # Alain Poupart
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
    "Miami Dolphins news today",
    "Miami Dolphins roster moves",
    "Miami Dolphins injury report",
    "Miami Dolphins OTA practice",
    "Miami Dolphins beat reporter",
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
🐬 [fact from the article. One or two sentences max. No speculation.]
[One genuine question that makes the reader think. Not cynical. Not speculative. Just curious.]
#NFL #MiamiDolphins #FinsUp
SOURCE_URL: [the article url]

Rules:
- Facts only. Nothing not stated in the source.
- Question must be genuine curiosity not rhetorical snark.
- No opinions, no analysis beyond what is in the article.
- No emojis except 🐬 at the start.
- Prioritize transactions, injuries, depth chart moves and scheme notes from beat reporters.
- Skip pure opinion pieces, rankings and hot takes.
- For secondary sources (PFT/Mike Florio, NBC Sports/Chris Simms, Pat McAfee Show, Rich Eisen/NFL Network) only post if the content is a confirmed story break or transaction. Never post their analysis or opinions.
- Generate one POST per distinct news item.
- Max 5 posts per run.
- If no new worthy news, output: NO_NEWS.
- Never use hyphens or em dashes.
- Never use Oxford commas.
- Keep sentence structure direct and clean.
- If an article is a follow-up to a story already in the posted log, only post it if it contains meaningful new facts of any kind. New details, a confirmation, a reversal, additional context that changes the story. If it is just rehashing the same information, skip it.
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


def bsky_post(jwt, did, text, reply_to=None):
    """Post a skeet. If reply_to is set, post as a thread reply."""
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.now(timezone.utc).isoformat()
    }

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


def story_id(text):
    """Fingerprint a story so we never repost it."""
    return hashlib.md5(text.strip()[:120].encode()).hexdigest()


def already_posted(log, text):
    sid = story_id(text)
    return any(entry.get("id") == sid for entry in log)


def mark_posted(log, text, url):
    log.append({
        "id": story_id(text),
        "text": text[:100],
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
    else:
        # RSS fallback — official Dolphins feed always works
        import xml.etree.ElementTree as ET
        rss_feeds = [
            "https://www.miamidolphins.com/rss/news",
            "https://www.profootballtalk.com/feed/",
            "https://www.espn.com/espn/rss/nfl/news",
            "https://www.nfl.com/rss/rsslanding?searchString=miami+dolphins",
            "https://syndication.bleacherreport.com/streams/teams/nfl-miami-dolphins.rss",
        ]
        for feed_url in rss_feeds:
            try:
                res = requests.get(feed_url, timeout=10)
                root = ET.fromstring(res.content)
                for item in root.findall(".//item")[:10]:
                    title = item.findtext("title", "")
                    link = item.findtext("link", "")
                    desc = item.findtext("description", "")
                    if title and link:
                        articles.append({
                            "title": title,
                            "description": desc,
                            "url": link,
                            "published": item.findtext("pubDate", "")
                        })
            except Exception as e:
                print(f"RSS error for {feed_url}: {e}")

    # Deduplicate by URL
    seen = set()
    unique = []
    for a in articles:
        if a["url"] not in seen:
            seen.add(a["url"])
            unique.append(a)

    return unique[:20]  # Cap at 20 for Claude context

# ── Claude digest generation ──────────────────────────────────────────────────

def generate_posts(articles, log):
    if not articles:
        print("No articles fetched.")
        return []

    client = Anthropic(api_key=ANTHROPIC_KEY)

    # Build news context
    news_text = ""
    for a in articles:
        news_text += f"TITLE: {a['title']}\n"
        news_text += f"URL: {a['url']}\n"
        if a.get("description"):
            news_text += f"DESCRIPTION: {a['description']}\n"
        news_text += "\n"

    # Add already-posted context to avoid repeats
    if log:
        recent = log[-50:]
        already = "\n".join(f"- {e['text']}" for e in recent)
        news_text += f"\nALREADY POSTED — do not repeat:\n{already}\n"

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": news_text}]
    )

    raw = message.content[0].text.strip()

    if raw == "NO_NEWS":
        print("Claude found no new worthy news.")
        return []

    # Parse POST blocks
    posts = []
    blocks = raw.split("POST:")[1:]  # Split on POST: marker
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
            if not already_posted(log, fact_text):
                posts.append({"fact": fact_text, "url": url})

    return posts[:5]  # Max 5 per run

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
            # Post 1: fact + question
            result = bsky_post(jwt, did, post["fact"])
            post_uri = result["uri"]
            post_cid = result["cid"]

            # Small delay between posts
            time.sleep(2)

            # Post 2: source link as reply
            reply_ref = {
                "root": {"uri": post_uri, "cid": post_cid},
                "parent": {"uri": post_uri, "cid": post_cid}
            }
            bsky_post(jwt, did, f"Source: {post['url']}", reply_to=reply_ref)

            # Log it
            mark_posted(log, post["fact"], post["url"])
            print(f"Posted: {post['fact'][:60]}...")

            time.sleep(3)  # Space out posts

        except Exception as e:
            print(f"Error posting: {e}")
            continue

    save_log(log)
    print("Done. Log saved.")


if __name__ == "__main__":
    run()
