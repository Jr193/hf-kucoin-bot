import asyncio
import aiohttp
import hmac
import hashlib
import base64
import json
import os
import psutil
import websockets
import time
import socket
import sys

# uvloop is great on Unix; skip on Windows
try:
    import uvloop  # type: ignore
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except Exception:
    pass

from dotenv import load_dotenv  # pip install python-dotenv
load_dotenv()  # Loads .env if present

# --- Environment (NEVER hardcode secrets) ---
API_KEY = os.getenv("KUCOIN_API_KEY", "demo_key")
API_SECRET = os.getenv("KUCOIN_API_SECRET", "demo_secret")
API_PASSPHRASE = os.getenv("KUCOIN_API_PASSPHRASE", "demo_pass")

# --- Config ---
PAIR = os.getenv("PAIR", "BTC-USDT")
AMOUNT_TO_BUY_USDT = float(os.getenv("AMOUNT_TO_BUY_USDT", "20"))
PRICE_OFFSET = float(os.getenv("PRICE_OFFSET", "1.001"))  # e.g. 0.1% above
BASE_URL = "https://api.kucoin.com"
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"  # default safe

latest_price = None
price_received = asyncio.Event()
increments = {}

def optimize_cpu():
    """Best-effort CPU tuning; ignored if not supported."""
    try:
        p = psutil.Process(os.getpid())
        p.nice(-20)
        if hasattr(p, "cpu_affinity"):
            p.cpu_affinity(list(range(os.cpu_count())))
    except Exception:
        pass

def generate_signature(timestamp, method, endpoint, body=""):
    """KuCoin request signature."""
    str_to_sign = f"{timestamp}{method}{endpoint}{body}"
    digest = hmac.new(API_SECRET.encode(), str_to_sign.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()

def generate_passphrase():
    """Signed passphrase v2."""
    digest = hmac.new(API_SECRET.encode(), API_PASSPHRASE.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()

def adjust_to_increment(value, increment):
    """Round down to KuCoin increments (price/size)."""
    if increment <= 0:
        return value
    # Use floor division with integer-like scaling to avoid FP drift
    steps = int(value / increment)
    return steps * increment

async def get_symbol_details(session):
    """Fetch symbol increments and cache locally."""
    global increments
    try:
        async with session.get(f"{BASE_URL}/api/v1/symbols") as resp:
            payload = await resp.json()
            for symbol in payload.get("data", []):
                if symbol.get("symbol") == PAIR:
                    increments = {
                        "priceIncrement": float(symbol["priceIncrement"]),
                        "baseIncrement": float(symbol["baseIncrement"]),
                    }
                    return
        raise RuntimeError("Symbol not found or API error")
    except Exception as e:
        print(f"❌ Error fetching increments for {PAIR}: {e}")
        sys.exit(1)

async def get_websocket_token(session):
    """Get public WS token & endpoint."""
    try:
        async with session.post(f"{BASE_URL}/api/v1/bullet-public") as resp:
            data = await resp.json()
            srv = data["data"]["instanceServers"][0]
            return srv["endpoint"], data["data"]["token"]
    except Exception as e:
        print(f"❌ Error fetching WS token: {e}")
        sys.exit(1)

async def websocket_listener(endpoint, token):
    """Subscribe to ticker and capture first valid price."""
    global latest_price
    try:
        async with websockets.connect(f"{endpoint}?token={token}") as ws:
            sock = ws.transport.get_extra_info("socket")
            if sock:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sub = {"type": "subscribe", "topic": f"/market/ticker:{PAIR}", "privateChannel": False}
            await ws.send(json.dumps(sub))
            async for message in ws:
                data = json.loads(message)
                if "data" in data and "price" in data["data"]:
                    latest_price = float(data["data"]["price"])
                    price_received.set()
                    break
    except Exception as e:
        print(f"❌ WebSocket error: {e}")
        sys.exit(1)

async def send_hf_limit_order(session):
    """Preview or send a HF limit order (dry-run by default)."""
    limit_price = adjust_to_increment(latest_price * PRICE_OFFSET, increments["priceIncrement"])
    size = adjust_to_increment(AMOUNT_TO_BUY_USDT / latest_price, increments["baseIncrement"])

    order_data = {
        "symbol": PAIR,
        "side": "buy",
        "type": "limit",
        "price": f"{limit_price:.6f}",
        "size": f"{size:.6f}",
    }

    if DRY_RUN:
        print("🔹 DRY RUN (no live order will be sent)")
        print("Order preview:", order_data)
        return

    url = f"{BASE_URL}/api/v1/hf/orders"
    timestamp = str(int(time.time() * 1000))
    body = json.dumps(order_data)
    headers = {
        "KC-API-KEY": API_KEY,
        "KC-API-SIGN": generate_signature(timestamp, "POST", "/api/v1/hf/orders", body),
        "KC-API-TIMESTAMP": timestamp,
        "KC-API-PASSPHRASE": generate_passphrase(),
        "KC-API-KEY-VERSION": "2",
        "Content-Type": "application/json",
    }

    start = time.perf_counter()
    try:
        async with session.post(url, headers=headers, data=body) as resp:
            elapsed = time.perf_counter() - start
            payload = await resp.json()
            if resp.status == 200 and payload.get("code") == "200000":
                print(f"⏱️ {elapsed:.4f}s — ✅ Order executed")
            else:
                print(f"❌ Order error: HTTP {resp.status} | {payload}")
    except Exception as e:
        print(f"❌ Error sending order: {e}")

async def main():
    print(f"🚀 Starting HF bot for {PAIR} (DRY_RUN={DRY_RUN})")
    optimize_cpu()
    async with aiohttp.ClientSession() as session:
        await get_symbol_details(session)
        endpoint, token = await get_websocket_token(session)
        ws_task = asyncio.create_task(websocket_listener(endpoint, token))
        await price_received.wait()
        await send_hf_limit_order(session)
        await ws_task

if __name__ == "__main__":
    asyncio.run(main())