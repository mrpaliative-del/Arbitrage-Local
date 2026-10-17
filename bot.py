
import os
import asyncio
import logging
import html
import math
from datetime import datetime, timezone
from urllib.parse import quote_plus

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from thefuzz import fuzz


# ============================================================
# CONFIGURATION
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

TOTAL_BANKROLL = float(os.getenv("TOTAL_BANKROLL", "100000"))
ROUND_NEAREST = max(1, int(os.getenv("ROUND_NEAREST", "500")))
SIMILARITY_THRESHOLD = int(os.getenv("SIMILARITY_THRESHOLD", "90"))
SCAN_INTERVAL_SECONDS = max(
    60, int(os.getenv("SCAN_INTERVAL_SECONDS", "900"))
)

HTTP_TIMEOUT = 15.0
MIN_ODDS = 1.01
MAX_ODDS = 1000.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("arbitrage_engine")

app = FastAPI(title="SolveEase Multi-Bookmaker Arbitrage Engine")

http_client = None
scanner_task = None
scan_lock = asyncio.Lock()
last_alert_signatures = set()

BOOKMAKERS = ("SportyBet", "Bet9ja", "1xBet", "Betway")

BOOKMAKER_HOME_URLS = {
    "SportyBet": "https://www.sportybet.com/ng/",
    "Bet9ja": "https://sports.bet9ja.com/desktop/home",
    "1xBet": "https://1xbet.ng/",
    "Betway": "https://www.betway.com.ng/",
}

# These are candidate public endpoints, not guaranteed stable APIs.
SPORTYBET_FEED_URL = (
    "https://www.sportybet.com.ng/api/kes/factsCentre/"
    "pc/getUpcomingEvents?sport=football&today_only=true"
)
BET9JA_FEED_URL = (
    "https://sports.bet9ja.com/desktop/feapi/mobile/"
    "getprematchevents"
)

# No verified live odds endpoint is configured for these two sources.
# Do not replace these with made-up odds or sample fixtures.


# ============================================================
# UTILITIES
# ============================================================

def safe_html(value):
    return html.escape(str(value), quote=True)


def parse_price(value):
    """Return a plausible decimal price, or None."""
    if isinstance(value, bool) or value is None:
        return None

    try:
        price = float(str(value).strip())
    except (TypeError, ValueError):
        return None

    if math.isfinite(price) and MIN_ODDS <= price <= MAX_ODDS:
        return price

    return None


def normalize_name(value):
    return " ".join(str(value or "").lower().split())


def bookmaker_deep_link(bookmaker, match_name):
    """
    Return a navigation URL. This is not guaranteed to be a direct
    event URL and does not prove that the bookmaker's odds are live.
    """
    query = quote_plus(
        str(match_name)
        .replace(" vs ", " ")
        .replace(" v ", " ")
    )

    if bookmaker == "SportyBet":
        return f"https://www.sportybet.com/ng/sport/football?q={query}"

    if bookmaker == "1xBet":
        return f"https://1xbet.ng/en/search/{query}"

    if bookmaker == "Betway":
        return (
            "https://www.betway.com.ng/sports/search"
            f"?searchTerm={query}"
        )

    return BOOKMAKER_HOME_URLS.get(
        bookmaker, "https://www.google.com/"
    )


def event_team_names(event):
    """Read only recognized team-name fields."""
    home = (
        event.get("homeTeamName")
        or event.get("homeTeam")
        or event.get("home")
        or event.get("H")
    )
    away = (
        event.get("awayTeamName")
        or event.get("awayTeam")
        or event.get("away")
        or event.get("A")
    )

    # Some APIs wrap team names in objects.
    if isinstance(home, dict):
        home = (
            home.get("name")
            or home.get("teamName")
            or home.get("n")
        )

    if isinstance(away, dict):
        away = (
            away.get("name")
            or away.get("teamName")
            or away.get("n")
        )

    if not isinstance(home, str) or not isinstance(away, str):
        return None, None

    home, away = home.strip(), away.strip()

    if not home or not away:
        return None, None

    return home, away


