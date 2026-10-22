
#!/usr/bin/env python3
"""
copy_paper.py - Bot de PAPER TRADING que copia las wallets de traders de FOMO (Solana).
 
- NO usa claves privadas ni dinero real: solo simula.
- Vigila las wallets, detecta compras/ventas y las replica con $100 simulados.
- Mide cuanto peor es TU precio frente al del trader (el retraso), que es lo que
  decide si copiar vale la pena.
 
Uso:
    pip install requests
    set HELIUS_API_KEY=tu_clave        (PowerShell: $env:HELIUS_API_KEY="tu_clave")
    python copy_paper.py               (corre el bot; Ctrl+C para parar, guarda estado)
    python copy_paper.py --resumen     (muestra resultados sin correr el bot)
    python copy_paper.py --reset       (borra estado y reinicia con el capital inicial)
 
IMPORTANTE: el RPC publico de Solana limita mucho. Crea una clave gratis en helius.dev.
"""
import os
import sys
import json
import time
import csv
import requests
from datetime import datetime, timezone
 
# ======================= CONFIGURACION =======================
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
RPC_URL = (f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
           if HELIUS_API_KEY else "https://api.mainnet-beta.solana.com")
 
# Nombre -> wallet DUENA (no la cuenta del token). Cambia los nombres si los sabes.
WALLETS = {
    "Rowdy":        "CzU8MaRcwvwUoNkwJFLbvtFWJugcEXAhDDQqNFE4ybb7",
    "pointfarmcap": "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit",
    "trader3":      "83b2LMf12aLec92kur3duRA8VvUkLaXUtML31aCcZNCM",
    # Para agregar otro trader, copia su wallet de la columna "Account" (no "Token Account"):
    # "otro": "DIRECCION_AQUI",
}
 
CAPITAL_USD = 100.0          # capital simulado inicial
# ESQUEMA POR CONVICCION: cuanto mas grande y mas progresiva es la compra del trader en un
# token, mas pones tu. Formato: (compra acumulada del trader en ese token >= X USD, tu posicion
# objetivo en USD). Las compras del trader menores al primer nivel se IGNORAN y no cuentan.
TIERS = [(1000, 4.0), (3000, 8.0), (10000, 15.0), (30000, 25.0)]
MIN_TRADER_BUY_USD = TIERS[0][0]
MAX_POSITIONS = 10           # maximo de posiciones abiertas a la vez
MAX_PER_TRADER_USD = 40.0    # tope que un mismo trader puede consumir de tu capital (deja cupo a los otros)
MAX_CHASE_PCT = 0.05         # solo entras si el precio de mercado esta a <=5% sobre la entrada promedio del trader
WANT_MAX_AGE_HOURS = 6       # una compra en espera caduca tras estas horas
FILL_CHECK_SECONDS = 30      # cada cuanto revisa si puede completar compras en espera
ENTRY_SLIPPAGE = 0.03        # slippage simulado en tu entrada (ademas del retraso real)
EXIT_SLIPPAGE = 0.03         # slippage simulado en tu salida
FEE_PCT = 0.01               # comision simulada por operacion (ajustala a la real)
STOP_LOSS_PCT = None         # ej. 0.40 = vender si cae 40%. None = copiar puro
MAX_HOLD_HOURS = None        # ej. 12 = cerrar tras 12 h. None = sin limite
POLL_SECONDS = 12            # cada cuantos segundos revisa las wallets (ver nota)
# NOTA: Helius gratis = 1M creditos/mes y cada consulta cuesta 1. Con 4 wallets, 12 s ~ 0.86M/mes.
# A 6 s te quedas sin creditos en ~17 dias. Si bajas POLL_SECONDS, sube de plan o reduce wallets.
SUMMARY_EVERY = 50           # imprime resumen cada N ciclos
STALE_SECONDS = 180          # no copia compras que el trader hizo hace mas de esto
MAX_RUN_MINUTES = float(os.getenv("MAX_RUN_MINUTES", "0"))  # 0 = sin limite (GitHub Actions usa ~345)
 
STATE_FILE = "copy_state.json"
TRADES_CSV = "copy_trades.csv"
# =============================================================
 
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTES = {WSOL, USDC, USDT}
SYSTEM_PROGRAM = "11111111111111111111111111111111"
 
