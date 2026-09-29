from fastapi import FastAPI, HTTPException, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import date
import sqlite3
import hashlib

app = FastAPI(title="AlphaTrade Pro")
DB_FILE = "trades.db"

# --- 1. डेटाबेस सेटअप (Users + Trades) ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # यूज़र्स टेबल
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            license_key TEXT NOT NULL
        )
    """)
    # ट्रेड्स टेबल (user_id के साथ लिंक)
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
            trade_date TEXT,
            net_pnl REAL,
            risk_reward_ratio REAL,
            status TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    conn.commit()
    conn.close()

init_db()

def hash_pass(pwd: str) -> str:
    return hashlib.sha256(pwd.encode()).hexdigest()

# --- 2. मॉडल्स ---
class TradeInput(BaseModel):
    user_id: int
    symbol: str
    trade_type: str
    entry_price: float
    exit_price: float
    quantity: int
    stop_loss: float
    mistake_tag: Optional[str] = "None"

# --- 3. ऑथेंटिकेशन और एपीआई ---
@app.post("/register")
def register(username: str = Form(...), password: str = Form(...), license_key: str = Form(...)):
    # टेस्टिंग के लिए मान्य लाइसेंस की
    VALID_KEYS = ["ALPHA-VIP-2026", "PRO-TRADER-99", "FREE-TRIAL-01"]
    if license_key not in VALID_KEYS:
        return HTMLResponse("<script>alert('गलत लाइसेंस की! पहले एक्सेस खरीदें।'); window.history.back();</script>")
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users (username, password_hash, license_key) VALUES (?, ?, ?)",
                       (username.lower().strip(), hash_pass(password), license_key))
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

@app.post("/trades/")
def add_trade(trade: TradeInput):
    trade_type = trade.trade_type.upper()
    if trade_type == "BUY":
        pnl = (trade.exit_price - trade.entry_price) * trade.quantity
        risk = (trade.entry_price - trade.stop_loss) * trade.quantity
    else:
        pnl = (trade.entry_price - trade.exit_price) * trade.quantity
        risk = (trade.stop_loss - trade.entry_price) * trade.quantity

    rr_ratio = round(abs(pnl / risk), 2) if risk > 0 else 0.0
    status = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN")

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO trades (user_id, symbol, trade_type, entry_price, exit_price, quantity, stop_loss, mistake_tag, trade_date, net_pnl, risk_reward_ratio, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (trade.user_id, trade.symbol.upper(), trade_type, trade.entry_price, trade.exit_price, trade.quantity, trade.stop_loss, trade.mistake_tag, str(date.today()), round(pnl, 2), rr_ratio, status))
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

# --- 4. यूआई पेज (Login, Register, Dashboard) ---
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
                    <input type="text" name="username" required class="w-full bg-slate-950 border border-slate-700 p-2.5 rounded-lg text-sm text-white focus:outline-none focus:border-emerald-500">
                </div>
                <div>
                    <label class="text-xs text-slate-400">पासवर्ड</label>
                    <input type="password" name="password" required class="w-full bg-slate-950 border border-slate-700 p-2.5 rounded-lg text-sm text-white focus:outline-none focus:border-emerald-500">
                </div>
                <button type="submit" class="w-full bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold py-2.5 rounded-lg transition duration-200">लॉगिन करें</button>
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
                    <label class="text-xs text-slate-400">लाइसेंस की (License Key)</label>
                    <input type="text" name="license_key" placeholder="उदा: FREE-TRIAL-01" required class="w-full bg-slate-950 border border-slate-700 p-2.5 rounded-lg text-sm text-white">
                    <p class="text-[10px] text-slate-500 mt-1">टेस्टिंग की: FREE-TRIAL-01 या ALPHA-VIP-2026</p>
                </div>
                <button type="submit" class="w-full bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold py-2.5 rounded-lg transition duration-200">रजिस्टर करें</button>
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
    <body class="bg-slate-950 text-slate-100 font-sans min-h-screen p-6">
        <div class="max-w-6xl mx-auto space-y-6">
            <div class="flex justify-between items-center border-b border-slate-800 pb-4">
                <div>
                    <h1 class="text-2xl font-bold text-emerald-400">⚡ AlphaTrade Pro</h1>
                    <p class="text-sm text-slate-400">यूज़र डैशबोर्ड</p>
                </div>
                <div class="flex items-center gap-4">
                    <span class="px-3 py-1 bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 rounded-full text-xs font-semibold">लाइसेंस एक्टिव</span>
                    <a href="/logout" class="text-xs bg-rose-500/20 text-rose-400 hover:bg-rose-500/30 px-3 py-1.5 rounded-lg border border-rose-500/30 transition">लॉगआउट</a>
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
                    <h2 class="text-lg font-semibold mb-4 text-slate-200">ट्रेड दर्ज करें</h2>
                    <form id="tradeForm" class="space-y-3" onsubmit="submitTrade(event)">
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
                                <label class="block text-xs text-slate-400 mb-1">क्वांटिटी</label>
                                <input type="number" id="quantity" value="50" required class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2 text-sm text-white">
                            </div>
                        </div>
                        <div class="grid grid-cols-3 gap-2">
                            <div>
                                <label class="block text-xs text-slate-400 mb-1">एंट्री</label>
                                <input type="number" step="0.1" id="entry_price" required class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2 text-sm text-white">
                            </div>
                            <div>
                                <label class="block text-xs text-slate-400 mb-1">एग्जिट</label>
                                <input type="number" step="0.1" id="exit_price" required class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2 text-sm text-white">
                            </div>
                            <div>
                                <label class="block text-xs text-slate-400 mb-1">SL</label>
                                <input type="number" step="0.1" id="stop_loss" required class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2 text-sm text-white">
                            </div>
                        </div>
                        <div>
                            <label class="block text-xs text-slate-400 mb-1">गलती</label>
                            <select id="mistake_tag" class="w-full bg-slate-950 border border-slate-700 rounded-lg p-2 text-sm text-white">
                                <option value="None">Rules Followed</option>
                                <option value="FOMO">FOMO</option>
                                <option value="Revenge Trading">Revenge Trading</option>
                                <option value="No SL">No SL</option>
                            </select>
                        </div>
                        <button type="submit" class="w-full bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold py-2 rounded-lg mt-2">ट्रेड जोड़ें</button>
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
                            </tr>
                        </thead>
                        <tbody id="tradesTable" class="divide-y border-slate-800"></tbody>
                    </table>
                </div>
            </div>
        </div>

        <script>
            const CURRENT_USER_ID = {user_id};
            let pnlChartInstance = null;
            let mistakeChartInstance = null;

            async function refreshData() {{
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
                    const row = `<tr>
                        <td class="py-3 text-xs text-slate-400">${{t.trade_date}}</td>
                        <td class="py-3 font-semibold">${{t.symbol}}</td>
                        <td class="py-3"><span class="px-2 py-0.5 rounded text-xs ${{t.trade_type === 'BUY' ? 'bg-emerald-950 text-emerald-400' : 'bg-rose-950 text-rose-400'}}">${{t.trade_type}}</span></td>
                        <td class="py-3 font-semibold ${{t.net_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}}">${{t.net_pnl >= 0 ? '+₹' + t.net_pnl : '-₹' + Math.abs(t.net_pnl)}}</td>
                        <td class="py-3">1:${{t.risk_reward_ratio}}</td>
                        <td class="py-3 text-xs text-slate-400">${{t.mistake_tag || 'None'}}</td>
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

            async function submitTrade(e) {{
                e.preventDefault();
                const payload = {{
                    user_id: CURRENT_USER_ID,
                    symbol: document.getElementById('symbol').value,
                    trade_type: document.getElementById('trade_type').value,
                    entry_price: parseFloat(document.getElementById('entry_price').value),
                    exit_price: parseFloat(document.getElementById('exit_price').value),
                    quantity: parseInt(document.getElementById('quantity').value),
                    stop_loss: parseFloat(document.getElementById('stop_loss').value),
                    mistake_tag: document.getElementById('mistake_tag').value
                }};
                await fetch('/trades/', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify(payload)
                }});
                document.getElementById('symbol').value = '';
                refreshData();
            }}

            refreshData();
        </script>
    </body>
    </html>
    """