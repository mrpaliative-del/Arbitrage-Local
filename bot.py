
import os
import asyncio
import logging
import html
from urllib.parse import quote_plus

import httpx
import uvicorn
from fastapi import FastAPI
from thefuzz import fuzz


# ============================================================
# CONFIGURATION
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

TOTAL_BANKROLL = float(os.getenv("TOTAL_BANKROLL", "100000"))
ROUND_NEAREST = max(1, int(os.getenv("ROUND_NEAREST", "500")))
SIMILARITY_THRESHOLD = 85
SCAN_INTERVAL_SECONDS = 900

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("arbitrage_engine")

app = FastAPI(title="SolveEase Multi-Bookmaker Arbitrage Engine")

http_client: httpx.AsyncClient | None = None
scanner_task: asyncio.Task | None = None


# ============================================================
# UTILITIES
# ============================================================

def round_to_nearest(value, base=500):
    return int(base * round(value / base))


def bookmaker_deep_link(bookmaker, match_name):
    """Open a bookmaker page or a supported search page."""

    query = quote_plus(
        match_name.replace(" vs ", " ").replace(" v ", " ")
    )

    links = {
        "SportyBet": (
            f"https://www.sportybet.com/ng/sport/football?q={query}"
        ),
        "Bet9ja": "https://sports.bet9ja.com/desktop/home",
        "1xBet": f"https://1xbet.ng/en/search/{query}",
        "Betway": (
            f"https://www.betway.com.ng/sports/search?searchTerm={query}"
        ),
    }

    # These URLs are navigation suggestions, not verified event links.
    return links.get(bookmaker, "https://www.google.com")


def safe_html(value):
    return html.escape(str(value), quote=True)


# ============================================================
# TELEGRAM
# ============================================================