_sol_cache = {"t": 0, "p": 150.0}
_dex_cache = {}
STATS = {}          # actividad vista por wallet (para saber que la deteccion funciona)
RPC_FAILS = [0]     # consultas RPC que fallaron del todo
 
 
def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
 
 
def log(msg):
    print(f"[{now_iso()}] {msg}", flush=True)
 
 
# ------------------------- RPC y precios -------------------------
def rpc(method, params, retries=4):
    for i in range(retries):
        try:
            r = requests.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1,
                                             "method": method, "params": params}, timeout=20)
            if r.status_code == 429:
                time.sleep(1.5 * (i + 1))
                continue
            r.raise_for_status()
            j = r.json()
            if "error" in j:
                time.sleep(1.0 * (i + 1))
                continue
            return j.get("result")
        except Exception:
            time.sleep(1.0 * (i + 1))
    RPC_FAILS[0] += 1
    return None
 
 
def sol_price():
    if time.time() - _sol_cache["t"] < 60:
        return _sol_cache["p"]
    try:
        r = requests.get("https://api.coinbase.com/v2/prices/SOL-USD/spot", timeout=10)
        _sol_cache["p"] = float(r.json()["data"]["amount"])
        _sol_cache["t"] = time.time()
    except Exception:
        pass  # usa el ultimo precio conocido
    return _sol_cache["p"]
 
 
def dex_info(mint, ttl=5):
    """Precio actual (USD) y simbolo desde DexScreener, usando el par con mas liquidez."""
    c = _dex_cache.get(mint)
    if c and time.time() - c[0] < ttl:
        return c[1]
    info = None
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=10)
        pairs = [p for p in (r.json().get("pairs") or [])
                 if p.get("chainId") == "solana" and p.get("priceUsd")
                 and (p.get("baseToken") or {}).get("address") == mint]
        if pairs:
            best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
            info = {"price": float(best["priceUsd"]),
                    "symbol": (best.get("baseToken") or {}).get("symbol", "?")}
    except Exception:
        info = None
    _dex_cache[mint] = (time.time(), info)
    return info
 
 
# ------------------------- Parseo de swaps -------------------------
def parse_swap_ex(tx, wallet):
    """Devuelve (swap, motivo). swap es None si no es un swap claro; motivo explica por que."""
    if not tx or not tx.get("meta"):
        return None, "no se pudo leer la transaccion"
    meta = tx["meta"]
    if meta.get("err"):
        return None, "transaccion fallida"
    keys = [k["pubkey"] if isinstance(k, dict) else k
            for k in tx["transaction"]["message"]["accountKeys"]]
 
    pre, post = {}, {}
    for b in meta.get("preTokenBalances") or []:
        if b.get("owner") == wallet:
            pre[b["mint"]] = pre.get(b["mint"], 0.0) + float(b["uiTokenAmount"].get("uiAmountString") or 0)
    for b in meta.get("postTokenBalances") or []:
        if b.get("owner") == wallet:
            post[b["mint"]] = post.get(b["mint"], 0.0) + float(b["uiTokenAmount"].get("uiAmountString") or 0)
    deltas = {m: post.get(m, 0.0) - pre.get(m, 0.0) for m in set(pre) | set(post)}
 
    sol = 0.0
    if wallet in keys:
        i = keys.index(wallet)
        sol = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
        if i == 0:                       # si paga la comision, no la contamos como costo
            sol += meta.get("fee", 0) / 1e9
    sol += deltas.get(WSOL, 0.0)
    quote_usd = sol * sol_price() + deltas.get(USDC, 0.0) + deltas.get(USDT, 0.0)
 
    others = {m: d for m, d in deltas.items() if m not in QUOTES and abs(d) > 0}
    if not others:
        return None, "ningun token propio cambio (transferencia o actividad sin swap)"
    if len(others) != 1:
        det = ", ".join(f"{m[:6]}..({d:+.4g})" for m, d in others.items())
        return None, f"cambiaron varios tokens a la vez: {det}"
    mint, d = next(iter(others.items()))
    if abs(quote_usd) < 1.0:
        return None, f"{mint[:6]}.. {d:+.4g} sin movimiento relevante de SOL/USDC (${quote_usd:+,.2f})"
 
    if d > 0 and quote_usd < 0:
        usd = -quote_usd
        return {"side": "BUY", "mint": mint, "usd": usd, "tokens": d, "price": usd / d,
                "frac_sold": 0.0}, ""
    if d < 0 and quote_usd > 0:
        before = pre.get(mint, 0.0)
        frac = min(1.0, (-d) / before) if before > 0 else 1.0
        return {"side": "SELL", "mint": mint, "usd": quote_usd, "tokens": -d,
                "price": quote_usd / (-d), "frac_sold": frac}, ""
    return None, (f"{mint[:6]}.. {d:+.4g} pero el SOL/USDC propio cambio ${quote_usd:+,.2f} "
                  f"(no cuadra como compra/venta)")
 
 
