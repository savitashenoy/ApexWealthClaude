from flask import Flask, request, jsonify, send_from_directory, make_response
from flask_cors import CORS
import json, os, uuid, hashlib, hmac, time
from datetime import datetime, date
import yfinance as yf

# ── Vercel KV (Redis) via upstash-redis ────────────────────────────────────────
# pip install upstash-redis
# Env vars set in Vercel dashboard:
#   KV_REST_API_URL   – e.g. https://xxx.upstash.io
#   KV_REST_API_TOKEN – your token
from upstash_redis import Redis as UpstashRedis

def get_kv():
    url   = os.environ.get('KV_REST_API_URL')
    token = os.environ.get('KV_REST_API_TOKEN')
    if not url or not token:
        raise RuntimeError('KV_REST_API_URL and KV_REST_API_TOKEN env vars must be set')
    return UpstashRedis(url=url, token=token)

def kv_get(key, default=None):
    try:
        kv  = get_kv()
        raw = kv.get(key)
        if raw is None:
            return default
        if isinstance(raw, (dict, list)):
            return raw
        return json.loads(raw)
    except Exception:
        return default

def kv_set(key, value):
    try:
        kv = get_kv()
        kv.set(key, json.dumps(value, default=str))
        return True
    except Exception:
        return False

def kv_delete(key):
    """Properly remove a key from KV (kv_set with None stores the string 'null')."""
    try:
        kv = get_kv()
        kv.delete(key)
        return True
    except Exception:
        return False

# KV key helpers
def user_key(email):       return f'user:{email}'
def holdings_key(uid):     return f'holdings:{uid}'
def watchlist_key(uid):    return f'watchlist:{uid}'
def trades_key(uid):       return f'trades:{uid}'
def users_index_key():     return 'users_index'   # list of all email addresses

# ── paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── app ────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

# ── password hashing (PBKDF2 — stdlib only, no C deps) ────────────────────────
_HASH_ITERS = 260_000

def hash_password(pwd):
    salt = os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac('sha256', pwd.encode(), salt.encode(), _HASH_ITERS).hex()
    return f'pbkdf2:{salt}:{h}'

def check_password(pwd, stored):
    try:
        if stored.startswith('pbkdf2:'):
            _, salt, h = stored.split(':', 2)
            candidate = hashlib.pbkdf2_hmac('sha256', pwd.encode(), salt.encode(), _HASH_ITERS).hex()
            return hmac.compare_digest(candidate, h)
        return hmac.compare_digest(hashlib.sha256(pwd.encode()).hexdigest(), stored)
    except Exception:
        return False

# ── yfinance helpers ───────────────────────────────────────────────────────────
def get_nse_ticker(symbol):
    return f'{symbol}.NS'

def _safe_float(v):
    try:
        if v is None or v != v: return None
        if hasattr(v, 'item'): v = v.item()
        return float(v)
    except Exception: return None

def fetch_quote(symbol):
    try:
        ticker = yf.Ticker(get_nse_ticker(symbol))
        hist   = ticker.history(period='2d')
        if hist.empty: return None
        latest     = hist.iloc[-1]
        ltp        = _safe_float(latest['Close'])
        if ltp is None: return None
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
    except Exception: return None

def _pct_change_from(hist, n):
    try:
        ltp  = _safe_float(hist['Close'].iloc[-1])
        idx  = max(0, len(hist) - 1 - n)
        base = _safe_float(hist['Close'].iloc[idx])
        return round(((ltp - base) / base * 100), 2) if ltp and base else 0
    except Exception: return 0

