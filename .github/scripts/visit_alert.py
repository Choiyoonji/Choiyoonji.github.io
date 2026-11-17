#!/usr/bin/env python3
"""Post new GoatCounter visits to a Discord channel.

GoatCounter only allows one raw export per hour, so instead this reads the
cumulative visitor stats (pages, referrers, countries, systems) since START,
compares them with the snapshot saved by the previous run, and posts whatever
went up. The snapshot is a small JSON file the workflow carries between runs
with actions/cache.

Environment:
  GOATCOUNTER_SITE      e.g. https://choiyoonji.goatcounter.com
  GOATCOUNTER_TOKEN     GoatCounter API key with "Read statistics"
  DISCORD_WEBHOOK_URL   Discord channel webhook URL
  SITE_URL              public site, used to link page paths
  STATE_FILE            snapshot path (default: visit-state.json)
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

START = "2026-11-01T00:00:00Z"  # before GoatCounter was added to the site
LIMIT = 100                     # rows per stats list; plenty for a personal site
USER_AGENT = "visit-alerts/1.0"
EMBED_COLOR = 0x5865F2
MAX_LINES = 10


def env(name, default=None):
    value = os.environ.get(name, default)
    value = (value or "").strip()  # pasted secrets often carry a newline
    if not value:
        sys.exit(f"error: {name} is not set (add it as a repository secret)")
    return value.rstrip("/") if name.endswith(("_SITE", "_URL")) else value


class ApiError(Exception):
    def __init__(self, where, status, detail):
        super().__init__(f"{where} answered {status}: {detail}")
        self.status = status


def request(url, *, token=None, body=None):
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    parts = urllib.parse.urlsplit(url)
    # Name the endpoint in errors, but never echo the webhook's secret path.
    where = parts.netloc + (parts.path if "/api/" in parts.path else "")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise ApiError(where, e.code, e.read().decode(errors="replace")[:300])
    except urllib.error.URLError as e:
        raise ApiError(where, "no response", e.reason)
    return json.loads(raw) if raw else None


def window_for(now):
    end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return {"start": START, "end": end.strftime("%Y-%m-%dT%H:%M:%SZ")}


def api_get(site, token, path, **params):
    time.sleep(0.3)  # API limit is 4 requests/second
    query = urllib.parse.urlencode(params)
    return request(f"{site}/api/v0/{path}?{query}", token=token)


def snapshot(site, token, now):
    window = window_for(now)

    def stat_map(page):
        # Breakdowns are extras: if one fails, still report the visit.
        try:
            rows = api_get(site, token, f"stats/{page}", limit=LIMIT, **window)["stats"]
        except ApiError as e:
            print(f"warning: skipping {page}: {e}")
            return {}
        return {f"{r.get('id') or ''}\t{r.get('name') or ''}": r["count"]
                for r in rows}

    hits = api_get(site, token, "stats/hits", limit=LIMIT, **window)["hits"]
    pages = {h["path"]: h["count"] for h in hits if not h.get("event")}
    return {
        "total": sum(pages.values()),
        "pages": pages,
        "titles": {h["path"]: h.get("title") or "" for h in hits},
        "refs": stat_map("toprefs"),
        "locations": stat_map("locations"),
        "systems": stat_map("systems"),
    }


def check(site, token, now):
    """Call each endpoint once and print what came back; posts nothing."""
    window = window_for(now)
    calls = [("me", {}), ("stats/hits", {"limit": LIMIT, **window}),
             ("stats/total", window), ("stats/toprefs", {"limit": LIMIT, **window}),
             ("stats/locations", {"limit": LIMIT, **window}),
             ("stats/systems", {"limit": LIMIT, **window}),
             ("stats/hits", {"limit": LIMIT})]
    print(f"window: {window['start']} .. {window['end']}")
    for path, params in calls:
        label = path + ("" if params.get("start") or path == "me" else " (default window)")
        try:
            data = api_get(site, token, path, **params)
        except ApiError as e:
            print(f"  FAIL {label}: {e}")
            continue
        rows = next((v for v in data.values() if isinstance(v, list)), None)
        summary = [f"{len(rows)} rows"] if rows is not None else []
        if "total" in data:
            summary.append(f"total={data['total']}")
        print(f"  ok   {label}: {', '.join(summary) or 'ok'}")


def increases(old, new):
    rows = [(k, n - old.get(k, 0)) for k, n in new.items()]
    return sorted([r for r in rows if r[1] > 0], key=lambda r: (-r[1], r[0]))


def flag(code):
    code = code[:2].upper()
    if len(code) == 2 and code.isalpha():
        return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code) + " "
    return ""


def field(name, rows, fmt):
    if not rows:
        return None
    lines = [fmt(k, d) for k, d in rows[:MAX_LINES]]
    if len(rows) > MAX_LINES:
        lines.append(f"…외 {len(rows) - MAX_LINES}개")
    return {"name": name, "value": "\n".join(lines)[:1024], "inline": False}


def build_embed(old, new, site, site_url, now):
    added = new["total"] - old["total"]

    def page(path, d):
        title = new["titles"].get(path, "")
        label = f"[`{path}`]({site_url}{path})" if path.startswith("/") else f"`{path}`"
        return f"{label} +{d}" + (f" · {title}" if title else "")

    def ref(key, d):
        name = key.split("\t", 1)[1]
        return f"{name or '(직접 방문)'} +{d}"

    def loc(key, d):
        code, name = key.split("\t", 1)
        return f"{flag(code)}{name or '(알 수 없음)'} +{d}"

    def system(key, d):
        name = key.split("\t", 1)[1]
        return f"{name or '(알 수 없음)'} +{d}"

    fields = [
        field("페이지", increases(old["pages"], new["pages"]), page),
        field("유입 경로", increases(old["refs"], new["refs"]), ref),
        field("국가", increases(old["locations"], new["locations"]), loc),
        field("기기", increases(old["systems"], new["systems"]), system),
    ]
    return {
        "title": f"새 방문 {added}건",
        "url": site,
        "color": EMBED_COLOR,
        "fields": [f for f in fields if f],
        "footer": {"text": f"누적 방문 {new['total']}건 · GoatCounter"},
        "timestamp": now.isoformat(),
    }


def post(webhook, payload):
    request(webhook, body={"username": "방문 알림", **payload})


def set_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}={value}\n")


def main():
    site = env("GOATCOUNTER_SITE")
    token = env("GOATCOUNTER_TOKEN")
    now = datetime.now(timezone.utc)
    if "--check" in sys.argv:
        return check(site, token, now)

    webhook = env("DISCORD_WEBHOOK_URL")
    hook = urllib.parse.urlsplit(webhook)
    if (os.environ.get("ALLOW_ANY_WEBHOOK") != "1" and
            (not hook.netloc.endswith(("discord.com", "discordapp.com"))
             or not hook.path.startswith("/api/webhooks/"))):
        sys.exit("error: DISCORD_WEBHOOK_URL is not a Discord webhook URL; "
                 "it should start with https://discord.com/api/webhooks/")
    site_url = env("SITE_URL", "https://choiyoonji.github.io")
    state_file = os.environ.get("STATE_FILE", "visit-state.json")

    old = None
    if os.path.exists(state_file):
        with open(state_file) as f:
            old = json.load(f)
    new = snapshot(site, token, now)

    if old is None:
        post(webhook, {"content": (
            f"✅ 방문 알림을 시작했어요. 지금까지 누적 방문 {new['total']}건이에요. "
            f"새 방문이 생기면 여기로 알려드릴게요.\n{site}")})
        print(f"first run: baseline total={new['total']}")
    elif new["total"] > old["total"]:
        post(webhook, {"embeds": [build_embed(old, new, site, site_url, now)]})
        print(f"posted: {new['total'] - old['total']} new (total={new['total']})")
    else:
        print(f"no new visits (total={new['total']})")

    changed = new != old
    if changed:
        with open(state_file, "w") as f:
            json.dump(new, f, ensure_ascii=False, indent=1)
    set_output("changed", str(changed).lower())


if __name__ == "__main__":
    try:
        main()
    except ApiError as e:
        sys.exit(f"error: {e}")
