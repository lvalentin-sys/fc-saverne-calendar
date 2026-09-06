#!/usr/bin/env python3
import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

FFF_URL = os.environ.get(
    "FFF_URL",
    "https://epreuves.fff.fr/competition/club/503964-f-c-saverne/equipe/2026_1975_SEM_1/resultat-calendrier",
)
TEAM_NEEDLE = os.environ.get("TEAM_NEEDLE", "saverne").lower()
CALENDAR_NAME = os.environ.get("CALENDAR_NAME", "FC Saverne S1")
CALENDAR_SLUG = os.environ.get("CALENDAR_SLUG", "fc-saverne-s1")
OUT_ICS = Path(os.environ.get("OUT_ICS", "docs/fc-saverne-s1.ics"))
OUT_JSON = Path(os.environ.get("OUT_JSON", "docs/matches.json"))
MIN_MATCHES = int(os.environ.get("MIN_MATCHES", "10"))
DEBUG_DIR = Path("debug")
TZID = "Europe/Paris"

DATE_KEYS = ("date", "ma_dat", "match_date", "initial_date", "date_match", "datetime", "start")
TIME_KEYS = ("time", "heure", "ma_heure", "match_time")
HOME_KEYS = ("home", "home_team", "equipe_domicile", "domicile", "homeTeam")
AWAY_KEYS = ("away", "away_team", "equipe_exterieure", "exterieur", "awayTeam")
HOME_SCORE_KEYS = ("home_score", "score_home", "score_domicile", "homeScore")
AWAY_SCORE_KEYS = ("away_score", "score_away", "score_exterieur", "awayScore")
ID_KEYS = ("id", "ma_no", "match_id", "matchId")
STATUS_KEYS = ("status_label", "status", "etat", "state")
COMP_KEYS = ("competition", "compet", "competition_name", "cp_name", "competition_label")
ROUND_KEYS = ("round", "journee", "journee_name", "tour", "round_label")
TERRAIN_KEYS = ("terrain", "venue", "stade")

