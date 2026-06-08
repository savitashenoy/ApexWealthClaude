from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import json, os, uuid
from datetime import datetime, date
import yfinance as yf
import bcrypt

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = Flask(__name__, static_folder=os.path.join(BASE_DIR, 'static'), static_url_path='/static')
CORS(app)

DATA_DIR = os.path.join(BASE_DIR, 'data')
os.makedirs(DATA_DIR, exist_ok=True)

USERS_FILE = os.path.join(DATA_DIR, 'users.json')
PORTFOLIOS_FILE = os.path.join(DATA_DIR, 'portfolios.json')
WATCHLISTS_FILE = os.path.join(DATA_DIR, 'watchlists.json')
TRADES_FILE = os.path.join(DATA_DIR, 'trades.json')

def load_json(path, default=None):
    if default is None:
        default = {}
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, default=str)

def hash_password(pwd):
    return bcrypt.hashpw(pwd.encode(), bcrypt.gensalt()).decode()

def check_password(pwd, hashed):
    try:
        return bcrypt.checkpw(pwd.encode(), hashed.encode())
    except Exception:
        return False

def get_nse_ticker(symbol):
    return f"{symbol}.NS"

def fetch_quote(symbol):
    try:
        ticker = yf.Ticker(get_nse_ticker(symbol))
        info = ticker.fast_info
        hist = ticker.history(period='2d')
        if hist.empty:
            return None
        latest = hist.iloc[-1]
        ltp = float(latest['Close'])
        prev_close = float(hist['Close'].iloc[-2]) if len(hist) >= 2 else ltp
        day_chg_pct = ((ltp - prev_close) / prev_close * 100) if prev_close else 0
        return {
            'symbol': symbol,
            'ltp': round(ltp, 2),
            'prev_close': round(prev_close, 2),
            'day_high': round(float(latest['High']), 2),
            'day_low': round(float(latest['Low']), 2),
            'volume': int(latest['Volume']) if latest.get('Volume') is not None else 0,
            'day_chg_pct': round(day_chg_pct, 2),
        }
    except Exception as e:
        return None


def fetch_return_profile(symbol, quote=None):
    """Return percentage changes for 1D, 1W, 1M and 1Y for a symbol."""
    returns = {'ret_1d': 0, 'ret_1w': 0, 'ret_1m': 0, 'ret_1y': 0}
    try:
        ticker = yf.Ticker(get_nse_ticker(symbol))
        hist = ticker.history(period='1y')
        if hist.empty:
            if quote:
                returns['ret_1d'] = round(float(quote.get('day_chg_pct', 0)), 2)
            return returns

        ltp = float(hist['Close'].iloc[-1])
        def pct_from_rows(rows_back):
            try:
                idx = max(0, len(hist) - 1 - rows_back)
                base = float(hist['Close'].iloc[idx])
                return round(((ltp - base) / base) * 100, 2) if base else 0
            except Exception:
                return 0

        returns['ret_1d'] = round(float(quote.get('day_chg_pct', 0)), 2) if quote else pct_from_rows(1)
        returns['ret_1w'] = pct_from_rows(5)
        returns['ret_1m'] = pct_from_rows(22)
        returns['ret_1y'] = pct_from_rows(min(252, max(1, len(hist) - 1)))
        return returns
    except Exception:
        if quote:
            returns['ret_1d'] = round(float(quote.get('day_chg_pct', 0)), 2)
        return returns

