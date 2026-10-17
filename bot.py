
import os
import re
import time
import asyncio
import logging
from datetime import datetime, timezone
from difflib import SequenceMatcher
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException

# ============================================================
# SolveEase Arbitrage Engine
# Render command:
# uvicorn bot:app --host 0.0.0.0 --port $PORT
#
# Analysis and Telegram alerts only. Never places bets.
# ============================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

# Prevent httpx from logging full request URLs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger("arbitrage_engine")

BANKROLL = float(os.getenv("BANKROLL_NGN", "100000"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_SECONDS", "900"))
MIN_PROFIT_NGN = float(os.getenv("MIN_PROFIT_NGN", "100"))
MIN_MARGIN_PERCENT = float(os.getenv("MIN_MARGIN_PERCENT", "0.5"))
STAKE_STEP = float(os.getenv("STAKE_STEP_NGN", "500"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "15"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SPORTYBET_URL = (
    "https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents"
)

BET9JA_URL = (
    "https://sports.bet9ja.com/desktop/feapi/"
    "PalimpsestAjax/GetEventsInGroupV2"
)

# These group IDs are community-documented examples, not
# guaranteed current IDs. Failed groups are reported explicitly.
BET9JA_GROUPS = {
    "Premier League": "170880",
    "La Liga": "180928",
    "Bundesliga": "180923",
    "Serie A": "167856",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
}

scan_lock = asyncio.Lock()
last_scan = {
    "started_at": None,
    "completed_at": None,
    "fixtures": 0,
    "opportunities": 0,
    "feeds": {},
    "error": None,
}
sent_alert_keys = set()


# -------------------- General helpers --------------------

def number(value):
    """Return a positive finite float, or None."""
    try:
        result = float(str(value).replace(",", "").strip())
        if result > 1.0 and result < 10000:
            return result
    except (TypeError, ValueError):
        pass
    return None


def clean_name(value):
    value = str(value or "").lower()
    value = re.sub(r"\b(fc|sc|afc|cf|club)\b", " ", value)
    value = re.sub(r"[^a-z0-9 ]", " ", value)
    return " ".join(value.split())


def names_match(a, b):
    a = clean_name(a)
    b = clean_name(b)
    if not a or not b:
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.88


def parse_timestamp(value):
    """Convert common timestamp formats to Unix seconds."""
    if value is None:
        return None
    try:
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        if stamp > 0:
            return stamp
    except (TypeError, ValueError):
        pass
    return None


def event_key(event):
    return (
        clean_name(event.get("home")),
        clean_name(event.get("away")),
    )


def valid_fixture(event):
    return (
        bool(event.get("home"))
        and bool(event.get("away"))
        and event.get("home") != event.get("away")
        and all(number(event["odds"].get(k)) for k in ("home", "draw", "away"))
    )


def make_fixture(bookmaker, home, away, odds, league="", kickoff=None,
                 source_id=None):
    fixture = {
        "bookmaker": bookmaker,
        "home": str(home or "").strip(),
        "away": str(away or "").strip(),
        "league": str(league or "").strip(),
        "kickoff": parse_timestamp(kickoff),
        "source_id": str(source_id or ""),
        "odds": {
            "home": number(odds.get("home")),
            "draw": number(odds.get("draw")),
            "away": number(odds.get("away")),
        },
        "observed_at": time.time(),
    }
    return fixture if valid_fixture(fixture) else None


# -------------------- SportyBet parser --------------------

def parse_sportybet(payload):
    """
    Expected documented structure:
    data.tournaments[].events[].markets[].outcomes[]

    Market 1 is treated as the standard 1X2 market.
    Unknown or incomplete market layouts are rejected.
    """
    fixtures = []

    if not isinstance(payload, dict):
        return fixtures

    if payload.get("bizCode") not in (None, 10000, "10000"):
        log.warning("SportyBet returned a non-success business code.")
        return fixtures

    data = payload.get("data", {})
    if not isinstance(data, dict):
        return fixtures

    tournaments = data.get("tournaments", [])
    if not isinstance(tournaments, list):
        return fixtures

    for tournament in tournaments:
        if not isinstance(tournament, dict):
            continue

        league = (
            tournament.get("name")
            or tournament.get("categoryName")
            or ""
        )

        events = tournament.get("events", [])
        if not isinstance(events, list):
            continue

        for event in events:
            if not isinstance(event, dict):
                continue

            # Skip clearly started/finished events.
            status = str(event.get("matchStatus", "")).lower()
            if status in {"live", "inplay", "finished", "ended"}:
                continue

            odds = {}
            markets = event.get("markets", [])

            if not isinstance(markets, list):
                continue

            for market in markets:
                if not isinstance(market, dict):
                    continue

                market_id = str(market.get("id", ""))
                market_desc = str(market.get("desc", "")).lower()

                # Do not mix handicap or other market types with 1X2.
                is_1x2 = (
                    market_id == "1"
                    or market_desc.strip() in {
                        "1x2", "match result", "full time result",
                        "fulltime result",
                    }
                )
                if not is_1x2:
                    continue

                market_status = str(market.get("status", "")).lower()
                if market_status in {"0", "closed", "suspended", "inactive"}:
                    continue

                outcomes = market.get("outcomes", [])
                if not isinstance(outcomes, list):
                    continue

                for outcome in outcomes:
                    if not isinstance(outcome, dict):
                        continue
                    if outcome.get("isActive") is False:
                        continue

                    outcome_id = str(outcome.get("id", "")).lower()
                    desc = str(outcome.get("desc", "")).strip().lower()
                    price = number(outcome.get("odds"))

                    if not price:
                        continue

                    if outcome_id in {"1", "home"} or desc in {
                        "1", "home", "home win"
                    }:
                        odds["home"] = price
                    elif outcome_id in {"2", "draw", "x"} or desc in {
                        "x", "draw"
                    }:
                        odds["draw"] = price
                    elif outcome_id in {"3", "away"} or desc in {
                        "2", "away", "away win"
                    }:
                        odds["away"] = price

                if all(k in odds for k in ("home", "draw", "away")):
                    break

            fixture = make_fixture(
                "SportyBet",
                event.get("homeTeamName"),
                event.get("awayTeamName"),
                odds,
                league=league,
                kickoff=event.get("estimateStartTime"),
                source_id=event.get("eventId"),
            )
            if fixture:
                fixtures.append(fixture)

    return fixtures


async def fetch_sportybet(client):
    params = {
        "sportId": "sr:sport:1",
        "marketId": "1",
        "pageSize": "100",
        "pageNum": "1",
        "todayGames": "false",
        "timeline": "168",
        "_t": str(int(time.time() * 1000)),
    }
    headers = {
        **HEADERS,
        "Current-Country": "NG",
        "Content-Type": "application/json",
        "Referer": "https://www.sportybet.com/ng/",
    }

    try:
        response = await client.get(
            SPORTYBET_URL,
            params=params,
            headers=headers,
            follow_redirects=False,
        )

        if response.status_code != 200:
            return [], f"HTTP {response.status_code}"

        content_type = response.headers.get("content-type", "").lower()
        if "json" not in content_type:
            return [], f"Unexpected content type: {content_type or 'unknown'}"

        try:
            payload = response.json()
        except ValueError:
            return [], "Response was not valid JSON"

        fixtures = parse_sportybet(payload)
        if not fixtures:
            return [], "Request succeeded but no valid 1X2 fixtures were parsed"

        return fixtures, "OK"

    except httpx.TimeoutException:
        return [], "Request timed out"
    except httpx.HTTPError as exc:
        return [], f"Network error: {type(exc).__name__}"


# -------------------- Bet9ja parser --------------------

def recursive_dicts(obj):
    """Yield dictionaries nested inside common JSON containers."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            if isinstance(value, (dict, list)):
                yield from recursive_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            if isinstance(value, (dict, list)):
                yield from recursive_dicts(value)


def first_value(obj, keys):
    for key in keys:
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
    return None


def parse_bet9ja(payload, league):
    """
    Supports common flat event fields and nested event objects.
    If Bet9ja changes its schema, unrecognized objects are rejected.
    """
    fixtures = []
    if not isinstance(payload, (dict, list)):
        return fixtures

    seen = set()

    for item in recursive_dicts(payload):
        home = first_value(item, [
            "homeTeamName", "homeTeam", "homeName", "home_team",
        ])
        away = first_value(item, [
            "awayTeamName", "awayTeam", "awayName", "away_team",
        ])

        # Some community examples use a single "Home - Away" field.
        match = first_value(item, ["match", "eventName", "fixtureName"])
        if (not home or not away) and isinstance(match, str):
            parts = re.split(r"\s+-\s+|\s+vs\.?\s+", match, maxsplit=1,
                             flags=re.IGNORECASE)
            if len(parts) == 2:
                home, away = parts

        odds = {}

        # Common flat field names used by some feeds.
        home_price = first_value(item, [
            "homeOdds", "oddsHome", "home_odds", "home",
        ])
        draw_price = first_value(item, [
            "drawOdds", "oddsDraw", "draw_odds", "draw",
        ])
        away_price = first_value(item, [
            "awayOdds", "oddsAway", "away_odds", "away",
        ])

        # If home/away are objects rather than team names, reject this shape.
        if isinstance(home, (dict, list)) or isinstance(away, (dict, list)):
            continue

        odds = {
            "home": number(home_price),
            "draw": number(draw_price),
            "away": number(away_price),
        }

        # Alternate outcome dictionary layout.
        nested_odds = item.get("odds")
        if isinstance(nested_odds, dict):
            odds = {
                "home": number(first_value(nested_odds, ["home", "1", "H"])),
                "draw": number(first_value(nested_odds, ["draw", "x", "X"])),
                "away": number(first_value(nested_odds, ["away", "2", "A"])),
            }

        fixture = make_fixture(
            "Bet9ja", home, away, odds, league=league,
            kickoff=first_value(item, [
                "time", "startTime", "start_time", "kickoff",
                "eventTime", "date",
            ]),
            source_id=first_value(item, [
                "match_id", "matchId", "eventId", "id",
            ]),
        )

        if not fixture:
            continue

        key = (
            clean_name(fixture["home"]),
            clean_name(fixture["away"]),
            fixture.get("source_id", ""),
        )
        if key not in seen:
            seen.add(key)
            fixtures.append(fixture)

    return fixtures


async def fetch_bet9ja_group(client, league, group_id):
    params = {
        "GROUPID": group_id,
        "DISP": "0",
        "GROUPMARKETID": "1",
        "matches": "true",
    }
    headers = {
        **HEADERS,
        "Referer": "https://sports.bet9ja.com/",
    }

    try:
        response = await client.get(
            BET9JA_URL,
            params=params,
            headers=headers,
            follow_redirects=False,
        )

        if response.status_code != 200:
            return [], f"HTTP {response.status_code}"

        try:
            payload = response.json()
        except ValueError:
            return [], "Response was not valid JSON"

        fixtures = parse_bet9ja(payload, league)
        if not fixtures:
            return [], "No valid 1X2 fixtures parsed from this group"

        return fixtures, "OK"

    except httpx.TimeoutException:
        return [], "Request timed out"
    except httpx.HTTPError as exc:
        return [], f"Network error: {type(exc).__name__}"


async def fetch_all_feeds(client):
    results = {}
    fixtures = []

    sporty_fixtures, sporty_status = await fetch_sportybet(client)
    results["SportyBet"] = {
        "status": sporty_status,
        "fixtures": len(sporty_fixtures),
    }
    fixtures.extend(sporty_fixtures)

    # Keep requests sequential to avoid unnecessary upstream load.
    for league, group_id in BET9JA_GROUPS.items():
        group_fixtures, status = await fetch_bet9ja_group(
            client, league, group_id
        )
        results[f"Bet9ja/{league}"] = {
            "status": status,
            "fixtures": len(group_fixtures),
        }
        fixtures.extend(group_fixtures)
        await asyncio.sleep(0.25)

    return fixtures, results


# -------------------- Matching and arbitrage math --------------------

def kickoff_compatible(a, b):
    ta = a.get("kickoff")
    tb = b.get("kickoff")
    if ta is None or tb is None:
        # Missing kickoff is not enough evidence to reject a match,
        # but team names and fixture orientation must still match.
        return True
    return abs(ta - tb) <= 3 * 60 * 60


def same_fixture(a, b):
    if not kickoff_compatible(a, b):
        return False

    direct = (
        names_match(a["home"], b["home"])
        and names_match(a["away"], b["away"])
    )
    reversed_names = (
        names_match(a["home"], b["away"])
        and names_match(a["away"], b["home"])
    )

    # Reversed home/away orientation is not the same 1X2 market.
    return direct and not reversed_names


def rounded_stakes(odds, bankroll):
    """
    Proportional dutching stakes rounded DOWN to the configured step.
    Any leftover bankroll is left unallocated.
    """
    inv = {k: 1.0 / odds[k] for k in ("home", "draw", "away")}
    total_inv = sum(inv.values())

    if total_inv <= 0:
        return None

    stakes = {}
    for outcome in ("home", "draw", "away"):
        raw = bankroll * inv[outcome] / total_inv
        stakes[outcome] = int(raw / STAKE_STEP) * STAKE_STEP

    if sum(stakes.values()) <= 0 or sum(stakes.values()) > bankroll:
        return None

    return stakes


def evaluate_group(events):
    """
    Group events by matching fixture. Use the best available price for
    each 1X2 outcome, preserving the bookmaker that offered it.
    """
    if len({e["bookmaker"] for e in events}) < 2:
        return None

    best = {}
    for outcome in ("home", "draw", "away"):
        candidates = [
            e for e in events
            if number(e["odds"].get(outcome))
        ]
        if not candidates:
            return None
        best[outcome] = max(
            candidates, key=lambda e: e["odds"][outcome]
        )

    # Ensure the three prices came from one consistently identified match.
    reference = best["home"]
    if not all(same_fixture(reference, best[o]) for o in ("draw", "away")):
        return None

    # At least two different bookmakers must contribute.
    contributing_books = {
        best[o]["bookmaker"] for o in ("home", "draw", "away")
    }
    if len(contributing_books) < 2:
        return None

    odds = {o: best[o]["odds"][o] for o in ("home", "draw", "away")}
    implied_sum = sum(1.0 / odds[o] for o in odds)

    if implied_sum >= 1.0:
        return None

    margin = ((1.0 / implied_sum) - 1.0) * 100.0
    stakes = rounded_stakes(odds, BANKROLL)
    if not stakes:
        return None

    total_stake = sum(stakes.values())
    returns = {
        o: stakes[o] * odds[o] for o in odds
    }
    worst_return = min(returns.values())
    worst_profit = worst_return - total_stake

    # Validate after rounding, not merely from the theoretical formula.
    if worst_profit < MIN_PROFIT_NGN:
        return None
    if margin < MIN_MARGIN_PERCENT:
        return None

    return {
        "home": reference["home"],
        "away": reference["away"],
        "league": reference.get("league", ""),
        "kickoff": reference.get("kickoff"),
        "odds": odds,
        "books": {o: best[o]["bookmaker"] for o in best},
        "stakes": stakes,
        "total_stake": total_stake,
        "worst_return": worst_return,
        "worst_profit": worst_profit,
        "margin_percent": margin,
        "implied_sum": implied_sum,
    }


def find_opportunities(fixtures):
    # Build groups using conservative same-orientation fixture matching.
    groups = []
    for fixture in fixtures:
        matched = False
        for group in groups:
            if same_fixture(group[0], fixture):
                group.append(fixture)
                matched = True
                break
        if not matched:
            groups.append([fixture])

    opportunities = []
    for group in groups:
        result = evaluate_group(group)
        if result:
            opportunities.append(result)

    opportunities.sort(key=lambda x: x["worst_profit"], reverse=True)
    return opportunities


# -------------------- Telegram --------------------

async def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram is not configured.")
        return False

    # Do not log the URL: it contains the secret bot token.
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(url, json=payload)

        if response.status_code != 200:
            log.warning("Telegram returned HTTP %s.", response.status_code)
            return False

        data = response.json()
        if not data.get("ok"):
            log.warning("Telegram API did not confirm message delivery.")
            return False

        log.info("Telegram message delivered.")
        return True

    except Exception as exc:
        log.warning("Telegram send failed: %s", type(exc).__name__)
        return False


def alert_key(opportunity):
    # Use rounded odds as part of the key so a changed opportunity
    # can alert again. This is in-memory deduplication only.
    odds_part = tuple(
        round(opportunity["odds"][k], 3)
        for k in ("home", "draw", "away")
    )
    return (
        clean_name(opportunity["home"]),
        clean_name(opportunity["away"]),
        odds_part,
    )


def format_alert(o):
    kickoff = "Unknown"
    if o.get("kickoff"):
        kickoff = datetime.fromtimestamp(
            o["kickoff"], tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")

    return (
        "⚽ SolveEase Arbitrage Candidate\n\n"
        f"{o['home']} vs {o['away']}\n"
        f"League: {o.get('league') or 'Unknown'}\n"
        f"Kickoff: {kickoff}\n\n"
        f"HOME: {o['odds']['home']:.2f} — {o['books']['home']}\n"
        f"DRAW: {o['odds']['draw']:.2f} — {o['books']['draw']}\n"
        f"AWAY: {o['odds']['away']:.2f} — {o['books']['away']}\n\n"
        f"Stake HOME: ₦{o['stakes']['home']:,.0f}\n"
        f"Stake DRAW: ₦{o['stakes']['draw']:,.0f}\n"
        f"Stake AWAY: ₦{o['stakes']['away']:,.0f}\n"
        f"Total stake: ₦{o['total_stake']:,.0f}\n"
        f"Lowest calculated return: ₦{o['worst_return']:,.0f}\n"
        f"Calculated worst-case profit: ₦{o['worst_profit']:,.0f}\n"
        f"Theoretical margin: {o['margin_percent']:.2f}%\n\n"
        "IMPORTANT: Candidate only. Recheck all three prices, market "
        "rules, limits and availability directly with the bookmakers "
        "before risking money. Odds can change or bets can be rejected."
    )


# -------------------- Scan cycle --------------------

async def perform_scan():
    global last_scan

    async with scan_lock:
        started = datetime.now(timezone.utc).isoformat()
        last_scan["started_at"] = started
        last_scan["error"] = None

        log.info("Starting bookmaker scan.")

        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT,
                headers=HEADERS,
            ) as client:
                fixtures, feed_results = await fetch_all_feeds(client)

            opportunities = find_opportunities(fixtures)

            last_scan.update({
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "fixtures": len(fixtures),
                "opportunities": len(opportunities),
                "feeds": feed_results,
                "error": None,
            })

            log.info(
                "Collected %s usable fixture records from configured feeds.",
                len(fixtures),
            )

            for name, result in feed_results.items():
                log.info(
                    "Feed %s: %s fixtures; status=%s",
                    name, result["fixtures"], result["status"]
                )

            if not fixtures:
                log.warning(
                    "No usable 1X2 fixtures. No opportunities will be invented."
                )
                return last_scan

            if not opportunities:
                log.info("No qualifying arbitrage opportunities found.")
                return last_scan

            log.info("Found %s qualifying candidates.", len(opportunities))

            for opportunity in opportunities:
                key = alert_key(opportunity)
                if key in sent_alert_keys:
                    continue

                delivered = await send_telegram(format_alert(opportunity))
                if delivered:
                    sent_alert_keys.add(key)

            # Bound memory usage.
            if len(sent_alert_keys) > 1000:
                sent_alert_keys.clear()

            return last_scan

        except Exception as exc:
            last_scan["error"] = type(exc).__name__
            last_scan["completed_at"] = datetime.now(timezone.utc).isoformat()
            log.exception("Scan cycle failed.")
            return last_scan


async def scan_loop():
    while True:
        try:
            await perform_scan()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Unexpected scanner loop error.")

        await asyncio.sleep(max(60, SCAN_INTERVAL))


# -------------------- FastAPI / Render --------------------

background_task = None


@asynccontextmanager
async def lifespan(app):
    global background_task

    log.info("SolveEase Arbitrage Engine starting.")

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        await send_telegram(
            "✅ SolveEase Arbitrage Engine started.\n"
            f"Bankroll setting: ₦{BANKROLL:,.0f}\n"
            f"Scan interval: {SCAN_INTERVAL} seconds.\n"
            "Startup does not confirm bookmaker feed availability."
        )
    else:
        log.warning("Telegram credentials are not configured.")

    background_task = asyncio.create_task(scan_loop())

    try:
        yield
    finally:
        if background_task:
            background_task.cancel()
            try:
                await background_task
            except asyncio.CancelledError:
                pass


app = FastAPI(
    title="SolveEase Arbitrage Engine",
    version="2.0.0",
    lifespan=lifespan,
)


@app.api_route("/", methods=["GET", "HEAD"])
async def health_check():
    return {
        "status": "online",
        "service": "SolveEase Arbitrage Engine",
        "telegram_configured": bool(
            TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
        ),
        "bankroll": BANKROLL,
        "scan_interval_seconds": SCAN_INTERVAL,
        "note": "Service health does not confirm bookmaker feed availability.",
    }


@app.get("/feed-status")
async def feed_status():
    return last_scan


@app.post("/scan-now")
async def scan_now():
    if scan_lock.locked():
        raise HTTPException(
            status_code=409,
            detail="A scan is already running. Check /feed-status.",
        )
    return await perform_scan()
