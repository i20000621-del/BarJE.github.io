from __future__ import annotations
import json, os, time
from pathlib import Path
import requests

try:
    import win32print
except ImportError:
    raise SystemExit("缺少 pywin32，請先執行：pip install pywin32 requests")

BASE = Path(__file__).resolve().parent
CONFIG = json.loads((BASE / 'config.json').read_text(encoding='utf-8'))
SERVER = CONFIG.get('base_url', 'http://127.0.0.1:8080').rstrip('/')
PRINT_AGENT_TOKEN = os.getenv('PRINT_AGENT_TOKEN', CONFIG.get('print_agent_token', 'print-agent-token-change-me'))
PRINTER_NAME = CONFIG.get('printer_name', '').strip()
POLL_SECONDS = float(CONFIG.get('print_poll_seconds', 2))

HEADERS = {'X-Print-Token': PRINT_AGENT_TOKEN}

def printer_name() -> str:
    return PRINTER_NAME or win32print.GetDefaultPrinter()

def enc(text: str) -> bytes:
    # XPRINTER 80mm 機種常見 ESC/POS + CP950/Big5 中文字碼。
    # 若你的驅動中文字型不同，可把 printer_encoding 改成 cp950 / big5。
    encoding = CONFIG.get('printer_encoding', 'cp950')
    return text.encode(encoding, errors='replace')

def escpos_order(order: dict) -> bytes:
    takeout = str(order['table_no']).startswith('T')
    title = '外 帶' if takeout else '廚 房 單'
    place = f"外帶號碼：{order['table_no']}" if takeout else f"桌號：{order['table_no']}"
    out = bytearray()
    out += b'\x1b\x40'                 # initialize
    out += b'\x1b\x61\x01'            # center
    out += b'\x1d\x21\x11'            # double size
    out += enc(title + '\n')
    out += b'\x1d\x21\x00'
    out += enc(place + '\n')
    out += enc(f"訂單：{order['order_id']}\n")
    out += enc(f"時間：{order['created_at']}\n")
    out += b'\x1b\x61\x00'            # left
    out += enc('-' * 32 + '\n')
    for item in order.get('items', []):
        out += b'\x1b\x45\x01'        # bold on
        out += enc(f"{item['name']}  x{item['qty']}\n")
        out += b'\x1b\x45\x00'
        if item.get('options'):
            out += enc(f"  選項：{item['options']}\n")
        if item.get('note'):
            out += enc(f"  備註：{item['note']}\n")
    out += enc('-' * 32 + '\n')
    out += b'\x1b\x61\x01'
    out += enc('訂單結束\n\n\n')
    out += b'\x1d\x56\x00'            # full cut
    return bytes(out)

def print_raw(order: dict) -> None:
    name = printer_name()
    data = escpos_order(order)
    h = win32print.OpenPrinter(name)
    try:
        job = win32print.StartDocPrinter(h, 1, ('QR POS Kitchen Order', None, 'RAW'))
        try:
            win32print.StartPagePrinter(h)
            win32print.WritePrinter(h, data)
            win32print.EndPagePrinter(h)
        finally:
            win32print.EndDocPrinter(h)
    finally:
        win32print.ClosePrinter(h)

def fetch_orders() -> list[dict]:
    r = requests.get(f'{SERVER}/api/print-pending', headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json().get('orders', [])

def mark_printed(order_id: int) -> None:
    r = requests.post(f'{SERVER}/api/print-marked/{order_id}', headers=HEADERS, timeout=10)
    r.raise_for_status()

print('Kitchen Print Agent 啟動')
print(f'伺服器：{SERVER}')
print(f'印表機：{printer_name()}')

while True:
    try:
        for order in fetch_orders():
            print_raw(order)
            mark_printed(int(order['order_id']))
            print(f"已列印：訂單 {order['order_id']} / {order['table_no']}")
    except Exception as e:
        print('列印代理錯誤：', e)
    time.sleep(POLL_SECONDS)