def event_start_time(event):
    """Return a source-provided event time, if recognizable."""
    for key in (
        "startTime", "start_time", "kickoff", "kickoffTime",
        "eventTime", "開始時間", "date"
    ):
        value = event.get(key)
        if value is not None and not isinstance(value, (dict, list)):
            return str(value)
    return None


def extract_outcome_label(value):
    """Map explicit 1X2 labels to the engine's canonical names."""
    if not isinstance(value, str):
        return None

    label = normalize_name(value).strip(" .:-_")

    if label in {
        "home", "1", "1x2 1", "home win", "team 1",
        "1st team", "local"
    }:
        return "Home"

    if label in {
        "draw", "x", "tie", "1x2 x", "the draw"
    }:
        return "Draw"

    if label in {
        "away", "2", "1x2 2", "away win", "team 2",
        "2nd team", "visitor"
    }:
        return "Away"

    return None


def extract_price_from_selection(selection):
    """Read prices only from explicit, recognizable price fields."""
    if isinstance(selection, (int, float, str)):
        return parse_price(selection)

    if not isinstance(selection, dict):
        return None

    for key in (
        "odds", "odd", "price", "decimalOdds",
        "decimalOdd", "value", "rate", "O"
    ):
        price = parse_price(selection.get(key))
        if price is not None:
            return price

    return None


def extract_from_explicit_1x2_dict(candidate):
    """
    Accept a direct outcome-to-price dictionary such as:
    {"Home": 2.1, "Draw": 3.2, "Away": 3.5}
    """
    if not isinstance(candidate, dict):
        return {}

    result = {}

    for key, value in candidate.items():
        outcome = extract_outcome_label(str(key))
        price = extract_price_from_selection(value)

        if outcome and price is not None:
            result[outcome] = price

    return result if len(result) == 3 else {}


def extract_from_market_list(markets):
    """
    Parse markets only when their name explicitly identifies a
    match-result / 1X2 market and all three outcomes are recognized.
    Unknown market schemas are skipped.
    """
    if not isinstance(markets, list):
        return {}

    accepted_market_names = {
        "1x2",
        "match result",
        "full time result",
        "full-time result",
        "3 way result",
        "three way result",
        "regular time result",
    }

    for market in markets:
        if not isinstance(market, dict):
            continue

        market_name = normalize_name(
            market.get("name")
            or market.get("marketName")
            or market.get("market")
            or market.get("title")
            or market.get("N")
            or ""
        )

        if market_name not in accepted_market_names:
            continue

        selections = (
            market.get("selections")
            or market.get("outcomes")
            or market.get("runners")
            or market.get("options")
            or market.get("E")
            or market.get("B")
        )

        if isinstance(selections, dict):
            result = extract_from_explicit_1x2_dict(selections)
            if result:
                return result

        if isinstance(selections, list):
            result = {}

            for selection in selections:
                if not isinstance(selection, dict):
                    continue

                label = (
                    selection.get("name")
                    or selection.get("label")
                    or selection.get("outcome")
                    or selection.get("selectionName")
                    or selection.get("N")
                )
                outcome = extract_outcome_label(label)
                price = extract_price_from_selection(selection)

                if outcome and price is not None:
                    result[outcome] = price

            if len(result) == 3:
                return result

    return {}


def extract_1x2_odds(event):
    """
    Conservative parser: recognize explicit 1X2 fields or named
    match-result markets. Never infer missing odds from other markets.
    """
    if not isinstance(event, dict):
        return {}

    for key in ("odds1x2", "odds", "matchResultOdds", "fullTimeResult"):
        result = extract_from_explicit_1x2_dict(event.get(key))
        if result:
            return result

    for key in ("markets", "marketList", "betMarkets", "M"):
        result = extract_from_market_list(event.get(key))
        if result:
            return result

    return {}