def parse_swap(tx, wallet):
    return parse_swap_ex(tx, wallet)[0]
 
 
# ------------------------- Estado y registro -------------------------
def new_state():
    return {"cash": CAPITAL_USD, "positions": {}, "closed": [], "last_sig": {}, "wants": {}}
 
 
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        st.setdefault("wants", {})
        return st
    return new_state()
 
 
def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)
 
 
def write_csv(row):
    new = not os.path.exists(TRADES_CSV)
    cols = ["time", "trader", "event", "mint", "symbol", "usd", "price",
            "trader_price", "slip_vs_trader_pct", "pnl_usd", "pnl_pct"]
    with open(TRADES_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in cols})
 
 
# ------------------------- Logica de copia -------------------------
def target_usd(cum):
    t = 0.0
    for thr, usd in TIERS:
        if cum >= thr:
            t = usd
    return t
 
 
def used_by_trader(state, trader):
    return sum(p["cost_usd"] for p in state["positions"].values() if p["trader"] == trader)
 
 
def try_fill(state, mint, quiet=True):
    """Intenta abrir/ampliar la posicion hasta el nivel que corresponde a lo que el trader lleva
    comprado. Devuelve 'ok' (compro), 'idle' (nada que comprar) o 'wait' (queda en espera)."""
    w = state["wants"].get(mint)
    if not w:
        return "idle"
    pos = state["positions"].get(mint)
    trader = w["trader"]
    need = target_usd(w["cum"]) - (pos["cost_usd"] if pos else 0.0)
    need = min(need, MAX_PER_TRADER_USD - used_by_trader(state, trader))
    if need < 1:
        return "idle"
    size = min(need, state["cash"])
    if size < 1:
        return "wait"                                   # sin saldo: espera a que otra posicion cierre
    if not pos and len(state["positions"]) >= MAX_POSITIONS:
        return "wait"
    info = dex_info(mint)
    if not info:
        return "wait"
    trader_avg = w["usd_sum"] / w["tok_sum"]
    premium = info["price"] / trader_avg - 1
    if premium > MAX_CHASE_PCT:
        if not quiet:
            log(f"{trader}: {mint[:6]}.. ya esta {premium*100:.0f}% sobre su entrada; espero a que vuelva")
        return "wait"
    entry = info["price"] * (1 + ENTRY_SLIPPAGE)
    tokens = size * (1 - FEE_PCT) / entry
    slip = (entry / trader_avg - 1) * 100
    have = pos["cost_usd"] if pos else 0.0
    state["cash"] -= size
    if pos:
        pos["tokens"] += tokens
        pos["cost_usd"] += size
        pos["entry_price"] = pos["cost_usd"] / pos["tokens"]
        event = "ADD"
    else:
        state["positions"][mint] = {
            "trader": trader, "symbol": info["symbol"], "tokens": tokens, "cost_usd": size,
            "entry_price": entry, "trader_price": trader_avg, "opened": time.time(),
        }
        event = "BUY"
    symbol = info["symbol"]
    log(f"COPIA {event} {symbol} de {trader}: +${size:.2f} (trader lleva ${w['cum']:,.0f}; "
        f"tu total ${have + size:.2f}) a {entry:.3e}, su entrada promedio {trader_avg:.3e} ({slip:+.1f}%)")
    write_csv({"time": now_iso(), "trader": trader, "event": event, "mint": mint,
               "symbol": symbol, "usd": round(size, 4), "price": entry,
               "trader_price": trader_avg, "slip_vs_trader_pct": round(slip, 2)})
    return "ok"
 
 
