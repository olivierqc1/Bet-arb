"""
╔══════════════════════════════════════════════════════╗
║   ARB SCANNER v9 — Mix de ligues illiquides         ║
║   Seuil: 0.5% | 4 bookmakers | 20 min              ║
║   Commandes: /pause /resume /stats /help             ║
╚══════════════════════════════════════════════════════╝

CHANGEMENTS v9:
  - Seuil baissé à 0.5% (au lieu de 1%)
  - Ligues moins liquides = plus de divergences de cotes
  - Ajout de Pinnacle (référence mondiale pour l'arb)
  - Mix ligues majeures + ligues sous le radar

BOOKMAKERS:
  - Betfair Exchange  → jamais flaggé
  - William Hill      → licence DGOJ Espagne
  - Bwin              → licence DGOJ Espagne
  - Pinnacle          → meilleures cotes du marché

QUOTA:
  9 sports × 3/heure × 12h × 30j = 9 720 req ✅
"""

import os
import sys
import requests
import time
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────────────
# ⚙️  CONFIG
# ─────────────────────────────────────────────────────

ODDS_API_KEY        = os.environ.get("ODDS_API_KEY", "YOUR_ODDS_API_KEY")
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")

PAPER_TRADING       = True
MIN_PROFIT_PCT      = 1.0   # Baissé de 1.0% à 0.5%
BANKROLL            = 100
POLL_INTERVAL       = 1200  # 20 minutes
LOG_FILE            = "arb_opportunities.json"

# ─────────────────────────────────────────────────────
# 🏟️  BOOKMAKERS
# ─────────────────────────────────────────────────────

BOOKS = ["betfair_ex_eu", "william_hill", "bwin", "pinnacle"]

BOOK_LABELS = {
    "betfair_ex_eu": "📗 BETFAIR ⭐",
    "william_hill":  "📘 WILLIAM HILL",
    "bwin":          "📙 BWIN",
    "pinnacle":      "📕 PINNACLE",
}

SAFE_BOOKS  = ["betfair_ex_eu", "pinnacle"]  # Pinnacle ne flag pas non plus
RISKY_BOOKS = ["william_hill", "bwin"]

# ─────────────────────────────────────────────────────
# 🏟️  SPORTS — Mix ligues majeures + sous le radar
# ─────────────────────────────────────────────────────

SPORTS = {
    # Ligues majeures (volume de matchs)
    "soccer_spain_la_liga":                 "⚽ La Liga",
    "basketball_nba":                       "🏀 NBA",

    # Ligues sous le radar (plus de divergences)
    "soccer_portugal_primeira_liga":        "⚽ Liga Portugal",
    "soccer_netherlands_eredivisie":        "⚽ Eredivisie",
    "soccer_turkey_super_league":           "⚽ Super Lig Turquie",
    "soccer_argentina_primera_division":    "⚽ Liga Argentina",
    "soccer_brazil_campeonato":             "⚽ Brasileirao",
    "soccer_usa_mls":                       "⚽ MLS",
    "soccer_scotland_premiership":          "⚽ Scottish Premier",
}

# ─────────────────────────────────────────────────────
# 🎮  ÉTAT GLOBAL
# ─────────────────────────────────────────────────────

state = {
    "paused": False,
    "last_update_id": 0,
}

session_stats = {
    "scans": 0,
    "api_calls": 0,
    "opps_found": 0,
    "best_profit_pct": 0.0,
    "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
}

# ─────────────────────────────────────────────────────
# 📝  LOGGING
# ─────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("arb_scanner.log"),
    ],
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────
# 📱  TELEGRAM — ENVOI
# ─────────────────────────────────────────────────────

def send_telegram(message: str, silent: bool = False):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_notification": silent,
    }
    try:
        r = requests.post(url, json=payload, timeout=5)
        r.raise_for_status()
    except Exception as e:
        log.error(f"Telegram send error: {e}")


# ─────────────────────────────────────────────────────
# 📱  TELEGRAM — COMMANDES
# ─────────────────────────────────────────────────────

