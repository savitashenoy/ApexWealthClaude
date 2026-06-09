from flask import Flask, request, jsonify, send_from_directory, make_response
from flask_cors import CORS
import json, os, uuid, hashlib, hmac, time
from datetime import datetime, date
import yfinance as yf

# ── paths ──────────────────────────────────────────────────────────────────────
# On Vercel the function lives at /var/task/api/index.py
# All writes must go to /tmp (the only writable location in serverless)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR  = '/tmp/apexwealth_data'
os.makedirs(DATA_DIR, exist_ok=True)

USERS_FILE      = os.path.join(DATA_DIR, 'users.json')
PORTFOLIOS_FILE = os.path.join(DATA_DIR, 'portfolios.json')
WATCHLISTS_FILE = os.path.join(DATA_DIR, 'watchlists.json')
TRADES_FILE     = os.path.join(DATA_DIR, 'trades.json')

# ── app ────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

# ── helpers ────────────────────────────────────────────────────────────────────
def load_json(path, default=None):
    if default is None:
        default = {}
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return default

def save_json(path, data):
    try:
        with open(path, 'w') as f:
            json.dump(data, f, indent=2, default=str)
    except Exception:
        pass

# ── password hashing (pbkdf2 — no C extensions, works on every Python env) ────
_HASH_ITERS = 260_000

def hash_password(pwd):
    salt = os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac('sha256', pwd.encode(), salt.encode(), _HASH_ITERS).hex()
    return f"pbkdf2:{salt}:{h}"

def check_password(pwd, stored):
    try:
        if stored.startswith('pbkdf2:'):
            _, salt, h = stored.split(':', 2)
            candidate = hashlib.pbkdf2_hmac('sha256', pwd.encode(), salt.encode(), _HASH_ITERS).hex()
            return hmac.compare_digest(candidate, h)
        # Legacy SHA-256 fallback (accounts created before this version)
        return hmac.compare_digest(hashlib.sha256(pwd.encode()).hexdigest(), stored)
    except Exception:
        return False

def get_nse_ticker(symbol):
    return f"{symbol}.NS"

def _safe_float(v):
    try:
        if v is None or v != v:
            return None
        if hasattr(v, 'item'):
            v = v.item()
        return float(v)
    except Exception:
        return None

def fetch_quote(symbol):
    try:
        ticker = yf.Ticker(get_nse_ticker(symbol))
        hist = ticker.history(period='2d')
        if hist.empty:
            return None
        latest   = hist.iloc[-1]
        ltp      = _safe_float(latest['Close'])
        if ltp is None:
            return None
        prev_close = _safe_float(hist['Close'].iloc[-2]) if len(hist) >= 2 else ltp
        day_chg_pct = round(((ltp - prev_close) / prev_close * 100), 2) if prev_close else 0
        return {
            'symbol':      symbol,
            'ltp':         round(ltp, 2),
            'prev_close':  round(prev_close, 2),
            'day_high':    round(_safe_float(latest['High']) or ltp, 2),
            'day_low':     round(_safe_float(latest['Low'])  or ltp, 2),
            'volume':      int(_safe_float(latest['Volume']) or 0),
            'day_chg_pct': day_chg_pct,
        }
    except Exception:
        return None

def fetch_return_profile(symbol, quote=None):
    returns = {'ret_1d': 0, 'ret_1w': 0, 'ret_1m': 0, 'ret_1y': 0}
    try:
        ticker = yf.Ticker(get_nse_ticker(symbol))
        hist = ticker.history(period='1y')
        if hist.empty:
            if quote:
                returns['ret_1d'] = round(float(quote.get('day_chg_pct', 0)), 2)
            return returns
        ltp = float(hist['Close'].iloc[-1])
        def pct(rows_back):
            try:
                idx  = max(0, len(hist) - 1 - rows_back)
                base = float(hist['Close'].iloc[idx])
                return round(((ltp - base) / base) * 100, 2) if base else 0
            except Exception:
                return 0
        returns['ret_1d'] = round(float(quote.get('day_chg_pct', 0)), 2) if quote else pct(1)
        returns['ret_1w'] = pct(5)
        returns['ret_1m'] = pct(22)
        returns['ret_1y'] = pct(min(252, max(1, len(hist) - 1)))
    except Exception:
        if quote:
            returns['ret_1d'] = round(float(quote.get('day_chg_pct', 0)), 2)
    return returns

def _pct_change_from(hist, lookback_rows):
    try:
        if hist is None or hist.empty:
            return 0
        ltp  = _safe_float(hist['Close'].iloc[-1])
        if ltp is None:
            return 0
        idx  = max(0, len(hist) - 1 - lookback_rows)
        base = _safe_float(hist['Close'].iloc[idx])
        if not base:
            return 0
        return round(((ltp - base) / base) * 100, 2)
    except Exception:
        return 0