def find_event_lists(payload):
    """
    Find event lists in common response wrappers without assuming
    that any arbitrary list is a list of fixtures.
    """
    if not isinstance(payload, dict):
        return []

    data = payload.get("data", payload)

    if isinstance(data, dict):
        tournaments = data.get("tournaments")
        if isinstance(tournaments, list):
            events = []
            for tournament in tournaments:
                if isinstance(tournament, dict):
                    batch = tournament.get("events")
                    if isinstance(batch, list):
                        events.extend(batch)
            if events:
                return events

        for key in ("events", "eventList", "fixtures", "G"):
            value = data.get(key)
            if isinstance(value, list):
                return value

    if isinstance(data, list):
        return data

    return []


def format_event(event, bookmaker):
    home, away = event_team_names(event)

    if not home or not away:
        return None

    odds = extract_1x2_odds(event)
    if set(odds) != {"Home", "Draw", "Away"}:
        return None

    return {
        "bookmaker": bookmaker,
        "match": f"{home} vs {away}",
        "home_team": home,
        "away_team": away,
        "start_time": event_start_time(event),
        "odds": odds,
        "link": bookmaker_deep_link(
            bookmaker, f"{home} vs {away}"
        ),
    }


# ============================================================
# TELEGRAM
# ============================================================

async def send_telegram_notification(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error(
            "Telegram not configured: set TELEGRAM_BOT_TOKEN "
            "and TELEGRAM_CHAT_ID."
        )
        return False

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        client = http_client

        if client is None:
            async with httpx.AsyncClient(timeout=15) as temp_client:
                response = await temp_client.post(url, json=payload)
        else:
            response = await client.post(url, json=payload)

        response.raise_for_status()
        result = response.json()

        if not result.get("ok"):
            logger.error(
                "Telegram API error: %s",
                result.get("description", "Unknown error"),
            )
            return False

        logger.info("Telegram message delivered.")
        return True

    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Telegram delivery failed: %s", exc)
    except Exception:
        logger.exception("Unexpected Telegram delivery error.")

    return False


# ============================================================
# BOOKMAKER FEEDS
# ============================================================

async def fetch_json(client, bookmaker, url):
    try:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()

        if not isinstance(payload, (dict, list)):
            logger.warning("%s returned unexpected JSON.", bookmaker)
            return None

        return payload

    except httpx.HTTPStatusError as exc:
        logger.warning(
            "%s HTTP error %s.",
            bookmaker, exc.response.status_code
        )
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("%s feed failed: %s", bookmaker, exc)
    except Exception:
        logger.exception("%s feed parsing failed.", bookmaker)

    return None


async def fetch_sportybet_feed(client):
    payload = await fetch_json(
        client, "SportyBet", SPORTYBET_FEED_URL
    )
    if payload is None:
        return []

    fixtures = []
    for event in find_event_lists(payload):
        if isinstance(event, dict):
            formatted = format_event(event, "SportyBet")
            if formatted:
                fixtures.append(formatted)

    logger.info(
        "SportyBet: %d fixtures with recognizable 1X2 odds.",
        len(fixtures),
    )
    return fixtures


async def fetch_bet9ja_feed(client):
    payload = await fetch_json(
        client, "Bet9ja", BET9JA_FEED_URL
    )
    if payload is None:
        return []

    fixtures = []
    for event in find_event_lists(payload):
        if isinstance(event, dict):
            formatted = format_event(event, "Bet9ja")
            if formatted:
                fixtures.append(formatted)

    logger.info(
        "Bet9ja: %d fixtures with recognizable 1X2 odds.",
        len(fixtures),
    )
    return fixtures


async def fetch_1xbet_feed(client):
    logger.warning(
        "1xBet: no verified odds endpoint is configured; skipped."
    )
    return []


async def fetch_betway_feed(client):
    logger.warning(
        "Betway: no verified odds endpoint is configured; skipped."
    )
    return []


# ============================================================
# MATCH GROUPING
# ============================================================

def same_fixture(a, b):
    """
    Require the same home/away orientation and sufficiently similar
    team names. If both sources provide kickoff times, reject a clear
    mismatch. Team similarity alone is not proof of event identity.
    """
    home_score = fuzz.token_sort_ratio(
        normalize_name(a["home_team"]),
        normalize_name(b["home_team"]),
    )
    away_score = fuzz.token_sort_ratio(
        normalize_name(a["away_team"]),
        normalize_name(b["away_team"]),
    )

    if (
        home_score < SIMILARITY_THRESHOLD
        or away_score < SIMILARITY_THRESHOLD
    ):
        return False

    time_a = a.get("start_time")
    time_b = b.get("start_time")

    # When the formats differ, don't pretend we have verified timing.
    # Only reject times when both parse cleanly as ISO timestamps.
    if time_a and time_b:
        try:
            dt_a = datetime.fromisoformat(
                str(time_a).replace("Z", "+00:00")
            )
            dt_b = datetime.fromisoformat(
                str(time_b).replace("Z", "+00:00")
            )

            if dt_a.tzinfo is None:
                dt_a = dt_a.replace(tzinfo=timezone.utc)
            if dt_b.tzinfo is None:
                dt_b = dt_b.replace(tzinfo=timezone.utc)

            if abs((dt_a - dt_b).total_seconds()) > 6 * 3600:
                return False

        except (TypeError, ValueError):
            pass

    return True


def group_fixtures(fixtures):
    """Build event groups from the records actually retrieved."""
    groups = []

    for fixture in fixtures:
        matched_group = None

        for group in groups:
            if same_fixture(fixture, group[0]):
                matched_group = group
                break

        if matched_group is None:
            groups.append([fixture])
        else:
            # Keep only one record per bookmaker in a group.
            existing = {
                item["bookmaker"] for item in matched_group
            }
            if fixture["bookmaker"] not in existing:
                matched_group.append(fixture)

    return groups


# ============================================================
# ARBITRAGE CALCULATION
# ============================================================

def round_stake_down(value, base):
    """Round down so rounding does not exceed the bankroll."""
    return int(math.floor(max(0, value) / base) * base)


def evaluate_fixture_group(group, bankroll):
    """
    Select the best available price for Home, Draw and Away across
    all retrieved bookmakers, then test the 1X2 arbitrage condition.
    """
    if bankroll <= 0:
        return None

    best = {}

    for fixture in group:
        odds = fixture.get("odds", {})

        for outcome in ("Home", "Draw", "Away"):
            price = parse_price(odds.get(outcome))
            if price is None:
                continue

            current = best.get(outcome)
            if current is None or price > current["odds"]:
                best[outcome] = {
                    "bookmaker": fixture["bookmaker"],
                    "selection": outcome,
                    "odds": price,
                    "link": fixture.get("link")
                    or BOOKMAKER_HOME_URLS.get(
                        fixture["bookmaker"], "https://www.google.com/"
                    ),
                }

    if set(best) != {"Home", "Draw", "Away"}:
        return None

    implied_sum = sum(
        1.0 / best[outcome]["odds"]
        for outcome in ("Home", "Draw", "Away")
    )

    # Strict condition: theoretical 1X2 arbitrage exists only if S < 1.
    if implied_sum >= 1.0:
        return None

    theoretical_margin = ((1.0 / implied_sum) - 1.0) * 100

    allocations = []

    for outcome in ("Home", "Draw", "Away"):
        item = best[outcome]

        raw_stake = bankroll / (item["odds"] * implied_sum)
        stake = round_stake_down(raw_stake, ROUND_NEAREST)

        if stake <= 0:
            return None

        allocations.append({
            **item,
            "stake": stake,
            "gross_return": round(stake * item["odds"], 2),
        })

    total_staked = sum(item["stake"] for item in allocations)

    if total_staked <= 0 or total_staked > bankroll:
        return None

    min_gross_return = min(
        item["gross_return"] for item in allocations
    )
    actual_worst_case_profit = round(
        min_gross_return - total_staked, 2
    )

    # Rounding may destroy the margin. Reject it if the actual
    # rounded stake plan does not remain profitable in all outcomes.
    if actual_worst_case_profit <= 0:
        return None

    first = group[0]

    return {
        "match": first["match"],
        "start_time": first.get("start_time"),
        "bookmakers_involved": sorted({
            item["bookmaker"] for item in allocations
        }),
        "margin": round(theoretical_margin, 3),
        "implied_sum": round(implied_sum, 6),
        "total_staked": total_staked,
        "net_profit": actual_worst_case_profit,
        "actual_roi": round(
            actual_worst_case_profit / total_staked * 100, 3
        ),
        "allocations": allocations,
    }


def match_and_evaluate_all(all_fixtures, bankroll):
    opportunities = []

    for group in group_fixtures(all_fixtures):
        if len(group) < 2:
            continue

        opportunity = evaluate_fixture_group(group, bankroll)
        if opportunity:
            opportunities.append(opportunity)

    opportunities.sort(
        key=lambda item: item["actual_roi"], reverse=True
    )
    return opportunities


# ============================================================
# SCAN LOOP
# ============================================================

async def scan_loop():
    global last_alert_signatures

    if http_client is None:
        logger.error("HTTP client is not initialized.")
        return {
            "fixtures": 0,
            "opportunities": 0,
            "status": "http_client_not_initialized",
        }

    if scan_lock.locked():
        logger.warning("A scan is already running; duplicate scan skipped.")
        return {
            "fixtures": 0,
            "opportunities": 0,
            "status": "scan_already_running",
        }

    async with scan_lock:
        logger.info("Starting bookmaker scan.")

        results = await asyncio.gather(
            fetch_sportybet_feed(http_client),
            fetch_bet9ja_feed(http_client),
            fetch_1xbet_feed(http_client),
            fetch_betway_feed(http_client),
            return_exceptions=True,
        )

        fixtures = []

        for result in results:
            if isinstance(result, list):
                fixtures.extend(result)
            elif isinstance(result, Exception):
                logger.error("Bookmaker task failed: %s", result)

        logger.info(
            "Collected %d fixture records from configured feeds.",
            len(fixtures),
        )

        if not fixtures:
            logger.warning(
                "No usable 1X2 fixtures. No opportunities will be invented."
            )
            return {
                "fixtures": 0,
                "opportunities": 0,
                "status": "no_usable_live_odds",
            }

        opportunities = match_and_evaluate_all(
            fixtures, TOTAL_BANKROLL
        )

        logger.info(
            "Scan complete: %d profitable rounded-stake candidates.",
            len(opportunities),
        )

        current_signatures = set()

        for arb in opportunities:
            signature = (
                normalize_name(arb["match"]),
                tuple(
                    (
                        item["bookmaker"],
                        item["selection"],
                        item["odds"],
                    )
                    for item in arb["allocations"]
                ),
            )
            current_signatures.add(signature)

            # Avoid repeatedly sending the same candidate every 15 minutes.
            if signature in last_alert_signatures:
                continue

            lines = [
                "🚨 <b>ARBITRAGE CANDIDATE</b>",
                "",
                f"⚽ <b>Fixture:</b> {safe_html(arb['match'])}",
                f"📊 <b>Theoretical margin:</b> {arb['margin']:.3f}%",
                f"💸 <b>Total stake:</b> ₦{arb['total_staked']:,.2f}",
                f"💰 <b>Worst-case profit after rounding:</b> "
                f"₦{arb['net_profit']:,.2f}",
                f"📈 <b>ROI on actual stakes:</b> "
                f"{arb['actual_roi']:.3f}%",
                f"🧮 <b>Implied probability sum:</b> "
                f"{arb['implied_sum']:.6f}",
                "",
                "<b>Selections:</b>",
            ]

            for item in arb["allocations"]:
                link = html.escape(item["link"], quote=True)
                bookmaker = safe_html(item["bookmaker"])
                selection = safe_html(item["selection"])

                lines.append(
                    f'• <a href="{link}">{bookmaker}</a> — '
                    f"{selection} @ {item['odds']:.3f}; "
                    f"stake ₦{item['stake']:,.2f}; "
                    f"gross return ₦{item['gross_return']:,.2f}"
                )

            lines.extend([
                "",
                "⚠️ <b>Verify before betting:</b> live prices, exact fixture, "
                "market settlement rules, limits, fees and account access. "
                "Links are navigation links, not verified event links. "
                "Profit is theoretical and is not guaranteed.",
            ])

            await send_telegram_notification("\n".join(lines))

        # Keep only currently detected opportunities. If one disappears
        # and later returns, it can trigger a new notification.
        last_alert_signatures = current_signatures

        return {
            "fixtures": len(fixtures),
            "opportunities": len(opportunities),
            "status": "scan_completed",
        }


async def continuous_scanner():
    await asyncio.sleep(2)

    while True:
        try:
            await scan_loop()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scanner cycle failed.")

        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


# ============================================================
# FASTAPI LIFECYCLE
# ============================================================

@app.on_event("startup")
async def startup_event():
    global http_client, scanner_task

    logger.info("SolveEase Arbitrage Engine starting.")

    http_client = httpx.AsyncClient(
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; SolveEaseArbitrage/1.0)"
            ),
            "Accept": "application/json",
        },
        timeout=httpx.Timeout(HTTP_TIMEOUT),
        follow_redirects=True,
    )

    await send_telegram_notification(
        "✅ <b>ARBITRAGE ENGINE ONLINE</b>\n\n"
        "The service started. This confirms service startup only, "
        "not bookmaker feed availability."
    )

    scanner_task = asyncio.create_task(continuous_scanner())