def check_telegram_commands():
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {
        "offset": state["last_update_id"] + 1,
        "timeout": 1,
        "allowed_updates": ["message"],
    }
    try:
        r = requests.get(url, params=params, timeout=5)
        r.raise_for_status()
        updates = r.json().get("result", [])

        for update in updates:
            state["last_update_id"] = update["update_id"]
            msg = update.get("message", {})
            text = msg.get("text", "").strip().lower()
            chat_id = str(msg.get("chat", {}).get("id", ""))

            if chat_id != str(TELEGRAM_CHAT_ID):
                continue

            if text == "/pause":
                if state["paused"]:
                    send_telegram("⏸ Scanner déjà en pause.")
                else:
                    state["paused"] = True
                    send_telegram(
                        "⏸ <b>Scanner mis en pause.</b>\n"
                        "Aucune requête API consommée.\n"
                        "Envoie /resume pour reprendre."
                    )
            elif text == "/resume":
                if not state["paused"]:
                    send_telegram("▶️ Scanner déjà actif.")
                else:
                    state["paused"] = False
                    send_telegram(
                        f"▶️ <b>Scanner repris!</b>\n"
                        f"Prochain scan dans ~{POLL_INTERVAL // 60} min."
                    )
            elif text == "/stats":
                send_stats_update()
            elif text == "/help":
                send_telegram(
                    "🤖 <b>Commandes disponibles:</b>\n\n"
                    "⏸ /pause — Met le scanner en pause\n"
                    "▶️ /resume — Reprend le scanner\n"
                    "📊 /stats — Rapport de session\n"
                    "❓ /help — Affiche ce message\n\n"
                    "💡 <b>Tips anti-flag:</b>\n"
                    "• Varie tes mises de ±1-2€\n"
                    "• Mise quelques heures avant le match\n"
                    "• /pause la nuit pour économiser l'API"
                )
    except Exception as e:
        log.error(f"Telegram getUpdates error: {e}")


def send_startup_message():
    mode = "📄 PAPER TRADING" if PAPER_TRADING else "💰 LIVE BETTING"
    sports_list = "\n".join(f"   {v}" for v in SPORTS.values())
    send_telegram(
        f"🚀 <b>Arb Scanner v9 démarré</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Mode: <b>{mode}</b>\n"
        f"Bookmakers:\n"
        f"   📗 Betfair (safe)\n"
        f"   📘 William Hill\n"
        f"   📙 Bwin\n"
        f"   📕 Pinnacle (safe)\n"
        f"Min profit: <b>{MIN_PROFIT_PCT}%</b>\n"
        f"Bankroll: <b>${BANKROLL}</b>\n"
        f"Interval: <b>{POLL_INTERVAL // 60} min</b>\n"
        f"Sports:\n{sports_list}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 /pause /resume /stats /help\n"
        f"💡 <i>Pense à /pause la nuit!</i>"
    )


def send_stats_update():
    elapsed = datetime.now() - datetime.strptime(session_stats["start_time"], "%Y-%m-%d %H:%M:%S")
    hours = elapsed.seconds // 3600
    minutes = (elapsed.seconds % 3600) // 60
    status = "⏸ EN PAUSE" if state["paused"] else "▶️ ACTIF"
    send_telegram(
        f"📊 <b>Rapport session</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Statut: <b>{status}</b>\n"
        f"⏱ Durée: {hours}h {minutes}m\n"
        f"🔍 Scans: {session_stats['scans']}\n"
        f"📡 Appels API: {session_stats['api_calls']}\n"
        f"🎯 Opps trouvées: {session_stats['opps_found']}\n"
        f"🏆 Meilleur profit: <b>{session_stats['best_profit_pct']}%</b>",
        silent=True,
    )


# ─────────────────────────────────────────────────────
# 🌐  ODDS API
# ─────────────────────────────────────────────────────

def fetch_odds(sport_key: str) -> list:
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "eu",
        "markets": "h2h",
        "oddsFormat": "decimal",
        "bookmakers": ",".join(BOOKS),
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        remaining = r.headers.get("x-requests-remaining", "?")
        used = r.headers.get("x-requests-used", "?")
        log.info(f"[{sport_key}] ✓ used: {used} | remaining: {remaining}")
        session_stats["api_calls"] += 1

        if remaining != "?" and int(remaining) < 500:
            send_telegram(
                f"⚠️ <b>Quota API bas!</b>\n"
                f"Seulement <b>{remaining}</b> requêtes restantes.\n"
                f"Envoie /pause pour économiser."
            )

        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Odds API error [{sport_key}]: {e}")
        return []


# ─────────────────────────────────────────────────────
# 🔍  DÉTECTION D'ARB
# ─────────────────────────────────────────────────────

