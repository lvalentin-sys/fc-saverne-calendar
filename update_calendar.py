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

FFF_URL = "https://epreuves.fff.fr/competition/club/503964-f-c-saverne/equipe/2026_1975_SEM_1/resultat-calendrier"
TEAM_NEEDLE = "saverne"
OUT_ICS = Path("docs/fc-saverne-s1.ics")
OUT_JSON = Path("docs/matches.json")
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
        "competition": comp or "Compétition FFF",
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
        "X-WR-CALNAME:FC Saverne S1",
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
            summary = f'{m["home"]} {m["home_score"]}–{m["away_score"]} {m["away"]}'
        else:
            summary = f'{m["home"]} – {m["away"]}'
        desc_parts = [m["competition"]]
        if m["round"]:
            desc_parts.append(m["round"])
        if m["status"]:
            desc_parts.append(m["status"])
        desc_parts += ["Mise à jour automatique depuis la FFF", FFF_URL]
        ev = [
            "BEGIN:VEVENT",
            f'UID:{esc(m["uid_key"])}@fc-saverne-fff',
            f"DTSTAMP:{now}",
            f"DTSTART;TZID={TZID}:{start.strftime('%Y%m%dT%H%M%S')}",
            f"DTEND;TZID={TZID}:{end.strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:{esc(summary)}",
            f"DESCRIPTION:{esc(' • '.join(desc_parts))}",
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

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="fr-FR",
            timezone_id="Europe/Paris",
            viewport={"width": 1440, "height": 1200},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/128.0.0.0 Safari/537.36"),
            extra_http_headers={
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                "DNT": "1",
            },
        )
        page = await context.new_page()

        async def on_response(resp):
            try:
                if "fff.fr" not in resp.url:
                    return
                response_urls.append(resp.url)
                ctype = (resp.headers.get("content-type") or "").lower()
                if "json" in ctype:
                    data = await resp.json()
                    captured.append(data)
            except Exception:
                pass

        page.on("response", on_response)

        try:
            await page.goto(FFF_URL, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(12000)
            # Scroll to trigger lazy-loaded competitions/results.
            for _ in range(5):
                await page.mouse.wheel(0, 1600)
                await page.wait_for_timeout(900)
            await page.wait_for_timeout(4000)

            (DEBUG_DIR / "page.html").write_text(await page.content(), encoding="utf-8")
            (DEBUG_DIR / "page.txt").write_text(await page.locator("body").inner_text(), encoding="utf-8")
            (DEBUG_DIR / "responses.txt").write_text("\n".join(response_urls), encoding="utf-8")
        finally:
            await browser.close()

    found = []
    for payload in captured:
        walk_json(payload, found)
    matches = dedupe(found)

    # Safety: never destroy a working subscribed calendar because FFF blocked one run.
    if len(matches) < 3:
        print(f"FFF extraction incomplete ({len(matches)} match(es)). Previous calendar kept.", file=sys.stderr)
        sys.exit(2)

    OUT_JSON.write_text(json.dumps(matches, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_ICS.write_text(make_ics(matches), encoding="utf-8", newline="")
    print(f"{len(matches)} match(es) written to {OUT_ICS}")

if __name__ == "__main__":
    asyncio.run(main())
