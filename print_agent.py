from __future__ import annotations
import json, time
from datetime import datetime
from pathlib import Path
import requests

BASE = Path(__file__).resolve().parent
CONFIG = json.loads((BASE / 'config.json').read_text(encoding='utf-8'))
PRINT_AGENT_TOKEN = os.getenv('PRINT_AGENT_TOKEN', CONFIG.get('print_agent_token', 'print-agent-token-change-me'))
SERVER = CONFIG.get('base_url','http://127.0.0.1:8080').rstrip('/')
QUEUE = BASE / 'print_queue'
QUEUE.mkdir(exist_ok=True)

def make_label(item:dict)->str:
    return (
        f"{'外帶號碼' if str(item['table_no']).startswith('T') else '桌號'}：{item['table_no']}\n"
        f"品項：{item['name']}\n"
        f"數量：{item['qty']}\n"
        f"選項：{item.get('options') or '-'}\n"
        f"備註：{item.get('note') or '-'}\n"
        f"時間：{item['created_at']}\n"
        f"明細ID：{item['id']}\n"
    )

def make_zpl(item:dict)->str:
    # Zebra ZPL 範例，正式使用可依標籤尺寸調整座標、字型與條碼。
    return f"""^XA
^CI28
^FO30,30^A0N,36,36^FD{'外帶號碼' if str(item['table_no']).startswith('T') else '桌號'}: {item['table_no']}^FS
^FO30,80^A0N,34,34^FD品項: {item['name']}^FS
^FO30,130^A0N,34,34^FD數量: {item['qty']}^FS
^FO30,180^A0N,28,28^FD選項: {item.get('options') or '-'}^FS
^FO30,220^A0N,28,28^FD備註: {item.get('note') or '-'}^FS
^FO30,260^A0N,24,24^FD{item['created_at']}^FS
^XZ"""

def print_file(item:dict):
    mode = CONFIG.get('printer_mode','file')
    content = make_zpl(item) if mode == 'zpl_file' else make_label(item)
    ext = 'zpl' if mode == 'zpl_file' else 'txt'
    name = QUEUE / f"label_{item['id']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}"
    name.write_text(content, encoding='utf-8')
    print(f"已產生標籤檔：{name}")

print('Kitchen Print Agent 啟動')
print(f'連線伺服器：{SERVER}')
print('目前 printer_mode=file，會先輸出到 print_queue 資料夾。')

while True:
    try:
        r = requests.get(f'{SERVER}/api/print-pending', timeout=5)
        r.raise_for_status()
        for item in r.json().get('items', []):
            print_file(item)
            requests.post(f"{SERVER}/api/print-marked/{item['id']}", timeout=5)
    except Exception as e:
        print('列印代理錯誤：', e)
    time.sleep(2)
