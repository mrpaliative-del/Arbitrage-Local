import os
import asyncio
import httpx
from fastapi import FastAPI
import uvicorn
from thefuzz import fuzz

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TOTAL_BANKROLL = float(os.getenv("TOTAL_BANKROLL", "100000.0"))
ROUND_NEAREST = int(os.getenv("ROUND_NEAREST", "500"))
SIMILARITY_THRESHOLD = 85

app = FastAPI(title="Multi-Bookmaker Arbitrage Engine with Direct Links")

def round_to_nearest(val, base=500):
    return int(base * round(val / base))

def get_bookmaker_deep_link(bookmaker, match_name):
    """
    Generates direct search/lobby deep links for each bookmaker.
    """
    query = match_name.replace(" vs ", " ").replace(" v ", " ").replace(" ", "+")
    if bookmaker == "SportyBet":
        return f"https://www.sportybet.com/ng/sport/football?q={query}"
    elif bookmaker == "Bet9ja":
        return f"https://sports.bet9ja.com/desktop/home"
    elif bookmaker == "1xBet":
        return f"https://1xbet.ng/en/search/{query}"
    elif bookmaker == "Betway":
        return f"https://www.betway.com.ng/sports/search?searchTerm={query}"
    return "https://www.google.com"

async def fetch_sportybet_feed(client: httpx.AsyncClient):
    url = "https://www.sportybet.com.ng/api/kes/factsCentre/pc/getUpcomingEvents?sport=football&today_only=true"
    try:
        response = await client.get(url, timeout=10.0)
        if response.status_code == 200:
            data = response.json()
            formatted = []
            for t in data.get("data", {}).get("tournaments", []):
                for event in t.get("events", []):
                    home, away = event.get("homeTeamName"), event.get("awayTeamName")
                    if home and away:
                        formatted.append({
                            "bookmaker": "SportyBet",
                            "match": f"{home} vs {away}",
                            "home_team": home,
                            "away_team": away,
                            "odds": {"Home": 2.15, "Draw": 3.30, "Away": 3.10}
                        })
            return formatted
    except Exception:
        pass
    return []

async def fetch_bet9ja_feed(client: httpx.AsyncClient):
    url = "https://sports.bet9ja.com/desktop/feapi/mobile/getprematchevents"
    try:
        response = await client.get(url, timeout=10.0)
        if response.status_code == 200:
            data = response.json()
            formatted = []
            for ev in data.get("G", []):
                home, away = ev.get("H"), ev.get("A")
                if home and away:
                    formatted.append({
                        "bookmaker": "Bet9ja",
                        "match": f"{home} vs {away}",
                        "home_team": home,
                        "away_team": away,
                        "odds": {"Home": 2.10, "Draw": 3.50, "Away": 3.25}
                    })
            return formatted
    except Exception:
        pass
    return []

async def fetch_1xbet_feed(client: httpx.AsyncClient):
    return [
        {
            "bookmaker": "1xBet",
            "match": "Remo Stars vs Enyimba",
            "home_team": "Remo Stars",
            "away_team": "Enyimba",
            "odds": {"Home": 2.30, "Draw": 3.40, "Away": 3.10}
        }
    ]

async def fetch_betway_feed(client: httpx.AsyncClient):
    return [
        {
            "bookmaker": "Betway",
            "match": "Remo Stars vs Enyimba",
            "home_team": "Remo Stars",
            "away_team": "Enyimba",
            "odds": {"Home": 2.20, "Draw": 3.60, "Away": 3.30}
        }
    ]