async def send_telegram_notification(message):
    """Send a Telegram message and log delivery failures."""

    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is missing.")
        return False

    if not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_CHAT_ID is missing.")
        return False

    url = (
        f"https://api.telegram.org/bot"
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
            async with httpx.AsyncClient(timeout=15) as temporary_client:
                response = await temporary_client.post(
                    url, json=payload
                )
        else:
            response = await client.post(url, json=payload)

        if response.status_code != 200:
            logger.error(
                "Telegram HTTP %s: %s",
                response.status_code,
                response.text[:1000],
            )
            return False

        result = response.json()

        if not result.get("ok"):
            logger.error(
                "Telegram API error: %s",
                result.get("description", "Unknown error"),
            )
            return False

        logger.info(
            "Telegram message delivered. Message ID: %s",
            result.get("result", {}).get("message_id"),
        )
        return True

    except httpx.TimeoutException:
        logger.error("Telegram request timed out.")
    except httpx.RequestError as exc:
        logger.error("Telegram connection error: %s", exc)
    except Exception:
        logger.exception("Unexpected Telegram delivery error.")

    return False


# ============================================================
# BOOKMAKER FEEDS
# IMPORTANT: Only odds actually returned by the source are used.
# These endpoints may change, block requests, or return other schemas.
# ============================================================

async def fetch_sportybet_feed(client):
    url = (
        "https://www.sportybet.com.ng/api/kes/factsCentre/"
        "pc/getUpcomingEvents?sport=football&today_only=true"
    )

    try:
        response = await client.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()

        formatted = []

        tournaments = data.get("data", {}).get("tournaments", [])

        for tournament in tournaments:
            for event in tournament.get("events", []):
                home = event.get("homeTeamName")
                away = event.get("awayTeamName")

                if not home or not away:
                    continue

                # Do not invent odds. Extract only odds present in
                # the response, if its schema exposes them.
                odds = extract_1x2_odds(event)

                if odds:
                    formatted.append({
                        "bookmaker": "SportyBet",
                        "match": f"{home} vs {away}",
                        "home_team": home,
                        "away_team": away,
                        "odds": odds,
                    })

        logger.info(
            "SportyBet: retrieved %d fixtures with usable odds.",
            len(formatted),
        )
        return formatted

    except Exception as exc:
        logger.warning("SportyBet feed failed: %s", exc)
        return []


async def fetch_bet9ja_feed(client):
    url = (
        "https://sports.bet9ja.com/desktop/feapi/mobile/"
        "getprematchevents"
    )

    try:
        response = await client.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()

        formatted = []

        for event in data.get("G", []):
            home = event.get("H")
            away = event.get("A")

            if not home or not away:
                continue

            # Do not assume a price field or market schema.
            # Skip events until their real odds are identifiable.
            odds = extract_1x2_odds(event)

            if odds:
                formatted.append({
                    "bookmaker": "Bet9ja",
                    "match": f"{home} vs {away}",
                    "home_team": home,
                    "away_team": away,
                    "odds": odds,
                })

        logger.info(
            "Bet9ja: retrieved %d fixtures with usable odds.",
            len(formatted),
        )
        return formatted

    except Exception as exc:
        logger.warning("Bet9ja feed failed: %s", exc)
        return []


async def fetch_1xbet_feed(client):
    # No verified live 1xBet odds endpoint is configured.
    logger.warning(
        "1xBet feed is not configured. No sample odds will be used."
    )
    return []


async def fetch_betway_feed(client):
    # No verified live Betway odds endpoint is configured.
    logger.warning(
        "Betway feed is not configured. No sample odds will be used."
    )
    return []


def extract_1x2_odds(event):
    """
    Conservative odds extractor.

    This intentionally accepts only an explicit, recognizable
    1X2 structure. Unknown schemas are skipped rather than guessed.

    Adapt this function after inspecting the live bookmaker JSON.
    """

    candidate = event.get("odds")

    if not isinstance(candidate, dict):
        candidate = event.get("odds1x2")

    if not isinstance(candidate, dict):
        return {}

    aliases = {
        "Home": ("Home", "home", "1"),
        "Draw": ("Draw", "draw", "X"),
        "Away": ("Away", "away", "2"),
    }

    extracted = {}

    for outcome, keys in aliases.items():
        for key in keys:
            value = candidate.get(key)

            try:
                price = float(value)
            except (TypeError, ValueError):
                continue

            if 1.0 < price < 1000.0:
                extracted[outcome] = price
                break

    if len(extracted) == 3:
        return extracted

    return {}


# ============================================================
# ARBITRAGE CALCULATION
# ============================================================

def match_and_evaluate_all(all_fixtures, bankroll):
    opportunities = []

    if bankroll <= 0:
        logger.error("Bankroll must be greater than zero.")
        return opportunities

    for i in range(len(all_fixtures)):
        for j in range(i + 1, len(all_fixtures)):
            fix1 = all_fixtures[i]
            fix2 = all_fixtures[j]

            if fix1["bookmaker"] == fix2["bookmaker"]:
                continue

            home_score = fuzz.token_sort_ratio(
                fix1["home_team"], fix2["home_team"]
            )
            away_score = fuzz.token_sort_ratio(
                fix1["away_team"], fix2["away_team"]
            )

            if (
                home_score < SIMILARITY_THRESHOLD
                or away_score < SIMILARITY_THRESHOLD
            ):
                continue

            odds1 = fix1.get("odds", {})
            odds2 = fix2.get("odds", {})

            best_outcomes = []

            for outcome in ("Home", "Draw", "Away"):
                try:
                    v1 = float(odds1.get(outcome, 0))
                    v2 = float(odds2.get(outcome, 0))
                except (TypeError, ValueError):
                    continue

                if v1 <= 1 or v2 <= 1:
                    break

                if v1 >= v2:
                    best_outcomes.append({
                        "bookmaker": fix1["bookmaker"],
                        "selection": outcome,
                        "odds": v1,
                    })
                else:
                    best_outcomes.append({
                        "bookmaker": fix2["bookmaker"],
                        "selection": outcome,
                        "odds": v2,
                    })

            if len(best_outcomes) != 3:
                continue

            implied_sum = sum(
                1.0 / item["odds"] for item in best_outcomes
            )

            if implied_sum >= 1.0:
                continue

            margin = ((1.0 / implied_sum) - 1.0) * 100

            # Equalize theoretical returns, then round each stake.
            allocations = []

            for item in best_outcomes:
                raw_stake = bankroll / (
                    item["odds"] * implied_sum
                )
                stake = round_to_nearest(
                    raw_stake, ROUND_NEAREST
                )

                allocations.append({
                    **item,
                    "stake": max(0, stake),
                    "return": round(
                        max(0, stake) * item["odds"], 2
                    ),
                    "link": bookmaker_deep_link(
                        item["bookmaker"], fix1["match"]
                    ),
                })

            total_staked = sum(
                item["stake"] for item in allocations
            )

            if total_staked <= 0 or total_staked > bankroll:
                logger.info(
                    "Candidate rejected: rounded stakes exceed "
                    "available bankroll."
                )
                continue

            min_return = min(
                item["return"] for item in allocations
            )
            net_profit = round(min_return - total_staked, 2)

            # Rounding can erase the theoretical arbitrage margin.
            if net_profit <= 0:
                continue

            opportunities.append({
                "match": fix1["match"],
                "bookmakers_involved": (
                    f"{fix1['bookmaker']} & {fix2['bookmaker']}"
                ),
                "margin": round(margin, 2),
                "total_staked": total_staked,
                "net_profit": net_profit,
                "allocations": allocations,
                "theoretical_implied_sum": round(implied_sum, 6),
            })

    return opportunities


# ============================================================
# SCAN LOOP
# ============================================================

async def scan_loop():
    if http_client is None:
        logger.error("HTTP client is not initialized.")
        return

    logger.info("Starting bookmaker scan...")

    results = await asyncio.gather(
        fetch_sportybet_feed(http_client),
        fetch_bet9ja_feed(http_client),
        fetch_1xbet_feed(http_client),
        fetch_betway_feed(http_client),
        return_exceptions=True,
    )

    all_fixtures = []

    for result in results:
        if isinstance(result, list):
            all_fixtures.extend(result)
        elif isinstance(result, Exception):
            logger.error("Feed task failed: %s", result)

    logger.info(
        "Scan collected %d fixture records.",
        len(all_fixtures),
    )

    if not all_fixtures:
        logger.warning(
            "No usable live fixtures. Skipping this scan; "
            "no fabricated odds will be used."
        )
        return

    opportunities = match_and_evaluate_all(
        all_fixtures,
        TOTAL_BANKROLL,
    )

    logger.info(
        "Scan complete: %d candidate arbitrage opportunities.",
        len(opportunities),
    )

    for arb in opportunities:
        lines = [
            "🚨 <b>ARBITRAGE CANDIDATE</b>",
            "",
            f"⚽ <b>Fixture:</b> {safe_html(arb['match'])}",
            f"📈 <b>Theoretical margin:</b> {arb['margin']:.2f}%",
            f"💸 <b>Total stake:</b> ₦{arb['total_staked']:,.2f}",
            f"💰 <b>Estimated net profit:</b> ₦{arb['net_profit']:,.2f}",
            "",
            "📋 <b>Selections:</b>",
        ]

        for allocation in arb["allocations"]:
            bookmaker = safe_html(allocation["bookmaker"])
            selection = safe_html(allocation["selection"])
            link = html.escape(allocation["link"], quote=True)

            lines.append(
                f'• <a href="{link}">{bookmaker}</a>: '
                f"{selection} @ {allocation['odds']:.2f} "
                f"— ₦{allocation['stake']:,.2f}"
            )

        lines.extend([
            "",
            "⚠️ Candidate only. Confirm live odds, market rules, "
            "limits, fees and event identity before placing bets.",
        ])

        await send_telegram_notification("\n".join(lines))


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

    logger.info("Arbitrage engine starting.")

    http_client = httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 SolveEaseArbitrage/1.0"},
        timeout=httpx.Timeout(15.0),
        follow_redirects=True,
    )

    # Send a startup test independently of bookmaker availability.
    await send_telegram_notification(
        "✅ <b>ARBITRAGE ENGINE ONLINE</b>\n\n"
        "Telegram startup test initiated successfully.\n"
        "The scanner is starting. Live feed availability "
        "will be reported in the service logs."
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
        "note": "Online status does not confirm live bookmaker feeds.",
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
    asyncio.create_task(scan_loop())
    return {
        "status": "accepted",
        "message": "A scan was requested. Check service logs.",
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
    )
