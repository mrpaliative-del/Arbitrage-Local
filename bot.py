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

# Optional browser engine. Install Playwright and Chromium to enable it.
try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    async_playwright = None
    PLAYWRIGHT_AVAILABLE = False


# ============================================================
# SolveEase Arbitrage Engine
# Free-source-first, analysis and Telegram alerts only.
# Does not place bets or bypass website access controls.
# ============================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger("arbitrage_engine")

BANKROLL = float(os.getenv("BANKROLL_NGN", "100000"))
SCAN_INTERVAL = max(60, int(os.getenv("SCAN_INTERVAL_SECONDS", "900")))
STAKE_STEP = max(1, float(os.getenv("STAKE_STEP_NGN", "500")))
MIN_PROFIT = float(os.getenv("MIN_PROFIT_NGN", "100"))
MIN_MARGIN = float(os.getenv("MIN_MARGIN_PERCENT", "0.5"))
MAX_PRICE_AGE = int(os.getenv("MAX_PRICE_AGE_SECONDS", "1200"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "15"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Existing feed endpoints. Their continued availability is not guaranteed.
SPORTYBET_URL = (
    "https://www.sportybet.com.ng/api/kes/factsCentre/"
    "pc/getUpcomingEvents"
)

# ============================================================
# Public Page Candidates & Browser Sources Configuration
# ============================================================

XBET_PAGE_URL = os.getenv(
    "XBET_PAGE_URL",
    "https://1xbet.ng/en/line/football",
).strip()

BET9JA_PAGE_URL = os.getenv(
    "BET9JA_PAGE_URL",
    "https://sports.bet9ja.com/?lang=en",
).strip()

BETWAY_PAGE_URL = os.getenv(
    "BETWAY_PAGE_URL",
    "https://beta.betway.com.ng/sport/soccer/ltu",
).strip()


def browser_source(prefix, url):
    return {
        "url": url,
        "event": os.getenv(f"{prefix}_EVENT_SELECTOR", "").strip(),
        "home": os.getenv(f"{prefix}_HOME_SELECTOR", "").strip(),
        "away": os.getenv(f"{prefix}_AWAY_SELECTOR", "").strip(),
        "home_odds": os.getenv(f"{prefix}_HOME_ODDS_SELECTOR", "").strip(),
        "draw_odds": os.getenv(f"{prefix}_DRAW_ODDS_SELECTOR", "").strip(),
        "away_odds": os.getenv(f"{prefix}_AWAY_ODDS_SELECTOR", "").strip(),
        "league": os.getenv(f"{prefix}_LEAGUE_SELECTOR", "").strip(),
    }


BROWSER_SOURCES = {
    "1xBet": browser_source("XBET", XBET_PAGE_URL),
    "Bet9ja": browser_source("BET9JA", BET9JA_PAGE_URL),
    "Betway": browser_source("BETWAY", BETWAY_PAGE_URL),
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
background_task = None
sent_alert_keys = set()

last_scan = {
    "started_at": None,
    "completed_at": None,
    "fixtures": 0,
    "opportunities": 0,
    "feeds": {},
    "error": None,
}


# -------------------- Helpers --------------------

def number(value):
    if value is None:
        return None
    try:
        text_value = str(value).replace(",", "").strip()
        match = re.search(r"\d+(?:\.\d+)?", text_value)
        if not match:
            return None
        result = float(match.group())
        return result if 1.01 <= result < 10000 else None
    except (ValueError, TypeError):
        return None


def clean_name(value):
    value = str(value or "").lower()
    value = re.sub(r"\b(fc|sc|afc|cf|club)\b", " ", value)
    value = re.sub(r"[^a-z0-9 ]", " ", value)
    return " ".join(value.split())


def names_match(a, b):
    a, b = clean_name(a), clean_name(b)
    if not a or not b:
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.88


def parse_timestamp(value):
    if value is None:
        return None
    try:
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        if stamp > 0:
            return stamp
    except (ValueError, TypeError):
        pass

    try:
        value = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, TypeError):
        return None


def make_fixture(bookmaker, home, away, odds, league="", kickoff=None,
                 source_id=None, price_updated_at=None):
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
        "price_updated_at": parse_timestamp(price_updated_at),
    }

    if (
        not fixture["home"]
        or not fixture["away"]
        or clean_name(fixture["home"]) == clean_name(fixture["away"])
        or not all(fixture["odds"].get(k) for k in ("home", "draw", "away"))
    ):
        return None

    return fixture


# -------------------- SportyBet adapter --------------------

def parse_sportybet(payload):
    fixtures = []
    if not isinstance(payload, dict):
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

        league = tournament.get("name", "")
        events = tournament.get("events", [])

        if not isinstance(events, list):
            continue

        for event in events:
            if not isinstance(event, dict):
                continue

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

                desc = str(market.get("desc", "")).strip().lower()
                market_id = str(market.get("id", ""))

                if market_id != "1" and desc not in {
                    "1x2", "match result", "full time result",
                    "fulltime result",
                }:
                    continue

                outcomes = market.get("outcomes", [])
                if not isinstance(outcomes, list):
                    continue

                for outcome in outcomes:
                    if not isinstance(outcome, dict):
                        continue

                    price = number(outcome.get("odds"))
                    if not price:
                        continue

                    oid = str(outcome.get("id", "")).lower()
                    name = str(outcome.get("desc", "")).lower()

                    if oid in {"1", "home"} or name in {"1", "home", "home win"}:
                        odds["home"] = price
                    elif oid in {"2", "draw", "x"} or name in {"x", "draw"}:
                        odds["draw"] = price
                    elif oid in {"3", "away"} or name in {"2", "away", "away win"}:
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
    try:
        response = await client.get(
            SPORTYBET_URL,
            params={
                "sport": "football",
                "today_only": "true",
            },
            headers={
                **HEADERS,
                "Referer": "https://www.sportybet.com/ng/",
            },
            follow_redirects=False,
        )

        if response.status_code != 200:
            return [], f"HTTP {response.status_code}"

        if "json" not in response.headers.get("content-type", "").lower():
            return [], "UNAVAILABLE: response is not JSON"

        try:
            payload = response.json()
        except ValueError:
            return [], "PARSER_FAILED: invalid JSON"

        fixtures = parse_sportybet(payload)
        return (
            fixtures,
            "OK" if fixtures else "PARSER_FAILED: no valid 1X2 fixtures",
        )

    except httpx.TimeoutException:
        return [], "TIMEOUT"
    except httpx.HTTPError as exc:
        return [], f"NETWORK_ERROR: {type(exc).__name__}"


# -------------------- Browser-based odds collection --------------------

async def fetch_browser_bookmaker(name, config):
    """
    Reads publicly rendered event cards using user-verified CSS selectors.
    No CAPTCHA bypass, login bypass, or anti-bot circumvention.
    """
    required = [
        "event", "home", "away",
        "home_odds", "draw_odds", "away_odds",
    ]

    if not PLAYWRIGHT_AVAILABLE:
        return [], "UNAVAILABLE: Playwright is not installed"

    if any(not config.get(key) for key in required):
        return [], "UNCONFIGURED: verified CSS selectors are required"

    fixtures = []

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(
                user_agent=HEADERS["User-Agent"]
            )
            page.set_default_timeout(12000)

            response = await page.goto(
                config["url"],
                wait_until="domcontentloaded",
                timeout=20000,
            )

            if response is None:
                await browser.close()
                return [], "UNAVAILABLE: no page response"

            if response.status >= 400:
                status = f"HTTP {response.status}"
                await browser.close()
                return [], status

            await page.wait_for_timeout(1500)

            cards = page.locator(config["event"])
            count = min(await cards.count(), 100)

            for i in range(count):
                card = cards.nth(i)

                async def read_text(selector):
                    locator = card.locator(selector)
                    if await locator.count() == 0:
                        return None
                    return (await locator.first.inner_text()).strip()

                home = await read_text(config["home"])
                away = await read_text(config["away"])
                home_odds = await read_text(config["home_odds"])
                draw_odds = await read_text(config["draw_odds"])
                away_odds = await read_text(config["away_odds"])

                league = None
                if config.get("league"):
                    league = await read_text(config["league"])

                fixture = make_fixture(
                    name,
                    home,
                    away,
                    {
                        "home": home_odds,
                        "draw": draw_odds,
                        "away": away_odds,
                    },
                    league=league or "",
                )
                if fixture:
                    fixture["price_updated_at"] = None
                    fixture["source"] = "browser"
                    fixtures.append(fixture)

            await browser.close()

        if not fixtures:
            return [], (
                "PARSER_FAILED: no valid events matched configured selectors"
            )

        return fixtures, (
            "CAPTURED: visible odds found; bookmaker price timestamp "
            "not verified"
        )

    except Exception as exc:
        return [], f"BROWSER_ERROR: {type(exc).__name__}"


# -------------------- Feed orchestration --------------------

async def fetch_all_feeds(client):
    fixtures = []
    statuses = {}

    # SportyBet API Adapter
    sporty, status = await fetch_sportybet(client)
    fixtures.extend(sporty)
    statuses["SportyBet"] = {
        "status": status,
        "fixtures": len(sporty),
    }

    # Browser-rendered pages for 1xBet, Bet9ja, and Betway
    for name, config in BROWSER_SOURCES.items():
        found, status = await fetch_browser_bookmaker(name, config)
        fixtures.extend(found)

        statuses[name] = {
            "status": status,
            "fixtures": len(found),
        }

        await asyncio.sleep(0.5)

    return fixtures, statuses


# -------------------- Fixture matching and calculations --------------------

def same_fixture(a, b):
    same_teams = (
        names_match(a["home"], b["home"])
        and names_match(a["away"], b["away"])
    )
    if not same_teams:
        return False

    ka, kb = a.get("kickoff"), b.get("kickoff")
    if ka is not None and kb is not None and abs(ka - kb) > 3 * 3600:
        return False

    return True


def price_is_fresh(event):
    updated = event.get("price_updated_at")
    if updated is None:
        return event.get("source") != "browser"

    age = time.time() - updated
    return 0 <= age <= MAX_PRICE_AGE


def calculate_stakes(odds):
    inverse = {k: 1 / odds[k] for k in ("home", "draw", "away")}
    total_inverse = sum(inverse.values())

    if total_inverse >= 1:
        return None

    stakes = {
        k: int((BANKROLL * inverse[k] / total_inverse) / STAKE_STEP)
        * STAKE_STEP
        for k in inverse
    }

    if sum(stakes.values()) <= 0 or sum(stakes.values()) > BANKROLL:
        return None

    return stakes


def evaluate_group(group):
    if len({x["bookmaker"] for x in group}) < 2:
        return None

    usable = [x for x in group if price_is_fresh(x)]
    if len({x["bookmaker"] for x in usable}) < 2:
        return None

    best = {}
    for outcome in ("home", "draw", "away"):
        choices = [
            x for x in usable
            if x["odds"].get(outcome) is not None
        ]
        if not choices:
            return None
        best[outcome] = max(choices, key=lambda x: x["odds"][outcome])

    reference = best["home"]
    if not all(same_fixture(reference, best[k]) for k in ("draw", "away")):
        return None

    if len({best[k]["bookmaker"] for k in best}) < 2:
        return None

    odds = {k: best[k]["odds"][k] for k in best}
    implied_sum = sum(1 / odds[k] for k in odds)
    if implied_sum >= 1:
        return None

    margin = ((1 / implied_sum) - 1) * 100
    stakes = calculate_stakes(odds)
    if not stakes:
        return None

    total_stake = sum(stakes.values())
    returns = {k: stakes[k] * odds[k] for k in stakes}
    worst_return = min(returns.values())
    profit = worst_return - total_stake

    if margin < MIN_MARGIN or profit < MIN_PROFIT:
        return None

    return {
        "home": reference["home"],
        "away": reference["away"],
        "league": reference.get("league", ""),
        "odds": odds,
        "books": {k: best[k]["bookmaker"] for k in best},
        "stakes": stakes,
        "total_stake": total_stake,
        "worst_return": worst_return,
        "worst_profit": profit,
        "margin_percent": margin,
    }


def find_opportunities(fixtures):
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

    results = []
    for group in groups:
        result = evaluate_group(group)
        if result:
            results.append(result)

    return sorted(results, key=lambda x: x["worst_profit"], reverse=True)


# -------------------- Recommendations --------------------

def build_recommendations():
    feeds = last_scan.get("feeds", {})
    recommendations = []

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        recommendations.append(
            "Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Render."
        )

    if not feeds:
        recommendations.append(
            "Run a scan and inspect /feed-status for source diagnostics."
        )

    for name, data in feeds.items():
        status = data.get("status", "")
        count = data.get("fixtures", 0)

        if count:
            recommendations.append(
                f"{name}: collected {count} fixture records; "
                "verify market and odds freshness before betting."
            )
        elif "UNCONFIGURED" in status:
            recommendations.append(
                f"{name}: configure and test its public-page CSS selectors "
                "before enabling browser collection."
            )
        elif "UNAVAILABLE" in status or "HTTP " in status:
            recommendations.append(
                f"{name}: source is unavailable or returned an HTTP error; "
                "do not treat it as a working odds feed."
            )
        elif "PARSER_FAILED" in status:
            recommendations.append(
                f"{name}: inspect the response schema or page selectors; "
                "do not guess odds fields."
            )
        elif status:
            recommendations.append(f"{name}: {status}.")

    if sum(x.get("fixtures", 0) for x in feeds.values()) == 0:
        recommendations.append(
            "No usable fixtures were collected. No arbitrage alert is justified."
        )

    if not PLAYWRIGHT_AVAILABLE:
        recommendations.append(
            "Browser mode is disabled. Install Playwright and Chromium "
            "before configuring browser source page selectors."
        )

    if not recommendations:
        recommendations.append(
            "Feeds returned fixture records. Continue checking actual prices, "
            "settlement rules, betting limits, and rounded worst-case profit."
        )

    return recommendations


# -------------------- Telegram alerts --------------------

async def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials are not configured.")
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": message,
                    "disable_web_page_preview": True,
                },
            )

        if response.status_code != 200:
            log.warning("Telegram returned HTTP %s.", response.status_code)
            return False

        if not response.json().get("ok"):
            log.warning("Telegram did not confirm message delivery.")
            return False

        log.info("Telegram message delivered.")
        return True

    except Exception as exc:
        log.warning("Telegram send failed: %s", type(exc).__name__)
        return False


