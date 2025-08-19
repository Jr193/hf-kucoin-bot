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
import uvloop  # Optimisation asyncio

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())  # Active uvloop pour accélérer

API_KEY = *****
API_SECRET = *****
API_PASSPHRASE = *****
PAIR = "***-USDT"
AMOUNT_TO_BUY_USDT = 20
PRICE_OFFSET = 1.098
BASE_URL = "https://api.kucoin.com"
latest_price = None
price_received = asyncio.Event()
increments = {}  # On garde tout en mémoire


def optimize_cpu():
    """ Optimise l'utilisation CPU pour plus de rapidité """
    try:
        p = psutil.Process(os.getpid())
        p.nice(-20)  # Priorité maximale
        p.cpu_affinity(list(range(os.cpu_count())))  # Utilisation totale du CPU
    except Exception:
        pass


def generate_signature(timestamp, method, endpoint, body=""):
    """ Génère la signature de requête KuCoin """
    str_to_sign = f"{timestamp}{method}{endpoint}{body}"
    return base64.b64encode(hmac.new(API_SECRET.encode(), str_to_sign.encode(), hashlib.sha256).digest()).decode()


def generate_passphrase():
    """ Génère la passphrase signée """
    return base64.b64encode(hmac.new(API_SECRET.encode(), API_PASSPHRASE.encode(), hashlib.sha256).digest()).decode()


def adjust_to_increment(value, increment):
    """ Ajuste un prix/taille à l'incrément requis par KuCoin """
    return (value // increment) * increment


async def get_symbol_details(session):
    """ Récupère les détails du symbole et garde les incréments en mémoire """
    global increments
    try:
        async with session.get(f"{BASE_URL}/api/v1/symbols") as response:
            symbols = (await response.json()).get('data', [])
            for symbol in symbols:
                if symbol['symbol'] == PAIR:
                    increments = {
                        "priceIncrement": float(symbol['priceIncrement']),
                        "baseIncrement": float(symbol['baseIncrement'])
                    }
                    break
    except Exception as e:
        print(f"❌ Erreur récupération incréments : {e}")
        sys.exit(1)


async def get_websocket_token(session):
    """ Récupère le token WebSocket """
    try:
        async with session.post(f"{BASE_URL}/api/v1/bullet-public") as response:
            data = await response.json()
            return data['data']['instanceServers'][0]['endpoint'], data['data']['token']
    except Exception as e:
        print(f"❌ Erreur récupération token WebSocket : {e}")
        sys.exit(1)


async def websocket_listener(endpoint, token):
    """ Se connecte au WebSocket et attend le premier prix valide """
    global latest_price
    try:
        async with websockets.connect(f"{endpoint}?token={token}") as ws:
            sock = ws.transport.get_extra_info('socket')
            if sock:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            await ws.send(json.dumps({"type": "subscribe", "topic": f"/market/ticker:{PAIR}", "privateChannel": False}))
            async for message in ws:
                data = json.loads(message)
                if 'data' in data and 'price' in data['data']:
                    latest_price = float(data['data']['price'])
                    price_received.set()
                    break
    except Exception as e:
        print(f"❌ Erreur WebSocket : {e}")
        sys.exit(1)


async def send_hf_limit_order(session):
    """ Envoie un ordre HF sur KuCoin """
    limit_price = adjust_to_increment(latest_price * PRICE_OFFSET, increments['priceIncrement'])
    size = adjust_to_increment(AMOUNT_TO_BUY_USDT / latest_price, increments['baseIncrement'])

    order_data = {
        "symbol": PAIR,
        "side": "buy",
        "type": "limit",
        "price": f"{limit_price:.6f}",
        "size": f"{size:.6f}"
    }

    url = f"{BASE_URL}/api/v1/hf/orders"
    timestamp = str(int(time.time() * 1000))
    body = json.dumps(order_data)
    headers = {
        "KC-API-KEY": API_KEY,
        "KC-API-SIGN": generate_signature(timestamp, "POST", "/api/v1/hf/orders", body),
        "KC-API-TIMESTAMP": timestamp,
        "KC-API-PASSPHRASE": generate_passphrase(),
        "KC-API-KEY-VERSION": "2",
        "Content-Type": "application/json"
    }
    
    start = time.perf_counter()
    try:
        async with session.post(url, headers=headers, data=body) as response:
            elapsed_time = time.perf_counter() - start
            resp_json = await response.json()
            if response.status == 200 and resp_json.get("code") == "200000":
                print(f"⏱️ {elapsed_time:.4f}s")
                print("✅ Ordre exécuté")
            else:
                print(f"❌ Erreur exécution ordre : {resp_json}")
    except Exception as e:
        print(f"❌ Erreur envoi ordre : {e}")


async def main():
    """ Fonction principale """
    optimize_cpu()
    async with aiohttp.ClientSession() as session:
        await get_symbol_details(session)  # Récupérer les incréments
        endpoint, token = await get_websocket_token(session)  # Récupérer WebSocket
        ws_task = asyncio.create_task(websocket_listener(endpoint, token))  # Écoute WebSocket

        await price_received.wait()  # Attente active du prix

        await send_hf_limit_order(session)  # Envoi de l'ordre

        await ws_task  # Ferme le WebSocket après l'ordre


if __name__ == "__main__":
    print(f"🚀 Lancement du bot HF optimisé avec asyncio pour {PAIR} !")
    asyncio.run(main())