def find_arb_opportunities(game: dict, sport_label: str) -> list:
    home = game.get("home_team", "Home")
    away = game.get("away_team", "Away")
    commence_raw = game.get("commence_time", "")

    # Filtre pré-match
    try:
        commence_dt = datetime.fromisoformat(commence_raw.replace("Z", "+00:00"))
        now_utc = datetime.now(timezone.utc)
        if commence_dt <= now_utc:
            return []
        commence_str = commence_dt.strftime("%d/%m %H:%M UTC")
        delta = commence_dt - now_utc
        hours_left = int(delta.total_seconds() // 3600)
        mins_left = int((delta.total_seconds() % 3600) // 60)
        time_left = f"{hours_left}h {mins_left}m"
    except Exception:
        commence_str = commence_raw
        time_left = "?"

    # Collecter cotes par bookie
    bookie_odds = {}
    for bookmaker in game.get("bookmakers", []):
        bookie_key = bookmaker["key"]
        if bookie_key not in BOOKS:
            continue
        for market in bookmaker.get("markets", []):
            if market["key"] != "h2h":
                continue
            odds_map = {o["name"]: o["price"] for o in market.get("outcomes", [])}
            if odds_map:
                bookie_odds[bookie_key] = odds_map

    if len(bookie_odds) < 2:
        return []

    all_outcomes = set()
    for odds in bookie_odds.values():
        all_outcomes.update(odds.keys())

    if len(all_outcomes) < 2:
        return []

    # Pour chaque outcome → meilleure cote et son bookie
    best = {}
    all_odds_by_outcome = {}

    for outcome in all_outcomes:
        all_odds_by_outcome[outcome] = {}
        best_price = 0
        best_bookie = None

        for bookie, odds in bookie_odds.items():
            price = odds.get(outcome)
            if price:
                all_odds_by_outcome[outcome][bookie] = price
                if price > best_price:
                    best_price = price
                    best_bookie = bookie

        if best_bookie:
            best[outcome] = {"odd": best_price, "bookie": best_bookie}

    if len(best) < 2:
        return []

    total_prob = sum(1 / v["odd"] for v in best.values())
    if total_prob >= 1.0:
        return []

    profit_pct = (1 / total_prob - 1) * 100
    if profit_pct < MIN_PROFIT_PCT:
        return []

    sides = []
    risky_involved = []

    for team_name, info in best.items():
        prob = 1 / info["odd"]
        stake = round((BANKROLL * prob) / total_prob, 2)
        if info["bookie"] in RISKY_BOOKS:
            risky_involved.append(BOOK_LABELS.get(info["bookie"], info["bookie"]))
        sides.append({
            "team": team_name,
            "odd": info["odd"],
            "bookie": info["bookie"],
            "stake": stake,
            "all_odds": all_odds_by_outcome.get(team_name, {}),
            "is_safe": info["bookie"] in SAFE_BOOKS,
        })

    return [{
        "sport": sport_label,
        "home": home,
        "away": away,
        "commence": commence_str,
        "time_left": time_left,
        "sides": sides,
        "profit_pct": round(profit_pct, 2),
        "profit": round(BANKROLL * (1 / total_prob - 1), 2),
        "risky_involved": risky_involved,
        "detected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }]


# ─────────────────────────────────────────────────────
# 💬  FORMAT ALERTE
# ─────────────────────────────────────────────────────

def format_alert(opp: dict) -> str:
    mode_tag = "📄 PAPER" if PAPER_TRADING else "💰 LIVE"
    p = opp["profit_pct"]
    profit_emoji = "🤑" if p >= 5 else "💰" if p >= 3 else "✅" if p >= 2 else "⚡" if p >= 1 else "🔹"

    msg = (
        f"{profit_emoji} <b>ARB DETECTED [{mode_tag}] — {opp['sport']}</b>\n"
        f"<b>{opp['away']} @ {opp['home']}</b>\n"
        f"🕐 {opp['commence']} (<b>{opp['time_left']} restant</b>)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )

    for side in opp["sides"]:
        label = BOOK_LABELS.get(side["bookie"], side["bookie"].upper())
        others = ", ".join(
            f"{BOOK_LABELS.get(bk, bk).split()[-1]}: {odd}"
            for bk, odd in side["all_odds"].items()
            if bk != side["bookie"]
        )
        msg += f"{label}\n"
        msg += f"   {side['team']} @ <b>{side['odd']}</b> ← meilleure\n"
        if others:
            msg += f"   (autres: {others})\n"
        msg += f"   Mise: <b>${side['stake']}</b>\n\n"

    msg += (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{profit_emoji} Profit garanti: <b>${opp['profit']}</b> (<b>{opp['profit_pct']}%</b>)\n"
        f"   Sur bankroll de ${BANKROLL}\n"
        f"⏱ Détecté: {opp['detected_at']}\n"
    )

    if opp["risky_involved"] and not PAPER_TRADING:
        risky_str = ", ".join(opp["risky_involved"])
        msg += f"\n⚠️ <b>Anti-flag:</b> varie ta mise ±1-2€ sur {risky_str}"
    elif PAPER_TRADING:
        msg += "\n📄 <i>Paper trade — aucun vrai pari placé</i>"

    return msg


# ─────────────────────────────────────────────────────
# 💾  LOG
# ─────────────────────────────────────────────────────

def log_opportunity(opp: dict):
    log_path = Path(LOG_FILE)
    existing = []
    if log_path.exists():
        try:
            with open(log_path, "r") as f:
                existing = json.load(f)
        except Exception:
            existing = []
    existing.append(opp)
    with open(log_path, "w") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)


# ─────────────────────────────────────────────────────
# 🔄  BOUCLE PRINCIPALE
# ─────────────────────────────────────────────────────

def run_scanner():
    log.info("🚀 ARB SCANNER v9 STARTED")
    send_startup_message()

    seen_opps = {}
    last_report = time.time()
    REPORT_INTERVAL = 3600

    while True:
        try:
            check_telegram_commands()

            if state["paused"]:
                log.info("⏸ En pause...")
                time.sleep(15)
                continue

            session_stats["scans"] += 1
            log.info(f"─── Scan #{session_stats['scans']} ───")

            all_opps = []
            for sport_key, sport_label in SPORTS.items():
                games = fetch_odds(sport_key)
                for game in games:
                    all_opps.extend(find_arb_opportunities(game, sport_label))

            all_opps.sort(key=lambda x: x["profit_pct"], reverse=True)

            if all_opps:
                log.info(f"🎯 {len(all_opps)} opportunité(s)")
                for opp in all_opps:
                    key = f"{opp['home']}-{opp['away']}-{opp['profit_pct']}"
                    now = time.time()
                    if key in seen_opps and (now - seen_opps[key]) < POLL_INTERVAL:
                        continue
                    seen_opps[key] = now
                    session_stats["opps_found"] += 1
                    if opp["profit_pct"] > session_stats["best_profit_pct"]:
                        session_stats["best_profit_pct"] = opp["profit_pct"]
                    send_telegram(format_alert(opp))
                    log_opportunity(opp)
                    log.info(f"✅ {opp['profit_pct']}% | {opp['away']} @ {opp['home']} | {opp['time_left']}")
                    time.sleep(1)
            else:
                log.info("❌ Aucune opportunité.")

            if time.time() - last_report > REPORT_INTERVAL:
                send_stats_update()
                last_report = time.time()

            seen_opps = {k: v for k, v in seen_opps.items() if time.time() - v < POLL_INTERVAL}

            elapsed = 0
            while elapsed < POLL_INTERVAL:
                time.sleep(15)
                elapsed += 15
                check_telegram_commands()
                if state["paused"]:
                    break

        except KeyboardInterrupt:
            log.info("Arrêt manuel.")
            send_stats_update()
            send_telegram("⛔ <b>Scanner arrêté.</b>")
            break
        except Exception as e:
            log.error(f"Erreur: {e}")
            time.sleep(30)


# ─────────────────────────────────────────────────────
# 📈  ANALYSE
# ─────────────────────────────────────────────────────

def analyze_results():
    log_path = Path(LOG_FILE)
    if not log_path.exists():
        print("Aucun fichier de log trouvé.")
        return
    with open(log_path, "r") as f:
        opps = json.load(f)
    if not opps:
        print("Aucune opportunité loggée.")
        return

    profits = [o["profit_pct"] for o in opps]
    print(f"\n{'═'*50}")
    print(f"  ANALYSE ARB — {len(opps)} opportunités")
    print(f"{'═'*50}")
    print(f"\n📊 Profit moyen:    {sum(profits)/len(profits):.2f}%")
    print(f"🏆 Meilleur profit: {max(profits):.2f}%")
    print(f"📉 Plus faible:     {min(profits):.2f}%")

    sports = {}
    for o in opps:
        sports[o["sport"]] = sports.get(o["sport"], 0) + 1
    print(f"\n📋 Par sport:")
    for sport, count in sorted(sports.items(), key=lambda x: -x[1]):
        print(f"   {sport}: {count} opps")

    total_profit = sum(o["profit"] for o in opps)
    print(f"\n💰 Profit total simulé (${BANKROLL}/opp): ${total_profit:.2f}")
    print(f"{'═'*50}\n")


# ─────────────────────────────────────────────────────
# 🚀  ENTRY POINT
# ─────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "analyze":
        analyze_results()
    else:
        run_scanner()
