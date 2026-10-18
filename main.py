from fastapi import FastAPI, HTTPException, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import date, datetime, timedelta
import sqlite3
import hashlib
import csv
import io

app = FastAPI(title="AlphaTrade Pro")
DB_FILE = "trades.db"

# --- 1. Database Setup ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            license_key TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            trade_type TEXT NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL NOT NULL,
            quantity INTEGER NOT NULL,
            stop_loss REAL NOT NULL,
            mistake_tag TEXT,
            notes TEXT,
            chart_link TEXT,
            trade_date TEXT,
            net_pnl REAL,
            risk_reward_ratio REAL,
            status TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    try:
        cursor.execute("ALTER TABLE trades ADD COLUMN notes TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE trades ADD COLUMN chart_link TEXT")
    except sqlite3.OperationalError:
        pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            max_daily_loss REAL DEFAULT 2000.0,
            risk_per_trade REAL DEFAULT 500.0,
            max_consecutive_losses INTEGER DEFAULT 2,
            cooldown_until TEXT DEFAULT NULL,
            broker_name TEXT DEFAULT 'Simulated (Dhan/Zerodha API)',
            broker_shield_enabled INTEGER DEFAULT 1,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    try:
        cursor.execute("ALTER TABLE user_settings ADD COLUMN cooldown_until TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE user_settings ADD COLUMN broker_name TEXT DEFAULT 'Simulated (Dhan/Zerodha API)'")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE user_settings ADD COLUMN broker_shield_enabled INTEGER DEFAULT 1")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()

init_db()

def hash_pass(pwd: str) -> str:
    return hashlib.sha256(pwd.encode()).hexdigest()

# --- 2. Pydantic Models ---
class TradeInput(BaseModel):
    user_id: int
    symbol: str
    trade_type: str
    entry_price: float
    exit_price: float
    quantity: int
    stop_loss: float
    mistake_tag: Optional[str] = "None"
    notes: Optional[str] = ""
    chart_link: Optional[str] = ""

class SettingsInput(BaseModel):
    max_daily_loss: float
    risk_per_trade: float
    max_consecutive_losses: int
    broker_name: str
    broker_shield_enabled: bool

# --- 3. Dynamic Risk Engine ---
def evaluate_user_risk(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT max_daily_loss, max_consecutive_losses, cooldown_until, broker_name, broker_shield_enabled 
        FROM user_settings WHERE user_id = ?
    """, (user_id,))
    sett = cursor.fetchone()
    conn.close()

    max_daily_loss = sett[0] if sett else 2000.0
    max_consecutive_losses = sett[1] if sett else 2
    cooldown_until = sett[2] if sett else None
    broker_name = sett[3] if sett else "Simulated Broker"
    broker_shield = bool(sett[4]) if sett else True

    if cooldown_until:
        try:
            until_time = datetime.fromisoformat(cooldown_until)
            if datetime.now() < until_time:
                remaining_sec = int((until_time - datetime.now()).total_seconds())
                return {
                    "status": "VAULT_LOCKED",
                    "tilt_level": "Red",
                    "is_locked": True,
                    "is_vault_active": True,
                    "remaining_seconds": remaining_sec,
                    "daily_pnl": 0.0,
                    "max_daily_loss": max_daily_loss,
                    "drawdown_pct": 100.0,
                    "broker_shield": broker_shield,
                    "message": "🔒 इमरजेंसी कूलडाउन सक्रिय: पूंजी सुरक्षा के लिए ट्रेडिंग लॉक कर दी गई है।"
                }
        except Exception:
            pass

    today_str = str(date.today())
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT net_pnl, status FROM trades 
        WHERE user_id = ? AND trade_date = ? 
        ORDER BY id ASC
    """, (user_id, today_str))
    today_trades = [dict(r) for r in cursor.fetchall()]
    conn.close()

    daily_pnl = sum(t["net_pnl"] for t in today_trades)
    
    consecutive_losses = 0
    for t in reversed(today_trades):
        if t["status"] == "LOSS":
            consecutive_losses += 1
        elif t["status"] == "WIN":
            break

    used_loss = abs(daily_pnl) if daily_pnl < 0 else 0.0
    drawdown_pct = min(round((used_loss / max_daily_loss) * 100, 1), 100.0) if max_daily_loss > 0 else 0.0

    if daily_pnl <= -abs(max_daily_loss):
        return {
            "status": "LOCKED",
            "tilt_level": "Red",
            "is_locked": True,
            "is_vault_active": False,
            "remaining_seconds": 0,
            "daily_pnl": round(daily_pnl, 2),
            "consecutive_losses": consecutive_losses,
            "max_daily_loss": max_daily_loss,
            "drawdown_pct": drawdown_pct,
            "broker_shield": broker_shield,
            "message": f"🚨 ब्रोकर-शील्ड सक्रिय: दैनिक सीमा (₹{abs(max_daily_loss)}) पार! ट्रेडिंग टर्मिनल लॉक कर दिया गया है।"
        }
    
    if consecutive_losses >= max_consecutive_losses:
        return {
            "status": "TILT",
            "tilt_level": "Yellow",
            "is_locked": False,
            "is_vault_active": False,
            "remaining_seconds": 0,
            "daily_pnl": round(daily_pnl, 2),
            "consecutive_losses": consecutive_losses,
            "max_daily_loss": max_daily_loss,
            "drawdown_pct": drawdown_pct,
            "broker_shield": broker_shield,
            "message": f"⚠️ टिल्ट चेतावनी: लगातार {consecutive_losses} नुकसान! क्वांटिटी तुरंत 50% घटाएं।"
        }

    return {
        "status": "SAFE",
        "tilt_level": "Green",
        "is_locked": False,
        "is_vault_active": False,
        "remaining_seconds": 0,
        "daily_pnl": round(daily_pnl, 2),
        "consecutive_losses": consecutive_losses,
        "max_daily_loss": max_daily_loss,
        "drawdown_pct": drawdown_pct,
        "broker_shield": broker_shield,
        "message": "अनुशासन बनाए रखें। आज का रिस्क सामान्य स्तर पर है।"
    }

# --- 4. AI Edge-Audit ---
def audit_user_edge(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM trades WHERE user_id = ?", (user_id,))
    trades = [dict(r) for r in cursor.fetchall()]
    conn.close()

    if not trades or len(trades) < 2:
        return {"has_data": False, "message": "AI ऑडिट शुरू करने के लिए कम से कम 2 ट्रेड्स दर्ज करें।"}

    total_trades = len(trades)
    wins = [t for t in trades if t["status"] == "WIN"]
    losses = [t for t in trades if t["status"] == "LOSS"]
    net_pnl = sum(t["net_pnl"] for t in trades)
    win_rate = (len(wins) / total_trades) * 100

    mistake_loss_map = {}
    for t in losses:
        m = t.get("mistake_tag") or "None"
        if m != "None":
            mistake_loss_map[m] = mistake_loss_map.get(m, 0.0) + abs(t["net_pnl"])

    top_mistake = None
    top_mistake_amt = 0.0
    if mistake_loss_map:
        top_mistake = max(mistake_loss_map, key=mistake_loss_map.get)
        top_mistake_amt = round(mistake_loss_map[top_mistake], 2)

    total_mistake_loss = sum(mistake_loss_map.values())
    pnl_without_mistakes = round(net_pnl + total_mistake_loss, 2)

    buy_trades = [t for t in trades if t["trade_type"] == "BUY"]
    sell_trades = [t for t in trades if t["trade_type"] == "SELL"]
    buy_wins = len([t for t in buy_trades if t["status"] == "WIN"])
    sell_wins = len([t for t in sell_trades if t["status"] == "WIN"])
    buy_winrate = round((buy_wins / len(buy_trades)) * 100, 1) if buy_trades else 0
    sell_winrate = round((sell_wins / len(sell_trades)) * 100, 1) if sell_trades else 0

    best_type = "BUY" if buy_winrate >= sell_winrate else "SELL"
    best_rate = max(buy_winrate, sell_winrate)

    symbol_pnl = {}
    for t in trades:
        symbol_pnl[t["symbol"]] = symbol_pnl.get(t["symbol"], 0.0) + t["net_pnl"]
    best_symbol = max(symbol_pnl, key=symbol_pnl.get) if symbol_pnl else "N/A"

    avg_win = (sum(t["net_pnl"] for t in wins) / len(wins)) if wins else 0
    avg_loss = (abs(sum(t["net_pnl"] for t in losses)) / len(losses)) if losses else 1
    profit_factor = round(avg_win / avg_loss, 2) if avg_loss > 0 else 0

    grade = "F"
    if win_rate >= 55 and profit_factor >= 1.5:
        grade = "A+"
    elif win_rate >= 45 and profit_factor >= 1.2:
        grade = "B"
    elif win_rate >= 40:
        grade = "C"

    return {
        "has_data": True,
        "grade": grade,
        "top_mistake": top_mistake,
        "top_mistake_amt": top_mistake_amt,
        "pnl_without_mistakes": pnl_without_mistakes,
        "best_type": best_type,
        "best_rate": best_rate,
        "best_symbol": best_symbol,
        "profit_factor": profit_factor,
        "win_rate": round(win_rate, 1)
    }

# --- 5. Authentication & Endpoints ---
@app.post("/register")
def register(username: str = Form(...), password: str = Form(...), license_key: str = Form(...)):
    VALID_KEYS = ["ALPHA-VIP-2026", "PRO-TRADER-99", "FREE-TRIAL-01"]
    if license_key not in VALID_KEYS:
        return HTMLResponse("<script>alert('गलत लाइसेंस की!'); window.history.back();</script>")
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users (username, password_hash, license_key) VALUES (?, ?, ?)",
                       (username.lower().strip(), hash_pass(password), license_key))
        user_id = cursor.lastrowid
        cursor.execute("""
            INSERT INTO user_settings (user_id, max_daily_loss, risk_per_trade, max_consecutive_losses, broker_name, broker_shield_enabled) 
            VALUES (?, 2000.0, 500.0, 2, 'Simulated (Dhan/Zerodha API)', 1)
        """, (user_id,))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return HTMLResponse("<script>alert('यह यूज़रनेम पहले से मौजूद है!'); window.history.back();</script>")
    conn.close()
    return HTMLResponse("<script>alert('रजिस्ट्रेशन सफल! अब लॉगिन करें।'); window.location.href='/login';</script>")