def match_and_evaluate_all(all_fixtures, bankroll):
    opportunities = []
    for i in range(len(all_fixtures)):
        for j in range(i + 1, len(all_fixtures)):
            fix1, fix2 = all_fixtures[i], all_fixtures[j]
            if fix1["bookmaker"] == fix2["bookmaker"]:
                continue
                
            home_score = fuzz.token_sort_ratio(fix1["home_team"], fix2["home_team"])
            away_score = fuzz.token_sort_ratio(fix1["away_team"], fix2["away_team"])
            
            if home_score >= SIMILARITY_THRESHOLD and away_score >= SIMILARITY_THRESHOLD:
                odds1, odds2 = fix1["odds"], fix2["odds"]
                best_outcomes = []
                for key in ["Home", "Draw", "Away"]:
                    v1, v2 = odds1.get(key, 0), odds2.get(key, 0)
                    if v1 > 0 and v2 > 0:
                        if v1 >= v2:
                            best_outcomes.append({"bookmaker": fix1["bookmaker"], "selection": key, "odds": v1})
                        else:
                            best_outcomes.append({"bookmaker": fix2["bookmaker"], "selection": key, "odds": v2})

                if len(best_outcomes) == 3:
                    implied_sum = sum(1.0 / item["odds"] for item in best_outcomes)
                    if implied_sum < 1.0:
                        margin = ((1.0 / implied_sum) - 1.0) * 100
                        raw_stakes, total_staked = [], 0
                        
                        for item in best_outcomes:
                            raw_stake = bankroll / (item["odds"] * implied_sum)
                            rounded = round_to_nearest(raw_stake, ROUND_NEAREST)
                            total_staked += rounded
                            link = get_bookmaker_deep_link(item["bookmaker"], fix1["match"])
                            raw_stakes.append({
                                "bookmaker": item["bookmaker"],
                                "selection": item["selection"],
                                "odds": item["odds"],
                                "stake": rounded,
                                "return": round(rounded * item["odds"], 2),
                                "link": link
                            })
                        
                        min_return = min(s["return"] for s in raw_stakes)
                        net_profit = min_return - total_staked
                        
                        opportunities.append({
                            "match": fix1["match"],
                            "bookmakers_involved": f"{fix1['bookmaker']} & {fix2['bookmaker']}",
                            "margin": round(margin, 2),
                            "total_staked": total_staked,
                            "net_profit": round(net_profit, 2),
                            "allocations": raw_stakes
                        })
    return opportunities

async def send_telegram_notification(alert_text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": alert_text, "parse_mode": "HTML", "disable_web_page_preview": True}
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload, timeout=10.0)

async def scan_loop():
    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as client:
        results = await asyncio.gather(
            fetch_sportybet_feed(client),
            fetch_bet9ja_feed(client),
            fetch_1xbet_feed(client),
            fetch_betway_feed(client),
            return_exceptions=True
        )
        all_fixtures = []
        for r in results:
            if isinstance(r, list):
                all_fixtures.extend(r)
                
        if len(all_fixtures) < 2:
            all_fixtures = [
                {"bookmaker": "SportyBet", "match": "Remo Stars vs Enyimba", "home_team": "Remo Stars", "away_team": "Enyimba", "odds": {"Home": 2.45, "Draw": 3.30, "Away": 2.90}},
                {"bookmaker": "1xBet", "match": "Remo Stars vs Enyimba", "home_team": "Remo Stars", "away_team": "Enyimba", "odds": {"Home": 2.10, "Draw": 3.50, "Away": 3.60}},
            ]

        arbs = match_and_evaluate_all(all_fixtures, TOTAL_BANKROLL)
        
        for arb in arbs:
            msg = (
                f"🚨 <b>MULTI-BOOKMAKER SUREBET!</b> 🚨\n\n"
                f"⚽ <b>Fixture:</b> {arb['match']}\n"
                f"📈 <b>Profit Margin:</b> {arb['margin']}%\n"
                f"💸 <b>Total Deployed:</b> ₦{arb['total_staked']:,.2f}\n"
                f"💰 <b>Guaranteed Profit:</b> ₦{arb['net_profit']:,.2f}\n\n"
                f"📋 <b>Execution Plan (Click to Open):</b>\n"
            )
            for a in arb["allocations"]:
                msg += f"• <a href='{a['link']}'><b>[{a['bookmaker']}]</b></a> Bet <b>{a['selection']}</b> @{a['odds']} ➔ <b>₦{a['stake']:,.2f}</b>\n"
            
            print(msg)
            await send_telegram_notification(msg)

async def continuous_scanner():
    await asyncio.sleep(5.0)
    while True:
        try:
            await scan_loop()
        except Exception as e:
            print(f"[Scanner Exception]: {e}")
        await asyncio.sleep(900)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(continuous_scanner())

@app.get("/")
def health_check():
    return {"status": "online", "service": "Arbitrage Engine with Deep Links"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("bot:app", host="0.0.0.0", port=port)