def opportunity_message(item):
    return (
        "⚽ SolveEase Arbitrage Candidate\n\n"
        f"{item['home']} vs {item['away']}\n"
        f"League: {item.get('league') or 'Unknown'}\n\n"
        f"HOME: {item['odds']['home']:.2f} — {item['books']['home']}\n"
        f"DRAW: {item['odds']['draw']:.2f} — {item['books']['draw']}\n"
        f"AWAY: {item['odds']['away']:.2f} — {item['books']['away']}\n\n"
        f"Stake HOME: ₦{item['stakes']['home']:,.0f}\n"
        f"Stake DRAW: ₦{item['stakes']['draw']:,.0f}\n"
        f"Stake AWAY: ₦{item['stakes']['away']:,.0f}\n"
        f"Total stake: ₦{item['total_stake']:,.0f}\n"
        f"Lowest calculated return: ₦{item['worst_return']:,.0f}\n"
        f"Calculated worst-case profit: ₦{item['worst_profit']:,.0f}\n"
        f"Theoretical margin: {item['margin_percent']:.2f}%\n\n"
        "Candidate only. Recheck prices and rules directly with each "
        "bookmaker. Odds may change and bets may be rejected."
    )


def opportunity_key(item):
    return (
        clean_name(item["home"]),
        clean_name(item["away"]),
        tuple(round(item["odds"][k], 3) for k in ("home", "draw", "away")),
    )