def open_position(state, trader, sw):
    """Registra una compra grande del trader y trata de copiarla (esquema TIERS)."""
    mint = sw["mint"]
    if sw["usd"] < MIN_TRADER_BUY_USD:
        return                                          # compra chica: no cuenta
    pos = state["positions"].get(mint)
    w = state["wants"].get(mint)
    if (pos and pos["trader"] != trader) or (w and w["trader"] != trader):
        log(f"{trader} tambien compro {mint[:6]}.., pero ese token sigue a otro trader")
        return
    if not w:
        w = {"trader": trader, "cum": 0.0, "usd_sum": 0.0, "tok_sum": 0.0, "since": time.time()}
        state["wants"][mint] = w
    w["cum"] += sw["usd"]
    w["usd_sum"] += sw["usd"]
    w["tok_sum"] += sw["tokens"]
    if try_fill(state, mint, quiet=False) == "wait":
        log(f"EN ESPERA: {trader} lleva ${w['cum']:,.0f} en {mint[:6]}..; se completa cuando haya saldo "
            f"y el precio siga cerca de su entrada")
 
 
def fill_waiting(state):
    """Completa compras en espera, empezando por las mas cercanas al precio de entrada del trader."""
    if state["cash"] < 1:
        return
    cands = []
    for mint, w in list(state["wants"].items()):
        if time.time() - w["since"] > WANT_MAX_AGE_HOURS * 3600:
            del state["wants"][mint]
            continue
        pos = state["positions"].get(mint)
        if target_usd(w["cum"]) - (pos["cost_usd"] if pos else 0.0) < 1:
            continue
        info = dex_info(mint)
        if info:
            cands.append((info["price"] / (w["usd_sum"] / w["tok_sum"]) - 1, mint))
    for _, mint in sorted(cands):
        if state["cash"] < 1:
            break
        try_fill(state, mint)
 
 
def close_position(state, mint, frac, reason, fallback_price=None):
    pos = state["positions"].get(mint)
    if not pos:
        return
    info = dex_info(mint, ttl=0)
    px = info["price"] if info else fallback_price
    if px is None:
        log(f"No hay precio para cerrar {pos['symbol']}; se deja abierta")
        return
    exit_px = px * (1 - EXIT_SLIPPAGE)
    tok = pos["tokens"] * frac
    proceeds = tok * exit_px * (1 - FEE_PCT)
    cost = pos["cost_usd"] * frac
    pnl = proceeds - cost
    state["cash"] += proceeds
    state["closed"].append({"time": now_iso(), "trader": pos["trader"], "symbol": pos["symbol"],
                            "mint": mint, "cost": cost, "proceeds": proceeds, "pnl": pnl,
                            "reason": reason})
    log(f"{reason} {pos['symbol']} ({pos['trader']}): {frac*100:.0f}% -> ${proceeds:.2f} "
        f"PnL {pnl:+.2f} ({pnl / cost * 100:+.0f}%)")
    write_csv({"time": now_iso(), "trader": pos["trader"], "event": reason, "mint": mint,
               "symbol": pos["symbol"], "usd": round(proceeds, 4), "price": exit_px,
               "pnl_usd": round(pnl, 4), "pnl_pct": round(pnl / cost * 100, 2)})
    if frac >= 0.98:
        del state["positions"][mint]
        state["wants"].pop(mint, None)
    else:
        pos["tokens"] -= tok
        pos["cost_usd"] -= cost
 
 
def handle_swap(state, trader, sw):
    if sw["side"] == "BUY":
        open_position(state, trader, sw)
        return
    mint = sw["mint"]
    w = state["wants"].get(mint)
    if w and w["trader"] == trader:
        if sw["frac_sold"] >= 0.9:
            del state["wants"][mint]                     # el trader salio: ya no hay nada que completar
        else:
            for k in ("cum", "usd_sum", "tok_sum"):
                w[k] *= (1 - sw["frac_sold"])
    pos = state["positions"].get(mint)
    if pos and pos["trader"] == trader:
        close_position(state, mint, sw["frac_sold"], "SELL", fallback_price=sw["price"])
        fill_waiting(state)                              # saldo liberado: entra a lo que quedo en espera
 
 
