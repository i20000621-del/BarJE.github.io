from __future__ import annotations
import json, sqlite3, os, hmac, hashlib, base64
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request, UploadFile, File, Header
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE = Path(__file__).resolve().parent
DB = BASE / 'pos_order.db'
CONFIG = json.loads((BASE / 'config.json').read_text(encoding='utf-8'))

app = FastAPI(title='QR POS Order V2.6')
app.mount('/static', StaticFiles(directory=str(BASE / 'static')), name='static')
UPLOAD_DIR = BASE / 'static' / 'uploads'
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
templates = Jinja2Templates(directory=str(BASE / 'templates'))

# -----------------------------
# V2.6 role login / cloud config
# -----------------------------
def cfg(key: str, default: str = '') -> str:
    return str(os.getenv(key.upper(), CONFIG.get(key, default)))

SECRET_KEY = cfg('secret_key', 'CHANGE_ME_V2_3_SECRET')
ROLE_PASSWORDS = {
    'admin': cfg('admin_password', 'admin123'),
    'kitchen': cfg('kitchen_password', 'kitchen123'),
    'checkout': cfg('checkout_password', 'checkout123'),
}
PRINT_AGENT_TOKEN = cfg('print_agent_token', 'print-agent-token-change-me')

ROLE_LABELS = {'admin': '後台', 'kitchen': '後廚端', 'checkout': '結帳端'}