# ── static / index ─────────────────────────────────────────────────────────────
@app.route('/')
def index():
    resp = make_response(send_from_directory(BASE_DIR, 'index.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma']        = 'no-cache'
    resp.headers['Expires']       = '0'
    return resp

@app.route('/admin')
def admin_page():
    resp = make_response(send_from_directory(BASE_DIR, 'admin.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma']        = 'no-cache'
    return resp

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory(os.path.join(BASE_DIR, 'static'), filename)

@app.route('/api/tickers', methods=['GET'])
def get_tickers():
    try:
        with open(os.path.join(BASE_DIR, 'static', 'tickers.json')) as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify([])

@app.route('/api/returns/<symbol>', methods=['GET'])
def get_returns(symbol):
    try:
        hist = yf.Ticker(get_nse_ticker(symbol)).history(period='1y')
        if hist.empty:
            return jsonify({'ret_1d':0,'ret_1w':0,'ret_1m':0,'ret_1y':0})
        def pct(n):
            try:
                ltp  = float(hist['Close'].iloc[-1])
                base = float(hist['Close'].iloc[max(0, len(hist)-1-n)])
                return round((ltp-base)/base*100, 2) if base else 0
            except: return 0
        return jsonify({'ret_1d':pct(1),'ret_1w':pct(5),'ret_1m':pct(22),
                        'ret_1y':pct(min(252,len(hist)-1))})
    except Exception:
        return jsonify({'ret_1d':0,'ret_1w':0,'ret_1m':0,'ret_1y':0})

# ── health / KV connectivity check ────────────────────────────────────────────
@app.route('/api/health', methods=['GET'])
def health():
    try:
        kv = get_kv()
        kv.set('__ping__', '1')
        return jsonify({'status':'ok','kv':'connected'})
    except Exception as e:
        return jsonify({'status':'degraded','kv':'error','detail':str(e)}), 200

# ── auth ───────────────────────────────────────────────────────────────────────
@app.route('/api/signup', methods=['POST'])
def signup():
    data     = request.get_json(silent=True) or {}
    email    = str(data.get('email','')).lower().strip()
    password = str(data.get('password',''))
    if not email or not password:
        return jsonify({'error':'Email and password required'}), 400
    if len(password) < 6:
        return jsonify({'error':'Password must be at least 6 characters'}), 400
    existing = kv_get(user_key(email))
    if existing:
        return jsonify({'error':'Email already registered'}), 409
    user_id = str(uuid.uuid4())
    user    = {'id':user_id,'email':email,'password':hash_password(password),
               'created':str(datetime.now()),
               'status':'pending',    # requires admin approval before login
               'disabled': False}     # can be toggled by admin
    kv_set(user_key(email), user)
    # Maintain index of all users for admin listing
    index = kv_get(users_index_key(), [])
    if email not in index:
        index.append(email)
        kv_set(users_index_key(), index)
    return jsonify({'message':'Account created – pending approval','user_id':user_id,
                    'email':email,'status':'pending'})

@app.route('/api/login', methods=['POST'])
def login():
    data     = request.get_json(silent=True) or {}
    email    = str(data.get('email','')).lower().strip()
    password = str(data.get('password',''))
    user     = kv_get(user_key(email))
    if not user or not check_password(password, user['password']):
        return jsonify({'error':'Invalid credentials'}), 401
    # Legacy accounts (created before status field) default to approved
    status   = user.get('status', 'approved')
    disabled = bool(user.get('disabled', False))
    if status == 'pending':
        return jsonify({'error':'Your account is pending admin approval. Please wait for an admin to activate your account.'}), 403
    if status == 'rejected':
        return jsonify({'error':'Your account has been rejected. Please contact support.'}), 403
    if disabled:
        return jsonify({'error':'Your account has been disabled. Please contact support.'}), 403
    return jsonify({'message':'Login successful','user_id':user['id'],'email':email})

@app.route('/api/change-password', methods=['POST'])
def change_password():
    data    = request.get_json(silent=True) or {}
    email   = str(data.get('email','')).lower().strip()
    old_pwd = str(data.get('old_password',''))
    new_pwd = str(data.get('new_password',''))
    user    = kv_get(user_key(email))
    if not user or not check_password(old_pwd, user['password']):
        return jsonify({'error':'Invalid credentials'}), 401
    user['password'] = hash_password(new_pwd)
    kv_set(user_key(email), user)
    return jsonify({'message':'Password changed'})

# ── portfolio ──────────────────────────────────────────────────────────────────
@app.route('/api/holdings/<user_id>', methods=['GET'])
def get_holdings(user_id):
    raw      = kv_get(holdings_key(user_id), [])
    enriched = []
    for h in raw:
        q = fetch_quote(h['symbol'])
        if q:
            invested = h['buy_price'] * h['qty']
            curr_val = q['ltp'] * h['qty']
            pnl      = curr_val - invested
            enriched.append({**h,'ltp':q['ltp'],'day_chg_pct':q['day_chg_pct'],
                             'invested':round(invested,2),'curr_value':round(curr_val,2),
                             'pnl':round(pnl,2),'pnl_pct':round(pnl/invested*100,2) if invested else 0})
        else:
            invested = h['buy_price'] * h['qty']
            enriched.append({**h,'ltp':h['buy_price'],'day_chg_pct':0,
                             'invested':round(invested,2),'curr_value':round(invested,2),
                             'pnl':0,'pnl_pct':0,'stale':True})
    return jsonify(enriched)

@app.route('/api/holdings/<user_id>', methods=['POST'])
def add_holding(user_id):
    data   = request.get_json(silent=True) or {}
    symbol = str(data.get('symbol','')).strip().upper()
    if not symbol or len(symbol) > 20:
        return jsonify({'error':'Invalid symbol'}), 400
    try:
        buy_price = float(data['buy_price']); qty = float(data['qty'])
    except (KeyError,TypeError,ValueError):
        return jsonify({'error':'buy_price and qty must be valid numbers'}), 400
    if buy_price <= 0: return jsonify({'error':'buy_price must be > 0'}), 400
    if qty <= 0:       return jsonify({'error':'qty must be > 0'}), 400
    holding = {'id':str(uuid.uuid4()),'symbol':symbol,
               'name':str(data.get('name',symbol))[:100],
               'buy_price':round(buy_price,6),'qty':round(qty,6),
               'date':data.get('date',str(date.today())),
               'industry':str(data.get('industry',data.get('sector','')))[:80],'sector':''}
    holdings = kv_get(holdings_key(user_id), [])
    holdings.append(holding)
    kv_set(holdings_key(user_id), holdings)
    return jsonify({'message':'Holding added','holding':holding})

@app.route('/api/holdings/<user_id>/<holding_id>', methods=['PUT'])
def edit_holding(user_id, holding_id):
    data     = request.get_json(silent=True) or {}
    holdings = kv_get(holdings_key(user_id), [])
    for i, h in enumerate(holdings):
        if h['id'] == holding_id:
            holdings[i] = {**h,
                'buy_price':float(data.get('buy_price',h['buy_price'])),
                'qty':      float(data.get('qty',h['qty'])),
                'date':     data.get('date',h['date'])}
            kv_set(holdings_key(user_id), holdings)
            return jsonify({'message':'Updated'})
    return jsonify({'error':'Not found'}), 404

@app.route('/api/holdings/<user_id>/<holding_id>', methods=['DELETE'])
def delete_holding(user_id, holding_id):
    holdings = [h for h in kv_get(holdings_key(user_id),[]) if h['id'] != holding_id]
    kv_set(holdings_key(user_id), holdings)
    return jsonify({'message':'Deleted'})

@app.route('/api/sell/<user_id>/<holding_id>', methods=['POST'])
def sell_holding(user_id, holding_id):
    data      = request.get_json(silent=True) or {}
    holdings  = kv_get(holdings_key(user_id), [])
    holding   = next((h for h in holdings if h['id'] == holding_id), None)
    if not holding: return jsonify({'error':'Not found'}), 404
    avail     = float(holding.get('qty',0))
    sell_qty  = float(data.get('qty', avail))
    sell_price= float(data.get('sell_price',0))
    if sell_price <= 0: return jsonify({'error':'Enter a valid sell price'}), 400
    if sell_qty <= 0 or sell_qty > avail:
        return jsonify({'error':f'Sell qty must be between 0 and {avail:g}'}), 400
    invested = holding['buy_price'] * sell_qty
    pnl      = (sell_price - holding['buy_price']) * sell_qty
    trade    = {'id':str(uuid.uuid4()),'symbol':holding['symbol'],'name':holding.get('name',holding['symbol']),
                'buy_price':holding['buy_price'],'sell_price':sell_price,'qty':sell_qty,
                'buy_date':holding['date'],'sell_date':str(date.today()),
                'pnl':round(pnl,2),'pnl_pct':round(pnl/invested*100,2) if invested else 0}
    tr = kv_get(trades_key(user_id), [])
    tr.append(trade)
    kv_set(trades_key(user_id), tr)
    if sell_qty >= avail:
        updated = [h for h in holdings if h['id'] != holding_id]
    else:
        updated = [dict(h, qty=round(h['qty']-sell_qty,6)) if h['id']==holding_id else h for h in holdings]
    kv_set(holdings_key(user_id), updated)
    return jsonify({'message':'Sold','trade':trade,'remaining_qty':max(0,round(avail-sell_qty,6))})

# ── watchlist (grouped, per-user) ────────────────────────────────────────────
DEFAULT_WL_GROUP = 'Default'

def _load_watchlist_data(user_id):
    """
    Returns {'groups': {name: [items]}, 'order': [name, ...]}.
    Migrates legacy flat-list format (pre-groups) into {'Default': [...]}.
    Always guarantees a 'Default' group exists.
    """
    raw = kv_get(watchlist_key(user_id), None)
    if raw is None:
        return {'groups': {DEFAULT_WL_GROUP: []}, 'order': [DEFAULT_WL_GROUP]}
    if isinstance(raw, list):
        # Legacy format — migrate to grouped structure
        data = {'groups': {DEFAULT_WL_GROUP: raw}, 'order': [DEFAULT_WL_GROUP]}
        kv_set(watchlist_key(user_id), data)
        return data
    if not isinstance(raw, dict):
        return {'groups': {DEFAULT_WL_GROUP: []}, 'order': [DEFAULT_WL_GROUP]}
    groups = raw.get('groups') or {}
    if DEFAULT_WL_GROUP not in groups:
        groups[DEFAULT_WL_GROUP] = []
    order = raw.get('order') or []
    # Ensure order contains every group, Default first
    for g in groups:
        if g not in order:
            order.append(g)
    order = [g for g in order if g in groups]
    if DEFAULT_WL_GROUP in order:
        order = [DEFAULT_WL_GROUP] + [g for g in order if g != DEFAULT_WL_GROUP]
    else:
        order = [DEFAULT_WL_GROUP] + order
    return {'groups': groups, 'order': order}

@app.route('/api/watchlist/<user_id>/groups', methods=['GET'])
def get_watchlist_groups(user_id):
    data = _load_watchlist_data(user_id)
    counts = {g: len(data['groups'].get(g, [])) for g in data['order']}
    return jsonify({'groups': data['order'], 'counts': counts})

@app.route('/api/watchlist/<user_id>/groups', methods=['POST'])
def create_watchlist_group(user_id):
    body = request.get_json(silent=True) or {}
    name = str(body.get('name', '')).strip()
    if not name:
        return jsonify({'error': 'Group name is required'}), 400
    if len(name) > 40:
        return jsonify({'error': 'Group name must be 40 characters or fewer'}), 400
    data = _load_watchlist_data(user_id)
    if name in data['groups']:
        return jsonify({'error': 'A group with this name already exists'}), 409
    data['groups'][name] = []
    data['order'].append(name)
    kv_set(watchlist_key(user_id), data)
    return jsonify({'message': 'Group created', 'groups': data['order']})

@app.route('/api/watchlist/<user_id>/groups/<name>', methods=['DELETE'])
def delete_watchlist_group(user_id, name):
    if name == DEFAULT_WL_GROUP:
        return jsonify({'error': 'The Default group cannot be deleted'}), 400
    data = _load_watchlist_data(user_id)
    if name not in data['groups']:
        return jsonify({'error': 'Group not found'}), 404
    del data['groups'][name]
    data['order'] = [g for g in data['order'] if g != name]
    kv_set(watchlist_key(user_id), data)
    return jsonify({'message': 'Group deleted', 'groups': data['order']})

@app.route('/api/watchlist/<user_id>', methods=['GET'])
def get_watchlist(user_id):
    group = request.args.get('group', DEFAULT_WL_GROUP)
    data  = _load_watchlist_data(user_id)
    return jsonify(data['groups'].get(group, []))

@app.route('/api/watchlist/<user_id>', methods=['POST'])
def add_watchlist(user_id):
    group = request.args.get('group', DEFAULT_WL_GROUP)
    item  = request.get_json(silent=True) or {}
    data  = _load_watchlist_data(user_id)
    if group not in data['groups']:
        data['groups'][group] = []
        data['order'].append(group)
    wl  = data['groups'][group]
    sym = str(item.get('symbol', '')).upper()
    if any(w['symbol'] == sym for w in wl):
        return jsonify({'error': 'Already in watchlist'}), 409
    wl.append({'symbol': sym, 'name': item.get('name', sym), 'added': str(date.today())})
    kv_set(watchlist_key(user_id), data)
    return jsonify({'message': 'Added to watchlist'})

@app.route('/api/watchlist/<user_id>/bulk', methods=['POST'])
def bulk_add_watchlist(user_id):
    """Bulk-import watchlist items into a group (from CSV/XLSX import)."""
    group = request.args.get('group', DEFAULT_WL_GROUP)
    items = request.get_json(silent=True) or []
    data  = _load_watchlist_data(user_id)
    if group not in data['groups']:
        data['groups'][group] = []
        data['order'].append(group)
    wl       = data['groups'][group]
    existing = {w['symbol'] for w in wl}
    added, skipped = 0, 0
    for it in items:
        sym = str(it.get('symbol', '')).upper().strip()
        if not sym or sym in existing:
            skipped += 1; continue
        wl.append({'symbol': sym, 'name': it.get('name', sym), 'added': str(date.today())})
        existing.add(sym); added += 1
    kv_set(watchlist_key(user_id), data)
    return jsonify({'message': f'Imported {added} tickers', 'added': added, 'skipped': skipped})

@app.route('/api/watchlist/<user_id>/<symbol>', methods=['DELETE'])
def remove_watchlist(user_id, symbol):
    group = request.args.get('group', DEFAULT_WL_GROUP)
    data  = _load_watchlist_data(user_id)
    wl    = data['groups'].get(group, [])
    data['groups'][group] = [w for w in wl if w['symbol'] != symbol.upper()]
    kv_set(watchlist_key(user_id), data)
    return jsonify({'message': 'Removed'})

# ── trades ─────────────────────────────────────────────────────────────────────
@app.route('/api/trades/<user_id>', methods=['GET'])
def get_trades(user_id):
    return jsonify(kv_get(trades_key(user_id), []))

# ── admin ──────────────────────────────────────────────────────────────────────
ADMIN_UID  = 'superuser'
ADMIN_PASS = 'June021999'
ADMIN_TOKEN = hashlib.sha256(f'{ADMIN_UID}:{ADMIN_PASS}:apexwealth-admin'.encode()).hexdigest()

def require_admin(req):
    """Check Authorization: Bearer <token> header."""
    auth = req.headers.get('Authorization','')
    token = auth.replace('Bearer ','').strip()
    return token == ADMIN_TOKEN

@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    data = request.get_json(silent=True) or {}
    if data.get('uid') == ADMIN_UID and data.get('password') == ADMIN_PASS:
        # Tell the frontend whether the users_index needs rebuilding
        index_size = len(kv_get(users_index_key(), []))
        return jsonify({'token': ADMIN_TOKEN, 'message': 'Admin login successful',
                        'index_size': index_size})
    return jsonify({'error': 'Invalid admin credentials'}), 401

@app.route('/api/admin/users', methods=['GET'])
def admin_list_users():
    if not require_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    emails = kv_get(users_index_key(), [])
    users  = []
    for email in emails:
        u = kv_get(user_key(email))
        if u:
            users.append({
                'id':           u.get('id', ''),
                'email':        u.get('email', email),
                'created':      u.get('created', ''),
                'has_password': bool(u.get('password')),
                # Legacy accounts without status field default to approved
                'status':   u.get('status', 'approved'),
                'disabled': bool(u.get('disabled', False)),
            })
    q = (request.args.get('q') or '').lower().strip()
    if q:
        users = [u for u in users if q in u['email'].lower()]
    return jsonify(users)

@app.route('/api/admin/users', methods=['POST'])
def admin_create_user():
    if not require_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    data     = request.get_json(silent=True) or {}
    email    = str(data.get('email', '')).lower().strip()
    password = str(data.get('password', ''))
    if not email or not password:
        return jsonify({'error': 'Email and password required'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    if kv_get(user_key(email)):
        return jsonify({'error': 'Email already registered'}), 409
    user_id = str(uuid.uuid4())
    user    = {'id': user_id, 'email': email, 'password': hash_password(password),
               'created': str(datetime.now()),
               'status': 'approved',   # admin-created users are pre-approved
               'disabled': False}
    kv_set(user_key(email), user)
    index = kv_get(users_index_key(), [])
    if email not in index:
        index.append(email)
        kv_set(users_index_key(), index)
    return jsonify({'message': 'User created', 'user_id': user_id, 'email': email})

@app.route('/api/admin/users/<user_id>', methods=['PUT'])
def admin_edit_user(user_id):
    if not require_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    data  = request.get_json(silent=True) or {}
    # Find user by ID
    emails = kv_get(users_index_key(), [])
    target_email = None
    for email in emails:
        u = kv_get(user_key(email))
        if u and u.get('id') == user_id:
            target_email = email; break
    if not target_email:
        return jsonify({'error': 'User not found'}), 404
    user = kv_get(user_key(target_email))
    new_email    = str(data.get('email', target_email)).lower().strip()
    new_password = str(data.get('password', '')).strip()
    if new_password and len(new_password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    if new_password:
        user['password'] = hash_password(new_password)
    # Status: approved / pending / rejected
    if 'status' in data:
        new_status = str(data['status']).lower().strip()
        if new_status in ('approved', 'pending', 'rejected'):
            user['status'] = new_status
    # Enabled / disabled toggle
    if 'disabled' in data:
        user['disabled'] = bool(data['disabled'])
    if new_email and new_email != target_email:
        # Move user to new email key
        if kv_get(user_key(new_email)):
            return jsonify({'error': 'New email already in use'}), 409
        user['email'] = new_email
        kv_set(user_key(new_email), user)
        kv_delete(user_key(target_email))   # properly remove old key
        index = kv_get(users_index_key(), [])
        index = [e for e in index if e != target_email]
        if new_email not in index:
            index.append(new_email)
        kv_set(users_index_key(), index)
    else:
        kv_set(user_key(target_email), user)
    return jsonify({'message': 'User updated'})

@app.route('/api/admin/users/<user_id>', methods=['DELETE'])
def admin_delete_user(user_id):
    if not require_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    emails = kv_get(users_index_key(), [])
    target_email = None
    for email in emails:
        u = kv_get(user_key(email))
        if u and u.get('id') == user_id:
            target_email = email; break
    if not target_email:
        return jsonify({'error': 'User not found'}), 404
    # Delete user data
    # Properly delete all data — kv_set(key, None) stores "null", kv_delete removes the key
    kv_delete(user_key(target_email))
    kv_delete(holdings_key(user_id))
    kv_delete(watchlist_key(user_id))
    kv_delete(trades_key(user_id))
    index = [e for e in emails if e != target_email]
    kv_set(users_index_key(), index)
    return jsonify({'message': 'User deleted'})

@app.route('/api/admin/stats', methods=['GET'])
def admin_stats():
    if not require_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    emails = kv_get(users_index_key(), [])
    return jsonify({'total_users': len(emails)})

@app.route('/api/admin/migrate-users', methods=['POST'])
def admin_migrate_users():
    """
    One-time migration: set status='approved' and disabled=False on every existing user
    that was created before the status field was introduced. Safe to run multiple times.
    """
    if not require_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    emails  = kv_get(users_index_key(), [])
    updated = 0
    for email in emails:
        u = kv_get(user_key(email))
        if not u:
            continue
        changed = False
        if 'status' not in u:
            u['status'] = 'approved'
            changed = True
        if 'disabled' not in u:
            u['disabled'] = False
            changed = True
        if changed:
            kv_set(user_key(email), u)
            updated += 1
    return jsonify({'message': f'Migration complete. {updated} user(s) updated, {len(emails)-updated} already had status field.',
                    'total': len(emails), 'updated': updated})


    """Token verification endpoint — frontend calls this to confirm session is valid."""
    if not require_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({'ok': True})

@app.route('/api/admin/rebuild-index', methods=['POST'])
def admin_rebuild_index():
    """
    Scan all known user keys and rebuild users_index.
    Called once after deploy so existing users (created before the index existed) appear in the list.
    Requires admin token.
    """
    if not require_admin(request):
        return jsonify({'error': 'Unauthorized'}), 401
    # The only way to find all users without a full KV scan is to carry the
    # current index and merge in any user that can be found via the index.
    # For accounts created BEFORE the index existed, the admin can supply a
    # list of known emails via the request body to seed them in.
    data   = request.get_json(silent=True) or {}
    extras = [str(e).lower().strip() for e in data.get('emails', []) if e]
    index  = kv_get(users_index_key(), [])
    added  = 0
    for email in extras:
        if email and email not in index:
            u = kv_get(user_key(email))
            if u:
                index.append(email)
                added += 1
    kv_set(users_index_key(), index)
    return jsonify({'message': f'Index rebuilt. {len(index)} total users, {added} newly added.', 'total': len(index)})

# ── market data ────────────────────────────────────────────────────────────────
@app.route('/api/quote/<symbol>', methods=['GET'])
def get_quote(symbol):
    q = fetch_quote(symbol)
    if q: return jsonify(q)
    return jsonify({'error':'Quote unavailable'}), 404

INDEX_MAP = {
    'nifty50':   {'name':'Nifty 50',  'ticker':'^NSEI'},
    'banknifty': {'name':'Nifty Bank', 'ticker':'^NSEBANK'},
    'sensex':    {'name':'BSE Sensex', 'ticker':'^BSESN'},
}

@app.route('/api/market/indices', methods=['GET'])
def market_indices():
    result = []
    for key, meta in INDEX_MAP.items():
        try:
            t        = yf.Ticker(meta['ticker'])
            hist     = t.history(period='1y')
            intraday = t.history(period='1d', interval='5m')
            src      = intraday if not intraday.empty else hist
            if src.empty: continue
            ltp  = _safe_float(src['Close'].iloc[-1])
            prev = _safe_float(hist['Close'].iloc[-2]) if len(hist) >= 2 else ltp
            chg  = round(((ltp-prev)/prev*100),2) if ltp and prev else 0
            result.append({'key':key,'name':meta['name'],'value':round(ltp,2) if ltp else None,
                           'chg':round((ltp-prev),2) if ltp and prev else 0,'chg_pct':chg,
                           'day_high':round(_safe_float(src['High'].max()) or ltp,2),
                           'day_low': round(_safe_float(src['Low'].min())  or ltp,2),
                           'ret_1d':chg,'ret_1w':_pct_change_from(hist,5),
                           'ret_1m':_pct_change_from(hist,22),
                           'ret_1y':_pct_change_from(hist,min(252,max(1,len(hist)-1)))})
        except Exception: pass
    return jsonify(result)

@app.route('/api/market/index-chart/<index_key>', methods=['GET'])
def market_index_chart(index_key):
    meta = INDEX_MAP.get(index_key.lower())
    if not meta: return jsonify({'error':'Unknown index'}), 404
    period  = request.args.get('period','1d')
    pmap    = {'1d':('1d','5m'),'1w':('5d','30m'),'1m':('1mo','1d'),'1y':('1y','1wk')}
    yp, iv  = pmap.get(period,('1d','5m'))
    try:
        t    = yf.Ticker(meta['ticker'])
        hist = t.history(period=yp, interval=iv)
        if hist.empty: hist = t.history(period=yp)
        data = [{'date':str(idx),'label':idx.strftime('%H:%M') if period=='1d' else idx.strftime('%d %b'),
                 'close':round(float(r['Close']),2),
                 'high': round(_safe_float(r.get('High')) or float(r['Close']),2),
                 'low':  round(_safe_float(r.get('Low'))  or float(r['Close']),2)}
                for idx, r in hist.iterrows() if _safe_float(r.get('Close'))]
        return jsonify({'key':index_key,'name':meta['name'],'period':period,'data':data})
    except Exception as e:
        return jsonify({'error':str(e)}), 500

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
_top_movers_cache = {'data':None,'expires':0}
_TOP_MOVERS_TTL   = 300

@app.route('/api/market/top-movers', methods=['GET'])
def top_movers():
    global _top_movers_cache
    now = time.time()
    if _top_movers_cache['data'] and now < _top_movers_cache['expires']:
        return jsonify(_top_movers_cache['data'])
    ns = [f'{s}.NS' for s in NIFTY50_SYMBOLS]
    movers = []
    try:
        raw = yf.download(tickers=ns,period='2d',interval='1d',
                          group_by='ticker',auto_adjust=True,progress=False,threads=True)
        for sym, nss in zip(NIFTY50_SYMBOLS, ns):
            try:
                h = raw[nss] if nss in raw.columns.get_level_values(0) else None
                if h is None or h.empty: continue
                h = h.dropna(subset=['Close'])
                ltp = _safe_float(h['Close'].iloc[-1])
                if ltp is None: continue
                prev = _safe_float(h['Close'].iloc[-2]) if len(h)>=2 else ltp
                chg  = round(((ltp-prev)/prev*100),2) if prev else 0
                movers.append({'symbol':sym,'ltp':round(ltp,2),'prev_close':round(prev,2),'day_chg_pct':chg})
            except Exception: continue
    except Exception:
        for sym in NIFTY50_SYMBOLS[:20]:
            q = fetch_quote(sym)
            if q: movers.append(q)
    gainers = sorted([m for m in movers if m['day_chg_pct']>0],key=lambda x:-x['day_chg_pct'])[:5]
    losers  = sorted([m for m in movers if m['day_chg_pct']<0],key=lambda x: x['day_chg_pct'])[:5]
    result  = {'gainers':gainers,'losers':losers}
    _top_movers_cache = {'data':result,'expires':now+_TOP_MOVERS_TTL}
    return jsonify(result)

# ── analysis ───────────────────────────────────────────────────────────────────
def _format_statement_date(col):
    try: return col.strftime('%d-%b-%Y')
    except: return str(col)[:10]

def _clean_val(v):
    try:
        if v is None or v!=v: return None
        if hasattr(v,'item'): v=v.item()
        return round(float(v),2) if isinstance(v,(int,float)) else str(v)
    except: return None

def _statement_to_payload(df, title, max_periods=4):
    try:
        if df is None or df.empty: return {'title':title,'columns':[],'rows':[]}
        df = df.iloc[:,:max_periods]
        columns = [_format_statement_date(c) for c in df.columns]
        rows = [{'metric':str(m),'values':{lbl:_clean_val(row.get(orig))
                 for orig,lbl in zip(df.columns,columns)}}
                for m,row in df.iterrows()]
        return {'title':title,'columns':columns,'rows':rows}
    except Exception as e:
        return {'title':title,'columns':[],'rows':[],'error':str(e)}

def _first_available(df, labels, col_idx=0):
    try:
        if df is None or df.empty or len(df.columns)<=col_idx: return None
        for label in labels:
            if label in df.index:
                return _safe_float(df.iloc[df.index.get_loc(label), col_idx])
        idx_lower = {str(i).lower():i for i in df.index}
        for label in labels:
            for low,real in idx_lower.items():
                if label.lower()==low or label.lower() in low:
                    return _safe_float(df.loc[real].iloc[col_idx])
    except Exception: pass
    return None

def _pct_change(new, old):
    try:
        if new is None or old in (None,0): return None
        return round(((new-old)/abs(old))*100,2)
    except: return None

def _cagr(values):
    vals=[v for v in values if v not in (None,0)]
    try:
        if len(vals)<2: return None,''
        l,o,y=vals[0],vals[-1],len(vals)-1
        if o<=0 or l<=0: return None,f'({y}Y)'
        return round(((l/o)**(1/y)-1)*100,2),f'({y}Y)'
    except: return None,''

def _fmt_b(v,suffix=''):
    if v is None: return '—'
    try:
        if suffix=='%': return f'{v:.1f}%'
        if suffix=='x': return f'{v:.2f}x'
        return f'{v:,.2f}'
    except: return str(v)

def _score_high(v,bad,ok,good,great):
    if v is None: return 5
    if v>=great: return 10
    if v>=good:  return 8
    if v>=ok:    return 6
    if v>=bad:   return 4
    return 2

def _score_low(v,great,good,ok,bad):
    if v is None: return 5
    if v<=great: return 10
    if v<=good:  return 8
    if v<=ok:    return 6
    if v<=bad:   return 4
    return 2

@app.route('/api/fundamentals/<symbol>', methods=['GET'])
def get_fundamentals(symbol):
    try:
        t = yf.Ticker(get_nse_ticker(symbol))
        return jsonify({'symbol':symbol.upper(),
            'annual_income_statement':    _statement_to_payload(t.financials,'Annual Income Statement'),
            'quarterly_income_statement': _statement_to_payload(t.quarterly_income_stmt,'Quarterly Income Statement'),
            'quarterly_balance_sheet':    _statement_to_payload(t.quarterly_balance_sheet,'Quarterly Balance Sheet'),
            'annual_cash_flow':           _statement_to_payload(t.get_cash_flow(freq='yearly'),'Annual Cash Flow')})
    except Exception as e:
        return jsonify({'error':str(e)}), 500

@app.route('/api/analysis/snapshot-score/<symbol>', methods=['GET'])
def get_snapshot_score(symbol):
    try:
        t    = yf.Ticker(get_nse_ticker(symbol))
        info = {}
        try: info = t.info or {}
        except: pass
        qinc = t.quarterly_income_stmt
        fin  = t.financials
        bs   = t.balance_sheet
        cf   = t.get_cash_flow(freq='yearly')
        hist = t.history(period='1y')

        growth_rows=[]
        for display,labels in [('Revenue',['Total Revenue','Operating Revenue']),
                                ('Operating Profit',['Operating Income','EBIT']),
                                ('Net Profit',['Net Income','Net Income Common Stockholders']),
                                ('Diluted EPS',['Diluted EPS','Basic EPS'])]:
            latest=_first_available(qinc,labels,0); prior=_first_available(qinc,labels,1); same_ly=_first_available(qinc,labels,4)
            growth_rows.append({'metric':display,'latest':round(latest,2) if latest is not None else None,
                'prior':round(prior,2) if prior is not None else None,
                'same_ly':round(same_ly,2) if same_ly is not None else None,
                'yoy_pct':_pct_change(latest,same_ly),'qoq_pct':_pct_change(latest,prior)})

        revenue=_first_available(fin,['Total Revenue','Operating Revenue'],0)
        prior_revenue=_first_available(fin,['Total Revenue','Operating Revenue'],1)
        net_income=_first_available(fin,['Net Income','Net Income Common Stockholders'],0)
        prior_ni=_first_available(fin,['Net Income','Net Income Common Stockholders'],1)
        ebit=_first_available(fin,['EBIT','Operating Income'],0)
        op_income=_first_available(fin,['Operating Income','EBIT'],0)
        ebitda=_first_available(fin,['EBITDA','Normalized EBITDA'],0)
        prior_ebitda=_first_available(fin,['EBITDA','Normalized EBITDA'],1)
        cf_cols=min(5,len(cf.columns) if cf is not None and not cf.empty else 0)
        cfo_vals=[_first_available(cf,['Operating Cash Flow','Total Cash From Operating Activities'],i) for i in range(cf_cols)]
        latest_cfo=cfo_vals[0] if cfo_vals else None
        capex=_first_available(cf,['Capital Expenditure','Capital Expenditures'],0) if cf is not None and not cf.empty else None
        fcf=(latest_cfo+capex) if latest_cfo is not None and capex is not None else None
        cfo_cagr,cfo_period=_cagr(cfo_vals)
        cfo_margin=round((latest_cfo/revenue)*100,2) if latest_cfo and revenue else None
        cfo_np_ratio=round(latest_cfo/net_income,2) if latest_cfo and net_income else None
        op_margin=round((op_income/revenue)*100,2) if op_income and revenue else None
        net_margin_ss=round((net_income/revenue)*100,2) if net_income and revenue else None
        fcf_margin=round((fcf/revenue)*100,2) if fcf and revenue else None
        total_assets=_first_available(bs,['Total Assets'],0); prior_assets=_first_available(bs,['Total Assets'],1)
        current_liab=_first_available(bs,['Current Liabilities','Total Current Liabilities'],0)
        cap_employed=(total_assets-current_liab) if total_assets and current_liab else None
        roce=round((ebit/cap_employed)*100,2) if ebit and cap_employed else None
        old_rev=_first_available(fin,['Total Revenue','Operating Revenue'],min(3,max(0,len(fin.columns)-1))) if fin is not None and not fin.empty else None
        rev_cagr=_pct_change(revenue,old_rev); net_margin=round((net_income/revenue)*100,2) if net_income and revenue else None
        roe=info.get('returnOnEquity')
        try: roe=round(float(roe)*100,2) if roe is not None and abs(float(roe))<2 else _safe_float(roe)
        except: roe=None
        debt=_first_available(bs,['Total Debt','Total Liabilities Net Minority Interest'],0)
        equity=_first_available(bs,['Stockholders Equity','Total Equity Gross Minority Interest'],0)
        de_ratio=round(debt/equity,2) if debt and equity else None
        curr_assets=_first_available(bs,['Current Assets','Total Current Assets'],0)
        curr_ratio=round(curr_assets/current_liab,2) if curr_assets and current_liab else None
        asset_turn=round(revenue/total_assets,2) if revenue and total_assets else None
        momentum_1y=None
        try:
            if hist is not None and not hist.empty and len(hist)>20:
                last,first=float(hist['Close'].iloc[-1]),float(hist['Close'].iloc[0])
                momentum_1y=round((last-first)/first*100,2) if first else None
        except: pass

        score_metrics=[
            {'metric':'Revenue Growth',   'value':rev_cagr,    'benchmark':'>20% strong','score':_score_high(rev_cagr,  0,8,15,25), 'pillar':'Growth',       'suffix':'%'},
            {'metric':'Net Margin',       'value':net_margin,  'benchmark':'>15% strong','score':_score_high(net_margin, 0,8,15,25), 'pillar':'Profitability','suffix':'%'},
            {'metric':'ROE',              'value':roe,         'benchmark':'>18% strong','score':_score_high(roe,        0,10,18,25),'pillar':'Profitability','suffix':'%'},
            {'metric':'CFO Margin',       'value':cfo_margin,  'benchmark':'>15% strong','score':_score_high(cfo_margin, 0,6,12,20), 'pillar':'Cash Flow',   'suffix':'%'},
            {'metric':'CFO / Net Profit', 'value':cfo_np_ratio,'benchmark':'>1.0x',      'score':_score_high(cfo_np_ratio,0.3,0.7,1.0,1.4),'pillar':'Cash Flow','suffix':'x'},
            {'metric':'Debt / Equity',    'value':de_ratio,    'benchmark':'<0.5x',       'score':_score_low(de_ratio,  0.2,0.5,1.0,2.0),'pillar':'Balance Sheet','suffix':'x'},
            {'metric':'Current Ratio',    'value':curr_ratio,  'benchmark':'>1.5x',       'score':_score_high(curr_ratio,0.8,1.1,1.5,2.0),'pillar':'Balance Sheet','suffix':'x'},
            {'metric':'Asset Turnover',   'value':asset_turn,  'benchmark':'>1.0x',       'score':_score_high(asset_turn,0.2,0.5,1.0,1.5),'pillar':'Efficiency','suffix':'x'},
            {'metric':'1Y Price Momentum','value':momentum_1y, 'benchmark':'>20%',        'score':_score_high(momentum_1y,-20,0,20,50),'pillar':'Momentum','suffix':'%'},
        ]
        weights={'Growth':20,'Profitability':20,'Cash Flow':25,'Balance Sheet':15,'Efficiency':10,'Momentum':10}
        icons={'Growth':'📈','Profitability':'💰','Cash Flow':'🌊','Balance Sheet':'⚖️','Efficiency':'⚙️','Momentum':'🚀'}
        pillars,total=[],0
        for p_name,wt in weights.items():
            vals=[m['score'] for m in score_metrics if m['pillar']==p_name]
            avg=sum(vals)/len(vals)*10 if vals else 50
            total+=avg*wt/100
            items=[{'text':f"{m['metric']}: {_fmt_b(m['value'],m['suffix'])}",'cls':'si-pass' if m['score']>=7 else 'si-warn' if m['score']>=5 else 'si-fail'}
                   for m in score_metrics if m['pillar']==p_name][:3]
            pillars.append({'name':p_name,'weight':wt,'score':round(avg,1),'icon':icons[p_name],'items':items})
        total=round(total,1)
        rating='BUY' if total>=70 else 'SELL' if total<45 else 'HOLD'
        pills=[{'text':f"ROE {_fmt_b(roe,'%')}",'cls':'good' if (roe or 0)>=18 else 'warn'},
               {'text':f"CFO/NP {_fmt_b(cfo_np_ratio,'x')}",'cls':'good' if (cfo_np_ratio or 0)>=1 else 'warn'},
               {'text':f"D/E {_fmt_b(de_ratio,'x')}",'cls':'good' if de_ratio is not None and de_ratio<=.5 else 'bad'}]
        details=[{'metric':m['metric'],'value':_fmt_b(m['value'],m['suffix']),'benchmark':m['benchmark'],
                  'score':f"{m['score']}/10",'signal':'✓' if m['score']>=7 else '~' if m['score']>=5 else '✗'}
                 for m in score_metrics]
        cans=[{'criterion':'C - Current EPS/Sales','metric':'QoQ Net Profit Growth','result':_fmt_b(growth_rows[2]['qoq_pct'],'%'),'pass':(growth_rows[2]['qoq_pct'] or 0)>20},
              {'criterion':'A - Annual earnings','metric':'Revenue growth','result':_fmt_b(rev_cagr,'%'),'pass':(rev_cagr or 0)>15},
              {'criterion':'N - New high / momentum','metric':'1Y Price Momentum','result':_fmt_b(momentum_1y,'%'),'pass':(momentum_1y or 0)>20},
              {'criterion':'S - Supply/demand','metric':'Volume available','result':'Yes' if hist is not None and not hist.empty else '—','pass':hist is not None and not hist.empty},
              {'criterion':'L - Leader','metric':'ROE','result':_fmt_b(roe,'%'),'pass':(roe or 0)>18},
              {'criterion':'I - Institutional quality','metric':'Market cap proxy','result':'Pass' if info.get('marketCap') else 'Limited','pass':bool(info.get('marketCap'))},
              {'criterion':'M - Market direction','metric':'Stock 1Y trend','result':_fmt_b(momentum_1y,'%'),'pass':(momentum_1y or 0)>0}]
        pio=[{'criterion':'Positive ROA','metric':'Net income positive','result':'Yes' if (net_income or 0)>0 else 'No','pass':(net_income or 0)>0},
             {'criterion':'Positive CFO','metric':'Operating cash flow','result':_fmt_b(latest_cfo),'pass':(latest_cfo or 0)>0},
             {'criterion':'Accrual quality','metric':'CFO > Net profit','result':_fmt_b(cfo_np_ratio,'x'),'pass':(cfo_np_ratio or 0)>1},
             {'criterion':'Lower leverage','metric':'D/E < 1','result':_fmt_b(de_ratio,'x'),'pass':de_ratio is not None and de_ratio<1},
             {'criterion':'Higher liquidity','metric':'Current ratio > 1','result':_fmt_b(curr_ratio,'x'),'pass':(curr_ratio or 0)>1},
             {'criterion':'No dilution proxy','metric':'Shares info available','result':'Check','pass':True},
             {'criterion':'Higher margin','metric':'Net margin positive','result':_fmt_b(net_margin,'%'),'pass':(net_margin or 0)>0},
             {'criterion':'Higher turnover','metric':'Asset turnover','result':_fmt_b(asset_turn,'x'),'pass':(asset_turn or 0)>0.5},
             {'criterion':'Profitability quality','metric':'ROE > 12%','result':_fmt_b(roe,'%'),'pass':(roe or 0)>12}]
        return jsonify({'symbol':symbol.upper(),'name':info.get('longName') or info.get('shortName') or symbol.upper(),
            'snapshot':{'growth':growth_rows,
                'profitability_cashflow':{'operating_margin':op_margin,'net_margin':net_margin_ss,'operating_cash_flow_margin':cfo_margin,'free_cash_flow_margin':fcf_margin},
                'growth_quality':{'revenue_growth':_pct_change(revenue,prior_revenue),'ebitda_growth':_pct_change(ebitda,prior_ebitda),'net_income_growth':_pct_change(net_income,prior_ni),'asset_growth':_pct_change(total_assets,prior_assets)},
                'cashflow':{'cfo_cagr':cfo_cagr,'cfo_cagr_period':cfo_period,'cfo_margin':cfo_margin,'cfo_np_ratio':cfo_np_ratio,'roce':roce}},
            'score':{'total':total,'rating':rating,'pillars':pillars,'pills':pills,'details':details,
                     'canslim':{'score':round(sum(1 for c in cans if c['pass'])/7*10),'criteria':cans},
                     'piotroski':{'score':sum(1 for c in pio if c['pass']),'criteria':pio}}})
    except Exception as e:
        return jsonify({'error':str(e)}), 500

@app.route('/api/chart/<symbol>', methods=['GET'])
def get_chart(symbol):
    period = request.args.get('period','1mo')
    try:
        hist = yf.Ticker(get_nse_ticker(symbol)).history(period=period)
        data = [{'date':str(idx.date()),'open':round(float(r['Open']),2),
                 'high':round(float(r['High']),2),'low':round(float(r['Low']),2),
                 'close':round(float(r['Close']),2),'volume':int(r['Volume'])}
                for idx,r in hist.iterrows()]
        return jsonify(data)
    except Exception as e:
        return jsonify({'error':str(e)}), 500



# ════════════════════════════════════════════════════════════════════════════
# SCREENER — 4-scanner stock screener (EMA, Volume, ORB/OHL, Price Action)
# Routes namespaced under /api/scr/* to avoid collision with main app routes.
# ════════════════════════════════════════════════════════════════════════════
import pandas as _pd
import numpy as _np
import operator as _operator
from flask import Response as _Response

_SCR_DATA_FILE = os.path.join(BASE_DIR, 'screener', 'data', 'ScannerData.xlsx')

# ── shared helpers ────────────────────────────────────────────────────────────
def _scr_normalize(symbol: str) -> str:
    s = str(symbol).strip().upper()
    if not s or s in {'NAN', 'NONE'}: return ''
    if s.startswith('NSE:'): s = s.replace('NSE:', '', 1)
    if '.' not in s: s = f'{s}.NS'
    return s

def _scr_clean(symbol: str) -> str:
    return _scr_normalize(symbol).replace('.NS','').replace('.BO','')

def _scr_sheets():
    if not os.path.exists(_SCR_DATA_FILE): return []
    return _pd.ExcelFile(_SCR_DATA_FILE).sheet_names

def _scr_load_symbols(sheet: str):
    if sheet not in _scr_sheets(): raise ValueError(f"Sheet '{sheet}' not found")
    df = _pd.read_excel(_SCR_DATA_FILE, sheet_name=sheet, header=None, dtype=str)
    symbols, seen = [], set()
    for v in df.values.ravel():
        s = _scr_normalize(v)
        if not s or s in seen or len(s) > 25 or ' ' in s: continue
        symbols.append(s); seen.add(s)
    return symbols

def _scr_ema(prices, period):
    return _pd.Series(prices).ewm(span=period, adjust=False).mean()

def _scr_rsi(prices, period=14):
    c = _pd.Series(prices).astype(float)
    d = c.diff()
    g = d.clip(lower=0); l = -d.clip(upper=0)
    ag = g.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    al = l.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = ag / al.replace(0, _np.nan)
    return 100 - (100 / (1 + rs))

def _scr_bb_pos(close, length=20, std=2.0, mode='volume'):
    s = _pd.Series(close)
    if len(s.dropna()) < length: return 'N/A'
    ma = s.rolling(length).mean(); sd = s.rolling(length).std()
    lo, up, mi = float((ma-std*sd).iloc[-1]), float((ma+std*sd).iloc[-1]), float(ma.iloc[-1])
    if any(_pd.isna(x) for x in [lo,up,mi]): return 'N/A'
    p = float(s.iloc[-1]); tol = 0.01
    if p > up+tol: return 'Above Band'
    if p < lo-tol: return 'Below Band'
    if abs(p-up)<=tol: return 'At Upper'
    if abs(p-lo)<=tol: return 'At Lower'
    if abs(p-mi)<=tol: return 'At Middle'
    if mode == 'priceaction':
        hu = mi + 0.5*(up-mi); hl = lo + 0.5*(mi-lo)
        if p > hu: return 'Upper Zone'
        if p > mi: return 'Above Mid'
        if p > hl: return 'Below Mid'
        return 'Lower Zone'
    bw = up - lo
    if bw <= 0: return 'Mid Band'
    pct = ((p - lo) / bw) * 100
    if pct > 75: return 'Upper Band'
    if pct > 60: return 'Above Mid'
    if pct >= 40: return 'Mid Band'
    if pct >= 25: return 'Below Mid'
    return 'Lower Band'

def _scr_fetch(symbol, interval, period=None, days=None, auto_adjust=False):
    symbol = _scr_normalize(symbol)
    if not symbol: return _pd.DataFrame()
    try:
        ticker = yf.Ticker(symbol)
        if period:
            df = ticker.history(period=period, interval=interval)
        else:
            end = datetime.now(); start = end - timedelta(days=days or 365)
            df = ticker.history(start=start, end=end, interval=interval)
        if df is None or df.empty: return _pd.DataFrame()
        # Flatten MultiIndex columns if present
        if isinstance(df.columns, _pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns={c: str(c).lower() for c in df.columns})
        keep = [c for c in df.columns if c in ('open','high','low','close','volume')]
        df = df[keep].dropna(subset=['close']) if keep else _pd.DataFrame()
        if df.empty: return _pd.DataFrame()
        if getattr(df.index,'tz',None) is not None:
            df.index = df.index.tz_localize(None)
        return df
    except Exception:
        return _pd.DataFrame()

# ── Scanner 1: EMA + RSI Bullish Reversal ────────────────────────────────────
def _scr_check_ema(symbol, config):
    tf  = config.get('timeframe','Weekly')
    lb  = int(config.get('lookback_days', 20))
    e1,e2,e3 = int(config.get('ema1',9)), int(config.get('ema2',18)), int(config.get('ema3',27))
    if tf == '60min':   interval, days = '1h',  max(1095, lb*7*3)
    elif tf == 'Daily': interval, days = '1d',  max(730,  lb*4)
    else:               interval, days = '1wk', max(1095, lb*7*3)
    min_needed = max(e1,e2,e3)*2 + 10
    df = _scr_fetch(symbol, interval=interval, days=days)
    if df.empty or len(df) < min_needed: return False, 'Insufficient data'
    df = df.copy(); c = df['close']
    df[f'e{e1}'] = _scr_ema(c,e1); df[f'e{e2}'] = _scr_ema(c,e2); df[f'e{e3}'] = _scr_ema(c,e3)
    df['rsi'] = _scr_rsi(c, 14); df = df.dropna()
    if len(df) < min_needed: return False, 'Insufficient indicator data'
    # Condition A: price was ever below ALL 3 EMAs in full history
    below = (df['close']<df[f'e{e1}']) & (df['close']<df[f'e{e2}']) & (df['close']<df[f'e{e3}'])
    # Condition B: currently above all 3 EMAs
    lat = df.iloc[-1]; cur = float(lat['close'])
    v1,v2,v3 = float(lat[f'e{e1}']), float(lat[f'e{e2}']), float(lat[f'e{e3}'])
    if v1<=0 or v2<=0 or v3<=0: return False, 'Invalid EMA values'
    if not below.any(): return False, 'Never below all EMAs'
    if not (cur>v1 and cur>v2 and cur>v3): return False, 'Not above all EMAs now'
    return True, {'symbol':_scr_normalize(symbol),'current_price':round(cur,2),'rsi14':round(float(lat['rsi']),2),
                  'ema1_diff_pct':round((cur-v1)/v1*100,2),'ema2_diff_pct':round((cur-v2)/v2*100,2),'ema3_diff_pct':round((cur-v3)/v3*100,2)}

# ── Scanner 2: Volume & Price Breakout ────────────────────────────────────────
def _scr_check_volume(symbol, config):
    iv  = config.get('interval','15m')
    vth = float(config.get('volume_threshold',2.0))
    pth = float(config.get('price_threshold',3.0))
    mp  = float(config.get('min_price',100.0))
    rth = float(config.get('rsi_threshold',55.0))
    rl  = int(config.get('rsi_length',14))
    period = '3mo' if iv=='1d' else '30d'
    df = _scr_fetch(symbol, interval=iv, period=period)
    if df.empty: return False, 'No data'
    # Resample sub-hourly to 1h for consistency
    if iv not in ('1h','1d'):
        try:
            df = df.resample('1h').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna(subset=['close'])
        except Exception:
            pass
    if len(df) < max(21, rl+5): return False, 'Insufficient data'
    pv = float(df.iloc[-10:-5]['volume'].mean()); cv = float(df.iloc[-5:]['volume'].mean())
    pp = float(df.iloc[-10:-5]['close'].mean());  cp = float(df.iloc[-5:]['close'].mean())
    ltp = float(df.iloc[-1]['close'])
    if pv<=0 or pp<=0: return False, 'Zero denominator'
    rsi_s = _scr_rsi(df['close'], rl); rsi_v = float(rsi_s.iloc[-1])
    if _pd.isna(rsi_v): return False, 'RSI unavailable'
    vr = cv/pv; pc = (cp-pp)/pp*100; bb = _scr_bb_pos(df['close'], mode='volume')
    if vr>=vth and pc>=pth and ltp>mp and rsi_v>rth and cv>pv:
        return True, {'symbol':_scr_normalize(symbol),'prev_5_vol':round(pv),'curr_5_vol':round(cv),
                      'current_price':round(ltp,2),'volume_ratio':round(vr,2),'price_change_pct':round(pc,2),
                      'rsi':round(rsi_v,1),'bb_position':bb}
    return False, 'No volume breakout'

# ── Scanner 3: ORB + Open High/Low ───────────────────────────────────────────
def _scr_check_ohl(symbol):
    df = _scr_fetch(symbol, interval='1d', period='1mo')
    if df.empty or len(df)<2: return False, 'Insufficient daily data'
    df = df.sort_index(); lat = df.iloc[-1]
    op,hi,lo,cl = float(lat['open']),float(lat['high']),float(lat['low']),float(lat['close'])
    if abs(op-hi)<0.05:   ohl,action = 'OpenHigh','Bearish'
    elif abs(op-lo)<0.05: ohl,action = 'OpenLow', 'Bullish'
    else: return False,'Neither OpenHigh nor OpenLow'
    rsi = _scr_rsi(df['close'],14).iloc[-1]
    pc = float(df['close'].iloc[-2]); chg = (cl-pc)/pc*100 if pc else 0
    return True, {'result_type':'ohl','symbol':_scr_clean(symbol),'ltp':round(cl,2),
                  'change_pct':round(chg,2),'rsi14':round(float(rsi),2) if not _pd.isna(rsi) else None,
                  'open_hl':ohl,'action_type':action}

def _scr_check_orb(symbol, config):
    st = config.get('start_time','09:15'); et = config.get('end_time','10:00')
    vm = float(config.get('vol_multiplier',1.5)); iv = config.get('interval','15m')
    df = _scr_fetch(symbol, interval=iv, period='2d')
    if df.empty or len(df)<5: return False,'Insufficient intraday data'
    # Restore timezone for between_time
    try:
        df.index = _pd.to_datetime(df.index).tz_localize('UTC').tz_convert('Asia/Kolkata')
    except Exception:
        try: df.index = df.index.tz_localize('Asia/Kolkata')
        except Exception: return False,'Timezone error'
    df = df.sort_index()
    last_date = df.index[-1].date()
    today = df[df.index.date == last_date].copy()
    if today.empty: return False,'No today data'
    try: rng = today.between_time(st, et)
    except Exception: return False,'Invalid time range'
    if rng.empty: return False,'Opening range empty'
    oh = float(rng['high'].max()); ol = float(rng['low'].min())
    arv = float(rng['volume'].mean()) or 1.0
    lat = today.iloc[-1]; cl = float(lat['close']); cvol = float(lat['volume'])
    if cvol <= arv*vm: return False,'Volume not met'
    if cl > oh:   sig, bl = 'Bullish', oh
    elif cl < ol: sig, bl = 'Bearish', ol
    else: return False,'No ORB breakout'
    dop = float(today.iloc[0]['open']); dhi = float(today['high'].max()); dlo = float(today['low'].min())
    ost = 'OpenHigh' if abs(dop-dhi)<0.05 else ('OpenLow' if abs(dop-dlo)<0.05 else '-')
    rsi = _scr_rsi(df['close'],14).iloc[-1]
    pc2 = float(df['close'].iloc[-2]) if len(df)>1 else cl
    chg = (cl-pc2)/pc2*100 if pc2 else 0
    volx = cvol/arv if arv else 0
    return True,{'result_type':'orb','symbol':_scr_clean(symbol),'signal':sig,'breakout_level':round(bl,2),
                 'ltp':round(cl,2),'open_hl':ost,'change_pct':round(chg,2),
                 'rsi14':round(float(rsi),2) if not _pd.isna(rsi) else None,'vol_x':round(volx,1)}

def _scr_check_orb_combined(symbol, config):
    rows = []
    if bool(config.get('run_ohl', True)):
        ok,d = _scr_check_ohl(symbol)
        if ok and isinstance(d, dict): rows.append(d)
    if bool(config.get('run_orb', True)):
        ok,d = _scr_check_orb(symbol, config)
        if ok and isinstance(d, dict): rows.append(d)
    return rows

# ── Scanner 4: Price Action condition builder ─────────────────────────────────
_SCR_INTRADAY = {'5 minute':'5m','15 minute':'15m','60 minute':'60m'}
_SCR_OPS = {'<':_operator.lt,'<=':_operator.le,'=':lambda a,b:abs(a-b)<1e-9,'>=':_operator.ge,'>':_operator.gt}

def _scr_candle_val(offset_str, period, vtype, dd, dw, dm, intra):
    try:
        offset = int(str(offset_str).split(' ')[0])
        if period in _SCR_INTRADAY: df = intra.get(_SCR_INTRADAY[period])
        elif period == 'Day':  df = dd
        elif period == 'Week': df = dw
        else:                  df = dm
        if df is None or df.empty: return None
        idx = offset - 1
        if abs(idx) > len(df): return None
        col = str(vtype).lower()
        if col not in df.columns: return None
        return float(df[col].iloc[idx])
    except Exception: return None

def _scr_check_pa(symbol, config):
    conds = [c for c in config.get('conditions',[]) if c.get('active')]
    if not conds: return False,'No active conditions'
    req_intra = {_SCR_INTRADAY[p] for c in conds for p in [c.get('period1'),c.get('period2')] if p in _SCR_INTRADAY}
    dd = _scr_fetch(symbol, interval='1d',  period='1y',  auto_adjust=True)
    if dd.empty or len(dd)<21: return False,'Insufficient daily data'
    dw = _scr_fetch(symbol, interval='1wk', period='5y',  auto_adjust=True)
    dm = _scr_fetch(symbol, interval='1mo', period='5y',  auto_adjust=True)
    if dw.empty or dm.empty: return False,'Insufficient weekly/monthly data'
    intra = {}
    for iv in req_intra:
        intra[iv] = _scr_fetch(symbol, interval=iv, period='60d', auto_adjust=True)
    for cond in conds:
        v1 = _scr_candle_val(cond.get('offset1','0 (current)'), cond.get('period1','Day'),  cond.get('value1','CLOSE'), dd,dw,dm,intra)
        v2 = _scr_candle_val(cond.get('offset2','-1 (ago)'),    cond.get('period2','Month'), cond.get('value2','HIGH'),  dd,dw,dm,intra)
        op = _SCR_OPS.get(cond.get('operator','<'))
        if v1 is None or v2 is None or op is None or not op(v1,v2): return False,'Condition not met'
    c = dd['close']; lat = dd.iloc[-1]; latw = dw.iloc[-1]; latm = dm.iloc[-1]
    rsi = _scr_rsi(c,14).iloc[-1]; bb = _scr_bb_pos(c, mode='priceaction')
    ltp = float(lat['close'])
    pd = float(dd['close'].iloc[-2]) if len(dd)>1 else ltp
    pw = float(dw['close'].iloc[-2]) if len(dw)>1 else ltp
    pm = float(dm['close'].iloc[-2]) if len(dm)>1 else ltp
    vol = int(lat.get('volume',0))
    v10 = float(dd['volume'].iloc[-11:-1].max()) if 'volume' in dd.columns and len(dd)>=11 else 0
    return True,{'symbol':_scr_normalize(symbol),'ltp':round(ltp,2),
                 'change_pct':round((ltp-pd)/pd*100,2) if pd else 0,
                 'rsi_val':round(float(rsi),2) if not _pd.isna(rsi) else None,'bb_pos':bb,
                 'd_close_pct':round((ltp-pd)/pd*100,2) if pd else 0,
                 'w_close_pct':round((ltp-pw)/pw*100,2) if pw else 0,
                 'm_close_pct':round((ltp-pm)/pm*100,2) if pm else 0,
                 'volume':vol,'vol10day_high':bool(vol>v10) if v10 else False}

# ── Scan dispatcher & streaming ───────────────────────────────────────────────
def _scr_scan_symbol(scanner, symbol, config):
    if scanner == 'ema':
        ok,d = _scr_check_ema(symbol, config); return [d] if ok and isinstance(d,dict) else []
    if scanner == 'volume':
        ok,d = _scr_check_volume(symbol, config); return [d] if ok and isinstance(d,dict) else []
    if scanner == 'orb':
        return _scr_check_orb_combined(symbol, config)
    if scanner == 'priceaction':
        ok,d = _scr_check_pa(symbol, config); return [d] if ok and isinstance(d,dict) else []
    raise ValueError('Unknown scanner')

def _scr_iter_events(sheet, scanner, config, max_symbols=None):
    symbols = _scr_load_symbols(sheet)
    if max_symbols: symbols = symbols[:int(max_symbols)]
    total = len(symbols); matches = 0; errors = []
    ev = lambda p: json.dumps(p, default=str) + '\n'
    yield ev({'type':'start','sheet':sheet,'scanner':scanner,'total':total,'scanned':0,'matches':0,'percent':0,'symbol':''})
    for i, sym in enumerate(symbols, 1):
        pct = round((i-1)/total*100, 2) if total else 100
        yield ev({'type':'progress','symbol':sym,'scanned':i-1,'total':total,'matches':matches,'percent':pct,'message':f'Scanning {sym} ({i}/{total})'})
        try:
            for row in _scr_scan_symbol(scanner, sym, config):
                matches += 1
                yield ev({'type':'result','symbol':sym,'row':row,'scanned':i,'total':total,'matches':matches,'percent':round(i/total*100,2) if total else 100})
        except Exception as exc:
            errors.append({'symbol':sym,'error':str(exc)[:160]})
        yield ev({'type':'progress','symbol':sym,'scanned':i,'total':total,'matches':matches,'percent':round(i/total*100,2) if total else 100,'message':f'Completed {sym} ({i}/{total})'})
    yield ev({'type':'done','sheet':sheet,'scanner':scanner,'scanned':total,'total':total,'matches':matches,'percent':100,'errors':errors[:25],'message':f'Scan completed: {matches} matches from {total} symbols.'})

# ── Screener API routes ───────────────────────────────────────────────────────
@app.route('/api/scr/sheets')
def scr_api_sheets():
    return jsonify({'sheets': _scr_sheets(), 'data_file': os.path.basename(_SCR_DATA_FILE)})

@app.route('/api/scr/symbols')
def scr_api_symbols():
    sheet = request.args.get('sheet','')
    try:
        syms = _scr_load_symbols(sheet)
        return jsonify({'sheet':sheet,'count':len(syms),'symbols':syms[:1000]})
    except Exception as e:
        return jsonify({'error':str(e)}), 400

@app.route('/api/scr/scan_stream/<scanner>', methods=['POST'])
def scr_api_scan_stream(scanner):
    if scanner not in {'ema','volume','orb','priceaction'}:
        return jsonify({'error':'Unknown scanner'}), 400
    payload = request.get_json(force=True) or {}
    sheet   = payload.get('sheet')
    config  = payload.get('config', {})
    max_sym = payload.get('max_symbols')
    try:
        return _Response(
            _scr_iter_events(sheet, scanner, config, max_sym),
            mimetype='application/x-ndjson',
            headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'}
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 400

if __name__ == '__main__':
    app.run(debug=True, port=5000)