def check_risk(state):
    if STOP_LOSS_PCT is None and MAX_HOLD_HOURS is None:
        return
    for mint in list(state["positions"].keys()):
        pos = state["positions"][mint]
        info = dex_info(mint)
        if STOP_LOSS_PCT is not None and info:
            if info["price"] <= pos["entry_price"] * (1 - STOP_LOSS_PCT):
                close_position(state, mint, 1.0, "STOP")
                continue
        if MAX_HOLD_HOURS is not None and time.time() - pos["opened"] > MAX_HOLD_HOURS * 3600:
            close_position(state, mint, 1.0, "TIMEOUT")
 
 
# ------------------------- Resumen -------------------------
def summary(state):
    value = 0.0
    lines = []
    for mint, pos in state["positions"].items():
        info = dex_info(mint)
        v = pos["tokens"] * info["price"] if info else 0.0
        value += v
        lines.append(f"   {pos['symbol']:<10} ({pos['trader']}) costo ${pos['cost_usd']:.2f} -> ${v:.2f}")
    equity = state["cash"] + value
    print("\n" + "=" * 60)
    print(f"Efectivo: ${state['cash']:.2f} | Posiciones: ${value:.2f} | "
          f"TOTAL: ${equity:.2f} ({(equity / CAPITAL_USD - 1) * 100:+.1f}% vs ${CAPITAL_USD:.0f})")
    if lines:
        print("Abiertas:")
        print("\n".join(lines))
    waiting = []
    for mint, w in state["wants"].items():
        pos = state["positions"].get(mint)
        need = min(target_usd(w["cum"]) - (pos["cost_usd"] if pos else 0.0),
                   MAX_PER_TRADER_USD - used_by_trader(state, w["trader"]))
        if need >= 1:
            waiting.append(f"   {mint[:6]}.. ({w['trader']}) falta ${need:.2f}")
    if waiting:
        print("En espera (sin saldo o precio lejos):")
        print("\n".join(waiting))
    if STATS:
        print("Actividad vista en este turno:")
        for n, a in STATS.items():
            print(f"   {n:<13} {a['txs']} tx, {a['swaps']} swaps, {a['buys']} compras "
                  f"({a['big_buys']} de >=${MIN_TRADER_BUY_USD:,.0f}) | ultimo swap: {a['last'] or '-'}")
        print(f"   Consultas RPC fallidas: {RPC_FAILS[0]}")
    by = {}
    for c in state["closed"]:
        d = by.setdefault(c["trader"], {"n": 0, "w": 0, "pnl": 0.0})
        d["n"] += 1
        d["w"] += 1 if c["pnl"] > 0 else 0
        d["pnl"] += c["pnl"]
    if by:
        print("Cerradas por trader:")
        for t, d in by.items():
            print(f"   {t:<10} {d['n']} ops, {d['w']} ganadoras, PnL ${d['pnl']:+.2f}")
    print("=" * 60 + "\n", flush=True)
 
 
# ------------------------- Bucle principal -------------------------
def validate_wallets():
    ok = {}
    for name, w in WALLETS.items():
        res = rpc("getAccountInfo", [w, {"encoding": "jsonParsed"}])
        val = (res or {}).get("value")
        if val is None:
            log(f"AVISO {name}: la direccion no existe o no tiene SOL; revisa que sea la wallet duena")
        elif val.get("owner") != SYSTEM_PROGRAM:
            log(f"AVISO {name}: parece una cuenta de token/programa, no una wallet. Se omite.")
            continue
        ok[name] = w
    return ok
 
 