def sign_value(value: str) -> str:
    sig = hmac.new(SECRET_KEY.encode(), value.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(value.encode() + b'.' + sig).decode()

def unsign_value(token: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(token.encode())
        value, sig = raw.rsplit(b'.', 1)
        good = hmac.new(SECRET_KEY.encode(), value, hashlib.sha256).digest()
        if hmac.compare_digest(sig, good):
            return value.decode()
    except Exception:
        pass
    return ''

def current_role(request: Request) -> str:
    return unsign_value(request.cookies.get('qrpos_role', ''))

def role_allowed(role: str, path: str) -> bool:
    if role == 'admin':
        return True
    if role == 'kitchen':
        return path.startswith('/kds') or path.startswith('/kitchen')
    if role == 'checkout':
        return path.startswith('/checkout') or path.startswith('/redeem-scan')
    return False

@app.middleware('http')
async def role_guard(request: Request, call_next):
    path = request.url.path
    open_prefixes = ['/static', '/t/', '/takeout', '/order/', '/member-center/', '/api/', '/login', '/logout', '/healthz', '/favicon.ico']
    protected_prefixes = ['/admin', '/kds', '/kitchen', '/checkout', '/redeem-scan', '/members', '/rewards', '/sales', '/prep', '/settings']
    if path == '/' or any(path.startswith(p) for p in open_prefixes):
        return await call_next(request)
    if any(path.startswith(p) for p in protected_prefixes):
        role = current_role(request)
        if not role_allowed(role, path):
            return RedirectResponse(f'/login?next={path}', status_code=303)
    return await call_next(request)

@app.get('/healthz')
def healthz():
    return {'ok': True, 'version': '2.6'}

@app.get('/login', response_class=HTMLResponse)
def login_page(request: Request, next: str = '/'):
    return templates.TemplateResponse(request, 'login.html', {'roles': ROLE_LABELS, 'next': next, 'error': ''})

@app.post('/login')
def login(role: str = Form(...), password: str = Form(...), next: str = Form('/')):
    if role in ROLE_PASSWORDS and hmac.compare_digest(password, ROLE_PASSWORDS[role]):
        resp = RedirectResponse(next or '/', status_code=303)
        resp.set_cookie('qrpos_role', sign_value(role), httponly=True, samesite='lax', secure=False, max_age=60*60*12)
        return resp
    return templates.TemplateResponse('login.html', {'roles': ROLE_LABELS, 'next': next, 'error': '密碼錯誤'}, status_code=401)

@app.get('/logout')
def logout():
    resp = RedirectResponse('/login', status_code=303)
    resp.delete_cookie('qrpos_role')
    return resp


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def today_key():
    return datetime.now().strftime('%Y%m%d')

def takeout_cookie_name():
    return 'qrpos_takeout_no'

def next_takeout_no() -> str:
    day = today_key()
    with conn() as c:
        row = c.execute('select last_no from takeout_counters where order_date=?', (day,)).fetchone()
        if row:
            n = int(row['last_no']) + 1
            c.execute('update takeout_counters set last_no=? where order_date=?', (n, day))
        else:
            n = 1
            c.execute('insert into takeout_counters(order_date,last_no) values(?,?)', (day, n))
        c.commit()
    return f'T{day}-{n:03d}'

def is_takeout_no(table_no: str) -> bool:
    return str(table_no).startswith('T') and '-' in str(table_no)

def table_label(table_no: str) -> str:
    return f'外帶 {table_no}' if is_takeout_no(table_no) else f'桌號 {table_no}'

def init_db():
    with conn() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS tables (
            table_no TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'empty',
            capacity INTEGER NOT NULL DEFAULT 4,
            area TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS menu_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            price INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            image_path TEXT DEFAULT '',
            description TEXT DEFAULT '',
            option_groups TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_no TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            payment_status TEXT NOT NULL DEFAULT 'unpaid',
            carrier TEXT DEFAULT '',
            tax_id TEXT DEFAULT '',
            donate_code TEXT DEFAULT '',
            payment_method TEXT DEFAULT '',
            linepay_txn_id TEXT DEFAULT '',
            invoice_no TEXT DEFAULT '',
            member_id INTEGER DEFAULT NULL,
            points_earned INTEGER NOT NULL DEFAULT 0,
            points_used INTEGER NOT NULL DEFAULT 0,
            discount_amount INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            paid_at TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            menu_item_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            qty INTEGER NOT NULL,
            price INTEGER NOT NULL,
            note TEXT DEFAULT '',
            options TEXT DEFAULT '',
            kitchen_status TEXT NOT NULL DEFAULT 'new',
            print_status TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            line_user_id TEXT UNIQUE DEFAULT '',
            display_name TEXT NOT NULL,
            phone TEXT DEFAULT '',
            total_points INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS points_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            change_points INTEGER NOT NULL,
            reason TEXT NOT NULL,
            order_id INTEGER DEFAULT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reward_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            points_cost INTEGER NOT NULL,
            discount_amount INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            description TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            reward_id INTEGER NOT NULL,
            reward_name TEXT NOT NULL,
            points_cost INTEGER NOT NULL,
            discount_amount INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'used',
            order_id INTEGER DEFAULT NULL,
            code TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ingredients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_item_id INTEGER NOT NULL,
            ingredient_name TEXT NOT NULL,
            qty_per_item REAL NOT NULL DEFAULT 1,
            unit TEXT NOT NULL DEFAULT '份'
        );
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS takeout_counters (
            order_date TEXT PRIMARY KEY,
            last_no INTEGER NOT NULL DEFAULT 0
        );
        ''')
        # migrations
        def cols(table): return [r[1] for r in c.execute(f'pragma table_info({table})').fetchall()]
        for table, additions in {
            'tables': {
                'capacity': "ALTER TABLE tables ADD COLUMN capacity INTEGER NOT NULL DEFAULT 4",
                'area': "ALTER TABLE tables ADD COLUMN area TEXT DEFAULT ''",
            },
            'menu_items': {
                'image_path': "ALTER TABLE menu_items ADD COLUMN image_path TEXT DEFAULT ''",
                'description': "ALTER TABLE menu_items ADD COLUMN description TEXT DEFAULT ''",
                'option_groups': "ALTER TABLE menu_items ADD COLUMN option_groups TEXT DEFAULT ''",
            },
            'order_items': {
                'options': "ALTER TABLE order_items ADD COLUMN options TEXT DEFAULT ''",
            },
            'orders': {
                'member_id': "ALTER TABLE orders ADD COLUMN member_id INTEGER DEFAULT NULL",
                'points_earned': "ALTER TABLE orders ADD COLUMN points_earned INTEGER NOT NULL DEFAULT 0",
                'points_used': "ALTER TABLE orders ADD COLUMN points_used INTEGER NOT NULL DEFAULT 0",
                'discount_amount': "ALTER TABLE orders ADD COLUMN discount_amount INTEGER NOT NULL DEFAULT 0",
            }
        }.items():
            existing = cols(table)
            for col, ddl in additions.items():
                if col not in existing:
                    c.execute(ddl)
        if c.execute('select count(*) from tables').fetchone()[0] == 0:
            c.executemany('insert into tables(table_no) values(?)', [(f'A{i:02d}',) for i in range(1, 11)])
        if c.execute('select count(*) from menu_items').fetchone()[0] == 0:
            sample = [('主餐','雞腿飯',120),('主餐','排骨飯',110),('主餐','牛肉麵',150),('飲料','紅茶',30),('飲料','奶茶',40),('小菜','燙青菜',45)]
            c.executemany('insert into menu_items(category,name,price) values(?,?,?)', sample)
        if c.execute('select count(*) from reward_items').fetchone()[0] == 0:
            rewards = [('折抵 30 元',10,30,'結帳折抵 30 元'),('兌換紅茶 1 杯',8,30,'核銷後由櫃台出杯'),('折抵 100 元',30,100,'結帳折抵 100 元')]
            c.executemany('insert into reward_items(name,points_cost,discount_amount,description) values(?,?,?,?)', rewards)
        c.commit()

init_db()

def rows(sql, args=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]

def one(sql, args=()):
    with conn() as c:
        r = c.execute(sql, args).fetchone()
        return dict(r) if r else None

def order_total(order_id:int)->int:
    r = one('select coalesce(sum(qty*price),0) total from order_items where order_id=?', (order_id,))
    return int(r['total'])

def unpaid_orders_for_table(table_no:str):
    orders = rows("select * from orders where table_no=? and payment_status='unpaid' order by id", (table_no,))
    for o in orders:
        o['items'] = rows('select * from order_items where order_id=?', (o['id'],))
        o['total'] = order_total(o['id'])
    return orders

def parse_option_groups(text: str) -> list[dict]:
    """
    支援格式：
      辣度=不辣,小辣,中辣,大辣
      辣度:不辣/小辣/中辣/大辣
      冰量：正常冰、少冰、去冰
    若沒有分隔符，會嘗試辨識常見選項，例如：不辣小辣中辣大辣。
    """
    import re
    groups = []
    common_tokens = [
        '正常冰','少冰','微冰','去冰','熱飲',
        '全糖','七分糖','半糖','微糖','無糖',
        '不辣','小辣','中辣','大辣',
        '正常','加飯','少飯','不要飯',
        '珍珠','椰果','布丁','仙草',
    ]
    token_pattern = re.compile('|'.join(map(re.escape, sorted(common_tokens, key=len, reverse=True))))
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        if '=' in line:
            name, values = line.split('=', 1)
        elif '：' in line:
            name, values = line.split('：', 1)
        elif ':' in line:
            name, values = line.split(':', 1)
        else:
            continue
        name = name.strip()
        values = values.strip()
        opts = [v.strip() for v in re.split(r'[,，、/／|｜;；\s]+', values) if v.strip()]
        if len(opts) <= 1 and values:
            matched = token_pattern.findall(values)
            if matched and ''.join(matched) == re.sub(r'[,，、/／|｜;；\s]+', '', values):
                opts = matched
        # 去除重複，保留順序
        dedup = []
        for opt in opts:
            if opt not in dedup:
                dedup.append(opt)
        if name and dedup:
            groups.append({'name': name, 'options': dedup})
    return groups

def save_upload(file: UploadFile | None) -> str:
    if not file or not file.filename:
        return ''
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ['.jpg', '.jpeg', '.png', '.webp', '.gif']:
        suffix = '.jpg'
    name = f"menu_{datetime.now().strftime('%Y%m%d%H%M%S%f')}{suffix}"
    target = UPLOAD_DIR / name
    data = file.file.read()
    if data:
        target.write_bytes(data)
        return f'/static/uploads/{name}'
    return ''

def find_member(keyword: str):
    kw = (keyword or '').strip()
    if not kw:
        return None
    return one('select * from members where id=?', (int(kw),)) if kw.isdigit() else one('select * from members where phone=? or line_user_id=?', (kw, kw))

def add_points(member_id:int, change:int, reason:str, order_id: Optional[int]=None):
    with conn() as c:
        c.execute('update members set total_points = total_points + ? where id=?', (change, member_id))
        c.execute('insert into points_ledger(member_id,change_points,reason,order_id,created_at) values(?,?,?,?,?)', (member_id, change, reason, order_id, now_str()))
        c.commit()

@app.get('/', response_class=HTMLResponse)
def portal(request: Request):
    return templates.TemplateResponse(request, 'portal.html', {'config':CONFIG})

@app.get('/admin', response_class=HTMLResponse)
def admin_home(request: Request):
    return templates.TemplateResponse(request, 'portal.html', {'config':CONFIG})

@app.get('/admin/tables', response_class=HTMLResponse)
def home(request: Request, error: str = '', message: str = ''):
    tables = rows('select * from tables order by table_no')
    return templates.TemplateResponse(request, 'home.html', {'tables':tables,'config':CONFIG,'error':error,'message':message})

@app.post('/admin/tables/add')
def add_table(table_no: str = Form(...), capacity: int = Form(4), area: str = Form('')):
    table_no = (table_no or '').strip().upper()
    area = (area or '').strip()
    if not table_no:
        return RedirectResponse('/admin/tables?error=請輸入桌號', status_code=303)
    with conn() as c:
        exists = c.execute('select 1 from tables where table_no=?', (table_no,)).fetchone()
        if exists:
            return RedirectResponse('/admin/tables?error=桌號已存在', status_code=303)
        c.execute('insert into tables(table_no,capacity,area,status) values(?,?,?,?)', (table_no, max(1, int(capacity or 1)), area, 'empty'))
        c.commit()
    return RedirectResponse('/admin/tables?message=桌號已新增', status_code=303)

@app.post('/admin/tables/update')
def update_table(old_table_no: str = Form(...), table_no: str = Form(...), capacity: int = Form(4), area: str = Form(''), status: str = Form('empty')):
    old_table_no = (old_table_no or '').strip().upper()
    table_no = (table_no or '').strip().upper()
    area = (area or '').strip()
    status = status if status in ['empty','occupied','reserved','disabled'] else 'empty'
    if not old_table_no or not table_no:
        return RedirectResponse('/admin/tables?error=桌號不可空白', status_code=303)
    with conn() as c:
        if old_table_no != table_no and c.execute('select 1 from tables where table_no=?', (table_no,)).fetchone():
            return RedirectResponse('/admin/tables?error=新桌號已存在', status_code=303)
        c.execute('update tables set table_no=?, capacity=?, area=?, status=? where table_no=?', (table_no, max(1, int(capacity or 1)), area, status, old_table_no))
        # 同步未結帳訂單桌號，避免改桌號後結帳端找不到舊桌號訂單
        if old_table_no != table_no:
            c.execute("update orders set table_no=? where table_no=? and payment_status='unpaid'", (table_no, old_table_no))
        c.commit()
    return RedirectResponse('/admin/tables?message=桌號已更新', status_code=303)

@app.post('/admin/tables/delete')
def delete_table(table_no: str = Form(...)):
    table_no = (table_no or '').strip().upper()
    with conn() as c:
        active = c.execute("select count(*) from orders where table_no=? and payment_status='unpaid'", (table_no,)).fetchone()[0]
        if active:
            return RedirectResponse('/admin/tables?error=此桌仍有未結帳訂單，不能刪除', status_code=303)
        c.execute('delete from tables where table_no=?', (table_no,))
        c.commit()
    return RedirectResponse('/admin/tables?message=桌號已刪除', status_code=303)


@app.get('/t/{table_no}', response_class=HTMLResponse)
def table_landing(request: Request, table_no:str):
    return templates.TemplateResponse(request, 'landing.html', {'table_no':table_no,'config':CONFIG})

@app.get('/takeout')
def takeout_entry(request: Request):
    cookie = request.cookies.get(takeout_cookie_name(), '')
    today = today_key()
    if cookie.startswith(f'T{today}-'):
        takeout_no = cookie
    else:
        takeout_no = next_takeout_no()
    resp = RedirectResponse(f'/order/{takeout_no}', status_code=303)
    resp.set_cookie(takeout_cookie_name(), takeout_no, httponly=True, samesite='lax', secure=False, max_age=60*60*12, path='/')
    return resp


def cart_cookie_name(table_no: str) -> str:
    safe = ''.join(ch for ch in table_no if ch.isalnum() or ch in ('_', '-')) or 'table'
    return 'qrpos_cart_' + safe

def load_cart(request: Request, table_no: str) -> list[dict]:
    raw = request.cookies.get(cart_cookie_name(table_no), '')
    if not raw:
        return []
    try:
        data = base64.urlsafe_b64decode(raw.encode()).decode('utf-8')
        cart = json.loads(data)
        if isinstance(cart, list):
            return cart
    except Exception:
        pass
    return []

def dump_cart(cart: list[dict]) -> str:
    data = json.dumps(cart, ensure_ascii=False, separators=(',', ':'))
    return base64.urlsafe_b64encode(data.encode('utf-8')).decode('ascii')

def cart_summary(cart: list[dict]) -> tuple[int, int]:
    total = 0
    count = 0
    for i in cart:
        qty = int(i.get('qty') or 0)
        price = int(i.get('price') or 0)
        total += qty * price
        count += qty
    return total, count

def render_cart_page(request: Request, table_no: str, cart: list[dict]) -> HTMLResponse:
    import html
    total, count = cart_summary(cart)
    rows_html = ''
    for idx, i in enumerate(cart):
        name = html.escape(str(i.get('name', '')))
        options = html.escape(str(i.get('options', '')))
        note = html.escape(str(i.get('note', '')))
        qty = int(i.get('qty') or 0)
        price = int(i.get('price') or 0)
        rows_html += f"""<div class="cart-item">
          <div><b>{name}</b><div class="muted">{options}</div><div class="muted">{note}</div></div>
          <div class="cart-item-right">x {qty}<br>NT$ {qty*price}<br><a class="remove-link" href="/order/{html.escape(table_no)}/cart/remove/{idx}">移除</a></div>
        </div>"""
    if not rows_html:
        rows_html = '<div class="card">購物車目前沒有餐點。</div>'
    disabled = 'disabled' if count <= 0 else ''
    content = f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>購物車</title><link rel="stylesheet" href="/static/style.css"></head><body>
    <div class="client-head"><b>{html.escape(str(CONFIG.get('store_name','QR POS Order')))}</b></div>
    <div class="wrap client-wrap">
      <a class="btn gray" href="/order/{html.escape(table_no)}">← 返回菜單</a>
      <h2>購物車｜桌號 {html.escape(table_no)}</h2>
      {rows_html}
      <div class="card cart-total"><span>共 {count} 項</span><b>總價：NT$ {total}</b></div>
      <form method="post" action="/order/{html.escape(table_no)}/cart/submit">
        <button class="btn green full-btn" type="submit" {disabled}>送出訂單</button>
      </form>
    </div></body></html>"""
    return HTMLResponse(content)

@app.get('/order/{table_no}/member/{member_id}', response_class=HTMLResponse)
def order_page_member(request: Request, table_no:str, member_id:int):
    member = one('select * from members where id=?', (member_id,))
    items = rows('select * from menu_items where enabled=1 order by category,id')
    cats = sorted(set(i['category'] for i in items))
    cart_total, cart_count = cart_summary(load_cart(request, table_no))
    return templates.TemplateResponse(request, 'order.html', {'table_no':table_no,'items':items,'cats':cats,'config':CONFIG,'member':member,'cart_total':cart_total,'cart_count':cart_count})

@app.get('/order/{table_no}', response_class=HTMLResponse)
def order_page(request: Request, table_no:str):
    items = rows('select * from menu_items where enabled=1 order by category,id')
    cats = sorted(set(i['category'] for i in items))
    cart_total, cart_count = cart_summary(load_cart(request, table_no))
    return templates.TemplateResponse(request, 'order.html', {'table_no':table_no,'items':items,'cats':cats,'config':CONFIG,'cart_total':cart_total,'cart_count':cart_count})

@app.get('/order/{table_no}/item/{item_id}', response_class=HTMLResponse)
def item_detail(request: Request, table_no:str, item_id:int):
    item = one('select * from menu_items where id=? and enabled=1', (item_id,))
    if not item:
        return RedirectResponse(f'/order/{table_no}', status_code=303)
    groups = parse_option_groups(item.get('option_groups',''))
    return templates.TemplateResponse(request, 'item_detail.html', {'table_no':table_no,'item':item,'groups':groups,'config':CONFIG})

@app.post('/order/{table_no}/submit')
def submit_order(table_no:str, item_ids: list[int] = Form(default=[]), qtys: list[int] = Form(default=[]), notes: list[str] = Form(default=[])):
    chosen = []
    with conn() as c:
        for iid, qty, note in zip(item_ids, qtys, notes):
            if qty and qty > 0:
                m = c.execute('select * from menu_items where id=? and enabled=1', (iid,)).fetchone()
                if m: chosen.append((m, qty, note))
        if not chosen:
            return RedirectResponse(f'/order/{table_no}', status_code=303)
        cur = c.execute('insert into orders(table_no,created_at) values(?,?)', (table_no, now_str()))
        oid = cur.lastrowid
        for m, qty, note in chosen:
            c.execute('insert into order_items(order_id,menu_item_id,name,qty,price,note,options) values(?,?,?,?,?,?,?)', (oid, m['id'], m['name'], qty, m['price'], note, ''))
        
        if not is_takeout_no(table_no):
            c.execute("update tables set status='dining' where table_no=?", (table_no,))
        c.commit()
    return RedirectResponse(f'/order/{table_no}/done?order_id={oid}', status_code=303)

@app.post('/order/{table_no}/item/{item_id}/submit')
async def submit_item_detail(request: Request, table_no:str, item_id:int, qty:int=Form(1), note:str=Form('')):
    return await add_item_to_cart(request, table_no, item_id, qty, note)

@app.post('/order/{table_no}/cart/add')
async def add_item_to_cart(request: Request, table_no:str, item_id:int=Form(...), qty:int=Form(1), note:str=Form('')):
    form = await request.form()
    with conn() as c:
        m = c.execute('select * from menu_items where id=? and enabled=1', (item_id,)).fetchone()
        if not m or qty <= 0:
            return RedirectResponse(f'/order/{table_no}', status_code=303)
        groups = parse_option_groups(m['option_groups'] if 'option_groups' in m.keys() else '')
        selected = []
        for g in groups:
            val = str(form.get('opt_' + g['name'], '')).strip()
            if val:
                selected.append(f"{g['name']}：{val}")
        cart = load_cart(request, table_no)
        cart.append({'id': int(m['id']), 'name': m['name'], 'price': int(m['price']), 'qty': int(qty), 'note': note, 'options': '；'.join(selected)})
    resp = RedirectResponse(f'/order/{table_no}', status_code=303)
    resp.set_cookie(cart_cookie_name(table_no), dump_cart(cart), httponly=True, samesite='lax', secure=False, max_age=60*60*8, path='/')
    return resp

@app.get('/order/{table_no}/cart', response_class=HTMLResponse)
def cart_page(request: Request, table_no: str):
    return render_cart_page(request, table_no, load_cart(request, table_no))

@app.get('/order/{table_no}/cart/remove/{index}')
def cart_remove(request: Request, table_no: str, index: int):
    cart = load_cart(request, table_no)
    if 0 <= index < len(cart):
        cart.pop(index)
    resp = RedirectResponse(f'/order/{table_no}/cart', status_code=303)
    resp.set_cookie(cart_cookie_name(table_no), dump_cart(cart), httponly=True, samesite='lax', secure=False, max_age=60*60*8, path='/')
    return resp

@app.post('/order/{table_no}/cart/submit')
def cart_submit(request: Request, table_no: str):
    cart = load_cart(request, table_no)
    if not cart:
        return RedirectResponse(f'/order/{table_no}', status_code=303)
    with conn() as c:
        cur = c.execute('insert into orders(table_no,created_at) values(?,?)', (table_no, now_str()))
        oid = cur.lastrowid
        for i in cart:
            c.execute('insert into order_items(order_id,menu_item_id,name,qty,price,note,options) values(?,?,?,?,?,?,?)', (oid, int(i.get('id') or 0), str(i.get('name','')), int(i.get('qty') or 0), int(i.get('price') or 0), str(i.get('note','')), str(i.get('options',''))))
        
        if not is_takeout_no(table_no):
            c.execute("update tables set status='dining' where table_no=?", (table_no,))
        c.commit()
    resp = RedirectResponse(f'/order/{table_no}/done?order_id={oid}', status_code=303)
    resp.delete_cookie(cart_cookie_name(table_no), path='/')
    return resp

@app.get('/order/{table_no}/done', response_class=HTMLResponse)
def order_done(request:Request, table_no:str, order_id:int):
    return templates.TemplateResponse(request, 'done.html', {'table_no':table_no,'order_id':order_id})

@app.get('/kitchen', response_class=HTMLResponse)
def kitchen(request:Request):
    items = rows('''select oi.*, o.table_no, o.created_at from order_items oi join orders o on o.id=oi.order_id
                    where o.payment_status='unpaid' order by o.created_at desc, oi.id desc''')
    return templates.TemplateResponse(request, 'kitchen.html', {'items':items})

@app.post('/kitchen/item/{item_id}/{status}')
def kitchen_status(item_id:int, status:str):
    if status not in ['accepted','done']:
        return RedirectResponse('/kitchen', status_code=303)
    with conn() as c:
        c.execute('update order_items set kitchen_status=? where id=?', (status,item_id)); c.commit()
    return RedirectResponse('/kitchen', status_code=303)

@app.get('/api/print-pending')
def api_print_pending(x_print_token: str = Header(default='')):
    if x_print_token != PRINT_AGENT_TOKEN:
        return JSONResponse({'error':'invalid print token'}, status_code=401)
    items = rows('''select oi.*, o.table_no, o.created_at from order_items oi join orders o on o.id=oi.order_id
                    where oi.print_status='pending' order by oi.id asc limit 20''')
    return {'items': items}

@app.post('/api/print-marked/{item_id}')
def api_print_marked(item_id:int, x_print_token: str = Header(default='')):
    if x_print_token != PRINT_AGENT_TOKEN:
        return JSONResponse({'error':'invalid print token'}, status_code=401)
    with conn() as c:
        c.execute("update order_items set print_status='printed' where id=?", (item_id,)); c.commit()
    return {'ok': True}

@app.get('/checkout', response_class=HTMLResponse)
def checkout(request:Request):
    tables = rows('select * from tables order by table_no')
    takeouts = rows("""select table_no, count(*) as order_count, min(created_at) as first_time
        from orders
        where payment_status='unpaid' and table_no like 'T%'
        group by table_no
        order by first_time desc""")
    return templates.TemplateResponse(request, 'checkout.html', {'tables':tables,'takeouts':takeouts})

@app.get('/checkout/{table_no}', response_class=HTMLResponse)
def checkout_table(request:Request, table_no:str, member_query: str = ''):
    orders = unpaid_orders_for_table(table_no)
    total = sum(o['total'] for o in orders)
    member = find_member(member_query) if member_query else None
    rewards = rows('select * from reward_items where enabled=1 order by points_cost, id')
    return templates.TemplateResponse(request, 'checkout_table.html', {'table_no':table_no,'orders':orders,'total':total,'member':member,'member_query':member_query,'rewards':rewards,'config':CONFIG})

@app.post('/checkout/{table_no}/pay')
def pay(table_no:str, payment_method:str=Form(...), carrier:str=Form(''), tax_id:str=Form(''), donate_code:str=Form(''), member_id: str=Form(''), reward_id: str=Form('')):
    with conn() as c:
        unpaid = c.execute("select id from orders where table_no=? and payment_status='unpaid'", (table_no,)).fetchall()
        if not unpaid:
            return RedirectResponse('/checkout', status_code=303)
        member = None
        if member_id.strip():
            member = c.execute('select * from members where id=?', (int(member_id),)).fetchone()
        reward = None
        if reward_id.strip():
            reward = c.execute('select * from reward_items where id=? and enabled=1', (int(reward_id),)).fetchone()
        subtotal = sum(order_total(r['id']) for r in unpaid)
        discount = 0
        points_used = 0
        redemption_id = None
        if member and reward and member['total_points'] >= reward['points_cost']:
            discount = min(int(reward['discount_amount']), subtotal)
            points_used = int(reward['points_cost'])
            c.execute('update members set total_points = total_points - ? where id=?', (points_used, member['id']))
            c.execute('insert into points_ledger(member_id,change_points,reason,order_id,created_at) values(?,?,?,?,?)', (member['id'], -points_used, f"兌換：{reward['name']}", None, now_str()))
            cur = c.execute('''insert into redemptions(member_id,reward_id,reward_name,points_cost,discount_amount,status,code,created_at)
                               values(?,?,?,?,?,'used',?,?)''', (member['id'], reward['id'], reward['name'], points_used, discount, f"R{datetime.now().strftime('%Y%m%d%H%M%S')}", now_str()))
            redemption_id = cur.lastrowid
        final_total = max(0, subtotal - discount)
        earned = int(final_total // int(CONFIG.get('points_per_amount', 100))) if member else 0
        now = now_str()
        for r in unpaid:
            invoice_no = '' if not CONFIG.get('einvoice_enabled') else 'API_RESERVED'
            txn_id = 'LINEPAY_SIMULATED' if payment_method == 'LINE Pay' else ''
            c.execute('''update orders set payment_status='paid', status='closed', payment_method=?, carrier=?, tax_id=?, donate_code=?, paid_at=?, linepay_txn_id=?, invoice_no=?, member_id=?, points_earned=?, points_used=?, discount_amount=? where id=?''',
                      (payment_method, carrier, tax_id, donate_code, now, txn_id, invoice_no, member['id'] if member else None, earned if r['id'] == unpaid[0]['id'] else 0, points_used if r['id'] == unpaid[0]['id'] else 0, discount if r['id'] == unpaid[0]['id'] else 0, r['id']))
        if member and earned:
            c.execute('update members set total_points = total_points + ? where id=?', (earned, member['id']))
            c.execute('insert into points_ledger(member_id,change_points,reason,order_id,created_at) values(?,?,?,?,?)', (member['id'], earned, f'消費集點：${final_total}', unpaid[0]['id'], now))
        if redemption_id:
            c.execute('update redemptions set order_id=? where id=?', (unpaid[0]['id'], redemption_id))
        
        if not is_takeout_no(table_no):
            c.execute("update tables set status='empty' where table_no=?", (table_no,))
        c.commit()
    return RedirectResponse('/checkout', status_code=303)

@app.get('/admin/menu', response_class=HTMLResponse)
def menu_admin(request:Request):
    items = rows('select * from menu_items order by category,id')
    return templates.TemplateResponse(request, 'menu_admin.html', {'items':items})

@app.post('/admin/menu/add')
def menu_add(category:str=Form(...), name:str=Form(...), price:int=Form(...), description:str=Form(''), option_groups:str=Form(''), image: UploadFile = File(None)):
    image_path = save_upload(image)
    with conn() as c:
        c.execute('insert into menu_items(category,name,price,description,option_groups,image_path) values(?,?,?,?,?,?)', (category,name,price,description,option_groups,image_path)); c.commit()
    return RedirectResponse('/admin/menu', status_code=303)

@app.post('/admin/menu/{item_id}/toggle')
def menu_toggle(item_id:int):
    with conn() as c:
        c.execute('update menu_items set enabled=case enabled when 1 then 0 else 1 end where id=?',(item_id,)); c.commit()
    return RedirectResponse('/admin/menu', status_code=303)

@app.get('/admin/menu/{item_id}/edit', response_class=HTMLResponse)
def menu_edit(request:Request, item_id:int):
    item = one('select * from menu_items where id=?', (item_id,))
    if not item:
        return RedirectResponse('/admin/menu', status_code=303)
    return templates.TemplateResponse(request, 'menu_edit.html', {'item':item})

@app.post('/admin/menu/{item_id}/edit')
def menu_edit_save(item_id:int, category:str=Form(...), name:str=Form(...), price:int=Form(...), description:str=Form(''), option_groups:str=Form(''), image: UploadFile = File(None)):
    image_path = save_upload(image)
    with conn() as c:
        if image_path:
            c.execute('update menu_items set category=?, name=?, price=?, description=?, option_groups=?, image_path=? where id=?', (category,name,price,description,option_groups,image_path,item_id))
        else:
            c.execute('update menu_items set category=?, name=?, price=?, description=?, option_groups=? where id=?', (category,name,price,description,option_groups,item_id))
        c.commit()
    return RedirectResponse('/admin/menu', status_code=303)

@app.get('/members', response_class=HTMLResponse)
def members_page(request:Request, q: str = ''):
    if q:
        members = rows("select * from members where display_name like ? or phone like ? or line_user_id like ? order by id desc", (f'%{q}%', f'%{q}%', f'%{q}%'))
    else:
        members = rows('select * from members order by id desc limit 100')
    return templates.TemplateResponse(request, 'members.html', {'members':members,'q':q,'config':CONFIG})

@app.post('/members/add')
def member_add(display_name:str=Form(...), phone:str=Form(''), line_user_id:str=Form('')):
    with conn() as c:
        c.execute('insert into members(display_name,phone,line_user_id,created_at) values(?,?,?,?)', (display_name,phone,line_user_id,now_str()))
        c.commit()
    return RedirectResponse('/members', status_code=303)

@app.get('/members/{member_id}', response_class=HTMLResponse)
def member_detail(request:Request, member_id:int):
    member = one('select * from members where id=?', (member_id,))
    if not member:
        return RedirectResponse('/members', status_code=303)
    ledger = rows('select * from points_ledger where member_id=? order by id desc limit 100', (member_id,))
    redemptions = rows('select * from redemptions where member_id=? order by id desc limit 100', (member_id,))
    return templates.TemplateResponse(request, 'member_detail.html', {'member':member,'ledger':ledger,'redemptions':redemptions})

@app.post('/members/{member_id}/adjust')
def member_adjust(member_id:int, change_points:int=Form(...), reason:str=Form('手動調整')):
    add_points(member_id, change_points, reason, None)
    return RedirectResponse(f'/members/{member_id}', status_code=303)

@app.get('/rewards', response_class=HTMLResponse)
def rewards_page(request:Request):
    rewards = rows('select * from reward_items order by enabled desc, points_cost, id')
    return templates.TemplateResponse(request, 'rewards.html', {'rewards':rewards})

@app.post('/rewards/add')
def reward_add(name:str=Form(...), points_cost:int=Form(...), discount_amount:int=Form(0), description:str=Form('')):
    with conn() as c:
        c.execute('insert into reward_items(name,points_cost,discount_amount,description) values(?,?,?,?)', (name,points_cost,discount_amount,description)); c.commit()
    return RedirectResponse('/rewards', status_code=303)

@app.post('/rewards/{reward_id}/toggle')
def reward_toggle(reward_id:int):
    with conn() as c:
        c.execute('update reward_items set enabled=case enabled when 1 then 0 else 1 end where id=?', (reward_id,)); c.commit()
    return RedirectResponse('/rewards', status_code=303)

@app.get('/member-center/{member_id}', response_class=HTMLResponse)
def member_center(request:Request, member_id:int):
    member = one('select * from members where id=?', (member_id,))
    if not member:
        return RedirectResponse('/members', status_code=303)
    rewards = rows('select * from reward_items where enabled=1 order by points_cost, id')
    redemptions = rows('select * from redemptions where member_id=? order by id desc limit 20', (member_id,))
    return templates.TemplateResponse(request, 'member_center.html', {'member':member,'rewards':rewards,'redemptions':redemptions,'config':CONFIG})



@app.post('/member-center/{member_id}/redeem/{reward_id}')
def member_create_redemption(member_id:int, reward_id:int):
    with conn() as c:
        m = c.execute('select * from members where id=?', (member_id,)).fetchone()
        r = c.execute('select * from reward_items where id=? and enabled=1', (reward_id,)).fetchone()
        if not m or not r or m['total_points'] < r['points_cost']:
            return RedirectResponse(f'/member-center/{member_id}', status_code=303)
        code = f"RD{member_id}{reward_id}{datetime.now().strftime('%Y%m%d%H%M%S')}"
        c.execute('update members set total_points = total_points - ? where id=?', (int(r['points_cost']), member_id))
        c.execute('insert into points_ledger(member_id,change_points,reason,order_id,created_at) values(?,?,?,?,?)', (member_id, -int(r['points_cost']), f"產生兌換券：{r['name']}", None, now_str()))
        c.execute("insert into redemptions(member_id,reward_id,reward_name,points_cost,discount_amount,status,code,created_at) values(?,?,?,?,?,'unused',?,?)", (member_id, reward_id, r['name'], r['points_cost'], r['discount_amount'], code, now_str()))
        c.commit()
    return RedirectResponse(f'/member-center/{member_id}', status_code=303)

@app.get('/redeem-scan', response_class=HTMLResponse)
def redeem_scan(request:Request, code:str=''):
    red = one("""select r.*, m.display_name, m.phone from redemptions r join members m on m.id=r.member_id where r.code=?""", (code,)) if code else None
    return templates.TemplateResponse(request, 'redeem_scan.html', {'code':code, 'redemption':red})

@app.post('/redeem-scan/use')
def redeem_use(code:str=Form(...)):
    with conn() as c:
        r = c.execute("select * from redemptions where code=? and status='unused'", (code,)).fetchone()
        if r:
            c.execute("update redemptions set status='used' where id=?", (r['id'],))
            c.commit()
    return RedirectResponse(f'/redeem-scan?code={code}', status_code=303)

@app.get('/kds', response_class=HTMLResponse)
def kds(request:Request):
    items = rows("""select oi.*, o.table_no, o.created_at from order_items oi join orders o on o.id=oi.order_id
                    where o.payment_status='unpaid' order by o.created_at asc, oi.id asc""")
    groups = {'new':[], 'accepted':[], 'done':[]}
    for it in items:
        groups.setdefault(it['kitchen_status'], []).append(it)
    return templates.TemplateResponse(request, 'kds.html', {'groups':groups})

@app.get('/prep', response_class=HTMLResponse)
def prep_page(request:Request, date: Optional[str]=None):
    target_date = date or datetime.now().strftime('%Y-%m-%d')
    menu_stats = rows("""select mi.id, oi.name, sum(oi.qty) as qty
        from order_items oi join orders o on o.id=oi.order_id join menu_items mi on mi.id=oi.menu_item_id
        where o.payment_status='paid' and substr(o.paid_at,1,10)=?
        group by mi.id, oi.name order by qty desc""", (target_date,))
    ingredient_stats = rows("""select ing.ingredient_name, ing.unit, sum(oi.qty * ing.qty_per_item) as qty
        from order_items oi join orders o on o.id=oi.order_id join ingredients ing on ing.menu_item_id=oi.menu_item_id
        where o.payment_status='paid' and substr(o.paid_at,1,10)=?
        group by ing.ingredient_name, ing.unit order by qty desc""", (target_date,))
    return templates.TemplateResponse(request, 'prep.html', {'target_date':target_date, 'menu_stats':menu_stats, 'ingredient_stats':ingredient_stats})

@app.get('/admin/ingredients', response_class=HTMLResponse)
def ingredients_page(request:Request):
    items = rows('select * from menu_items order by category,id')
    ingredients = rows("""select ing.*, mi.name as menu_name from ingredients ing join menu_items mi on mi.id=ing.menu_item_id order by mi.category, mi.id, ing.id""")
    return templates.TemplateResponse(request, 'ingredients.html', {'items':items,'ingredients':ingredients})

@app.post('/admin/ingredients/add')
def ingredient_add(menu_item_id:int=Form(...), ingredient_name:str=Form(...), qty_per_item:float=Form(1), unit:str=Form('份')):
    with conn() as c:
        c.execute('insert into ingredients(menu_item_id,ingredient_name,qty_per_item,unit) values(?,?,?,?)', (menu_item_id, ingredient_name, qty_per_item, unit)); c.commit()
    return RedirectResponse('/admin/ingredients', status_code=303)

@app.post('/admin/ingredients/{ing_id}/delete')
def ingredient_delete(ing_id:int):
    with conn() as c:
        c.execute('delete from ingredients where id=?', (ing_id,)); c.commit()
    return RedirectResponse('/admin/ingredients', status_code=303)

@app.get('/settings', response_class=HTMLResponse)
def settings_page(request:Request):
    return templates.TemplateResponse(request, 'settings.html', {'config':CONFIG})

@app.get('/sales', response_class=HTMLResponse)
def sales(request:Request, date: Optional[str] = None):
    target_date = date or datetime.now().strftime('%Y-%m-%d')
    paid = rows("select * from orders where payment_status='paid' and substr(paid_at,1,10)=? order by paid_at desc", (target_date,))
    for o in paid:
        o['total'] = order_total(o['id'])
    stats = rows('''
        select oi.name as name, sum(oi.qty) as qty, sum(oi.qty * oi.price) as amount
        from order_items oi
        join orders o on o.id = oi.order_id
        where o.payment_status='paid' and substr(o.paid_at,1,10)=?
        group by oi.name
        order by qty desc, amount desc, name asc
    ''', (target_date,))
    total_qty = sum(int(s['qty'] or 0) for s in stats)
    total_amount = sum(int(s['amount'] or 0) for s in stats)
    return templates.TemplateResponse(request, 'sales.html', {'orders':paid,'stats':stats,'target_date':target_date,'total_qty':total_qty,'total_amount':total_amount})


# V2.2：後台獨立網址 aliases
@app.get('/admin/members', response_class=HTMLResponse)
def admin_members_page(request:Request, q: str = ''):
    return members_page(request, q)

@app.get('/admin/rewards', response_class=HTMLResponse)
def admin_rewards_page(request:Request):
    return rewards_page(request)

@app.get('/admin/sales', response_class=HTMLResponse)
def admin_sales_page(request:Request, date: Optional[str] = None):
    return sales(request, date)

@app.get('/admin/prep', response_class=HTMLResponse)
def admin_prep_page(request:Request, date: Optional[str]=None):
    return prep_page(request, date)

@app.get('/admin/settings', response_class=HTMLResponse)
def admin_settings_page(request:Request):
    return settings_page(request)