def norm(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip()

def team_name(value):
    if isinstance(value, str):
        return norm(value)
    if isinstance(value, dict):
        for k in ("short_name", "name", "nom", "label", "club_name", "team_name"):
            if value.get(k):
                return norm(value[k])
    return ""

def deep_label(value):
    if isinstance(value, str):
        return norm(value)
    if isinstance(value, dict):
        for k in ("name", "label", "short_name", "nom", "title"):
            if value.get(k):
                return norm(value[k])
    return ""

def first_value(obj, keys):
    for k in keys:
        if k in obj and obj[k] not in (None, "", []):
            return obj[k]
    return None

def parse_date(value):
    if not value:
        return None
    s = str(value).strip()
    # ISO or YYYY-MM-DD...
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # DD/MM/YYYY
    m = re.search(r"(\d{2})/(\d{2})/(20\d{2})", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None

def parse_time(value):
    if value is None:
        return None
    s = str(value).strip().lower().replace("h", ":")
    m = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", s)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None

def score_value(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except Exception:
        return None

def venue_text(value):
    if isinstance(value, str):
        return norm(value)
    if isinstance(value, dict):
        bits = []
        for k in ("name", "address", "zip_code", "city"):
            if value.get(k):
                bits.append(norm(value[k]))
        return ", ".join(dict.fromkeys(bits))
    return ""

def looks_like_match(obj):
    if not isinstance(obj, dict):
        return False
    home = team_name(first_value(obj, HOME_KEYS))
    away = team_name(first_value(obj, AWAY_KEYS))
    date = parse_date(first_value(obj, DATE_KEYS))
    if not home or not away or not date:
        return False
    return TEAM_NEEDLE in home.lower() or TEAM_NEEDLE in away.lower()

def extract_match(obj):
    home = team_name(first_value(obj, HOME_KEYS))
    away = team_name(first_value(obj, AWAY_KEYS))
    date = parse_date(first_value(obj, DATE_KEYS))
    time = parse_time(first_value(obj, TIME_KEYS)) or "15:00"
    hs = score_value(first_value(obj, HOME_SCORE_KEYS))
    aws = score_value(first_value(obj, AWAY_SCORE_KEYS))
    mid = first_value(obj, ID_KEYS)
    status = deep_label(first_value(obj, STATUS_KEYS))
    comp = deep_label(first_value(obj, COMP_KEYS))
    rnd = deep_label(first_value(obj, ROUND_KEYS))
    venue = venue_text(first_value(obj, TERRAIN_KEYS))

    # Additional common nested fields
    if not comp:
        for path in (("poule", "competition"), ("engagement", "competition"), ("poule",)):
            cur = obj
            try:
                for p in path:
                    cur = cur[p]
                comp = deep_label(cur)
                if comp:
                    break
            except Exception:
                pass

    # Stable key: prefer FFF match id. Fallback deliberately excludes date
    # so a postponed match can update instead of duplicating.
    if mid:
        stable = f"fff-{mid}"
    else:
        stable_src = "|".join([home.lower(), away.lower(), comp.lower(), rnd.lower()])
        stable = hashlib.sha1(stable_src.encode("utf-8")).hexdigest()[:20]

    return {
        "uid_key": stable,
        "fff_id": str(mid) if mid is not None else None,
        "date": date,
        "time": time,
        "home": home,
        "away": away,
        "home_score": hs,
        "away_score": aws,
        "competition": comp or "CompÃ©tition FFF",
        "round": rnd,
        "status": status,
        "venue": venue,
        "source": FFF_URL,
    }

def walk_json(value, found):
    if isinstance(value, dict):
        if looks_like_match(value):
            found.append(extract_match(value))
        for v in value.values():
            walk_json(v, found)
    elif isinstance(value, list):
        for v in value:
            walk_json(v, found)

MONTHS_FR = {
    "JAN": 1, "FÃV": 2, "FEV": 2, "MAR": 3, "AVR": 4,
    "MAI": 5, "JUN": 6, "JUIL": 7, "AOÃ": 8, "AOÃT": 8, "AOU": 8, "AOUT": 8,
    "SEP": 9, "OCT": 10, "NOV": 11, "DÃC": 12, "DEC": 12,
}

def extract_matches_from_text(text):
    """Fallback for the rendered FFF page when its API bodies are opaque."""
    lines = [norm(line) for line in text.splitlines() if norm(line)]
    date_re = re.compile(
        r"^(?:LUN|MAR|MER|JEU|VEN|SAM|DIM) (\d{2}) "
        r"(JAN|FÃV|FEV|MAR|AVR|MAI|JUN|JUIL|AOÃ|AOÃT|AOU|AOUT|SEP|OCT|NOV|DÃC|DEC) "
        r"(20\d{2}) - (\d{1,2})H(\d{2})$", re.I)
    date_indexes = [i for i, line in enumerate(lines) if date_re.match(line)]
    found = []

    for pos, start_idx in enumerate(date_indexes):
        end_idx = date_indexes[pos + 1] if pos + 1 < len(date_indexes) else len(lines)
        block = lines[start_idx:end_idx]
        m = date_re.match(block[0])
        if not m or len(block) < 4:
            continue

        day, month_name, year, hour, minute = m.groups()
        month = MONTHS_FR[month_name.upper()]
        date = f"{year}-{month:02d}-{int(day):02d}"
        time = f"{int(hour):02d}:{minute}"
        comp_line = block[1]

        items = []
        for item in block[2:]:
            low = item.lower()
            if low == "ajouter au favoris" or low.startswith("navigation "):
                break
            items.append(item)

        teams = [item for item in items
                 if not re.fullmatch(r"\d+", item)
                 and not re.fullmatch(r"\d{1,2}:\d{2}", item)]
        if len(teams) < 2:
            continue
        home, away = teams[0], teams[-1]
        if TEAM_NEEDLE not in home.lower() and TEAM_NEEDLE not in away.lower():
            continue

        scores = [int(item) for item in items if re.fullmatch(r"\d+", item)]
        home_score = scores[0] if len(scores) >= 2 else None
        away_score = scores[1] if len(scores) >= 2 else None
        if " - " in comp_line:
            competition, rnd = comp_line.split(" - ", 1)
        else:
            competition, rnd = comp_line, ""

        stable_src = "|".join([home.lower(), away.lower(), competition.lower(), rnd.lower()])
        found.append({
            "uid_key": hashlib.sha1(stable_src.encode("utf-8")).hexdigest()[:20],
            "fff_id": None,
            "date": date,
            "time": time,
            "home": home,
            "away": away,
            "home_score": home_score,
            "away_score": away_score,
            "competition": competition,
            "round": rnd,
            "status": "",
            "venue": "",
            "source": FFF_URL,
        })
    return found

def dedupe(matches):
    by = {}
    for m in matches:
        key = m["uid_key"]
        prev = by.get(key)
        if prev is None:
            by[key] = m
            continue
        # Keep the richer object.
        score = sum(bool(m.get(k)) for k in ("competition","round","status","venue","fff_id"))
        pscore = sum(bool(prev.get(k)) for k in ("competition","round","status","venue","fff_id"))
        if score >= pscore:
            by[key] = m
    return sorted(by.values(), key=lambda x: (x["date"], x["time"], x["home"], x["away"]))

def team_fingerprint(name):
    words = re.findall(r"[a-z0-9]+", name.lower())
    ignored = {"fc", "f", "c", "as", "us", "es", "s", "zorn"}
    return " ".join(word for word in words if word not in ignored)

def merge_partial(previous, fresh):
    """Update visible matches without deleting the rest of the season."""
    merged = [dict(match) for match in previous]
    for incoming in fresh:
        target = None
        for existing in merged:
            same_teams = (
                team_fingerprint(existing["home"]) == team_fingerprint(incoming["home"])
                and team_fingerprint(existing["away"]) == team_fingerprint(incoming["away"])
            )
            same_day = existing["date"] == incoming["date"]
            if existing["uid_key"] == incoming["uid_key"] or same_teams or same_day:
                target = existing
                break
        if target is None:
            merged.append(incoming)
            continue
        for key in ("date", "time", "home_score", "away_score", "competition", "round", "status", "venue"):
            if incoming.get(key) not in (None, ""):
                target[key] = incoming[key]
    return dedupe(merged)

def esc(s):
    return (str(s).replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace(";", "\\;")
            .replace(",", "\\,"))

def fold(line, limit=73):
    if len(line.encode("utf-8")) <= limit:
        return [line]
    out, cur = [], ""
    for ch in line:
        test = cur + ch
        if len(test.encode("utf-8")) > limit:
            out.append(cur)
            cur = " " + ch
        else:
            cur = test
    if cur:
        out.append(cur)
    return out

def make_ics(matches):
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//FC Saverne//Calendrier FFF automatique//FR",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{CALENDAR_NAME}",
        "X-WR-TIMEZONE:Europe/Paris",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
        "BEGIN:VTIMEZONE",
        "TZID:Europe/Paris",
        "X-LIC-LOCATION:Europe/Paris",
        "BEGIN:DAYLIGHT",
        "TZOFFSETFROM:+0100",
        "TZOFFSETTO:+0200",
        "TZNAME:CEST",
        "DTSTART:19700329T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
        "END:DAYLIGHT",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:+0200",
        "TZOFFSETTO:+0100",
        "TZNAME:CET",
        "DTSTART:19701025T030000",
        "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]
    for m in matches:
        start = datetime.strptime(m["date"] + " " + m["time"], "%Y-%m-%d %H:%M")
        end = start + timedelta(hours=2)
        result_known = m["home_score"] is not None and m["away_score"] is not None
        if result_known:
            summary = f'{m["home"]} {m["home_score"]}â{m["away_score"]} {m["away"]}'
        else:
            summary = f'{m["home"]} â {m["away"]}'
        desc_parts = [m["competition"]]
        if m["round"]:
            desc_parts.append(m["round"])
        if m["status"]:
            desc_parts.append(m["status"])
        desc_parts += ["Mise Ã  jour automatique depuis la FFF", FFF_URL]
        ev = [
            "BEGIN:VEVENT",
            f'UID:{esc(m["uid_key"])}@{CALENDAR_SLUG}-fff',
            f"DTSTAMP:{now}",
            f"DTSTART;TZID={TZID}:{start.strftime('%Y%m%dT%H%M%S')}",
            f"DTEND;TZID={TZID}:{end.strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:{esc(summary)}",
            f"DESCRIPTION:{esc(' â¢ '.join(desc_parts))}",
            f"URL:{FFF_URL}",
            "STATUS:CONFIRMED",
            "TRANSP:OPAQUE",
        ]
        if m["venue"]:
            ev.append(f'LOCATION:{esc(m["venue"])}')
        ev.append("END:VEVENT")
        lines.extend(ev)
    lines.append("END:VCALENDAR")
    folded = []
    for line in lines:
        folded.extend(fold(line))
    return "\r\n".join(folded) + "\r\n"

async def main():
    DEBUG_DIR.mkdir(exist_ok=True)
    captured = []
    response_urls = []
    page_text = ""

    api_key = os.environ.get("ZENROWS_API_KEY")
    if not api_key:
        print("ZENROWS_API_KEY is missing.", file=sys.stderr)
        sys.exit(2)

    async with async_playwright() as p:
        connection_url = f"wss://browser.zenrows.com?apikey={api_key}"
        browser = await p.chromium.connect_over_cdp(connection_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        await context.set_extra_http_headers({
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "DNT": "1",
        })
        page = await context.new_page()

        async def on_response(resp):
            try:
                response_urls.append(f"{resp.status} {resp.url}")
                ctype = (resp.headers.get("content-type") or "").lower()
                if "json" in ctype:
                    data = await resp.json()
                    captured.append(data)
            except Exception:
                pass

        page.on("response", on_response)

        try:
            await page.goto(FFF_URL, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(2500)
            for label in ("Refuser", "Tout refuser", "Continuer sans accepter"):
                button = page.get_by_text(label, exact=True)
                if await button.count():
                    await button.first.click()
                    await page.wait_for_timeout(3000)
                    break
            await page.wait_for_timeout(6000)

            # Read every month displayed by the FFF carousel. The FFF changes
            # the navigation markup regularly, so try text and attributes.
            page_texts = []
            seen_pages = set()
            for _ in range(12):
                current_text = await page.locator("body").inner_text()
                page_key = hashlib.sha1(current_text.encode("utf-8")).hexdigest()
                if page_key in seen_pages:
                    break
                seen_pages.add(page_key)
                page_texts.append(current_text)

                next_button = page.get_by_text(re.compile(r"navigation suivante", re.I))
                if not await next_button.count():
                    next_button = page.locator(
                        '[aria-label*="suivante" i], [title*="suivante" i], '
                        'button:has(img[alt*="suivante" i]), '
                        'a:has(img[alt*="suivante" i]), img[alt*="suivante" i]'
                    )
                visible_buttons = []
                for candidate in await next_button.all():
                    if await candidate.is_visible():
                        visible_buttons.append(candidate)
                if not visible_buttons:
                    break
                try:
                    await visible_buttons[-1].click()
                    await page.wait_for_timeout(1800)
                except Exception:
                    break

            (DEBUG_DIR / "page.html").write_text(await page.content(), encoding="utf-8")
            page_text = "\n".join(page_texts)
            (DEBUG_DIR / "page.txt").write_text(page_text, encoding="utf-8")
            (DEBUG_DIR / "responses.txt").write_text("\n".join(response_urls), encoding="utf-8")
        finally:
            await browser.close()

    found = []
    for payload in captured:
        walk_json(payload, found)
    found.extend(extract_matches_from_text(page_text))
    matches = dedupe(found)

    previous = []
    if OUT_JSON.exists():
        try:
            previous = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        except Exception:
            previous = []

    if len(matches) < MIN_MATCHES and len(previous) >= MIN_MATCHES:
        print(f"Partial FFF view ({len(matches)} match(es)); merging with {len(previous)} existing matches.")
        matches = merge_partial(previous, matches)

    # Safety: never destroy a working subscribed calendar because FFF blocked one run.
    if len(matches) < MIN_MATCHES:
        print(f"FFF page text: {page_text[:2000]}", file=sys.stderr)
        print(f"FFF responses ({len(response_urls)}): {response_urls[-100:]}", file=sys.stderr)
        print(f"FFF JSON responses: {len(captured)}", file=sys.stderr)
        print(f"FFF extraction incomplete ({len(matches)} match(es)). Previous calendar kept.", file=sys.stderr)
        sys.exit(2)

    OUT_JSON.write_text(json.dumps(matches, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_ICS.write_text(make_ics(matches), encoding="utf-8", newline="")
    print(f"{len(matches)} match(es) written to {OUT_ICS}")

if __name__ == "__main__":
    asyncio.run(main())
