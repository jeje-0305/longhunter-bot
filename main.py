import asyncio
import aiohttp
import os
from datetime import datetime

# ── 설정 ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")
REFRESH_MIN    = int(os.environ.get("REFRESH_MIN", "3"))

LS_LOW         = float(os.environ.get("LS_LOW",  "0.50"))  # 롱 사냥 상한
LS_HIGH        = float(os.environ.get("LS_HIGH", "1.50"))  # 숏 사냥 하한
MIN_CHANGE     = float(os.environ.get("MIN_CHANGE", "1.0"))
TOP_N          = int(os.environ.get("TOP_N", "80"))

# 이전 결과 저장 (추가/삭제 감지용)
prev_long  = set()
prev_short = set()

# ── Telegram 전송 ──────────────────────────────────────────────────────────
async def send_msg(session, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
            await r.json()
    except Exception as e:
        print(f"[텔레그램 오류] {e}")

# ── 명령어 처리 (설정 변경) ────────────────────────────────────────────────
async def handle_commands(session):
    global LS_LOW, LS_HIGH, MIN_CHANGE
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
        
        updates = data.get("result", [])
        if not updates:
            return
        
        last_update_id = updates[-1]["update_id"]
        
        # 읽은 메시지 처리 완료 표시
        ack_url = f"{url}?offset={last_update_id + 1}"
        async with session.get(ack_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            await r.json()
        
        for update in updates:
            msg = update.get("message", {})
            text = msg.get("text", "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))
            
            if chat_id != CHAT_ID:
                continue
            
            if text.startswith("/threshold"):
                parts = text.split()
                if len(parts) == 2:
                    try:
                        LS_LOW = float(parts[1])
                        await send_msg(session, f"✅ 롱 사냥 상한 → <b>{LS_LOW}</b> 로 변경됨")
                    except:
                        await send_msg(session, "❌ 숫자를 입력해주세요. 예: /threshold 0.40")
            
            elif text.startswith("/short"):
                parts = text.split()
                if len(parts) == 2:
                    try:
                        LS_HIGH = float(parts[1])
                        await send_msg(session, f"✅ 숏 사냥 하한 → <b>{LS_HIGH}</b> 로 변경됨")
                    except:
                        await send_msg(session, "❌ 숫자를 입력해주세요. 예: /short 1.50")
            
            elif text.startswith("/minchange"):
                parts = text.split()
                if len(parts) == 2:
                    try:
                        MIN_CHANGE = float(parts[1])
                        await send_msg(session, f"✅ 최소 변동률 → <b>{MIN_CHANGE}%</b> 로 변경됨")
                    except:
                        await send_msg(session, "❌ 숫자를 입력해주세요. 예: /minchange 1.0")
            
            elif text == "/status":
                now = datetime.now().strftime("%H:%M:%S")
                status = (
                    f"📊 <b>현재 설정</b> ({now})\n"
                    f"롱 사냥 상한: <b>{LS_LOW}</b>\n"
                    f"숏 사냥 하한: <b>{LS_HIGH}</b>\n"
                    f"최소 변동률: <b>{MIN_CHANGE}%</b>\n"
                    f"갱신 주기: <b>{REFRESH_MIN}분</b>"
                )
                await send_msg(session, status)
            
            elif text == "/help":
                help_text = (
                    "📋 <b>사용 가능한 명령어</b>\n\n"
                    "/threshold 0.40 — 롱 사냥 L/S 상한 변경\n"
                    "/short 1.50 — 숏 사냥 L/S 하한 변경\n"
                    "/minchange 1.0 — 최소 변동률 변경\n"
                    "/status — 현재 설정 확인\n"
                    "/help — 도움말"
                )
                await send_msg(session, help_text)

    except Exception as e:
        print(f"[명령어 처리 오류] {e}")

# ── Binance API ────────────────────────────────────────────────────────────
async def fetch_active_symbols(session):
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
        data = await r.json()
    return set(
        s["symbol"] for s in data["symbols"]
        if s["status"] == "TRADING" and s["contractType"] == "PERPETUAL"
    )

async def fetch_tickers(session):
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
        return await r.json()

async def fetch_ls(session, symbol):
    try:
        url = f"https://fapi.binance.com/futures/data/topLongShortAccountRatio?symbol={symbol}&period=5m&limit=1"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
        if data and len(data) > 0:
            return float(data[0]["longShortRatio"])
    except:
        pass
    return None

# ── 메인 스캔 ──────────────────────────────────────────────────────────────
async def scan(session):
    global prev_long, prev_short

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 스캔 시작...")

    active_symbols, tickers = await asyncio.gather(
        fetch_active_symbols(session),
        fetch_tickers(session)
    )

    # 우상향 + 거래량 상위 필터
    candidates = []
    for t in tickers:
        if t["symbol"] not in active_symbols:
            continue
        change = float(t["priceChangePercent"])
        volume = float(t["quoteVolume"])
        if change >= MIN_CHANGE:
            candidates.append({
                "symbol": t["symbol"],
                "price":  float(t["lastPrice"]),
                "change": change,
                "volume": volume,
            })

    candidates.sort(key=lambda x: x["volume"], reverse=True)
    candidates = candidates[:TOP_N]

    # L/S 조회 (5개씩 배치)
    enriched = []
    batch = 5
    for i in range(0, len(candidates), batch):
        chunk = candidates[i:i+batch]
        ratios = await asyncio.gather(*[fetch_ls(session, t["symbol"]) for t in chunk])
        for t, ls in zip(chunk, ratios):
            if ls is not None:
                enriched.append({**t, "ls": ls})
        if i + batch < len(candidates):
            await asyncio.sleep(0.2)

    # 조건 분리
    curr_long  = {t["symbol"]: t for t in enriched if t["ls"] <= LS_LOW}
    curr_short = {t["symbol"]: t for t in enriched if t["ls"] >= LS_HIGH}

    curr_long_set  = set(curr_long.keys())
    curr_short_set = set(curr_short.keys())

    now = datetime.now().strftime("%H:%M:%S")

    # ── 롱 사냥 알림 ──────────────────────────────────────────────────────
    added_long   = curr_long_set - prev_long
    removed_long = prev_long - curr_long_set

    for sym in added_long:
        t = curr_long[sym]
        emoji = "🔥" if t["ls"] <= 0.25 else "📈"
        msg = (
            f"{emoji} <b>롱 사냥 진입</b>\n"
            f"심볼: <b>{sym.replace('USDT','')}</b>\n"
            f"L/S 비율: <b>{t['ls']:.3f}</b> (≤ {LS_LOW})\n"
            f"현재가: ${t['price']:,.4f}\n"
            f"24h 변동: +{t['change']:.2f}%\n"
            f"⏰ {now}"
        )
        await send_msg(session, msg)
        print(f"[알림] 롱 진입: {sym} L/S={t['ls']:.3f}")

    for sym in removed_long:
        msg = (
            f"❌ <b>롱 사냥 이탈</b>\n"
            f"심볼: <b>{sym.replace('USDT','')}</b>\n"
            f"L/S 조건 벗어남 (>{LS_LOW})\n"
            f"⏰ {now}"
        )
        await send_msg(session, msg)
        print(f"[알림] 롱 이탈: {sym}")

    # ── 숏 사냥 알림 ──────────────────────────────────────────────────────
    added_short   = curr_short_set - prev_short
    removed_short = prev_short - curr_short_set

    for sym in added_short:
        t = curr_short[sym]
        emoji = "🔥" if t["ls"] >= 2.5 else "📉"
        msg = (
            f"{emoji} <b>숏 사냥 진입</b>\n"
            f"심볼: <b>{sym.replace('USDT','')}</b>\n"
            f"L/S 비율: <b>{t['ls']:.3f}</b> (≥ {LS_HIGH})\n"
            f"현재가: ${t['price']:,.4f}\n"
            f"24h 변동: +{t['change']:.2f}%\n"
            f"⏰ {now}"
        )
        await send_msg(session, msg)
        print(f"[알림] 숏 진입: {sym} L/S={t['ls']:.3f}")

    for sym in removed_short:
        msg = (
            f"❌ <b>숏 사냥 이탈</b>\n"
            f"심볼: <b>{sym.replace('USDT','')}</b>\n"
            f"L/S 조건 벗어남 (<{LS_HIGH})\n"
            f"⏰ {now}"
        )
        await send_msg(session, msg)
        print(f"[알림] 숏 이탈: {sym}")

    prev_long  = curr_long_set
    prev_short = curr_short_set

    print(f"[{now}] 완료 — 롱 {len(curr_long_set)}개 / 숏 {len(curr_short_set)}개")

# ── 루프 ──────────────────────────────────────────────────────────────────
async def main():
    print("🚀 Long Hunter Bot 시작!")
    async with aiohttp.ClientSession() as session:
        # 시작 알림
        await send_msg(session,
            "🚀 <b>Long Hunter Bot 시작!</b>\n"
            f"롱 사냥: L/S ≤ {LS_LOW}\n"
            f"숏 사냥: L/S ≥ {LS_HIGH}\n"
            f"최소 변동률: {MIN_CHANGE}%\n"
            f"갱신: {REFRESH_MIN}분마다\n\n"
            "명령어 보려면 /help"
        )

        while True:
            try:
                await handle_commands(session)
                await scan(session)
            except Exception as e:
                print(f"[오류] {e}")
            await asyncio.sleep(REFRESH_MIN * 60)

if __name__ == "__main__":
    asyncio.run(main())