def poll_wallet(state, name, w):
    params = {"limit": 25, "commitment": "confirmed"}
    last = state["last_sig"].get(w)
    if last:
        params["until"] = last
    sigs = rpc("getSignaturesForAddress", [w, params])
    if not sigs:
        return
    state["last_sig"][w] = sigs[0]["signature"]
    st = STATS.setdefault(name, {"txs": 0, "swaps": 0, "buys": 0, "big_buys": 0, "last": ""})
    for s in reversed(sigs):               # del mas viejo al mas nuevo
        if s.get("err"):
            continue
        st["txs"] += 1
        tx = rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed",
                                                      "maxSupportedTransactionVersion": 0,
                                                      "commitment": "confirmed"}])
        sw, why = parse_swap_ex(tx, w)
        if not sw:
            if not why.startswith("ningun token"):
                log(f"SKIP {name} {s['signature'][:8]}..: {why}")
            continue
        if sw:
            st["swaps"] += 1
            st["last"] = now_iso()
            small = ""
            if sw["side"] == "BUY":
                st["buys"] += 1
                if sw["usd"] >= MIN_TRADER_BUY_USD:
                    st["big_buys"] += 1
                else:
                    small = " (chica: no cuenta)"
            log(f"VISTO {name} {sw['side']} {sw['mint'][:6]}.. ${sw['usd']:,.0f}{small}")
            age = time.time() - (tx.get("blockTime") or time.time())
            if sw["side"] == "BUY" and age > STALE_SECONDS:
                log(f"Compra de {name} de hace {age:.0f}s: demasiado vieja, no se copia")
                continue
            handle_swap(state, name, sw)
 
 
def self_test(wallets):
    """Al arrancar, revisa las ultimas transacciones de cada wallet y dice si las entiende. No opera."""
    log("PRUEBA de deteccion: reviso las ultimas transacciones de cada wallet (no opera)")
    for name, w in wallets.items():
        sigs = rpc("getSignaturesForAddress", [w, {"limit": 8, "commitment": "confirmed"}])
        if not sigs:
            log(f"PRUEBA {name}: no pude leer transacciones (fallo el RPC o la wallet no tiene actividad)")
            continue
        ok = 0
        for sg in sigs:
            if sg.get("err"):
                continue
            tx = rpc("getTransaction", [sg["signature"], {"encoding": "jsonParsed",
                                                           "maxSupportedTransactionVersion": 0,
                                                           "commitment": "confirmed"}])
            age = (time.time() - (tx.get("blockTime") or time.time())) / 60 if tx else 0
            sw, why = parse_swap_ex(tx, w)
            if sw:
                ok += 1
                log(f"PRUEBA {name} hace {age:.0f} min: {sw['side']} {sw['mint'][:6]}.. ${sw['usd']:,.0f}")
            else:
                log(f"PRUEBA {name} hace {age:.0f} min: no es un swap claro -> {why}")
        log(f"PRUEBA {name}: {ok} swaps reconocidos de {len(sigs)} transacciones")
 
 
def main():
    if "--reset" in sys.argv:
        for f in (STATE_FILE, TRADES_CSV):
            if os.path.exists(f):
                os.remove(f)
        log("Estado reiniciado.")
        return
    state = load_state()
    if "--resumen" in sys.argv:
        summary(state)
        return
 
    if not HELIUS_API_KEY:
        log("AVISO: sin HELIUS_API_KEY usas el RPC publico, que suele dar errores 429.")
    wallets = validate_wallets()
    for name, w in wallets.items():          # al iniciar, ignora el historial viejo
        if w not in state["last_sig"]:
            sigs = rpc("getSignaturesForAddress", [w, {"limit": 1}])
            state["last_sig"][w] = sigs[0]["signature"] if sigs else None
    save_state(state)
    log(f"Vigilando {len(wallets)} wallets. Capital simulado ${state['cash']:.2f}. Ctrl+C para parar.")
    self_test(wallets)
 
    cycle = 0
    last_fill = 0.0
    start = time.time()
    try:
        while True:
            if MAX_RUN_MINUTES and time.time() - start > MAX_RUN_MINUTES * 60:
                log("Tiempo maximo de ejecucion alcanzado; guardando estado y saliendo.")
                break
            for name, w in wallets.items():
                try:
                    poll_wallet(state, name, w)
                except Exception as e:
                    log(f"Error con {name}: {e}")
            check_risk(state)
            if time.time() - last_fill > FILL_CHECK_SECONDS:
                fill_waiting(state)
                last_fill = time.time()
            save_state(state)
            cycle += 1
            if cycle % SUMMARY_EVERY == 0:
                summary(state)
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        save_state(state)
        summary(state)
 
 
if __name__ == "__main__":
    main()