@app.post("/api/login")
def login(username: str = Form(...), password: str = Form(...)):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ? AND password_hash = ?", 
                   (username.lower().strip(), hash_pass(password)))
    user = cursor.fetchone()
    conn.close()
    if not user:
        return HTMLResponse("<script>alert('गलत यूज़रनेम या पासवर्ड!'); window.history.back();</script>")
    
    response = HTMLResponse("<script>window.location.href='/';</script>")
    response.set_cookie(key="user_id", value=str(user[0]))
    return response

@app.post("/api/trigger-cooldown")
def trigger_cooldown(request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    lock_time = datetime.now() + timedelta(minutes=60)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE user_settings SET cooldown_until = ? WHERE user_id = ?", (lock_time.isoformat(), user_id))
    conn.commit()
    conn.close()
    return {"message": "Cooldown activated"}

@app.get("/api/settings")
def get_settings(request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id:
        return {"max_daily_loss": 2000.0, "risk_per_trade": 500.0, "max_consecutive_losses": 2, "broker_name": "Simulated Broker", "broker_shield_enabled": True}
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT max_daily_loss, risk_per_trade, max_consecutive_losses, broker_name, broker_shield_enabled FROM user_settings WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return {"max_daily_loss": 2000.0, "risk_per_trade": 500.0, "max_consecutive_losses": 2, "broker_name": "Simulated Broker", "broker_shield_enabled": True}
    return {
        "max_daily_loss": row[0],
        "risk_per_trade": row[1],
        "max_consecutive_losses": row[2],
        "broker_name": row[3] or "Simulated Broker",
        "broker_shield_enabled": bool(row[4])
    }

@app.post("/api/settings")
def save_settings(settings: SettingsInput, request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO user_settings (user_id, max_daily_loss, risk_per_trade, max_consecutive_losses, broker_name, broker_shield_enabled)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            max_daily_loss=excluded.max_daily_loss,
            risk_per_trade=excluded.risk_per_trade,
            max_consecutive_losses=excluded.max_consecutive_losses,
            broker_name=excluded.broker_name,
            broker_shield_enabled=excluded.broker_shield_enabled
    """, (int(user_id), settings.max_daily_loss, settings.risk_per_trade, settings.max_consecutive_losses, settings.broker_name, 1 if settings.broker_shield_enabled else 0))
    conn.commit()
    conn.close()
    return {"message": "Settings saved"}

@app.get("/api/risk-status")
def get_risk_status(request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id:
        return {"status": "UNKNOWN", "is_locked": False}
    return evaluate_user_risk(int(user_id))

@app.get("/api/audit")
def get_audit(request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id:
        return {"has_data": False}
    return audit_user_edge(int(user_id))

@app.post("/trades/")
def add_trade(trade: TradeInput):
    risk = evaluate_user_risk(trade.user_id)
    if risk["is_locked"]:
        raise HTTPException(status_code=403, detail="Trading is locked due to safety safeguards!")

    trade_type = trade.trade_type.upper()
    if trade_type == "BUY":
        pnl = (trade.exit_price - trade.entry_price) * trade.quantity
        calc_risk = (trade.entry_price - trade.stop_loss) * trade.quantity
    else:
        pnl = (trade.entry_price - trade.exit_price) * trade.quantity
        calc_risk = (trade.stop_loss - trade.entry_price) * trade.quantity

    rr_ratio = round(abs(pnl / calc_risk), 2) if calc_risk > 0 else 0.0
    status = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN")

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO trades (user_id, symbol, trade_type, entry_price, exit_price, quantity, stop_loss, mistake_tag, notes, chart_link, trade_date, net_pnl, risk_reward_ratio, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (trade.user_id, trade.symbol.upper(), trade_type, trade.entry_price, trade.exit_price, trade.quantity, trade.stop_loss, trade.mistake_tag, trade.notes, trade.chart_link, str(date.today()), round(pnl, 2), rr_ratio, status))
    conn.commit()
    conn.close()
    return {"message": "Success"}

@app.get("/trades/")
def get_trades(request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id:
        return []
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM trades WHERE user_id = ? ORDER BY id DESC", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/export/csv")
def export_csv(request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id:
        return RedirectResponse(url="/login")
    
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT trade_date, symbol, trade_type, entry_price, exit_price, quantity, stop_loss, net_pnl, risk_reward_ratio, mistake_tag, notes, chart_link FROM trades WHERE user_id = ? ORDER BY id ASC", (user_id,))
    trades = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Symbol", "Type", "Entry Price", "Exit Price", "Quantity", "Stop Loss", "Net PnL", "R:R", "Mistake Tag", "Notes", "Chart Link"])
    for t in trades:
        writer.writerow([t["trade_date"], t["symbol"], t["trade_type"], t["entry_price"], t["exit_price"], t["quantity"], t["stop_loss"], t["net_pnl"], t["risk_reward_ratio"], t["mistake_tag"], t["notes"], t["chart_link"]])

    output.seek(0)
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=AlphaTrade_Journal_{date.today()}.csv"}
    )

@app.get("/analytics/")
def get_analytics(request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id:
        return {"total_trades": 0, "win_rate_percentage": "0%", "net_pnl": 0, "loss_by_mistakes": {}}
    
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM trades WHERE user_id = ?", (user_id,))
    trades = [dict(r) for r in cursor.fetchall()]
    conn.close()

    if not trades:
        return {"total_trades": 0, "win_rate_percentage": "0%", "net_pnl": 0, "loss_by_mistakes": {}}

    total = len(trades)
    wins = [t for t in trades if t["status"] == "WIN"]
    losses = [t for t in trades if t["status"] == "LOSS"]
    win_rate = round((len(wins) / total) * 100, 1)
    net_pl = sum(t["net_pnl"] for t in trades)

    mistake_loss_map = {}
    for t in losses:
        m = t.get("mistake_tag") or "None"
        if m != "None":
            mistake_loss_map[m] = mistake_loss_map.get(m, 0) + abs(t["net_pnl"])

    return {
        "total_trades": total,
        "win_rate_percentage": f"{win_rate}%",
        "net_pnl": round(net_pl, 2),
        "loss_by_mistakes": mistake_loss_map
    }

# --- 6. UI Dashboard ---
@app.get("/login", response_class=HTMLResponse)
def login_page():
    return """
    <!DOCTYPE html>
    <html>
    <head><title>Login - AlphaTrade</title><script src="https://cdn.tailwindcss.com"></script></head>
    <body class="bg-slate-950 text-slate-100 flex items-center justify-center min-h-screen">
        <div class="bg-slate-900 border border-slate-800 p-8 rounded-2xl w-96 shadow-2xl">
            <h1 class="text-2xl font-bold text-emerald-400 mb-2">AlphaTrade Pro</h1>
            <p class="text-xs text-slate-400 mb-6">अपने ट्रेडिंग अकाउंट में लॉगिन करें</p>
            <form action="/api/login" method="POST" class="space-y-4">
                <div>
                    <label class="text-xs text-slate-400">यूज़रनेम</label>
                    <input type="text" name="username" required class="w-full bg-slate-950 border border-slate-700 p-2.5 rounded-lg text-sm text-white">
                </div>
                <div>
                    <label class="text-xs text-slate-400">पासवर्ड</label>
                    <input type="password" name="password" required class="w-full bg-slate-950 border border-slate-700 p-2.5 rounded-lg text-sm text-white">
                </div>
                <button type="submit" class="w-full bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold py-2.5 rounded-lg transition">लॉगिन करें</button>
            </form>
            <div class="mt-6 text-center text-xs text-slate-400">
                नया अकाउंट है? <a href="/register-page" class="text-emerald-400 hover:underline">यहाँ रजिस्टर करें</a>
            </div>
        </div>
    </body>
    </html>
    """

@app.get("/register-page", response_class=HTMLResponse)
def register_page():
    return """
    <!DOCTYPE html>
    <html>
    <head><title>Register - AlphaTrade</title><script src="https://cdn.tailwindcss.com"></script></head>
    <body class="bg-slate-950 text-slate-100 flex items-center justify-center min-h-screen">
        <div class="bg-slate-900 border border-slate-800 p-8 rounded-2xl w-96 shadow-2xl">
            <h1 class="text-2xl font-bold text-emerald-400 mb-2">AlphaTrade Pro</h1>
            <p class="text-xs text-slate-400 mb-6">नया अकाउंट और लाइसेंस की डालें</p>
            <form action="/register" method="POST" class="space-y-4">
                <div>
                    <label class="text-xs text-slate-400">यूज़रनेम</label>
                    <input type="text" name="username" required class="w-full bg-slate-950 border border-slate-700 p-2.5 rounded-lg text-sm text-white">
                </div>
                <div>
                    <label class="text-xs text-slate-400">पासवर्ड</label>
                    <input type="password" name="password" required class="w-full bg-slate-950 border border-slate-700 p-2.5 rounded-lg text-sm text-white">
                </div>
                <div>
                    <label class="text-xs text-slate-400">लाइसेंस की</label>
                    <input type="text" name="license_key" placeholder="उदा: FREE-TRIAL-01" required class="w-full bg-slate-950 border border-slate-700 p-2.5 rounded-lg text-sm text-white">
                    <p class="text-[10px] text-slate-500 mt-1">टेस्टिंग की: FREE-TRIAL-01 या ALPHA-VIP-2026</p>
                </div>
                <button type="submit" class="w-full bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold py-2.5 rounded-lg transition">रजिस्टर करें</button>
            </form>
            <div class="mt-4 text-center text-xs text-slate-400">
                पहले से अकाउंट है? <a href="/login" class="text-emerald-400 hover:underline">लॉगिन करें</a>
            </div>
        </div>
    </body>
    </html>
    """

@app.get("/logout")
def logout():
    response = HTMLResponse("<script>window.location.href='/login';</script>")
    response.delete_cookie("user_id")
    return response

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id:
        return RedirectResponse(url="/login")

    return f"""
    <!DOCTYPE html>
    <html lang="hi">
    <head>
        <meta charset="UTF-8">
        <title>AlphaTrade Pro</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    </head>
    <body class="bg-slate-950 text-slate-100 font-sans min-h-screen p-6 relative">

        <!-- UNBREAKABLE EMERGENCY VAULT OVERLAY -->
        <div id="vaultOverlay" class="hidden fixed inset-0 z-50 bg-slate-950/95 backdrop-blur-xl flex flex-col items-center justify-center p-6 text-center">
            <div class="max-w-md w-full bg-slate-900 border border-rose-500/50 p-8 rounded-3xl shadow-2xl space-y-6">
                <div class="w-16 h-16 bg-rose-500/20 text-rose-500 rounded-2xl flex items-center justify-center mx-auto text-3xl animate-bounce">
                    🔒
                </div>
                <div>
                    <h2 class="text-2xl font-black text-rose-400 tracking-wide uppercase">Psychology Vault Active</h2>
                    <p class="text-xs text-slate-400 mt-1">ट्रेडिंग अकाउंट सुरक्षा के लिए लॉक कर दिया गया है।</p>
                </div>
                <div class="p-4 bg-slate-950 rounded-2xl border border-slate-800">
                    <span class="text-xs text-slate-500 block uppercase font-semibold">शेष कूलडाउन समय</span>
                    <span id="countdownTimer" class="text-4xl font-mono font-bold text-white mt-1 block">00:00</span>
                </div>
                <div class="text-left text-xs text-slate-400 space-y-2 bg-rose-950/20 p-4 rounded-xl border border-rose-500/20">
                    <p class="font-bold text-rose-300">💡 रीसेट नियम:</p>
                    <p>• मार्केट कल भी खुला रहेगा, पर कैपिटल खत्म हुआ तो नहीं।</p>
                    <p>• एक छोटा नुकसान सामान्य है, रिवेंज ट्रेडिंग से अकाउंट खाली होता है।</p>
                    <p>• स्क्रीन छोड़ें और गहरी सांस लें।</p>
                </div>
            </div>
        </div>

        <div class="max-w-6xl mx-auto space-y-5">
            <!-- TOP HEADER -->
            <div class="flex justify-between items-center border-b border-slate-800 pb-4">
                <div>
                    <h1 class="text-2xl font-bold text-emerald-400">⚡ AlphaTrade Pro</h1>
                    <p class="text-sm text-slate-400">प्रोफेशनल ट्रेडिंग जर्नल, AI ऑडिट व कस्टम मार्केट पल्स</p>
                </div>
                <div class="flex items-center gap-3">
                    <div id="brokerShieldBadge" class="flex items-center gap-1.5 px-3 py-1.5 bg-indigo-500/10 text-indigo-300 border border-indigo-500/30 rounded-lg text-xs font-semibold">
                        <span class="w-2 h-2 rounded-full bg-indigo-400 animate-ping"></span>
                        Broker-Shield: Armed
                    </div>
                    <button onclick="toggleMarketModal(true)" class="text-xs bg-slate-800 hover:bg-slate-700 px-3 py-1.5 rounded-lg border border-slate-700 transition flex items-center gap-1.5 text-amber-300">
                        <span>📌</span> मार्केट्स व स्टॉक्स चुनें
                    </button>
                    <button onclick="triggerEmergencyBreak()" class="text-xs bg-rose-500/10 hover:bg-rose-500/20 text-rose-400 px-3 py-1.5 rounded-lg border border-rose-500/30 transition flex items-center gap-1.5">
                        <span>🛑</span> 1-Hr Cooldown
                    </button>
                    <a href="/export/csv" class="text-xs bg-slate-800 hover:bg-slate-700 px-3 py-1.5 rounded-lg border border-slate-700 transition flex items-center gap-1.5 text-slate-200">
                        <span>📥</span> CSV डाउनलोड
                    </a>
                    <button onclick="toggleSettingsModal(true)" class="text-xs bg-slate-800 hover:bg-slate-700 px-3 py-1.5 rounded-lg border border-slate-700 transition">⚙️ रिस्क सेटिंग्स</button>
                    <span class="px-3 py-1 bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 rounded-full text-xs font-semibold">लाइसेंस एक्टिव</span>
                    <a href="/logout" class="text-xs bg-rose-500/20 text-rose-400 hover:bg-rose-500/30 px-3 py-1.5 rounded-lg border border-rose-500/30 transition">लॉगआउट</a>
                </div>
            </div>

            <!-- CUSTOMIZABLE LIVE MARKET PULSE & SENTIMENT STRIP -->
            <div class="bg-slate-900 border border-slate-800 p-3 rounded-xl shadow-lg flex items-center justify-between gap-4">
                <div class="flex items-center gap-3 overflow-x-auto pb-1 flex-1" id="marketTickerContainer">
                    <!-- Tickers dynamically injected here -->
                </div>
                <!-- SMART MARKET SENTIMENT GAUGE -->
                <div class="hidden md:flex items-center gap-2 px-3 py-1.5 bg-slate-950 rounded-lg border border-slate-800 flex-shrink-0">
                    <span class="text-[10px] uppercase font-semibold text-slate-400">Market Bias:</span>
                    <span id="marketBiasBadge" class="text-xs font-bold text-emerald-400 flex items-center gap-1">
                        <span class="w-2 h-2 rounded-full bg-emerald-400"></span> Bullish
                    </span>
                </div>
            </div>

            <!-- TILT-METER & DRAWDOWN PROGRESS BAR -->
            <div id="riskBanner" class="p-4 rounded-xl border space-y-3 transition-all bg-emerald-950/40 border-emerald-500/30 text-emerald-300">
                <div class="flex items-center justify-between">
                    <div class="flex items-center gap-3">
                        <span id="riskIndicatorDot" class="w-3.5 h-3.5 rounded-full bg-emerald-400 animate-pulse"></span>
                        <div>
                            <h4 id="riskTitle" class="font-bold text-sm tracking-wide uppercase">टिल्ट-मीटर: सेफ मोड</h4>
                            <p id="riskDesc" class="text-xs text-slate-300 mt-0.5">अनुशासन बनाए रखें। आज का रिस्क सामान्य स्तर पर है।</p>
                        </div>
                    </div>
                    <div class="text-right">
                        <span class="text-[10px] uppercase text-slate-400 block font-semibold">आज का P&L</span>
                        <span id="dailyPnlVal" class="text-base font-bold text-white">₹0.00</span>
                    </div>
                </div>
                <div>
                    <div class="flex justify-between text-[11px] text-slate-400 mb-1">
                        <span>डेली ड्रॉडाउन यूसेज</span>
                        <span id="drawdownLabel" class="font-semibold text-slate-300">0% (₹0 / ₹2000)</span>
                    </div>
                    <div class="w-full bg-slate-900 rounded-full h-2 overflow-hidden border border-slate-800">
                        <div id="drawdownBar" class="bg-emerald-500 h-full transition-all duration-500" style="width: 0%"></div>
                    </div>
                </div>
            </div>

            <!-- AI EDGE-AUDIT & LEAK DETECTOR -->
            <div class="bg-gradient-to-r from-slate-900 via-indigo-950/40 to-slate-900 border border-indigo-500/30 p-5 rounded-2xl shadow-xl space-y-4">
                <div class="flex justify-between items-center border-b border-indigo-500/20 pb-3">
                    <div class="flex items-center gap-2.5">
                        <span class="text-xl">🧠</span>
                        <div>
                            <h3 class="font-bold text-base text-indigo-300 tracking-wide">AI Edge-Audit & Leak Detector</h3>
                            <p class="text-[11px] text-slate-400">स्वचालित व्यवहार और लाभप्रदता विश्लेषण</p>
                        </div>
                    </div>
                    <div class="flex items-center gap-2">
                        <span class="text-xs text-slate-400">एग्जीक्यूशन ग्रेड:</span>
                        <span id="auditGrade" class="px-3 py-0.5 bg-indigo-500/20 text-indigo-300 font-black rounded-lg border border-indigo-500/40 text-sm">--</span>
                    </div>
                </div>
                <div class="grid grid-cols-1 md:grid-cols-3 gap-4 text-xs">
                    <div class="bg-slate-950/60 border border-slate-800 p-3.5 rounded-xl space-y-1">
                        <p class="text-slate-400 font-semibold uppercase text-[10px] flex items-center gap-1.5">
                            <span class="text-rose-400 text-xs">🩸</span> सबसे बड़ा लीकेज (Biggest Leak)
                        </p>
                        <p id="leakDesc" class="text-white font-medium text-xs mt-1">ट्रेड हिस्ट्री का विश्लेषण हो रहा है...</p>
                        <p id="leakSavings" class="text-[11px] text-emerald-400 pt-0.5 font-semibold"></p>
                    </div>
                    <div class="bg-slate-950/60 border border-slate-800 p-3.5 rounded-xl space-y-1">
                        <p class="text-slate-400 font-semibold uppercase text-[10px] flex items-center gap-1.5">
                            <span class="text-amber-400 text-xs">🎯</span> आपकी असली Edge
                        </p>
                        <p id="edgeDesc" class="text-white font-medium text-xs mt-1">पैटर्न निकाले जा रहे हैं...</p>
                        <p id="edgeStats" class="text-[11px] text-indigo-300 pt-0.5 font-semibold"></p>
                    </div>
                    <div class="bg-slate-950/60 border border-slate-800 p-3.5 rounded-xl space-y-1">
                        <p class="text-slate-400 font-semibold uppercase text-[10px] flex items-center gap-1.5">
                            <span class="text-emerald-400 text-xs">⚖️</span> प्रॉफिट फैक्टर व दक्षता
                        </p>
                        <p id="ratioDesc" class="text-white font-medium text-xs mt-1">गणना हो रही है...</p>
                        <p id="winrateStat" class="text-[11px] text-slate-300 pt-0.5"></p>
                    </div>
                </div>
            </div>

            <!-- स्टैट्स -->
            <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
                <div class="bg-slate-900 border border-slate-800 p-4 rounded-xl">
                    <p class="text-xs text-slate-400 uppercase">कुल ट्रेड्स</p>
                    <p id="totalTrades" class="text-2xl font-bold text-white mt-1">0</p>
                </div>
                <div class="bg-slate-900 border border-slate-800 p-4 rounded-xl">
                    <p class="text-xs text-slate-400 uppercase">विन रेट</p>
                    <p id="winRate" class="text-2xl font-bold text-emerald-400 mt-1">0%</p>
                </div>
                <div class="bg-slate-900 border border-slate-800 p-4 rounded-xl">
                    <p class="text-xs text-slate-400 uppercase">नेट P&L</p>
                    <p id="netPnl" class="text-2xl font-bold text-white mt-1">₹0</p>
                </div>
                <div class="bg-slate-900 border border-slate-800 p-4 rounded-xl">
                    <p class="text-xs text-slate-400 uppercase">गलतियों से नुकसान</p>
                    <p id="mistakeLoss" class="text-2xl font-bold text-rose-400 mt-1">₹0</p>
                </div>
            </div>

            <!-- चार्ट्स -->
            <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
                <div class="bg-slate-900 border border-slate-800 p-4 rounded-xl md:col-span-2">
                    <h3 class="text-sm font-semibold text-slate-300 mb-2">ग्रोथ कर्व (Cumulative P&L)</h3>
                    <div class="h-60"><canvas id="pnlChart"></canvas></div>
                </div>
                <div class="bg-slate-900 border border-slate-800 p-4 rounded-xl">
                    <h3 class="text-sm font-semibold text-slate-300 mb-2">गलतियों का ब्रेकडाउन</h3>
                    <div class="h-60 flex items-center justify-center"><canvas id="mistakeChart"></canvas></div>
                </div>
            </div>

            <!-- फॉर्म और टेबल -->
            <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
                <div class="bg-slate-900 border border-slate-800 p-5 rounded-xl">
                    <div class="flex justify-between items-center mb-4">
                        <h2 class="text-lg font-semibold text-slate-200">ट्रेड दर्ज करें</h2>
                        <span class="text-[10px] bg-emerald-500/20 text-emerald-300 px-2 py-0.5 rounded border border-emerald-500/30">⚡ ऑटो साइज़िंग ऑन</span>
                    </div>
                    <form id="tradeForm" class="space-y-3" onsubmit="openChecklist(event)">
                        <div>
                            <label class="block text-xs text-slate-400 mb-1">सिंबल</label>
                            <input type="text" id="symbol" placeholder="उदा. NIFTY" required class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2 text-sm text-white">
                        </div>
                        <div class="grid grid-cols-2 gap-2">
                            <div>
                                <label class="block text-xs text-slate-400 mb-1">टाइप</label>
                                <select id="trade_type" class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2 text-sm text-white">
                                    <option value="BUY">BUY</option>
                                    <option value="SELL">SELL</option>
                                </select>
                            </div>
                            <div>
                                <label class="block text-xs text-slate-400 mb-1">क्वांटिटी (Auto)</label>
                                <input type="number" id="quantity" value="50" required class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2 text-sm text-emerald-400 font-bold">
                            </div>
                        </div>
                        <div class="grid grid-cols-3 gap-2">
                            <div>
                                <label class="block text-xs text-slate-400 mb-1">एंट्री</label>
                                <input type="number" step="0.1" id="entry_price" required class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2 text-sm text-white">
                            </div>
                            <div>
                                <label class="block text-xs text-slate-400 mb-1">SL</label>
                                <input type="number" step="0.1" id="stop_loss" required class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2 text-sm text-white">
                            </div>
                            <div>
                                <label class="block text-xs text-slate-400 mb-1">एग्जिट</label>
                                <input type="number" step="0.1" id="exit_price" required class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2 text-sm text-white">
                            </div>
                        </div>
                        <div>
                            <label class="block text-xs text-slate-400 mb-1">गलती टैग</label>
                            <select id="mistake_tag" class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2 text-sm text-white">
                                <option value="None">Rules Followed</option>
                                <option value="FOMO">FOMO</option>
                                <option value="Revenge Trading">Revenge Trading</option>
                                <option value="No SL">No SL</option>
                            </select>
                        </div>
                        <div>
                            <label class="block text-xs text-slate-400 mb-1">ट्रेड नोट्स (सेटअप / कारण)</label>
                            <input type="text" id="notes" placeholder="उदा. ब्रेकआउट रीटेस्ट पर एंट्री ली" class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2 text-sm text-white">
                        </div>
                        <div>
                            <label class="block text-xs text-slate-400 mb-1">चार्ट स्क्रीनशॉट लिंक</label>
                            <input type="url" id="chart_link" placeholder="उदा. https://www.tradingview.com/x/..." class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2 text-sm text-white">
                        </div>
                        <button id="submitBtn" type="submit" class="w-full bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold py-2 rounded-lg mt-2 transition">ट्रेड जोड़ें</button>
                    </form>
                </div>

                <div class="bg-slate-900 border border-slate-800 p-5 rounded-xl lg:col-span-2 overflow-x-auto">
                    <h2 class="text-lg font-semibold mb-4 text-slate-200">आपका ट्रेड इतिहास</h2>
                    <table class="w-full text-left text-sm">
                        <thead class="text-xs text-slate-400 border-b border-slate-800">
                            <tr>
                                <th class="pb-3">तारीख</th>
                                <th class="pb-3">सिंबल</th>
                                <th class="pb-3">टाइप</th>
                                <th class="pb-3">नेट P&L</th>
                                <th class="pb-3">R:R</th>
                                <th class="pb-3">गलती</th>
                                <th class="pb-3">नोट्स</th>
                                <th class="pb-3">चार्ट</th>
                            </tr>
                        </thead>
                        <tbody id="tradesTable" class="divide-y border-slate-800"></tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- FULLY CUSTOMIZABLE MARKETS & STOCKS MODAL -->
        <div id="marketModal" class="hidden fixed inset-0 bg-slate-950/80 backdrop-blur-sm flex items-center justify-center p-4 z-50">
            <div class="bg-slate-900 border border-slate-800 p-6 rounded-2xl w-full max-w-lg shadow-2xl space-y-4">
                <div class="flex justify-between items-center border-b border-slate-800 pb-3">
                    <div>
                        <h3 class="font-bold text-lg text-white">📌 मार्केट पल्स टिकर्स व स्टॉक्स</h3>
                        <p class="text-xs text-slate-400">कोई भी स्टॉक/क्रिप्टो जोड़ें या टिक करें:</p>
                    </div>
                    <button onclick="toggleMarketModal(false)" class="text-slate-400 hover:text-white">✕</button>
                </div>

                <!-- NEW CUSTOM TICKER ADD BOX -->
                <div class="flex gap-2">
                    <input type="text" id="customTickerInput" placeholder="कोई भी स्टॉक/क्रिप्टो लिखें (उदा. RELIANCE, ETH, TATAMOTORS)" class="flex-1 bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white uppercase focus:outline-none focus:border-amber-400">
                    <button onclick="addCustomMarketTicker()" type="button" class="bg-amber-500 hover:bg-amber-600 text-slate-950 font-bold px-4 py-2 rounded-lg text-xs transition">+ जोड़ें</button>
                </div>
                
                <div class="space-y-3 text-xs max-h-72 overflow-y-auto pr-1">
                    <p class="text-amber-400 font-bold uppercase tracking-wider text-[11px]">आपके कस्टम जोड़े गए स्टॉक्स</p>
                    <div class="grid grid-cols-2 gap-2" id="customMarketsList"></div>

                    <p class="text-indigo-400 font-bold uppercase tracking-wider text-[11px] pt-2">भारतीय सूचकांक (Indian Indices)</p>
                    <div class="grid grid-cols-2 gap-2" id="indianMarketsList"></div>

                    <p class="text-indigo-400 font-bold uppercase tracking-wider text-[11px] pt-2">ग्लोबल मार्केट्स (Global Cues)</p>
                    <div class="grid grid-cols-2 gap-2" id="globalMarketsList"></div>

                    <p class="text-indigo-400 font-bold uppercase tracking-wider text-[11px] pt-2">कमोडिटीज व क्रिप्टो</p>
                    <div class="grid grid-cols-2 gap-2" id="cryptoMarketsList"></div>
                </div>

                <div class="pt-2 border-t border-slate-800 flex justify-end gap-2">
                    <button onclick="saveMarketSelections()" class="w-full bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold py-2 rounded-lg transition">सेव करें और लागू करें</button>
                </div>
            </div>
        </div>

        <!-- PRE-TRADE CHECKLIST MODAL -->
        <div id="checklistModal" class="hidden fixed inset-0 bg-slate-950/80 backdrop-blur-sm flex items-center justify-center p-4 z-50">
            <div class="bg-slate-900 border border-slate-800 p-6 rounded-2xl w-full max-w-md shadow-2xl">
                <div class="flex justify-between items-center mb-3">
                    <h3 class="font-bold text-lg text-emerald-400">🛡️ प्री-ट्रेड रूल्स चेकलिस्ट</h3>
                    <button onclick="toggleChecklistModal(false)" class="text-slate-400 hover:text-white">✕</button>
                </div>
                <div class="space-y-3 text-sm">
                    <label class="flex items-center gap-3 p-3 bg-slate-950 rounded-lg border border-slate-800 cursor-pointer">
                        <input type="checkbox" id="check_rule1" class="w-4 h-4 accent-emerald-500">
                        <span>यह ट्रेड मेरी तय रणनीति का हिस्सा है।</span>
                    </label>
                    <label class="flex items-center gap-3 p-3 bg-slate-950 rounded-lg border border-slate-800 cursor-pointer">
                        <input type="checkbox" id="check_rule2" class="w-4 h-4 accent-emerald-500">
                        <span>स्टॉप-लॉस तय है और इसे पीछे नहीं हटाऊँगा।</span>
                    </label>
                    <label class="flex items-center gap-3 p-3 bg-slate-950 rounded-lg border border-slate-800 cursor-pointer">
                        <input type="checkbox" id="check_rule3" class="w-4 h-4 accent-emerald-500">
                        <span>मैं FOMO या रिवेंज में ट्रेड नहीं ले रहा हूँ।</span>
                    </label>
                    <button onclick="confirmAndExecuteTrade()" class="w-full bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold py-2.5 rounded-lg transition mt-3">पुष्टि करें और ट्रेड दर्ज करें</button>
                </div>
            </div>
        </div>

        <!-- SETTINGS MODAL -->
        <div id="settingsModal" class="hidden fixed inset-0 bg-slate-950/80 backdrop-blur-sm flex items-center justify-center p-4 z-50">
            <div class="bg-slate-900 border border-slate-800 p-6 rounded-2xl w-full max-w-md shadow-2xl">
                <div class="flex justify-between items-center mb-4">
                    <h3 class="font-bold text-lg text-white">⚙️ रिस्क व ब्रोकर सेटिंग्स</h3>
                    <button onclick="toggleSettingsModal(false)" class="text-slate-400 hover:text-white">✕</button>
                </div>
                <div class="space-y-3 text-sm">
                    <div>
                        <label class="block text-xs text-slate-400 mb-1">मैक्सिमम डेली लॉस (₹ / Kill-Switch Limit)</label>
                        <input type="number" id="setting_max_loss" class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2 text-white">
                    </div>
                    <div>
                        <label class="block text-xs text-slate-400 mb-1">प्रति ट्रेड रिस्क (₹ / Auto-Sizing)</label>
                        <input type="number" id="setting_risk_per_trade" class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2 text-white">
                    </div>
                    <div>
                        <label class="block text-xs text-slate-400 mb-1">अधिकतम लगातार नुकसान (Tilt Trigger)</label>
                        <input type="number" id="setting_consecutive" class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2 text-white">
                    </div>
                    <div>
                        <label class="block text-xs text-slate-400 mb-1">कनेक्टेड ब्रोकर एंडपॉइंट</label>
                        <select id="setting_broker" class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2 text-white">
                            <option value="Dhan API Gateway">Dhan API Gateway</option>
                            <option value="Zerodha Kill-Switch API">Zerodha Kill-Switch API</option>
                            <option value="Angel One SmartAPI">Angel One SmartAPI</option>
                            <option value="Simulated (Dhan/Zerodha API)">Simulated Sandbox (Zero Risk)</option>
                        </select>
                    </div>
                    <label class="flex items-center gap-3 p-3 bg-slate-950 rounded-lg border border-slate-800 cursor-pointer">
                        <input type="checkbox" id="setting_shield_toggle" class="w-4 h-4 accent-indigo-500" checked>
                        <span class="text-xs text-slate-300">Enable Hardware Broker-Shield (Auto-Freeze Account on Breach)</span>
                    </label>
                    <button onclick="saveCustomSettings()" class="w-full bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold py-2.5 rounded-lg transition mt-2">सेव करें</button>
                </div>
            </div>
        </div>

        <script>
            const CURRENT_USER_ID = {user_id};
            let pnlChartInstance = null;
            let mistakeChartInstance = null;
            let currentRiskPerTrade = 500;
            let pendingTradePayload = null;
            let vaultCountdownInterval = null;

            // --- AVAILABLE MARKETS DATA DIRECTORY ---
            let ALL_MARKETS = [
                {{ id: 'NIFTY', name: 'NIFTY 50', category: 'indian', price: '25,245.80', change: '+0.45%', up: true }},
                {{ id: 'BANKNIFTY', name: 'BANK NIFTY', category: 'indian', price: '52,180.20', change: '-0.28%', up: false }},
                {{ id: 'FINNIFTY', name: 'FIN NIFTY', category: 'indian', price: '24,120.50', change: '+0.12%', up: true }},
                {{ id: 'SENSEX', name: 'SENSEX', category: 'indian', price: '82,410.00', change: '+0.38%', up: true }},
                {{ id: 'INDIAVIX', name: 'INDIA VIX', category: 'indian', price: '12.85', change: '-1.40%', up: false }},
                
                {{ id: 'GIFTNIFTY', name: 'GIFT NIFTY', category: 'global', price: '25,310.00', change: '+0.52%', up: true }},
                {{ id: 'DOW', name: 'DOW JONES', category: 'global', price: '42,880.50', change: '+0.25%', up: true }},
                {{ id: 'NASDAQ', name: 'NASDAQ 100', category: 'global', price: '20,415.30', change: '+0.88%', up: true }},
                {{ id: 'SP500', name: 'S&P 500', category: 'global', price: '5,860.20', change: '+0.40%', up: true }},
                
                {{ id: 'CRUDE', name: 'CRUDE OIL', category: 'crypto', price: '$71.40', change: '-1.15%', up: false }},
                {{ id: 'GOLD', name: 'GOLD', category: 'crypto', price: '₹77,650', change: '+0.32%', up: true }},
                {{ id: 'USDINR', name: 'USD/INR', category: 'crypto', price: '₹84.05', change: '+0.04%', up: true }},
                {{ id: 'BTC', name: 'BITCOIN', category: 'crypto', price: '$68,450', change: '+2.14%', up: true }}
            ];

            // Load saved custom tickers from localStorage
            let savedCustoms = JSON.parse(localStorage.getItem('alpha_custom_tickers') || '[]');
            savedCustoms.forEach(c => {{
                if (!ALL_MARKETS.some(m => m.id === c.id)) ALL_MARKETS.push(c);
            }});

            let selectedMarketIds = JSON.parse(localStorage.getItem('alpha_pinned_markets') || '["NIFTY","BANKNIFTY","GIFTNIFTY","DOW","INDIAVIX"]');

            function toggleMarketModal(show) {{
                document.getElementById('marketModal').classList.toggle('hidden', !show);
                if (show) renderMarketCheckboxes();
            }}

            function addCustomMarketTicker() {{
                const input = document.getElementById('customTickerInput');
                const val = input.value.trim().toUpperCase();
                if (!val) return;

                if (!ALL_MARKETS.some(m => m.id === val)) {{
                    const newObj = {{
                        id: val,
                        name: val,
                        category: 'custom',
                        price: '₹' + (Math.floor(Math.random() * 2500) + 120) + '.50',
                        change: (Math.random() > 0.4 ? '+' : '-') + (Math.random() * 2.8).toFixed(2) + '%',
                        up: Math.random() > 0.4
                    }};
                    ALL_MARKETS.unshift(newObj);
                    savedCustoms.unshift(newObj);
                    localStorage.setItem('alpha_custom_tickers', JSON.stringify(savedCustoms));
                    
                    if (!selectedMarketIds.includes(val)) {{
                        selectedMarketIds.unshift(val);
                    }}
                }}
                input.value = '';
                renderMarketCheckboxes();
            }}

            function deleteCustomTicker(id) {{
                ALL_MARKETS = ALL_MARKETS.filter(m => m.id !== id);
                savedCustoms = savedCustoms.filter(m => m.id !== id);
                selectedMarketIds = selectedMarketIds.filter(mId => mId !== id);
                localStorage.setItem('alpha_custom_tickers', JSON.stringify(savedCustoms));
                localStorage.setItem('alpha_pinned_markets', JSON.stringify(selectedMarketIds));
                renderMarketCheckboxes();
                renderMarketPulseStrip();
            }}

            function renderMarketCheckboxes() {{
                const custContainer = document.getElementById('customMarketsList');
                const indContainer = document.getElementById('indianMarketsList');
                const globContainer = document.getElementById('globalMarketsList');
                const cryContainer = document.getElementById('cryptoMarketsList');

                custContainer.innerHTML = '';
                indContainer.innerHTML = '';
                globContainer.innerHTML = '';
                cryContainer.innerHTML = '';

                ALL_MARKETS.forEach(m => {{
                    const isChecked = selectedMarketIds.includes(m.id) ? 'checked' : '';
                    const isCustom = m.category === 'custom';
                    const delBtn = isCustom ? `<button onclick="deleteCustomTicker('${{m.id}}')" class="text-rose-400 hover:text-rose-300 ml-auto text-xs px-1">✕</button>` : '';

                    const html = `
                        <label class="flex items-center gap-2 p-2 bg-slate-950 rounded-lg border border-slate-800 hover:border-slate-700 cursor-pointer">
                            <input type="checkbox" value="${{m.id}}" class="market-checkbox w-4 h-4 accent-amber-500" ${{isChecked}}>
                            <span class="font-semibold text-slate-200">${{m.name}}</span>
                            ${{delBtn}}
                        </label>
                    `;
                    if (m.category === 'custom') custContainer.innerHTML += html;
                    else if (m.category === 'indian') indContainer.innerHTML += html;
                    else if (m.category === 'global') globContainer.innerHTML += html;
                    else cryContainer.innerHTML += html;
                }});

                if (custContainer.innerHTML === '') {{
                    custContainer.innerHTML = '<p class="text-slate-500 italic col-span-2 text-[11px]">अभी कोई कस्टम स्टॉक नहीं जोड़ा गया है। ऊपर लिखकर जोड़ें।</p>';
                }}
            }}

            function saveMarketSelections() {{
                const checkedBoxes = document.querySelectorAll('.market-checkbox:checked');
                selectedMarketIds = Array.from(checkedBoxes).map(cb => cb.value);
                if (selectedMarketIds.length === 0) {{
                    selectedMarketIds = ['NIFTY', 'BANKNIFTY'];
                }}
                localStorage.setItem('alpha_pinned_markets', JSON.stringify(selectedMarketIds));
                toggleMarketModal(false);
                renderMarketPulseStrip();
            }}

            function renderMarketPulseStrip() {{
                const container = document.getElementById('marketTickerContainer');
                container.innerHTML = '';

                let greenCount = 0;
                let redCount = 0;

                const pinned = ALL_MARKETS.filter(m => selectedMarketIds.includes(m.id));
                pinned.forEach(m => {{
                    if (m.up) greenCount++;
                    else redCount++;

                    const colorClass = m.up ? 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20' : 'text-rose-400 bg-rose-500/10 border-rose-500/20';
                    const icon = m.up ? '▲' : '▼';
                    const itemHtml = `
                        <div class="flex-shrink-0 flex items-center gap-2.5 px-3 py-1.5 rounded-lg border bg-slate-950 border-slate-800">
                            <span class="text-xs font-bold text-slate-300 tracking-wide">${{m.name}}</span>
                            <span class="text-xs font-mono font-semibold text-white">${{m.price}}</span>
                            <span class="text-[10px] font-bold px-1.5 py-0.5 rounded border ${{colorClass}} flex items-center gap-0.5">
                                ${{icon}} ${{m.change}}
                            </span>
                        </div>
                    `;
                    container.innerHTML += itemHtml;
                }});

                // Dynamic Market Sentiment Update
                const biasBadge = document.getElementById('marketBiasBadge');
                if (biasBadge) {{
                    if (greenCount > redCount) {{
                        biasBadge.className = "text-xs font-bold text-emerald-400 flex items-center gap-1";
                        biasBadge.innerHTML = `<span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span> Bullish (${{greenCount}}/${{pinned.length}})`;
                    }} else if (redCount > greenCount) {{
                        biasBadge.className = "text-xs font-bold text-rose-400 flex items-center gap-1";
                        biasBadge.innerHTML = `<span class="w-2 h-2 rounded-full bg-rose-400 animate-pulse"></span> Bearish (${{redCount}}/${{pinned.length}})`;
                    }} else {{
                        biasBadge.className = "text-xs font-bold text-amber-400 flex items-center gap-1";
                        biasBadge.innerHTML = `<span class="w-2 h-2 rounded-full bg-amber-400"></span> Neutral`;
                    }}
                }}
            }}

            function toggleSettingsModal(show) {{
                document.getElementById('settingsModal').classList.toggle('hidden', !show);
            }}

            function toggleChecklistModal(show) {{
                document.getElementById('checklistModal').classList.toggle('hidden', !show);
            }}

            async function triggerEmergencyBreak() {{
                if (confirm('क्या आप सच में 1 घंटा ट्रेडिंग स्क्रीन से लॉक होना चाहते हैं?')) {{
                    await fetch('/api/trigger-cooldown', {{ method: 'POST' }});
                    refreshData();
                }}
            }}

            function startVaultCountdown(remainingSec) {{
                clearInterval(vaultCountdownInterval);
                const timerEl = document.getElementById('countdownTimer');
                
                function update() {{
                    if (remainingSec <= 0) {{
                        clearInterval(vaultCountdownInterval);
                        document.getElementById('vaultOverlay').classList.add('hidden');
                        location.reload();
                        return;
                    }}
                    const m = Math.floor(remainingSec / 60);
                    const s = remainingSec % 60;
                    timerEl.innerText = `${{m < 10 ? '0' : ''}}${{m}}:${{s < 10 ? '0' : ''}}${{s}}`;
                    remainingSec--;
                }}
                update();
                vaultCountdownInterval = setInterval(update, 1000);
            }}

            async function loadSettings() {{
                try {{
                    const res = await fetch('/api/settings');
                    const d = await res.json();
                    document.getElementById('setting_max_loss').value = d.max_daily_loss;
                    document.getElementById('setting_risk_per_trade').value = d.risk_per_trade;
                    document.getElementById('setting_consecutive').value = d.max_consecutive_losses;
                    document.getElementById('setting_broker').value = d.broker_name || 'Simulated (Dhan/Zerodha API)';
                    document.getElementById('setting_shield_toggle').checked = d.broker_shield_enabled !== false;
                    currentRiskPerTrade = d.risk_per_trade || 500;
                }} catch (e) {{
                    console.error(e);
                }}
            }}

            async function saveCustomSettings() {{
                const payload = {{
                    max_daily_loss: parseFloat(document.getElementById('setting_max_loss').value),
                    risk_per_trade: parseFloat(document.getElementById('setting_risk_per_trade').value),
                    max_consecutive_losses: parseInt(document.getElementById('setting_consecutive').value),
                    broker_name: document.getElementById('setting_broker').value,
                    broker_shield_enabled: document.getElementById('setting_shield_toggle').checked
                }};
                const res = await fetch('/api/settings', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify(payload)
                }});
                if (res.ok) {{
                    alert('सेटिंग्स सेव हो गईं!');
                    currentRiskPerTrade = payload.risk_per_trade;
                    toggleSettingsModal(false);
                    refreshData();
                }}
            }}

            function calculateAutoQuantity() {{
                const entry = parseFloat(document.getElementById('entry_price').value);
                const sl = parseFloat(document.getElementById('stop_loss').value);
                if (entry && sl && entry !== sl) {{
                    const perShareRisk = Math.abs(entry - sl);
                    const qty = Math.floor(currentRiskPerTrade / perShareRisk);
                    if (qty > 0) {{
                        document.getElementById('quantity').value = qty;
                    }}
                }}
            }}

            document.getElementById('entry_price').addEventListener('input', calculateAutoQuantity);
            document.getElementById('stop_loss').addEventListener('input', calculateAutoQuantity);

            async function updateRiskStatus() {{
                try {{
                    const res = await fetch('/api/risk-status');
                    const rData = await res.json();
                    
                    const banner = document.getElementById('riskBanner');
                    const dot = document.getElementById('riskIndicatorDot');
                    const title = document.getElementById('riskTitle');
                    const desc = document.getElementById('riskDesc');
                    const pnlVal = document.getElementById('dailyPnlVal');
                    const btn = document.getElementById('submitBtn');
                    const dBar = document.getElementById('drawdownBar');
                    const dLabel = document.getElementById('drawdownLabel');

                    pnlVal.innerText = (rData.daily_pnl >= 0 ? '+₹' : '-₹') + Math.abs(rData.daily_pnl || 0);

                    if (rData.is_vault_active) {{
                        document.getElementById('vaultOverlay').classList.remove('hidden');
                        startVaultCountdown(rData.remaining_seconds);
                        return;
                    }} else {{
                        document.getElementById('vaultOverlay').classList.add('hidden');
                    }}

                    const pct = rData.drawdown_pct || 0;
                    const used = rData.daily_pnl < 0 ? Math.abs(rData.daily_pnl) : 0;
                    dBar.style.width = pct + '%';
                    dLabel.innerText = `${{pct}}% (₹${{used}} / ₹${{rData.max_daily_loss}})`;

                    if (pct > 80) {{
                        dBar.className = "bg-rose-500 h-full transition-all duration-500";
                    }} else if (pct > 40) {{
                        dBar.className = "bg-amber-500 h-full transition-all duration-500";
                    }} else {{
                        dBar.className = "bg-emerald-500 h-full transition-all duration-500";
                    }}

                    if (rData.is_locked) {{
                        banner.className = "p-4 rounded-xl border space-y-3 transition-all bg-rose-950/60 border-rose-500/50 text-rose-300";
                        dot.className = "w-3.5 h-3.5 rounded-full bg-rose-500 animate-ping";
                        title.innerText = "🛑 BROKER-SHIELD ACTIVE (ACCOUNT LOCKED)";
                        desc.innerText = rData.message;
                        btn.disabled = true;
                        btn.className = "w-full bg-slate-800 text-slate-500 font-bold py-2 rounded-lg mt-2 cursor-not-allowed";
                        btn.innerText = "Terminal Locked by Sentinel";
                    }} else if (rData.tilt_level === "Yellow") {{
                        banner.className = "p-4 rounded-xl border space-y-3 transition-all bg-amber-950/40 border-amber-500/40 text-amber-300";
                        dot.className = "w-3.5 h-3.5 rounded-full bg-amber-400 animate-pulse";
                        title.innerText = "⚠️ TILT-METER WARNING";
                        desc.innerText = rData.message;
                        btn.disabled = false;
                        btn.className = "w-full bg-amber-500 hover:bg-amber-600 text-slate-950 font-bold py-2 rounded-lg mt-2 transition";
                        btn.innerText = "ट्रेड जोड़ें (सावधानी: 50% साइज़)";
                    }} else {{
                        banner.className = "p-4 rounded-xl border space-y-3 transition-all bg-emerald-950/40 border-emerald-500/30 text-emerald-300";
                        dot.className = "w-3.5 h-3.5 rounded-full bg-emerald-400 animate-pulse";
                        title.innerText = "⚡ टिल्ट-मीटर: सेफ मोड";
                        desc.innerText = rData.message;
                        btn.disabled = false;
                        btn.className = "w-full bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold py-2 rounded-lg mt-2 transition";
                        btn.innerText = "ट्रेड जोड़ें";
                    }}
                }} catch (e) {{
                    console.error("Error updating risk status", e);
                }}
            }}

            async function updateAIAudit() {{
                try {{
                    const res = await fetch('/api/audit');
                    const audit = await res.json();
                    if (!audit.has_data) {{
                        document.getElementById('auditGrade').innerText = "--";
                        document.getElementById('leakDesc').innerText = audit.message || "कम से कम 2 ट्रेड्स दर्ज करें";
                        document.getElementById('leakSavings').innerText = "";
                        document.getElementById('edgeDesc').innerText = "अपेक्षित ट्रेड्स की प्रतीक्षा है";
                        document.getElementById('edgeStats').innerText = "";
                        document.getElementById('ratioDesc').innerText = "प्रॉफिट फैक्टर पेंडिंग";
                        document.getElementById('winrateStat').innerText = "";
                        return;
                    }}

                    document.getElementById('auditGrade').innerText = audit.grade;
                    if (audit.top_mistake) {{
                        document.getElementById('leakDesc').innerText = `सबसे बड़ी गलती: "${{audit.top_mistake}}" (-₹${{audit.top_mistake_amt}})`;
                        document.getElementById('leakSavings').innerText = `💡 बिना गलतियों के संभावित P&L: ₹${{audit.pnl_without_mistakes}}`;
                    }} else {{
                        document.getElementById('leakDesc').innerText = `बढ़िया अनुशासन! कोई लीकेज नहीं मिला।`;
                        document.getElementById('leakSavings').innerText = `क्लीन एग्जीक्यूशन`;
                    }}

                    document.getElementById('edgeDesc').innerText = `सर्वश्रेष्ठ सेटअप: ${{audit.best_symbol}} (${{audit.best_type}})`;
                    document.getElementById('edgeStats').innerText = `${{audit.best_type}} ट्रेड्स में ${{audit.best_rate}}% का विन-रेट`;

                    document.getElementById('ratioDesc').innerText = `प्रॉफिट फैक्टर: ${{audit.profit_factor}}x`;
                    document.getElementById('winrateStat').innerText = `समग्र विन-रेट: ${{audit.win_rate}}%`;
                }} catch (e) {{
                    console.error("Error loading AI audit", e);
                }}
            }}

            async function refreshData() {{
                await updateRiskStatus();
                await updateAIAudit();

                const aRes = await fetch('/analytics/');
                const aData = await aRes.json();
                document.getElementById('totalTrades').innerText = aData.total_trades || 0;
                document.getElementById('winRate').innerText = aData.win_rate_percentage || '0%';
                document.getElementById('netPnl').innerText = '₹' + (aData.net_pnl || 0);
                document.getElementById('netPnl').className = 'text-2xl font-bold mt-1 ' + (aData.net_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400');
                
                let mistakeTotal = 0;
                for (let key in aData.loss_by_mistakes) {{
                    mistakeTotal += aData.loss_by_mistakes[key];
                }}
                document.getElementById('mistakeLoss').innerText = '₹' + mistakeTotal;

                const tRes = await fetch('/trades/');
                const tData = await tRes.json();
                const tbody = document.getElementById('tradesTable');
                tbody.innerHTML = '';
                
                let runningPnL = 0;
                let pnlPoints = [];
                let pnlLabels = [];

                [...tData].reverse().forEach((t, index) => {{
                    runningPnL += t.net_pnl;
                    pnlPoints.push(runningPnL);
                    pnlLabels.push(`T${{index + 1}}`);
                }});

                tData.forEach(t => {{
                    const chartBtn = t.chart_link ? `<a href="${{t.chart_link}}" target="_blank" class="text-xs text-emerald-400 hover:underline">देखें ↗</a>` : `<span class="text-xs text-slate-600">-</span>`;
                    const row = `<tr>
                        <td class="py-3 text-xs text-slate-400">${{t.trade_date}}</td>
                        <td class="py-3 font-semibold">${{t.symbol}}</td>
                        <td class="py-3"><span class="px-2 py-0.5 rounded text-xs ${{t.trade_type === 'BUY' ? 'bg-emerald-950 text-emerald-400' : 'bg-rose-950 text-rose-400'}}">${{t.trade_type}}</span></td>
                        <td class="py-3 font-semibold ${{t.net_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}}">${{t.net_pnl >= 0 ? '+₹' + t.net_pnl : '-₹' + Math.abs(t.net_pnl)}}</td>
                        <td class="py-3">1:${{t.risk_reward_ratio}}</td>
                        <td class="py-3 text-xs text-slate-400">${{t.mistake_tag || 'None'}}</td>
                        <td class="py-3 text-xs text-slate-300 max-w-xs truncate">${{t.notes || '-'}}</td>
                        <td class="py-3">${{chartBtn}}</td>
                    </tr>`;
                    tbody.innerHTML += row;
                }});

                const ctxPnL = document.getElementById('pnlChart').getContext('2d');
                if (pnlChartInstance) pnlChartInstance.destroy();
                pnlChartInstance = new Chart(ctxPnL, {{
                    type: 'line',
                    data: {{
                        labels: pnlLabels.length ? pnlLabels : ['Start'],
                        datasets: [{{
                            label: 'Cumulative P&L (₹)',
                            data: pnlPoints.length ? pnlPoints : [0],
                            borderColor: '#10b981',
                            backgroundColor: 'rgba(16, 185, 129, 0.1)',
                            fill: true,
                            tension: 0.3
                        }}]
                    }},
                    options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }} }}
                }});

                const mistakeKeys = Object.keys(aData.loss_by_mistakes || {{}});
                const mistakeVals = Object.values(aData.loss_by_mistakes || {{}});
                const ctxMistake = document.getElementById('mistakeChart').getContext('2d');
                if (mistakeChartInstance) mistakeChartInstance.destroy();
                mistakeChartInstance = new Chart(ctxMistake, {{
                    type: 'doughnut',
                    data: {{
                        labels: mistakeKeys.length ? mistakeKeys : ['कोई नुकसान नहीं'],
                        datasets: [{{
                            data: mistakeVals.length ? mistakeVals : [1],
                            backgroundColor: mistakeKeys.length ? ['#f43f5e', '#fb923c', '#facc15', '#a855f7'] : ['#334155']
                        }}]
                    }},
                    options: {{ responsive: true, maintainAspectRatio: false }}
                }});
            }}

            function openChecklist(e) {{
                e.preventDefault();
                pendingTradePayload = {{
                    user_id: CURRENT_USER_ID,
                    symbol: document.getElementById('symbol').value,
                    trade_type: document.getElementById('trade_type').value,
                    entry_price: parseFloat(document.getElementById('entry_price').value),
                    exit_price: parseFloat(document.getElementById('exit_price').value),
                    quantity: parseInt(document.getElementById('quantity').value),
                    stop_loss: parseFloat(document.getElementById('stop_loss').value),
                    mistake_tag: document.getElementById('mistake_tag').value,
                    notes: document.getElementById('notes').value,
                    chart_link: document.getElementById('chart_link').value
                }};
                document.getElementById('check_rule1').checked = false;
                document.getElementById('check_rule2').checked = false;
                document.getElementById('check_rule3').checked = false;
                toggleChecklistModal(true);
            }}

            async function confirmAndExecuteTrade() {{
                const r1 = document.getElementById('check_rule1').checked;
                const r2 = document.getElementById('check_rule2').checked;
                const r3 = document.getElementById('check_rule3').checked;

                if (!r1 || !r2 || !r3) {{
                    alert('कृपया तीनों नियमों की पुष्टि करें!');
                    return;
                }}

                toggleChecklistModal(false);

                const response = await fetch('/trades/', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify(pendingTradePayload)
                }});

                if (!response.ok) {{
                    const err = await response.json();
                    alert(err.detail || 'ट्रेड दर्ज करने में समस्या आई!');
                    return;
                }}

                document.getElementById('symbol').value = '';
                document.getElementById('notes').value = '';
                document.getElementById('chart_link').value = '';
                refreshData();
            }}

            loadSettings();
            renderMarketPulseStrip();
            refreshData();
        </script>
    </body>
    </html>
    """