# -------------------- Scanner --------------------

async def perform_scan():
    async with scan_lock:
        last_scan["started_at"] = datetime.now(timezone.utc).isoformat()
        last_scan["error"] = None
        log.info("Starting bookmaker scan.")

        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT,
                headers=HEADERS,
            ) as client:
                fixtures, statuses = await fetch_all_feeds(client)

            opportunities = find_opportunities(fixtures)

            last_scan.update({
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "fixtures": len(fixtures),
                "opportunities": len(opportunities),
                "feeds": statuses,
            })

            for name, result in statuses.items():
                log.info(
                    "Feed %s: fixtures=%s status=%s",
                    name, result["fixtures"], result["status"],
                )

            log.info("Collected %s usable fixture records.", len(fixtures))

            for item in opportunities:
                key = opportunity_key(item)
                if key in sent_alert_keys:
                    continue
                if await send_telegram(opportunity_message(item)):
                    sent_alert_keys.add(key)

            if len(sent_alert_keys) > 1000:
                sent_alert_keys.clear()

            if not opportunities:
                log.info("No qualifying arbitrage opportunities found.")

            return last_scan

        except Exception as exc:
            last_scan["error"] = type(exc).__name__
            last_scan["completed_at"] = datetime.now(timezone.utc).isoformat()
            log.exception("Scan failed.")
            return last_scan


async def scan_loop():
    while True:
        try:
            await perform_scan()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Unexpected scanner loop error.")

        await asyncio.sleep(SCAN_INTERVAL)


@asynccontextmanager
async def lifespan(app):
    global background_task

    log.info("SolveEase Arbitrage Engine starting.")

    await send_telegram(
        "✅ SolveEase Arbitrage Engine started.\n"
        f"Bankroll setting: ₦{BANKROLL:,.0f}\n"
        f"Scan interval: {SCAN_INTERVAL} seconds.\n"
        "Startup confirms service initialization only, not odds availability."
    )

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
    version="3.1.0",
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
        "playwright_available": PLAYWRIGHT_AVAILABLE,
        "note": "Online status does not prove odds feed availability.",
    }


@app.get("/feed-status")
async def feed_status():
    return last_scan


@app.get("/recommendations")
async def recommendations():
    return {
        "recommendations": build_recommendations(),
        "next_action": (
            "Review feed status, configure only verified selectors, "
            "and confirm usable odds before relying on alerts."
        ),
    }


@app.post("/scan-now")
async def scan_now():
    if scan_lock.locked():
        raise HTTPException(
            status_code=409,
            detail="A scan is already running. Check /feed-status.",
        )
    return await perform_scan()
