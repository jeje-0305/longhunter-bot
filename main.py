import asyncio
import aiohttp
import os
import json
from datetime import datetime, timezone, timedelta

# ── 설정 ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")
REFRESH_MIN    = int(os.environ.get("REFRESH_MIN", "3"))

LS_LOW     = float(os.environ.get("LS_LOW",  "0.50"))
LS_HIGH    = float(os.environ.get("LS_HIGH", "1.50"))
MIN_CHANGE = float(os.environ.get("MIN_CHANGE", "1.0"))
TOP_N      = int(os.environ.get("TOP_N", "80"))

STATE_FILE = "state.json"
KST = timezone(timedelta(hours=9))

def now_kst(fmt="%H:%M:%S"):
    return datetime.now(KST).strftime(fmt)

# ── 상태 영속화 ────────────────────────────────────────────────────────────
def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                return set(data.get("long", [])), set(data.get("short", []))
    except Exception as e:
        print(f"[상태 로드 오류] {e}")
    return set(), set()

def save_state(long_set, short_set):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"long": list(long_set), "short": list(short_set)}, f)
    except Exception as e:
        print(f"[상태 저장 오류] {e}")

prev_long, prev_short = load_state()
next_scan_time  = None
force_scan_flag = False

# ── Telegram 전송 ──────────────────────────────────────────────────────────
async def send_msg(session, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
            await r.json()
    except Exception as e:
        print(f"[텔레그램 오류] {e}")

# ── 요약 메시지 ────────────────────────────────────────────────────────────
def build_summary(curr_long: dict, curr_short: dict) -> str:
    lines = [f"📊 <b>스캔 결과</b> ({now_kst()} KST)\n"]

    if curr_long:
        lines.append(f"📈 <b>롱 사냥 {len(curr_long)}개</b> (L/S ≤ {LS_LOW})")
        for t in sorted(curr_long.values(), key=lambda x: x["ls"]):
            sym = t["symbol"].replace("USDT", "")
            lines.append(f"  • {sym}  L/S <b>{t['ls']:.3f}</b>  {t['change']:+.2f}%")
    else:
        lines.append("📈 <b>롱 사냥 0개</b>")

    lines.append("")

    if curr_short:
        lines.append(f"📉 <b>숏 사냥 {len(curr_short)}개</b> (L/S ≥ {LS_HIGH})")
        for t in sorted(curr_short.values(), key=lambda x: x["ls"], reverse=True):
            sym = t["symbol"].replace("USDT", "")
            lines.append(f"  • {sym}  L/S <b>{t['ls']:.3f}</b>  {t['change']:+.2f}%")
    else:
        lines.append("📉 <b>숏 사냥 0개</b>")

    return "\n".join(lines)

# ── Binance API (재시도 포함) ──────────────────────────────────────────────
async def fetch_with_retry(session, url, retries=3, delay=2.0):
    """GET 요청 — 실패 시 최대 retries번 재시도"""
    for attempt in range(1, retries + 1):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                data = await r.json()
            return data
        except Exception as e:
            print(f"[fetch 오류 {attempt}/{retries}] {url[:60]} → {e}")
            if attempt < retries:
                await asyncio.sleep(delay)
    return None

async def fetch_active_symbols(session):
    """활성 무기한 선물 심볼 목록"""
    data = await fetch_with_retry(session, "https://fapi.binance.com/fapi/v1/exchangeInfo")
    if data is None:
        raise RuntimeError("exchangeInfo 응답 없음 (3회 재시도 실패)")
    if "symbols" not in data:
        # Binance 오류 응답 그대로 출력
        raise RuntimeError(f"exchangeInfo 이상 응답: {str(data)[:200]}")
    return set(
        s["symbol"] for s in data["symbols"]
        if s["status"] == "TRADING" and s["contractType"] == "PERPETUAL"
    )

async def fetch_tickers(session):
    """24h 티커 전체"""
    data = await fetch_with_retry(session, "https://fapi.binance.com/fapi/v1/ticker/24hr")
    if data is None:
        raise RuntimeError("ticker/24hr 응답 없음 (3회 재시도 실패)")
    if not isinstance(data, list):
        raise RuntimeError(f"ticker 이상 응답: {str(data)[:200]}")
    return data

async def fetch_ls(session, symbol):
    """L/S 비율 조회 — 실패 시 None"""
    url = (
        f"https://fapi.binance.com/futures/data/topLongShortAccountRatio"
        f"?symbol={symbol}&period=5m&limit=1"
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
        if isinstance(data, list) and len(data) > 0:
            return float(data[0]["longShortRatio"])
    except Exception:
        pass
    return None

# ── 메인 스캔 ──────────────────────────────────────────────────────────────
async def scan(session):
    global prev_long, prev_short

    print(f"[{now_kst()} KST] 스캔 시작")

    # STEP 1: 심볼 + 티커
    try:
        active_symbols, tickers = await asyncio.gather(
            fetch_active_symbols(session),
            fetch_tickers(session)
        )
    except Exception as e:
        await send_msg(session,
            f"⚠️ <b>스캔 오류</b> — API 수집 실패\n"
            f"<code>{str(e)[:300]}</code>\n"
            f"⏰ {now_kst()} KST"
        )
        print(f"[오류] API 수집: {e}")
        return

    # STEP 2: 후보 필터
    candidates = []
    for t in tickers:
        if t["symbol"] not in active_symbols:
            continue
        change = float(t["priceChangePercent"])
        volume = float(t["quoteVolume"])
        if abs(change) >= MIN_CHANGE:
            candidates.append({
                "symbol": t["symbol"],
                "price":  float(t["lastPrice"]),
                "change": change,
                "volume": volume,
            })

    candidates.sort(key=lambda x: x["volume"], reverse=True)
    candidates = candidates[:TOP_N]
    print(f"[{now_kst()} KST] 후보 {len(candidates)}개")

    await send_msg(session,
        f"🔍 <b>스캔 진행중</b> ({now_kst()} KST)\n"
        f"변동률 ≥ {MIN_CHANGE}% 후보: <b>{len(candidates)}개</b>\n"
        f"L/S 조회 시작..."
    )

    if len(candidates) == 0:
        await send_msg(session,
            f"⚠️ 후보 종목 0개\n"
            f"/minchange {max(0.1, MIN_CHANGE - 0.5):.1f} 로 낮춰보세요."
        )
        return

    # STEP 3: L/S 조회
    enriched   = []
    ls_success = 0
    ls_fail    = 0
    batch      = 5

    for i in range(0, len(candidates), batch):
        chunk  = candidates[i:i+batch]
        ratios = await asyncio.gather(*[fetch_ls(session, t["symbol"]) for t in chunk])
        for t, ls in zip(chunk, ratios):
            if ls is not None:
                enriched.append({**t, "ls": ls})
                ls_success += 1
            else:
                ls_fail += 1
        if i + batch < len(candidates):
            await asyncio.sleep(0.3)

    print(f"[{now_kst()} KST] L/S 성공 {ls_success} / 실패 {ls_fail}")

    if ls_success == 0:
        await send_msg(session,
            f"⚠️ <b>L/S 조회 전체 실패</b>\n"
            f"후보 {len(candidates)}개 중 L/S 데이터 0개 수집\n"
            f"Binance API 지역 제한일 수 있습니다.\n"
            f"⏰ {now_kst()} KST"
        )
        return

    # STEP 4: 조건 분류
    curr_long  = {t["symbol"]: t for t in enriched if t["ls"] <= LS_LOW}
    curr_short = {t["symbol"]: t for t in enriched if t["ls"] >= LS_HIGH}

    curr_long_set  = set(curr_long.keys())
    curr_short_set = set(curr_short.keys())
    now = now_kst()

    # 롱 진입/이탈
    for sym in curr_long_set - prev_long:
        t     = curr_long[sym]
        emoji = "🔥" if t["ls"] <= 0.25 else "📈"
        await send_msg(session,
            f"{emoji} <b>롱 사냥 진입</b>\n"
            f"심볼: <b>{sym.replace('USDT','')}</b>\n"
            f"L/S: <b>{t['ls']:.3f}</b> (≤ {LS_LOW})\n"
            f"현재가: ${t['price']:,.4f}\n"
            f"24h: {t['change']:+.2f}%\n"
            f"⏰ {now} KST"
        )

    for sym in prev_long - curr_long_set:
        await send_msg(session,
            f"❌ <b>롱 사냥 이탈</b>\n"
            f"심볼: <b>{sym.replace('USDT','')}</b>\n"
            f"L/S 조건 벗어남 (>{LS_LOW})\n"
            f"⏰ {now} KST"
        )

    # 숏 진입/이탈
    for sym in curr_short_set - prev_short:
        t     = curr_short[sym]
        emoji = "🔥" if t["ls"] >= 2.5 else "📉"
        await send_msg(session,
            f"{emoji} <b>숏 사냥 진입</b>\n"
            f"심볼: <b>{sym.replace('USDT','')}</b>\n"
            f"L/S: <b>{t['ls']:.3f}</b> (≥ {LS_HIGH})\n"
            f"현재가: ${t['price']:,.4f}\n"
            f"24h: {t['change']:+.2f}%\n"
            f"⏰ {now} KST"
        )

    for sym in prev_short - curr_short_set:
        await send_msg(session,
            f"❌ <b>숏 사냥 이탈</b>\n"
            f"심볼: <b>{sym.replace('USDT','')}</b>\n"
            f"L/S 조건 벗어남 (<{LS_HIGH})\n"
            f"⏰ {now} KST"
        )

    # 저장 + 요약
    prev_long  = curr_long_set
    prev_short = curr_short_set
    save_state(prev_long, prev_short)

    await send_msg(session, build_summary(curr_long, curr_short))
    print(f"[{now_kst()} KST] 완료 — 롱 {len(curr_long_set)}개 / 숏 {len(curr_short_set)}개")

# ── 명령어 처리 ────────────────────────────────────────────────────────────
async def handle_commands(session):
    global LS_LOW, LS_HIGH, MIN_CHANGE, force_scan_flag
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()

        updates = data.get("result", [])
        if not updates:
            return

        last_update_id = updates[-1]["update_id"]
        ack_url = f"{url}?offset={last_update_id + 1}"
        async with session.get(ack_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            await r.json()

        for update in updates:
            msg     = update.get("message", {})
            text    = msg.get("text", "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))

            if chat_id != CHAT_ID:
                continue

            if text.startswith("/low"):
                parts = text.split()
                if len(parts) == 2:
                    try:
                        LS_LOW = float(parts[1])
                        await send_msg(session, f"✅ 롱 사냥 상한 → <b>{LS_LOW}</b> 로 변경됨")
                    except:
                        await send_msg(session, "❌ 숫자를 입력해주세요. 예: /low 0.40")

            elif text.startswith("/high"):
                parts = text.split()
                if len(parts) == 2:
                    try:
                        LS_HIGH = float(parts[1])
                        await send_msg(session, f"✅ 숏 사냥 하한 → <b>{LS_HIGH}</b> 로 변경됨")
                    except:
                        await send_msg(session, "❌ 숫자를 입력해주세요. 예: /high 1.50")

            elif text.startswith("/minchange"):
                parts = text.split()
                if len(parts) == 2:
                    try:
                        MIN_CHANGE = float(parts[1])
                        await send_msg(session, f"✅ 최소 변동률 → <b>{MIN_CHANGE}%</b> 로 변경됨")
                    except:
                        await send_msg(session, "❌ 숫자를 입력해주세요. 예: /minchange 1.0")

            elif text == "/status":
                next_str = next_scan_time.strftime("%H:%M:%S") if next_scan_time else "계산중"
                await send_msg(session,
                    f"📊 <b>현재 설정</b> ({now_kst()} KST)\n"
                    f"롱 사냥 상한: <b>{LS_LOW}</b>\n"
                    f"숏 사냥 하한: <b>{LS_HIGH}</b>\n"
                    f"최소 변동률: <b>{MIN_CHANGE}%</b>\n"
                    f"갱신 주기: <b>{REFRESH_MIN}분</b>\n"
                    f"현재 롱: <b>{len(prev_long)}개</b>\n"
                    f"현재 숏: <b>{len(prev_short)}개</b>\n"
                    f"다음 스캔: <b>{next_str} KST</b>"
                )

            elif text == "/scan":
                force_scan_flag = True
                await send_msg(session, "🔄 즉시 스캔 시작...")

            elif text == "/list":
                if not prev_long and not prev_short:
                    await send_msg(session, f"📭 현재 감지된 종목 없음 ({now_kst()} KST)")
                else:
                    lines = [f"📋 <b>현재 감지 목록</b> ({now_kst()} KST)\n"]
                    if prev_long:
                        lines.append(f"📈 롱 사냥 {len(prev_long)}개:")
                        lines.append("  " + ", ".join(s.replace("USDT","") for s in sorted(prev_long)))
                    if prev_short:
                        lines.append(f"📉 숏 사냥 {len(prev_short)}개:")
                        lines.append("  " + ", ".join(s.replace("USDT","") for s in sorted(prev_short)))
                    await send_msg(session, "\n".join(lines))

            elif text == "/help":
                await send_msg(session,
                    "📋 <b>사용 가능한 명령어</b>\n\n"
                    "/low 0.40 — 롱 사냥 L/S 상한 변경\n"
                    "/high 1.50 — 숏 사냥 L/S 하한 변경\n"
                    "/minchange 1.0 — 최소 변동률 변경\n"
                    "/scan — 즉시 스캔 실행\n"
                    "/list — 현재 감지 목록 확인\n"
                    "/status — 현재 설정 + 다음 스캔 시간\n"
                    "/help — 도움말"
                )

    except Exception as e:
        print(f"[명령어 처리 오류] {e}")

# ── 루프 ──────────────────────────────────────────────────────────────────
async def command_loop(session):
    while True:
        try:
            await handle_commands(session)
        except Exception as e:
            print(f"[명령어 루프 오류] {e}")
        await asyncio.sleep(2)

async def scan_loop(session):
    global force_scan_flag, next_scan_time
    while True:
        try:
            await scan(session)
        except Exception as e:
            print(f"[스캔 루프 오류] {e}")
            try:
                await send_msg(session,
                    f"⚠️ <b>스캔 루프 오류</b>\n"
                    f"<code>{str(e)[:300]}</code>\n"
                    f"⏰ {now_kst()} KST"
                )
            except:
                pass

        next_scan_time = datetime.now(KST) + timedelta(minutes=REFRESH_MIN)
        print(f"[다음 스캔] {next_scan_time.strftime('%H:%M:%S')} KST")

        for _ in range(REFRESH_MIN * 30):
            await asyncio.sleep(2)
            if force_scan_flag:
                force_scan_flag = False
                next_scan_time  = None
                print("[즉시 스캔 트리거]")
                break

async def main():
    print("🚀 Long Hunter Bot 시작!")
    async with aiohttp.ClientSession() as session:
        await send_msg(session,
            "🚀 <b>Long Hunter Bot 시작!</b>\n"
            f"롱 사냥: L/S ≤ {LS_LOW}\n"
            f"숏 사냥: L/S ≥ {LS_HIGH}\n"
            f"최소 변동률: {MIN_CHANGE}%\n"
            f"갱신: {REFRESH_MIN}분마다\n"
            f"시작: {now_kst('%Y-%m-%d %H:%M:%S')} KST\n\n"
            "명령어 보려면 /help"
        )
        await asyncio.gather(
            command_loop(session),
            scan_loop(session)
        )

if __name__ == "__main__":
    asyncio.run(main())