# Serve index.html with no-cache headers to prevent browser serving stale versions
@app.route('/')
def index():
    from flask import make_response
    resp = make_response(send_from_directory(BASE_DIR, 'index.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

# ─── AUTH ─────────────────────────────────────────────────────────────────────

@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.json
    email = data.get('email', '').lower().strip()
    password = data.get('password', '')
    if not email or not password:
        return jsonify({'error': 'Email and password required'}), 400
    users = load_json(USERS_FILE)
    if email in users:
        return jsonify({'error': 'Email already registered'}), 409
    user_id = str(uuid.uuid4())
    users[email] = {'id': user_id, 'email': email, 'password': hash_password(password), 'created': str(datetime.now())}
    save_json(USERS_FILE, users)
    return jsonify({'message': 'Account created', 'user_id': user_id, 'email': email})

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    email = data.get('email', '').lower().strip()
    password = data.get('password', '')
    users = load_json(USERS_FILE)
    user = users.get(email)
    if not user or not check_password(password, user['password']):
        return jsonify({'error': 'Invalid credentials'}), 401
    return jsonify({'message': 'Login successful', 'user_id': user['id'], 'email': email})

@app.route('/api/change-password', methods=['POST'])
def change_password():
    data = request.json
    email = data.get('email', '').lower().strip()
    old_pwd = data.get('old_password', '')
    new_pwd = data.get('new_password', '')
    users = load_json(USERS_FILE)
    user = users.get(email)
    if not user or not check_password(old_pwd, user['password']):
        return jsonify({'error': 'Invalid credentials'}), 401
    users[email]['password'] = hash_password(new_pwd)
    save_json(USERS_FILE, users)
    return jsonify({'message': 'Password changed'})

# ─── PORTFOLIO ────────────────────────────────────────────────────────────────

@app.route('/api/holdings/<user_id>', methods=['GET'])
def get_holdings(user_id):
    portfolios = load_json(PORTFOLIOS_FILE)
    holdings = portfolios.get(user_id, [])
    enriched = []
    for h in holdings:
        q = fetch_quote(h['symbol'])
        if q:
            ltp = q['ltp']
            invested = h['buy_price'] * h['qty']
            curr_val = ltp * h['qty']
            pnl = curr_val - invested
            pnl_pct = (pnl / invested * 100) if invested else 0
            enriched.append({**h, 'ltp': ltp, 'day_chg_pct': q['day_chg_pct'],
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
    data = request.json or {}
    # Validate required fields
    symbol = str(data.get('symbol', '')).strip().upper()
    if not symbol or len(symbol) > 20:
        return jsonify({'error': 'Invalid symbol'}), 400
    try:
        buy_price = float(data['buy_price'])
        qty = float(data['qty'])
    except (KeyError, TypeError, ValueError):
        return jsonify({'error': 'buy_price and qty must be valid numbers'}), 400
    if buy_price <= 0:
        return jsonify({'error': 'buy_price must be greater than 0'}), 400
    if qty <= 0:
        return jsonify({'error': 'qty must be greater than 0'}), 400

    portfolios = load_json(PORTFOLIOS_FILE)
    if user_id not in portfolios:
        portfolios[user_id] = []
    holding = {
        'id': str(uuid.uuid4()),
        'symbol': symbol,
        'name': str(data.get('name', symbol))[:100],
        'buy_price': round(buy_price, 6),
        'qty': round(qty, 6),
        'date': data.get('date', str(date.today())),
        'industry': str(data.get('industry', data.get('sector', '')))[:80],
        'sector': ''
    }
    portfolios[user_id].append(holding)
    save_json(PORTFOLIOS_FILE, portfolios)
    return jsonify({'message': 'Holding added', 'holding': holding})

@app.route('/api/holdings/<user_id>/<holding_id>', methods=['PUT'])
def edit_holding(user_id, holding_id):
    data = request.json
    portfolios = load_json(PORTFOLIOS_FILE)
    holdings = portfolios.get(user_id, [])
    for i, h in enumerate(holdings):
        if h['id'] == holding_id:
            holdings[i] = {**h, 'buy_price': float(data.get('buy_price', h['buy_price'])),
                           'qty': float(data.get('qty', h['qty'])), 'date': data.get('date', h['date'])}
            save_json(PORTFOLIOS_FILE, portfolios)
            return jsonify({'message': 'Updated'})
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/holdings/<user_id>/<holding_id>', methods=['DELETE'])
def delete_holding(user_id, holding_id):
    portfolios = load_json(PORTFOLIOS_FILE)
    holdings = portfolios.get(user_id, [])
    portfolios[user_id] = [h for h in holdings if h['id'] != holding_id]
    save_json(PORTFOLIOS_FILE, portfolios)
    return jsonify({'message': 'Deleted'})

@app.route('/api/sell/<user_id>/<holding_id>', methods=['POST'])
def sell_holding(user_id, holding_id):
    data = request.json
    sell_price = float(data.get('sell_price', 0))
    portfolios = load_json(PORTFOLIOS_FILE)
    holdings = portfolios.get(user_id, [])
    holding = next((h for h in holdings if h['id'] == holding_id), None)
    if not holding:
        return jsonify({'error': 'Not found'}), 404

    available_qty = float(holding.get('qty', 0))
    sell_qty = float(data.get('qty', available_qty))
    if sell_price <= 0:
        return jsonify({'error': 'Enter a valid sell price'}), 400
    if sell_qty <= 0:
        return jsonify({'error': 'Enter a valid sell quantity'}), 400
    if sell_qty > available_qty:
        return jsonify({'error': f'Sell quantity cannot exceed available quantity ({available_qty:g})'}), 400

    # Record trade for the sold quantity only
    trades = load_json(TRADES_FILE)
    if user_id not in trades:
        trades[user_id] = []
    invested_for_sold_qty = holding['buy_price'] * sell_qty
    pnl = (sell_price - holding['buy_price']) * sell_qty
    trade = {
        'id': str(uuid.uuid4()),
        'symbol': holding['symbol'],
        'name': holding.get('name', holding['symbol']),
        'buy_price': holding['buy_price'],
        'sell_price': sell_price,
        'qty': sell_qty,
        'buy_date': holding['date'],
        'sell_date': str(date.today()),
        'pnl': round(pnl, 2),
        'pnl_pct': round((pnl / invested_for_sold_qty) * 100, 2) if invested_for_sold_qty else 0
    }
    trades[user_id].append(trade)
    save_json(TRADES_FILE, trades)

    # Full sell removes the holding; partial sell reduces remaining quantity
    if sell_qty == available_qty:
        portfolios[user_id] = [h for h in holdings if h['id'] != holding_id]
    else:
        for h in holdings:
            if h['id'] == holding_id:
                h['qty'] = round(available_qty - sell_qty, 6)
                break
        portfolios[user_id] = holdings
    save_json(PORTFOLIOS_FILE, portfolios)
    return jsonify({'message': 'Sold', 'trade': trade, 'remaining_qty': max(0, round(available_qty - sell_qty, 6))})

# ─── WATCHLIST ────────────────────────────────────────────────────────────────

@app.route('/api/watchlist/<user_id>', methods=['GET'])
def get_watchlist(user_id):
    watchlists = load_json(WATCHLISTS_FILE)
    items = watchlists.get(user_id, [])
    enriched = []
    for item in items:
        q = fetch_quote(item['symbol'])
        returns = fetch_return_profile(item['symbol'], q)
        if q:
            enriched.append({**item, **q, **returns})
        else:
            enriched.append({**item, **returns})
    return jsonify(enriched)

@app.route('/api/watchlist/<user_id>', methods=['POST'])
def add_watchlist(user_id):
    data = request.json
    watchlists = load_json(WATCHLISTS_FILE)
    if user_id not in watchlists:
        watchlists[user_id] = []
    symbol = data['symbol'].upper()
    if any(w['symbol'] == symbol for w in watchlists[user_id]):
        return jsonify({'error': 'Already in watchlist'}), 409
    watchlists[user_id].append({'symbol': symbol, 'name': data.get('name', symbol),
                                 'industry': data.get('industry', data.get('sector', '')), 'added': str(date.today())})
    save_json(WATCHLISTS_FILE, watchlists)
    return jsonify({'message': 'Added to watchlist'})

@app.route('/api/watchlist/<user_id>/<symbol>', methods=['DELETE'])
def remove_watchlist(user_id, symbol):
    watchlists = load_json(WATCHLISTS_FILE)
    watchlists[user_id] = [w for w in watchlists.get(user_id, []) if w['symbol'] != symbol]
    save_json(WATCHLISTS_FILE, watchlists)
    return jsonify({'message': 'Removed'})

# ─── TRADES HISTORY ───────────────────────────────────────────────────────────

@app.route('/api/trades/<user_id>', methods=['GET'])
def get_trades(user_id):
    trades = load_json(TRADES_FILE)
    return jsonify(trades.get(user_id, []))

# ─── MARKET DATA ──────────────────────────────────────────────────────────────

@app.route('/api/quote/<symbol>', methods=['GET'])
def get_quote(symbol):
    q = fetch_quote(symbol)
    if q:
        return jsonify(q)
    return jsonify({'error': 'Quote unavailable'}), 404

INDEX_MAP = {
    'nifty50': {'name': 'Nifty 50', 'ticker': '^NSEI'},
    'banknifty': {'name': 'Nifty Bank', 'ticker': '^NSEBANK'},
    'sensex': {'name': 'BSE Sensex', 'ticker': '^BSESN'},
}

def _pct_change_from(hist, lookback_rows):
    try:
        if hist is None or hist.empty:
            return 0
        ltp = _safe_float(hist['Close'].iloc[-1])
        if ltp is None:
            return 0
        idx = max(0, len(hist) - 1 - lookback_rows)
        base = _safe_float(hist['Close'].iloc[idx])
        if not base:
            return 0
        return round(((ltp - base) / base) * 100, 2)
    except Exception:
        return 0

@app.route('/api/market/indices', methods=['GET'])
def market_indices():
    result = []
    for key, meta in INDEX_MAP.items():
        try:
            t = yf.Ticker(meta['ticker'])
            hist = t.history(period='1y')
            intraday = t.history(period='1d', interval='5m')
            source = intraday if not intraday.empty else hist
            if source.empty:
                continue
            ltp = _safe_float(source['Close'].iloc[-1])
            day_open = _safe_float(source['Open'].iloc[0]) if not source.empty else ltp
            prev = _safe_float(hist['Close'].iloc[-2]) if len(hist) >= 2 else day_open
            day_high = _safe_float(source['High'].max())
            day_low = _safe_float(source['Low'].min())
            chg_abs = round((ltp - prev), 2) if ltp is not None and prev is not None else 0
            chg_pct = round(((ltp - prev) / prev) * 100, 2) if ltp is not None and prev else 0
            result.append({
                'key': key,
                'name': meta['name'],
                'value': round(ltp, 2) if ltp is not None else None,
                'chg': chg_abs,
                'chg_pct': chg_pct,
                'day_high': round(day_high, 2) if day_high is not None else None,
                'day_low': round(day_low, 2) if day_low is not None else None,
                'ret_1d': chg_pct,
                'ret_1w': _pct_change_from(hist, 5),
                'ret_1m': _pct_change_from(hist, 22),
                'ret_1y': _pct_change_from(hist, min(252, max(1, len(hist)-1))),
            })
        except Exception:
            pass
    return jsonify(result)

@app.route('/api/market/index-chart/<index_key>', methods=['GET'])
def market_index_chart(index_key):
    meta = INDEX_MAP.get(index_key.lower())
    if not meta:
        return jsonify({'error': 'Unknown index'}), 404
    period = request.args.get('period', '1d')
    period_map = {
        '1d': ('1d', '5m'),
        '1w': ('5d', '30m'),
        '1m': ('1mo', '1d'),
        '1y': ('1y', '1wk'),
    }
    yf_period, interval = period_map.get(period, ('1d', '5m'))
    try:
        t = yf.Ticker(meta['ticker'])
        hist = t.history(period=yf_period, interval=interval)
        if hist.empty and interval != '1d':
            hist = t.history(period=yf_period)
        data = []
        for idx, row in hist.iterrows():
            close = _safe_float(row.get('Close'))
            if close is None:
                continue
            label = idx.strftime('%H:%M') if period == '1d' else idx.strftime('%d %b')
            data.append({
                'date': str(idx),
                'label': label,
                'close': round(close, 2),
                'high': round(_safe_float(row.get('High')) or close, 2),
                'low': round(_safe_float(row.get('Low')) or close, 2),
            })
        return jsonify({'key': index_key, 'name': meta['name'], 'period': period, 'data': data})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

NIFTY50_SYMBOLS = [
    'ADANIENT', 'ADANIPORTS', 'APOLLOHOSP', 'ASIANPAINT', 'AXISBANK',
    'BAJAJ-AUTO', 'BAJFINANCE', 'BAJAJFINSV', 'BPCL', 'BHARTIARTL',
    'BRITANNIA', 'CIPLA', 'COALINDIA', 'DIVISLAB', 'DRREDDY',
    'EICHERMOT', 'GRASIM', 'HCLTECH', 'HDFCBANK', 'HDFCLIFE',
    'HEROMOTOCO', 'HINDALCO', 'HINDUNILVR', 'ICICIBANK', 'ITC',
    'INDUSINDBK', 'INFY', 'JSWSTEEL', 'KOTAKBANK', 'LT',
    'LTIM', 'M&M', 'MARUTI', 'NESTLEIND', 'NTPC',
    'ONGC', 'POWERGRID', 'RELIANCE', 'SBILIFE', 'SHRIRAMFIN',
    'SBIN', 'SUNPHARMA', 'TCS', 'TATACONSUM', 'TATAMOTORS',
    'TATASTEEL', 'TECHM', 'TITAN', 'ULTRACEMCO', 'WIPRO',
]

# Cache: {'data': {...}, 'expires': <timestamp>}
_top_movers_cache = {'data': None, 'expires': 0}
_TOP_MOVERS_TTL = 300  # 5 minutes

@app.route('/api/market/top-movers', methods=['GET'])
def top_movers():
    """Batch-fetch all NIFTY 50 stocks and return top 5 gainers/losers.
    Results are cached for 5 minutes to avoid yfinance rate-limiting.
    """
    import time
    global _top_movers_cache

    now = time.time()
    if _top_movers_cache['data'] is not None and now < _top_movers_cache['expires']:
        return jsonify(_top_movers_cache['data'])

    # Build NS-suffixed tickers for batch download
    ns_tickers = [f'{s}.NS' for s in NIFTY50_SYMBOLS]
    movers = []
    try:
        # Download 2 days of data for all symbols in one round-trip
        raw = yf.download(
            tickers=ns_tickers,
            period='2d',
            interval='1d',
            group_by='ticker',
            auto_adjust=True,
            progress=False,
            threads=True,
        )

        for sym, ns in zip(NIFTY50_SYMBOLS, ns_tickers):
            try:
                # Multi-ticker download nests columns under the ticker symbol
                if len(ns_tickers) > 1:
                    hist = raw[ns] if ns in raw.columns.get_level_values(0) else None
                else:
                    hist = raw

                if hist is None or hist.empty or len(hist) < 1:
                    continue

                hist = hist.dropna(subset=['Close'])
                if hist.empty:
                    continue

                ltp = _safe_float(hist['Close'].iloc[-1])
                if ltp is None:
                    continue

                prev_close = _safe_float(hist['Close'].iloc[-2]) if len(hist) >= 2 else ltp
                day_chg_pct = round(((ltp - prev_close) / prev_close * 100), 2) if prev_close else 0

                movers.append({
                    'symbol': sym,
                    'ltp': round(ltp, 2),
                    'prev_close': round(prev_close, 2) if prev_close else round(ltp, 2),
                    'day_chg_pct': day_chg_pct,
                })
            except Exception:
                continue

    except Exception:
        # Batch download failed — fall back to sequential for a subset
        for sym in NIFTY50_SYMBOLS[:20]:
            q = fetch_quote(sym)
            if q:
                movers.append(q)

    gainers = sorted([m for m in movers if m['day_chg_pct'] > 0], key=lambda x: -x['day_chg_pct'])[:5]
    losers  = sorted([m for m in movers if m['day_chg_pct'] < 0], key=lambda x:  x['day_chg_pct'])[:5]
    result = {'gainers': gainers, 'losers': losers}

    _top_movers_cache['data'] = result
    _top_movers_cache['expires'] = now + _TOP_MOVERS_TTL

    return jsonify(result)



def _format_statement_date(col):
    try:
        return col.strftime('%d-%b-%Y')
    except Exception:
        return str(col)[:10]

def _clean_financial_value(value):
    try:
        if value is None:
            return None
        # pandas/numpy NaN support without importing pandas globally
        if value != value:
            return None
        if hasattr(value, 'item'):
            value = value.item()
        if isinstance(value, (int, float)):
            return round(float(value), 2)
        return str(value)
    except Exception:
        return None

def _statement_to_payload(df, title, max_periods=4):
    try:
        if df is None or df.empty:
            return {'title': title, 'columns': [], 'rows': []}
        df = df.iloc[:, :max_periods]
        columns = [_format_statement_date(c) for c in df.columns]
        rows = []
        for metric, row in df.iterrows():
            values = {}
            for original_col, label in zip(df.columns, columns):
                values[label] = _clean_financial_value(row.get(original_col))
            rows.append({'metric': str(metric), 'values': values})
        return {'title': title, 'columns': columns, 'rows': rows}
    except Exception as e:
        return {'title': title, 'columns': [], 'rows': [], 'error': str(e)}

@app.route('/api/fundamentals/<symbol>', methods=['GET'])
def get_fundamentals(symbol):
    """Return last 4 annual/quarterly financial statement columns from yfinance."""
    try:
        ticker = yf.Ticker(get_nse_ticker(symbol))
        return jsonify({
            'symbol': symbol.upper(),
            'annual_income_statement': _statement_to_payload(ticker.financials, 'Annual Income Statement'),
            'quarterly_income_statement': _statement_to_payload(ticker.quarterly_income_stmt, 'Quarterly Income Statement'),
            'quarterly_balance_sheet': _statement_to_payload(ticker.quarterly_balance_sheet, 'Quarterly Balance Sheet'),
            'annual_cash_flow': _statement_to_payload(ticker.get_cash_flow(freq='yearly'), 'Annual Cash Flow'),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _safe_float(v):
    try:
        if v is None or v != v:
            return None
        if hasattr(v, 'item'):
            v = v.item()
        return float(v)
    except Exception:
        return None

def _first_available(df, labels, col_idx=0):
    try:
        if df is None or df.empty or len(df.columns) <= col_idx:
            return None
        for label in labels:
            if label in df.index:
                return _safe_float(df.iloc[df.index.get_loc(label), col_idx])
        # fallback: case-insensitive contains
        idx_lower = {str(i).lower(): i for i in df.index}
        for label in labels:
            l = label.lower()
            for low, real in idx_lower.items():
                if l == low or l in low:
                    return _safe_float(df.loc[real].iloc[col_idx])
    except Exception:
        return None
    return None

def _pct_change(new, old):
    try:
        if new is None or old in (None, 0):
            return None
        return round(((new - old) / abs(old)) * 100, 2)
    except Exception:
        return None

def _cagr(values):
    vals = [v for v in values if v not in (None, 0)]
    try:
        if len(vals) < 2:
            return None, ''
        latest, oldest = vals[0], vals[-1]
        years = len(vals) - 1
        if oldest <= 0 or latest <= 0:
            return None, f'({years}Y)'
        return round(((latest / oldest) ** (1 / years) - 1) * 100, 2), f'({years}Y)'
    except Exception:
        return None, ''

def _fmt_backend(v, suffix=''):
    if v is None:
        return '—'
    try:
        if suffix == '%':
            return f"{v:.1f}%"
        if suffix == 'x':
            return f"{v:.2f}x"
        return f"{v:,.2f}"
    except Exception:
        return str(v)

def _score_high(v, bad, ok, good, great):
    if v is None:
        return 5
    if v >= great: return 10
    if v >= good: return 8
    if v >= ok: return 6
    if v >= bad: return 4
    return 2

def _score_low(v, great, good, ok, bad):
    if v is None:
        return 5
    if v <= great: return 10
    if v <= good: return 8
    if v <= ok: return 6
    if v <= bad: return 4
    return 2

@app.route('/api/analysis/snapshot-score/<symbol>', methods=['GET'])
def get_snapshot_score(symbol):
    """Snapshot and Score data for Analysis tabs, derived from yfinance statements."""
    try:
        yf_symbol = get_nse_ticker(symbol)
        t = yf.Ticker(yf_symbol)
        info = {}
        try:
            info = t.info or {}
        except Exception:
            info = {}
        qinc = t.quarterly_income_stmt
        fin = t.financials
        bs = t.balance_sheet
        qbs = t.quarterly_balance_sheet
        cf = t.get_cash_flow(freq='yearly')
        hist = t.history(period='1y')

        # Snapshot growth rows
        growth_rows = []
        q_metrics = [
            ('Revenue', ['Total Revenue', 'Operating Revenue']),
            ('Operating Profit', ['Operating Income', 'EBIT']),
            ('Net Profit', ['Net Income', 'Net Income Common Stockholders']),
            ('Diluted EPS', ['Diluted EPS', 'Basic EPS'])
        ]
        for display, labels in q_metrics:
            latest = _first_available(qinc, labels, 0)
            prior = _first_available(qinc, labels, 1)
            same_ly = _first_available(qinc, labels, 4)
            growth_rows.append({
                'metric': display,
                'latest': round(latest, 2) if latest is not None else None,
                'prior': round(prior, 2) if prior is not None else None,
                'same_ly': round(same_ly, 2) if same_ly is not None else None,
                'yoy_pct': _pct_change(latest, same_ly),
                'qoq_pct': _pct_change(latest, prior)
            })

        revenue = _first_available(fin, ['Total Revenue', 'Operating Revenue'], 0)
        prior_revenue = _first_available(fin, ['Total Revenue', 'Operating Revenue'], 1)
        net_income = _first_available(fin, ['Net Income', 'Net Income Common Stockholders'], 0)
        prior_net_income = _first_available(fin, ['Net Income', 'Net Income Common Stockholders'], 1)
        ebit = _first_available(fin, ['EBIT', 'Operating Income'], 0)
        operating_income = _first_available(fin, ['Operating Income', 'EBIT'], 0)
        ebitda = _first_available(fin, ['EBITDA', 'Normalized EBITDA'], 0)
        prior_ebitda = _first_available(fin, ['EBITDA', 'Normalized EBITDA'], 1)
        cfo_vals = []
        for i in range(min(5, len(cf.columns) if cf is not None and not cf.empty else 0)):
            cfo_vals.append(_first_available(cf, ['Operating Cash Flow', 'Total Cash From Operating Activities'], i))
        latest_cfo = cfo_vals[0] if cfo_vals else None
        capex = _first_available(cf, ['Capital Expenditure', 'Capital Expenditures'], 0) if cf is not None and not cf.empty else None
        free_cash_flow = (latest_cfo + capex) if latest_cfo is not None and capex is not None else None
        cfo_cagr, cfo_period = _cagr(cfo_vals)
        cfo_margin = round((latest_cfo / revenue) * 100, 2) if latest_cfo is not None and revenue else None
        cfo_np_ratio = round(latest_cfo / net_income, 2) if latest_cfo is not None and net_income else None
        operating_margin = round((operating_income / revenue) * 100, 2) if operating_income is not None and revenue else None
        net_margin_snapshot = round((net_income / revenue) * 100, 2) if net_income is not None and revenue else None
        operating_cf_margin = round((latest_cfo / revenue) * 100, 2) if latest_cfo is not None and revenue else None
        free_cf_margin = round((free_cash_flow / revenue) * 100, 2) if free_cash_flow is not None and revenue else None
        total_assets = _first_available(bs, ['Total Assets'], 0)
        prior_total_assets = _first_available(bs, ['Total Assets'], 1)
        current_liab = _first_available(bs, ['Current Liabilities', 'Total Current Liabilities'], 0)
        capital_employed = (total_assets - current_liab) if total_assets is not None and current_liab is not None else None
        roce = round((ebit / capital_employed) * 100, 2) if ebit is not None and capital_employed else None

        # More score metrics
        latest_rev = revenue
        old_rev = _first_available(fin, ['Total Revenue', 'Operating Revenue'], min(3, max(0, len(fin.columns)-1))) if fin is not None and not fin.empty else None
        rev_cagr = _pct_change(latest_rev, old_rev)
        net_margin = round((net_income / revenue) * 100, 2) if net_income is not None and revenue else None
        roe = info.get('returnOnEquity')
        try: roe = round(float(roe) * 100, 2) if roe is not None and abs(float(roe)) < 2 else _safe_float(roe)
        except Exception: roe = None
        debt = _first_available(bs, ['Total Debt', 'Total Liabilities Net Minority Interest'], 0)
        equity = _first_available(bs, ['Stockholders Equity', 'Total Equity Gross Minority Interest'], 0)
        de_ratio = round(debt / equity, 2) if debt is not None and equity else None
        current_assets = _first_available(bs, ['Current Assets', 'Total Current Assets'], 0)
        current_ratio = round(current_assets / current_liab, 2) if current_assets is not None and current_liab else None
        asset_turnover = round(revenue / total_assets, 2) if revenue is not None and total_assets else None
        momentum_1y = None
        try:
            if hist is not None and not hist.empty and len(hist) > 20:
                last = float(hist['Close'].iloc[-1])
                first = float(hist['Close'].iloc[0])
                momentum_1y = round((last - first) / first * 100, 2) if first else None
        except Exception:
            pass

        revenue_growth_latest = _pct_change(revenue, prior_revenue)
        ebitda_growth = _pct_change(ebitda, prior_ebitda)
        net_income_growth = _pct_change(net_income, prior_net_income)
        asset_growth = _pct_change(total_assets, prior_total_assets)

        score_metrics = [
            {'metric':'Revenue Growth', 'value':rev_cagr, 'benchmark':'>20% strong', 'score':_score_high(rev_cagr, 0, 8, 15, 25), 'pillar':'Growth', 'suffix':'%'},
            {'metric':'Net Margin', 'value':net_margin, 'benchmark':'>15% strong', 'score':_score_high(net_margin, 0, 8, 15, 25), 'pillar':'Profitability', 'suffix':'%'},
            {'metric':'ROE', 'value':roe, 'benchmark':'>18% strong', 'score':_score_high(roe, 0, 10, 18, 25), 'pillar':'Profitability', 'suffix':'%'},
            {'metric':'CFO Margin', 'value':cfo_margin, 'benchmark':'>15% strong', 'score':_score_high(cfo_margin, 0, 6, 12, 20), 'pillar':'Cash Flow', 'suffix':'%'},
            {'metric':'CFO / Net Profit', 'value':cfo_np_ratio, 'benchmark':'>1.0x strong', 'score':_score_high(cfo_np_ratio, 0.3, 0.7, 1.0, 1.4), 'pillar':'Cash Flow', 'suffix':'x'},
            {'metric':'Debt / Equity', 'value':de_ratio, 'benchmark':'<0.5x strong', 'score':_score_low(de_ratio, 0.2, 0.5, 1.0, 2.0), 'pillar':'Balance Sheet', 'suffix':'x'},
            {'metric':'Current Ratio', 'value':current_ratio, 'benchmark':'>1.5x healthy', 'score':_score_high(current_ratio, 0.8, 1.1, 1.5, 2.0), 'pillar':'Balance Sheet', 'suffix':'x'},
            {'metric':'Asset Turnover', 'value':asset_turnover, 'benchmark':'>1.0x efficient', 'score':_score_high(asset_turnover, 0.2, 0.5, 1.0, 1.5), 'pillar':'Efficiency', 'suffix':'x'},
            {'metric':'1Y Price Momentum', 'value':momentum_1y, 'benchmark':'>20% positive', 'score':_score_high(momentum_1y, -20, 0, 20, 50), 'pillar':'Momentum', 'suffix':'%'},
        ]
        weights = {'Growth':20, 'Profitability':20, 'Cash Flow':25, 'Balance Sheet':15, 'Efficiency':10, 'Momentum':10}
        icons = {'Growth':'📈', 'Profitability':'💰', 'Cash Flow':'🌊', 'Balance Sheet':'⚖️', 'Efficiency':'⚙️', 'Momentum':'🚀'}
        pillars = []
        total = 0
        for p_name, wt in weights.items():
            vals = [m['score'] for m in score_metrics if m['pillar'] == p_name]
            avg = sum(vals) / len(vals) * 10 if vals else 50
            total += avg * wt / 100
            items = []
            for m in [m for m in score_metrics if m['pillar'] == p_name][:3]:
                cls = 'si-pass' if m['score'] >= 7 else 'si-warn' if m['score'] >= 5 else 'si-fail'
                items.append({'text': f"{m['metric']}: {_fmt_backend(m['value'], m['suffix'])}", 'cls': cls})
            pillars.append({'name':p_name, 'weight':wt, 'score':round(avg, 1), 'icon':icons[p_name], 'items':items})
        total = round(total, 1)
        rating = 'BUY' if total >= 70 else 'SELL' if total < 45 else 'HOLD'
        pills = [
            {'text': f"ROE {_fmt_backend(roe, '%')}", 'cls': 'good' if (roe or 0) >= 18 else 'warn'},
            {'text': f"CFO/NP {_fmt_backend(cfo_np_ratio, 'x')}", 'cls': 'good' if (cfo_np_ratio or 0) >= 1 else 'warn'},
            {'text': f"D/E {_fmt_backend(de_ratio, 'x')}", 'cls': 'good' if de_ratio is not None and de_ratio <= .5 else 'bad'},
        ]
        details = [{'metric':m['metric'], 'value':_fmt_backend(m['value'], m['suffix']), 'benchmark':m['benchmark'], 'score':f"{m['score']}/10", 'signal':'✓' if m['score']>=7 else '~' if m['score']>=5 else '✗'} for m in score_metrics]

        # Simple CANSLIM and Piotroski models
        cans_criteria = [
            {'criterion':'C - Current EPS/Sales', 'metric':'QoQ Net Profit Growth', 'result':_fmt_backend(growth_rows[2]['qoq_pct'], '%'), 'pass': (growth_rows[2]['qoq_pct'] or 0) > 20},
            {'criterion':'A - Annual earnings', 'metric':'Revenue growth', 'result':_fmt_backend(rev_cagr, '%'), 'pass': (rev_cagr or 0) > 15},
            {'criterion':'N - New high / momentum', 'metric':'1Y Price Momentum', 'result':_fmt_backend(momentum_1y, '%'), 'pass': (momentum_1y or 0) > 20},
            {'criterion':'S - Supply/demand', 'metric':'Latest volume available', 'result': 'Available' if hist is not None and not hist.empty else '—', 'pass': hist is not None and not hist.empty},
            {'criterion':'L - Leader', 'metric':'ROE', 'result':_fmt_backend(roe, '%'), 'pass': (roe or 0) > 18},
            {'criterion':'I - Institutional quality', 'metric':'Market cap/liquidity proxy', 'result':'Pass' if info.get('marketCap') else 'Limited', 'pass': bool(info.get('marketCap'))},
            {'criterion':'M - Market direction', 'metric':'Stock 1Y trend', 'result':_fmt_backend(momentum_1y, '%'), 'pass': (momentum_1y or 0) > 0},
        ]
        cans_score = round(sum(1 for c in cans_criteria if c['pass']) / 7 * 10)
        pio_criteria = [
            {'criterion':'Positive ROA', 'metric':'Net income positive', 'result':'Yes' if (net_income or 0)>0 else 'No', 'pass': (net_income or 0)>0},
            {'criterion':'Positive CFO', 'metric':'Operating cash flow', 'result':_fmt_backend(latest_cfo), 'pass': (latest_cfo or 0)>0},
            {'criterion':'Accrual quality', 'metric':'CFO > Net profit', 'result':_fmt_backend(cfo_np_ratio, 'x'), 'pass': (cfo_np_ratio or 0)>1},
            {'criterion':'Lower leverage', 'metric':'Debt/equity < 1', 'result':_fmt_backend(de_ratio, 'x'), 'pass': de_ratio is not None and de_ratio < 1},
            {'criterion':'Higher liquidity', 'metric':'Current ratio > 1', 'result':_fmt_backend(current_ratio, 'x'), 'pass': (current_ratio or 0)>1},
            {'criterion':'No dilution proxy', 'metric':'Shares info available', 'result':'Check', 'pass': True},
            {'criterion':'Higher margin', 'metric':'Net margin positive', 'result':_fmt_backend(net_margin, '%'), 'pass': (net_margin or 0)>0},
            {'criterion':'Higher turnover', 'metric':'Asset turnover', 'result':_fmt_backend(asset_turnover, 'x'), 'pass': (asset_turnover or 0)>0.5},
            {'criterion':'Profitability quality', 'metric':'ROE > 12%', 'result':_fmt_backend(roe, '%'), 'pass': (roe or 0)>12},
        ]
        pio_score = sum(1 for c in pio_criteria if c['pass'])
        return jsonify({
            'symbol': symbol.upper(),
            'name': info.get('longName') or info.get('shortName') or symbol.upper(),
            'snapshot': {
                'growth': growth_rows,
                'profitability_cashflow': {
                    'operating_margin': operating_margin,
                    'net_margin': net_margin_snapshot,
                    'operating_cash_flow_margin': operating_cf_margin,
                    'free_cash_flow_margin': free_cf_margin
                },
                'growth_quality': {
                    'revenue_growth': revenue_growth_latest,
                    'ebitda_growth': ebitda_growth,
                    'net_income_growth': net_income_growth,
                    'asset_growth': asset_growth
                },
                'cashflow': {'cfo_cagr': cfo_cagr, 'cfo_cagr_period': cfo_period, 'cfo_margin': cfo_margin, 'cfo_np_ratio': cfo_np_ratio, 'roce': roce}
            },
            'score': {'total': total, 'rating': rating, 'pillars': pillars, 'pills': pills, 'details': details, 'canslim': {'score': cans_score, 'criteria': cans_criteria}, 'piotroski': {'score': pio_score, 'criteria': pio_criteria}}
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/chart/<symbol>', methods=['GET'])
def get_chart(symbol):
    period = request.args.get('period', '1mo')
    try:
        ticker = yf.Ticker(get_nse_ticker(symbol))
        hist = ticker.history(period=period)
        data = []
        for idx, row in hist.iterrows():
            data.append({
                'date': str(idx.date()),
                'open': round(float(row['Open']), 2),
                'high': round(float(row['High']), 2),
                'low': round(float(row['Low']), 2),
                'close': round(float(row['Close']), 2),
                'volume': int(row['Volume'])
            })
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)