# ── static / index ─────────────────────────────────────────────────────────────
@app.route('/')
def index():
    resp = make_response(send_from_directory(BASE_DIR, 'index.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma']        = 'no-cache'
    resp.headers['Expires']       = '0'
    return resp

@app.route('/api/returns/<symbol>', methods=['GET'])
def get_returns(symbol):
    """Return 1d/1w/1m/1y percentage changes for a symbol."""
    try:
        t    = yf.Ticker(get_nse_ticker(symbol))
        hist = t.history(period='1y')
        if hist.empty:
            return jsonify({'ret_1d':0,'ret_1w':0,'ret_1m':0,'ret_1y':0})
        ltp = float(hist['Close'].iloc[-1])
        def pct(n):
            try:
                idx  = max(0, len(hist)-1-n)
                base = float(hist['Close'].iloc[idx])
                return round((ltp-base)/base*100, 2) if base else 0
            except: return 0
        return jsonify({'ret_1d': pct(1), 'ret_1w': pct(5), 'ret_1m': pct(22),
                        'ret_1y': pct(min(252, len(hist)-1))})
    except Exception as e:
        return jsonify({'ret_1d':0,'ret_1w':0,'ret_1m':0,'ret_1y':0})

@app.route('/api/tickers', methods=['GET'])
def get_tickers():
    """Serve tickers.json via the API function as a CDN-independent fallback."""
    tickers_path = os.path.join(BASE_DIR, 'static', 'tickers.json')
    try:
        with open(tickers_path) as f:
            data = json.load(f)
        return jsonify(data)
    except Exception as e:
        return jsonify([]), 200

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory(os.path.join(BASE_DIR, 'static'), filename)

# ── auth ───────────────────────────────────────────────────────────────────────
@app.route('/api/signup', methods=['POST'])
def signup():
    data     = request.get_json(silent=True) or {}
    email    = str(data.get('email', '')).lower().strip()
    password = str(data.get('password', ''))
    if not email or not password:
        return jsonify({'error': 'Email and password required'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    users = load_json(USERS_FILE)
    if email in users:
        return jsonify({'error': 'Email already registered'}), 409
    user_id = str(uuid.uuid4())
    users[email] = {
        'id': user_id, 'email': email,
        'password': hash_password(password),
        'created': str(datetime.now())
    }
    save_json(USERS_FILE, users)
    return jsonify({'message': 'Account created', 'user_id': user_id, 'email': email})

@app.route('/api/login', methods=['POST'])
def login():
    data     = request.get_json(silent=True) or {}
    email    = str(data.get('email', '')).lower().strip()
    password = str(data.get('password', ''))
    users    = load_json(USERS_FILE)
    user     = users.get(email)
    if not user or not check_password(password, user['password']):
        return jsonify({'error': 'Invalid credentials'}), 401
    return jsonify({'message': 'Login successful', 'user_id': user['id'], 'email': email})

@app.route('/api/change-password', methods=['POST'])
def change_password():
    data    = request.get_json(silent=True) or {}
    email   = str(data.get('email', '')).lower().strip()
    old_pwd = str(data.get('old_password', ''))
    new_pwd = str(data.get('new_password', ''))
    users   = load_json(USERS_FILE)
    user    = users.get(email)
    if not user or not check_password(old_pwd, user['password']):
        return jsonify({'error': 'Invalid credentials'}), 401
    users[email]['password'] = hash_password(new_pwd)
    save_json(USERS_FILE, users)
    return jsonify({'message': 'Password changed'})

# ── portfolio ──────────────────────────────────────────────────────────────────
@app.route('/api/holdings/<user_id>', methods=['GET'])
def get_holdings(user_id):
    portfolios = load_json(PORTFOLIOS_FILE)
    holdings   = portfolios.get(user_id, [])
    enriched   = []
    for h in holdings:
        q = fetch_quote(h['symbol'])
        if q:
            invested = h['buy_price'] * h['qty']
            curr_val = q['ltp'] * h['qty']
            pnl      = curr_val - invested
            pnl_pct  = (pnl / invested * 100) if invested else 0
            enriched.append({**h, 'ltp': q['ltp'], 'day_chg_pct': q['day_chg_pct'],
                             'invested': round(invested, 2), 'curr_value': round(curr_val, 2),
                             'pnl': round(pnl, 2), 'pnl_pct': round(pnl_pct, 2)})
        else:
            invested = h['buy_price'] * h['qty']
            enriched.append({**h, 'ltp': h['buy_price'], 'day_chg_pct': 0,
                             'invested': round(invested, 2), 'curr_value': round(invested, 2),
                             'pnl': 0, 'pnl_pct': 0, 'stale': True})
    return jsonify(enriched)

@app.route('/api/holdings/<user_id>', methods=['POST'])
def add_holding(user_id):
    data   = request.get_json(silent=True) or {}
    symbol = str(data.get('symbol', '')).strip().upper()
    if not symbol or len(symbol) > 20:
        return jsonify({'error': 'Invalid symbol'}), 400
    try:
        buy_price = float(data['buy_price'])
        qty       = float(data['qty'])
    except (KeyError, TypeError, ValueError):
        return jsonify({'error': 'buy_price and qty must be valid numbers'}), 400
    if buy_price <= 0:
        return jsonify({'error': 'buy_price must be > 0'}), 400
    if qty <= 0:
        return jsonify({'error': 'qty must be > 0'}), 400
    portfolios = load_json(PORTFOLIOS_FILE)
    if user_id not in portfolios:
        portfolios[user_id] = []
    holding = {
        'id':       str(uuid.uuid4()),
        'symbol':   symbol,
        'name':     str(data.get('name', symbol))[:100],
        'buy_price': round(buy_price, 6),
        'qty':      round(qty, 6),
        'date':     data.get('date', str(date.today())),
        'industry': str(data.get('industry', data.get('sector', '')))[:80],
        'sector':   ''
    }
    portfolios[user_id].append(holding)
    save_json(PORTFOLIOS_FILE, portfolios)
    return jsonify({'message': 'Holding added', 'holding': holding})

@app.route('/api/holdings/<user_id>/<holding_id>', methods=['PUT'])
def edit_holding(user_id, holding_id):
    data       = request.get_json(silent=True) or {}
    portfolios = load_json(PORTFOLIOS_FILE)
    holdings   = portfolios.get(user_id, [])
    for i, h in enumerate(holdings):
        if h['id'] == holding_id:
            holdings[i] = {**h,
                'buy_price': float(data.get('buy_price', h['buy_price'])),
                'qty':       float(data.get('qty',       h['qty'])),
                'date':      data.get('date', h['date'])}
            save_json(PORTFOLIOS_FILE, portfolios)
            return jsonify({'message': 'Updated'})
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/holdings/<user_id>/<holding_id>', methods=['DELETE'])
def delete_holding(user_id, holding_id):
    portfolios = load_json(PORTFOLIOS_FILE)
    portfolios[user_id] = [h for h in portfolios.get(user_id, []) if h['id'] != holding_id]
    save_json(PORTFOLIOS_FILE, portfolios)
    return jsonify({'message': 'Deleted'})

@app.route('/api/sell/<user_id>/<holding_id>', methods=['POST'])
def sell_holding(user_id, holding_id):
    data       = request.get_json(silent=True) or {}
    sell_price = float(data.get('sell_price', 0))
    portfolios = load_json(PORTFOLIOS_FILE)
    holdings   = portfolios.get(user_id, [])
    holding    = next((h for h in holdings if h['id'] == holding_id), None)
    if not holding:
        return jsonify({'error': 'Not found'}), 404
    available_qty = float(holding.get('qty', 0))
    sell_qty      = float(data.get('qty', available_qty))
    if sell_price <= 0:
        return jsonify({'error': 'Enter a valid sell price'}), 400
    if sell_qty <= 0 or sell_qty > available_qty:
        return jsonify({'error': f'Sell qty must be between 0 and {available_qty:g}'}), 400
    trades = load_json(TRADES_FILE)
    if user_id not in trades:
        trades[user_id] = []
    invested = holding['buy_price'] * sell_qty
    pnl      = (sell_price - holding['buy_price']) * sell_qty
    trade = {
        'id':         str(uuid.uuid4()),
        'symbol':     holding['symbol'],
        'name':       holding.get('name', holding['symbol']),
        'buy_price':  holding['buy_price'],
        'sell_price': sell_price,
        'qty':        sell_qty,
        'buy_date':   holding['date'],
        'sell_date':  str(date.today()),
        'pnl':        round(pnl, 2),
        'pnl_pct':    round((pnl / invested) * 100, 2) if invested else 0
    }
    trades[user_id].append(trade)
    save_json(TRADES_FILE, trades)
    if sell_qty >= available_qty:
        portfolios[user_id] = [h for h in holdings if h['id'] != holding_id]
    else:
        for h in holdings:
            if h['id'] == holding_id:
                h['qty'] = round(available_qty - sell_qty, 6)
                break
    save_json(PORTFOLIOS_FILE, portfolios)
    return jsonify({'message': 'Sold', 'trade': trade,
                    'remaining_qty': max(0, round(available_qty - sell_qty, 6))})

# ── watchlist ──────────────────────────────────────────────────────────────────
@app.route('/api/watchlist/<user_id>', methods=['GET'])
def get_watchlist(user_id):
    watchlists = load_json(WATCHLISTS_FILE)
    enriched   = []
    for item in watchlists.get(user_id, []):
        q       = fetch_quote(item['symbol'])
        returns = fetch_return_profile(item['symbol'], q)
        enriched.append({**item, **(q or {}), **returns})
    return jsonify(enriched)

@app.route('/api/watchlist/<user_id>', methods=['POST'])
def add_watchlist(user_id):
    data       = request.get_json(silent=True) or {}
    watchlists = load_json(WATCHLISTS_FILE)
    if user_id not in watchlists:
        watchlists[user_id] = []
    symbol = str(data.get('symbol', '')).upper()
    if any(w['symbol'] == symbol for w in watchlists[user_id]):
        return jsonify({'error': 'Already in watchlist'}), 409
    watchlists[user_id].append({
        'symbol':   symbol,
        'name':     data.get('name', symbol),
        'industry': data.get('industry', data.get('sector', '')),
        'added':    str(date.today())
    })
    save_json(WATCHLISTS_FILE, watchlists)
    return jsonify({'message': 'Added to watchlist'})

@app.route('/api/watchlist/<user_id>/<symbol>', methods=['DELETE'])
def remove_watchlist(user_id, symbol):
    watchlists = load_json(WATCHLISTS_FILE)
    watchlists[user_id] = [w for w in watchlists.get(user_id, []) if w['symbol'] != symbol]
    save_json(WATCHLISTS_FILE, watchlists)
    return jsonify({'message': 'Removed'})

# ── trades ─────────────────────────────────────────────────────────────────────
@app.route('/api/trades/<user_id>', methods=['GET'])
def get_trades(user_id):
    trades = load_json(TRADES_FILE)
    return jsonify(trades.get(user_id, []))

# ── market data ────────────────────────────────────────────────────────────────
@app.route('/api/quote/<symbol>', methods=['GET'])
def get_quote(symbol):
    q = fetch_quote(symbol)
    if q:
        return jsonify(q)
    return jsonify({'error': 'Quote unavailable'}), 404

INDEX_MAP = {
    'nifty50':   {'name': 'Nifty 50',   'ticker': '^NSEI'},
    'banknifty': {'name': 'Nifty Bank',  'ticker': '^NSEBANK'},
    'sensex':    {'name': 'BSE Sensex',  'ticker': '^BSESN'},
}

@app.route('/api/market/indices', methods=['GET'])
def market_indices():
    result = []
    for key, meta in INDEX_MAP.items():
        try:
            t        = yf.Ticker(meta['ticker'])
            hist     = t.history(period='1y')
            intraday = t.history(period='1d', interval='5m')
            source   = intraday if not intraday.empty else hist
            if source.empty:
                continue
            ltp      = _safe_float(source['Close'].iloc[-1])
            prev     = _safe_float(hist['Close'].iloc[-2]) if len(hist) >= 2 else ltp
            chg_pct  = round(((ltp - prev) / prev) * 100, 2) if ltp and prev else 0
            result.append({
                'key': key, 'name': meta['name'],
                'value':    round(ltp, 2) if ltp else None,
                'chg':      round((ltp - prev), 2) if ltp and prev else 0,
                'chg_pct':  chg_pct,
                'day_high': round(_safe_float(source['High'].max()) or ltp, 2),
                'day_low':  round(_safe_float(source['Low'].min())  or ltp, 2),
                'ret_1d':   chg_pct,
                'ret_1w':   _pct_change_from(hist, 5),
                'ret_1m':   _pct_change_from(hist, 22),
                'ret_1y':   _pct_change_from(hist, min(252, max(1, len(hist)-1))),
            })
        except Exception:
            pass
    return jsonify(result)

@app.route('/api/market/index-chart/<index_key>', methods=['GET'])
def market_index_chart(index_key):
    meta = INDEX_MAP.get(index_key.lower())
    if not meta:
        return jsonify({'error': 'Unknown index'}), 404
    period     = request.args.get('period', '1d')
    period_map = {'1d': ('1d','5m'), '1w': ('5d','30m'), '1m': ('1mo','1d'), '1y': ('1y','1wk')}
    yf_period, interval = period_map.get(period, ('1d','5m'))
    try:
        t    = yf.Ticker(meta['ticker'])
        hist = t.history(period=yf_period, interval=interval)
        if hist.empty:
            hist = t.history(period=yf_period)
        data = []
        for idx, row in hist.iterrows():
            c = _safe_float(row.get('Close'))
            if c is None:
                continue
            label = idx.strftime('%H:%M') if period == '1d' else idx.strftime('%d %b')
            data.append({'date': str(idx), 'label': label, 'close': round(c, 2),
                         'high': round(_safe_float(row.get('High')) or c, 2),
                         'low':  round(_safe_float(row.get('Low'))  or c, 2)})
        return jsonify({'key': index_key, 'name': meta['name'], 'period': period, 'data': data})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

NIFTY50_SYMBOLS = [
    'ADANIENT','ADANIPORTS','APOLLOHOSP','ASIANPAINT','AXISBANK',
    'BAJAJ-AUTO','BAJFINANCE','BAJAJFINSV','BPCL','BHARTIARTL',
    'BRITANNIA','CIPLA','COALINDIA','DIVISLAB','DRREDDY',
    'EICHERMOT','GRASIM','HCLTECH','HDFCBANK','HDFCLIFE',
    'HEROMOTOCO','HINDALCO','HINDUNILVR','ICICIBANK','ITC',
    'INDUSINDBK','INFY','JSWSTEEL','KOTAKBANK','LT',
    'LTIM','M&M','MARUTI','NESTLEIND','NTPC',
    'ONGC','POWERGRID','RELIANCE','SBILIFE','SHRIRAMFIN',
    'SBIN','SUNPHARMA','TCS','TATACONSUM','TATAMOTORS',
    'TATASTEEL','TECHM','TITAN','ULTRACEMCO','WIPRO',
]

_top_movers_cache   = {'data': None, 'expires': 0}
_TOP_MOVERS_TTL     = 300

@app.route('/api/market/top-movers', methods=['GET'])
def top_movers():
    global _top_movers_cache
    now = time.time()
    if _top_movers_cache['data'] and now < _top_movers_cache['expires']:
        return jsonify(_top_movers_cache['data'])
    ns_tickers = [f'{s}.NS' for s in NIFTY50_SYMBOLS]
    movers     = []
    try:
        raw = yf.download(tickers=ns_tickers, period='2d', interval='1d',
                          group_by='ticker', auto_adjust=True, progress=False, threads=True)
        for sym, ns in zip(NIFTY50_SYMBOLS, ns_tickers):
            try:
                hist = raw[ns] if ns in raw.columns.get_level_values(0) else None
                if hist is None or hist.empty:
                    continue
                hist = hist.dropna(subset=['Close'])
                if hist.empty:
                    continue
                ltp        = _safe_float(hist['Close'].iloc[-1])
                if ltp is None:
                    continue
                prev_close = _safe_float(hist['Close'].iloc[-2]) if len(hist) >= 2 else ltp
                day_chg_pct = round(((ltp - prev_close) / prev_close * 100), 2) if prev_close else 0
                movers.append({'symbol': sym, 'ltp': round(ltp,2),
                               'prev_close': round(prev_close,2) if prev_close else round(ltp,2),
                               'day_chg_pct': day_chg_pct})
            except Exception:
                continue
    except Exception:
        for sym in NIFTY50_SYMBOLS[:20]:
            q = fetch_quote(sym)
            if q:
                movers.append(q)
    gainers = sorted([m for m in movers if m['day_chg_pct'] > 0], key=lambda x: -x['day_chg_pct'])[:5]
    losers  = sorted([m for m in movers if m['day_chg_pct'] < 0], key=lambda x:  x['day_chg_pct'])[:5]
    result  = {'gainers': gainers, 'losers': losers}
    _top_movers_cache = {'data': result, 'expires': now + _TOP_MOVERS_TTL}
    return jsonify(result)

# ── analysis / score ───────────────────────────────────────────────────────────
def _format_statement_date(col):
    try:    return col.strftime('%d-%b-%Y')
    except: return str(col)[:10]

def _clean_val(value):
    try:
        if value is None or value != value: return None
        if hasattr(value, 'item'):          value = value.item()
        if isinstance(value, (int,float)):  return round(float(value), 2)
        return str(value)
    except Exception: return None

def _statement_to_payload(df, title, max_periods=4):
    try:
        if df is None or df.empty: return {'title':title,'columns':[],'rows':[]}
        df      = df.iloc[:, :max_periods]
        columns = [_format_statement_date(c) for c in df.columns]
        rows    = []
        for metric, row in df.iterrows():
            values = {lbl: _clean_val(row.get(orig)) for orig, lbl in zip(df.columns, columns)}
            rows.append({'metric': str(metric), 'values': values})
        return {'title': title, 'columns': columns, 'rows': rows}
    except Exception as e:
        return {'title': title, 'columns': [], 'rows': [], 'error': str(e)}

@app.route('/api/fundamentals/<symbol>', methods=['GET'])
def get_fundamentals(symbol):
    try:
        t = yf.Ticker(get_nse_ticker(symbol))
        return jsonify({
            'symbol': symbol.upper(),
            'annual_income_statement':    _statement_to_payload(t.financials,              'Annual Income Statement'),
            'quarterly_income_statement': _statement_to_payload(t.quarterly_income_stmt,  'Quarterly Income Statement'),
            'quarterly_balance_sheet':    _statement_to_payload(t.quarterly_balance_sheet, 'Quarterly Balance Sheet'),
            'annual_cash_flow':           _statement_to_payload(t.get_cash_flow(freq='yearly'), 'Annual Cash Flow'),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def _first_available(df, labels, col_idx=0):
    try:
        if df is None or df.empty or len(df.columns) <= col_idx: return None
        for label in labels:
            if label in df.index:
                return _safe_float(df.iloc[df.index.get_loc(label), col_idx])
        idx_lower = {str(i).lower(): i for i in df.index}
        for label in labels:
            l = label.lower()
            for low, real in idx_lower.items():
                if l == low or l in low:
                    return _safe_float(df.loc[real].iloc[col_idx])
    except Exception: pass
    return None

def _pct_change(new, old):
    try:
        if new is None or old in (None, 0): return None
        return round(((new - old) / abs(old)) * 100, 2)
    except Exception: return None

def _cagr(values):
    vals = [v for v in values if v not in (None, 0)]
    try:
        if len(vals) < 2: return None, ''
        latest, oldest, years = vals[0], vals[-1], len(vals)-1
        if oldest <= 0 or latest <= 0: return None, f'({years}Y)'
        return round(((latest/oldest)**(1/years)-1)*100, 2), f'({years}Y)'
    except Exception: return None, ''

def _fmt_b(v, suffix=''):
    if v is None: return '—'
    try:
        if suffix == '%': return f"{v:.1f}%"
        if suffix == 'x': return f"{v:.2f}x"
        return f"{v:,.2f}"
    except Exception: return str(v)

def _score_high(v, bad, ok, good, great):
    if v is None: return 5
    if v >= great: return 10
    if v >= good:  return 8
    if v >= ok:    return 6
    if v >= bad:   return 4
    return 2

def _score_low(v, great, good, ok, bad):
    if v is None: return 5
    if v <= great: return 10
    if v <= good:  return 8
    if v <= ok:    return 6
    if v <= bad:   return 4
    return 2

@app.route('/api/analysis/snapshot-score/<symbol>', methods=['GET'])
def get_snapshot_score(symbol):
    try:
        t    = yf.Ticker(get_nse_ticker(symbol))
        info = {}
        try: info = t.info or {}
        except Exception: pass
        qinc = t.quarterly_income_stmt
        fin  = t.financials
        bs   = t.balance_sheet
        cf   = t.get_cash_flow(freq='yearly')
        hist = t.history(period='1y')

        growth_rows = []
        for display, labels in [('Revenue',['Total Revenue','Operating Revenue']),
                                 ('Operating Profit',['Operating Income','EBIT']),
                                 ('Net Profit',['Net Income','Net Income Common Stockholders']),
                                 ('Diluted EPS',['Diluted EPS','Basic EPS'])]:
            latest  = _first_available(qinc, labels, 0)
            prior   = _first_available(qinc, labels, 1)
            same_ly = _first_available(qinc, labels, 4)
            growth_rows.append({'metric':display,
                'latest':  round(latest,2) if latest is not None else None,
                'prior':   round(prior,2)  if prior  is not None else None,
                'same_ly': round(same_ly,2) if same_ly is not None else None,
                'yoy_pct': _pct_change(latest, same_ly),
                'qoq_pct': _pct_change(latest, prior)})

        revenue        = _first_available(fin, ['Total Revenue','Operating Revenue'], 0)
        prior_revenue  = _first_available(fin, ['Total Revenue','Operating Revenue'], 1)
        net_income     = _first_available(fin, ['Net Income','Net Income Common Stockholders'], 0)
        prior_ni       = _first_available(fin, ['Net Income','Net Income Common Stockholders'], 1)
        ebit           = _first_available(fin, ['EBIT','Operating Income'], 0)
        op_income      = _first_available(fin, ['Operating Income','EBIT'], 0)
        ebitda         = _first_available(fin, ['EBITDA','Normalized EBITDA'], 0)
        prior_ebitda   = _first_available(fin, ['EBITDA','Normalized EBITDA'], 1)
        cfo_vals       = [_first_available(cf, ['Operating Cash Flow','Total Cash From Operating Activities'], i)
                          for i in range(min(5, len(cf.columns) if cf is not None and not cf.empty else 0))]
        latest_cfo     = cfo_vals[0] if cfo_vals else None
        capex          = _first_available(cf, ['Capital Expenditure','Capital Expenditures'], 0) if cf is not None and not cf.empty else None
        fcf            = (latest_cfo + capex) if latest_cfo is not None and capex is not None else None
        cfo_cagr, cfo_period = _cagr(cfo_vals)
        cfo_margin     = round((latest_cfo/revenue)*100,2) if latest_cfo and revenue else None
        cfo_np_ratio   = round(latest_cfo/net_income,2)  if latest_cfo and net_income else None
        op_margin      = round((op_income/revenue)*100,2) if op_income and revenue else None
        net_margin_ss  = round((net_income/revenue)*100,2) if net_income and revenue else None
        op_cf_margin   = round((latest_cfo/revenue)*100,2) if latest_cfo and revenue else None
        fcf_margin     = round((fcf/revenue)*100,2) if fcf and revenue else None
        total_assets   = _first_available(bs, ['Total Assets'], 0)
        prior_assets   = _first_available(bs, ['Total Assets'], 1)
        current_liab   = _first_available(bs, ['Current Liabilities','Total Current Liabilities'], 0)
        cap_employed   = (total_assets - current_liab) if total_assets and current_liab else None
        roce           = round((ebit/cap_employed)*100,2) if ebit and cap_employed else None
        old_rev        = _first_available(fin, ['Total Revenue','Operating Revenue'],
                           min(3, max(0, len(fin.columns)-1))) if fin is not None and not fin.empty else None
        rev_cagr       = _pct_change(revenue, old_rev)
        net_margin     = round((net_income/revenue)*100,2) if net_income and revenue else None
        roe            = info.get('returnOnEquity')
        try: roe = round(float(roe)*100,2) if roe is not None and abs(float(roe)) < 2 else _safe_float(roe)
        except Exception: roe = None
        debt           = _first_available(bs, ['Total Debt','Total Liabilities Net Minority Interest'], 0)
        equity         = _first_available(bs, ['Stockholders Equity','Total Equity Gross Minority Interest'], 0)
        de_ratio       = round(debt/equity,2) if debt and equity else None
        curr_assets    = _first_available(bs, ['Current Assets','Total Current Assets'], 0)
        curr_ratio     = round(curr_assets/current_liab,2) if curr_assets and current_liab else None
        asset_turn     = round(revenue/total_assets,2) if revenue and total_assets else None
        momentum_1y    = None
        try:
            if hist is not None and not hist.empty and len(hist) > 20:
                last, first = float(hist['Close'].iloc[-1]), float(hist['Close'].iloc[0])
                momentum_1y = round((last-first)/first*100,2) if first else None
        except Exception: pass

        score_metrics = [
            {'metric':'Revenue Growth',    'value':rev_cagr,    'benchmark':'>20% strong', 'score':_score_high(rev_cagr,   0,8,15,25),  'pillar':'Growth',        'suffix':'%'},
            {'metric':'Net Margin',        'value':net_margin,  'benchmark':'>15% strong', 'score':_score_high(net_margin,  0,8,15,25),  'pillar':'Profitability', 'suffix':'%'},
            {'metric':'ROE',               'value':roe,         'benchmark':'>18% strong', 'score':_score_high(roe,          0,10,18,25), 'pillar':'Profitability', 'suffix':'%'},
            {'metric':'CFO Margin',        'value':cfo_margin,  'benchmark':'>15% strong', 'score':_score_high(cfo_margin,  0,6,12,20),  'pillar':'Cash Flow',     'suffix':'%'},
            {'metric':'CFO / Net Profit',  'value':cfo_np_ratio,'benchmark':'>1.0x strong','score':_score_high(cfo_np_ratio,0.3,0.7,1.0,1.4),'pillar':'Cash Flow','suffix':'x'},
            {'metric':'Debt / Equity',     'value':de_ratio,    'benchmark':'<0.5x strong', 'score':_score_low(de_ratio,    0.2,0.5,1.0,2.0),'pillar':'Balance Sheet','suffix':'x'},
            {'metric':'Current Ratio',     'value':curr_ratio,  'benchmark':'>1.5x healthy','score':_score_high(curr_ratio, 0.8,1.1,1.5,2.0),'pillar':'Balance Sheet','suffix':'x'},
            {'metric':'Asset Turnover',    'value':asset_turn,  'benchmark':'>1.0x efficient','score':_score_high(asset_turn,0.2,0.5,1.0,1.5),'pillar':'Efficiency','suffix':'x'},
            {'metric':'1Y Price Momentum', 'value':momentum_1y, 'benchmark':'>20% positive','score':_score_high(momentum_1y,-20,0,20,50),'pillar':'Momentum',     'suffix':'%'},
        ]
        weights = {'Growth':20,'Profitability':20,'Cash Flow':25,'Balance Sheet':15,'Efficiency':10,'Momentum':10}
        icons   = {'Growth':'📈','Profitability':'💰','Cash Flow':'🌊','Balance Sheet':'⚖️','Efficiency':'⚙️','Momentum':'🚀'}
        pillars, total = [], 0
        for p_name, wt in weights.items():
            vals = [m['score'] for m in score_metrics if m['pillar'] == p_name]
            avg  = sum(vals)/len(vals)*10 if vals else 50
            total += avg * wt / 100
            items = [{'text':f"{m['metric']}: {_fmt_b(m['value'],m['suffix'])}",
                      'cls':'si-pass' if m['score']>=7 else 'si-warn' if m['score']>=5 else 'si-fail'}
                     for m in score_metrics if m['pillar'] == p_name][:3]
            pillars.append({'name':p_name,'weight':wt,'score':round(avg,1),'icon':icons[p_name],'items':items})
        total  = round(total, 1)
        rating = 'BUY' if total >= 70 else 'SELL' if total < 45 else 'HOLD'
        pills  = [
            {'text':f"ROE {_fmt_b(roe,'%')}",          'cls':'good' if (roe or 0)>=18 else 'warn'},
            {'text':f"CFO/NP {_fmt_b(cfo_np_ratio,'x')}",   'cls':'good' if (cfo_np_ratio or 0)>=1 else 'warn'},
            {'text':f"D/E {_fmt_b(de_ratio,'x')}",     'cls':'good' if de_ratio is not None and de_ratio<=.5 else 'bad'},
        ]
        details = [{'metric':m['metric'],'value':_fmt_b(m['value'],m['suffix']),'benchmark':m['benchmark'],
                    'score':f"{m['score']}/10",'signal':'✓' if m['score']>=7 else '~' if m['score']>=5 else '✗'}
                   for m in score_metrics]
        cans_criteria = [
            {'criterion':'C - Current EPS/Sales',    'metric':'QoQ Net Profit Growth', 'result':_fmt_b(growth_rows[2]['qoq_pct'],'%'), 'pass':(growth_rows[2]['qoq_pct'] or 0)>20},
            {'criterion':'A - Annual earnings',       'metric':'Revenue growth',         'result':_fmt_b(rev_cagr,'%'),                  'pass':(rev_cagr or 0)>15},
            {'criterion':'N - New high / momentum',   'metric':'1Y Price Momentum',      'result':_fmt_b(momentum_1y,'%'),               'pass':(momentum_1y or 0)>20},
            {'criterion':'S - Supply/demand',         'metric':'Volume available',        'result':'Yes' if hist is not None and not hist.empty else '—','pass':hist is not None and not hist.empty},
            {'criterion':'L - Leader',                'metric':'ROE',                    'result':_fmt_b(roe,'%'),                       'pass':(roe or 0)>18},
            {'criterion':'I - Institutional quality', 'metric':'Market cap proxy',        'result':'Pass' if info.get('marketCap') else 'Limited','pass':bool(info.get('marketCap'))},
            {'criterion':'M - Market direction',      'metric':'Stock 1Y trend',          'result':_fmt_b(momentum_1y,'%'),               'pass':(momentum_1y or 0)>0},
        ]
        pio_criteria = [
            {'criterion':'Positive ROA',      'metric':'Net income positive',  'result':'Yes' if (net_income or 0)>0 else 'No','pass':(net_income or 0)>0},
            {'criterion':'Positive CFO',      'metric':'Operating cash flow',  'result':_fmt_b(latest_cfo),                    'pass':(latest_cfo or 0)>0},
            {'criterion':'Accrual quality',   'metric':'CFO > Net profit',     'result':_fmt_b(cfo_np_ratio,'x'),              'pass':(cfo_np_ratio or 0)>1},
            {'criterion':'Lower leverage',    'metric':'D/E < 1',              'result':_fmt_b(de_ratio,'x'),                  'pass':de_ratio is not None and de_ratio<1},
            {'criterion':'Higher liquidity',  'metric':'Current ratio > 1',    'result':_fmt_b(curr_ratio,'x'),                'pass':(curr_ratio or 0)>1},
            {'criterion':'No dilution proxy', 'metric':'Shares info available','result':'Check',                               'pass':True},
            {'criterion':'Higher margin',     'metric':'Net margin positive',  'result':_fmt_b(net_margin,'%'),                'pass':(net_margin or 0)>0},
            {'criterion':'Higher turnover',   'metric':'Asset turnover',       'result':_fmt_b(asset_turn,'x'),                'pass':(asset_turn or 0)>0.5},
            {'criterion':'Profitability quality','metric':'ROE > 12%',         'result':_fmt_b(roe,'%'),                       'pass':(roe or 0)>12},
        ]
        return jsonify({
            'symbol': symbol.upper(),
            'name':   info.get('longName') or info.get('shortName') or symbol.upper(),
            'snapshot': {
                'growth': growth_rows,
                'profitability_cashflow': {'operating_margin':op_margin,'net_margin':net_margin_ss,
                                           'operating_cash_flow_margin':op_cf_margin,'free_cash_flow_margin':fcf_margin},
                'growth_quality': {'revenue_growth':_pct_change(revenue,prior_revenue),'ebitda_growth':_pct_change(ebitda,prior_ebitda),
                                   'net_income_growth':_pct_change(net_income,prior_ni),'asset_growth':_pct_change(total_assets,prior_assets)},
                'cashflow': {'cfo_cagr':cfo_cagr,'cfo_cagr_period':cfo_period,'cfo_margin':cfo_margin,'cfo_np_ratio':cfo_np_ratio,'roce':roce}
            },
            'score': {'total':total,'rating':rating,'pillars':pillars,'pills':pills,'details':details,
                      'canslim':{'score':round(sum(1 for c in cans_criteria if c['pass'])/7*10),'criteria':cans_criteria},
                      'piotroski':{'score':sum(1 for c in pio_criteria if c['pass']),'criteria':pio_criteria}}
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/chart/<symbol>', methods=['GET'])
def get_chart(symbol):
    period = request.args.get('period', '1mo')
    try:
        hist = yf.Ticker(get_nse_ticker(symbol)).history(period=period)
        data = [{'date': str(idx.date()), 'open': round(float(r['Open']),2),
                 'high': round(float(r['High']),2), 'low': round(float(r['Low']),2),
                 'close': round(float(r['Close']),2), 'volume': int(r['Volume'])}
                for idx, r in hist.iterrows()]
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)