@app.on_event("shutdown")
async def shutdown_event():
    global http_client, scanner_task

    if scanner_task is not None:
        scanner_task.cancel()
        try:
            await scanner_task
        except asyncio.CancelledError:
            pass
        scanner_task = None

    if http_client is not None:
        await http_client.aclose()
        http_client = None


# ============================================================
# HEALTH AND MANUAL TEST ENDPOINTS
# ============================================================

@app.get("/")
async def health_check():
    return {
        "status": "online",
        "service": "SolveEase Arbitrage Engine",
        "telegram_configured": bool(
            TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
        ),
        "bankroll": TOTAL_BANKROLL,
        "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
        "bookmakers": {
            "SportyBet": "candidate feed configured; live odds unverified",
            "Bet9ja": "candidate feed configured; live odds unverified",
            "1xBet": "not configured",
            "Betway": "not configured",
        },
        "note": (
            "Online status does not prove that feeds return usable odds. "
            "A candidate is not a guaranteed or risk-free profit."
        ),
    }


@app.get("/test-telegram")
async def test_telegram():
    delivered = await send_telegram_notification(
        "✅ <b>TELEGRAM DELIVERY TEST</b>\n\n"
        "This message was triggered through the test endpoint."
    )

    return {
        "telegram_delivered": delivered,
        "message": (
            "Telegram accepted the message."
            if delivered
            else "Delivery failed. Check service logs."
        ),
    }


@app.post("/scan-now")
async def scan_now():
    if scan_lock.locked():
        raise HTTPException(
            status_code=409,
            detail="A scan is already running.",
        )

    # Return the scan result so the caller can see whether fixtures
    # were actually found instead of receiving an unconditional success.
    return await scan_loop()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
    )
