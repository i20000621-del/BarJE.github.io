from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import requests
import win32con
import win32gui
import win32print
import win32ui
from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).resolve().parent
CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))

SERVER = CONFIG.get("base_url", "http://127.0.0.1:8080").rstrip("/")
TOKEN = os.getenv(
    "PRINT_AGENT_TOKEN",
    CONFIG.get("print_agent_token", "print-agent-token-change-me"),
)
PRINTER_NAME = CONFIG.get("printer_name", "POS80 Printer")
POLL_SECONDS = float(CONFIG.get("print_poll_seconds", 2))

HEADERS = {"X-Print-Token": TOKEN}

# 80 mm thermal printer: common printable width at 203 dpi.
PAPER_WIDTH = int(CONFIG.get("printer_width_px", 576))
MARGIN = 24

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msjh.ttc",   # Microsoft JhengHei
    r"C:\Windows\Fonts\mingliu.ttc",
    r"C:\Windows\Fonts\kaiu.ttf",
]


def font_path() -> str:
    configured = CONFIG.get("printer_font", "")
    if configured and Path(configured).exists():
        return configured
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return p
    raise RuntimeError("找不到可用的繁體中文字型，請在 config.json 設定 printer_font。")


FONT_PATH = font_path()


def font(size: int, index: int = 0):
    try:
        return ImageFont.truetype(FONT_PATH, size=size, index=index)
    except TypeError:
        return ImageFont.truetype(FONT_PATH, size=size)


F_TITLE = font(46)
F_BIG = font(40)
F_NORMAL = font(30)
F_SMALL = font(25)
F_BOLD = font(31)


def text_width(draw, text, fnt):
    box = draw.textbbox((0, 0), str(text), font=fnt)
    return box[2] - box[0]


def center(draw, y, text, fnt):
    text = str(text)
    x = max(MARGIN, (PAPER_WIDTH - text_width(draw, text, fnt)) // 2)
    draw.text((x, y), text, font=fnt, fill=0)


def wrap_text(draw, text, fnt, max_width):
    text = str(text or "").strip()
    if not text:
        return []
    lines, current = [], ""
    for ch in text:
        trial = current + ch
        if current and text_width(draw, trial, fnt) > max_width:
            lines.append(current)
            current = ch
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def normalize_order(payload: dict) -> dict:
    # Supports either {"order": {...}} or a direct order object.
    order = payload.get("order", payload)
    items = order.get("items") or payload.get("items") or []

    # Compatibility with an endpoint that returns one pending item at a time.
    if not items and order.get("name"):
        items = [order]

    return {
        "order_id": order.get("order_id") or order.get("id") or "",
        "table_no": str(order.get("table_no") or ""),
        "created_at": order.get("created_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "items": items,
    }


def render_receipt(order: dict) -> Image.Image:
    # First pass on a tall canvas; crop after drawing.
    img = Image.new("1", (PAPER_WIDTH, 4000), 1)
    d = ImageDraw.Draw(img)
    y = 24

    table_no = str(order.get("table_no", ""))
    is_takeout = table_no.startswith("T") and "-" in table_no

    center(d, y, "外 帶 單" if is_takeout else "廚 房 單", F_TITLE)
    y += 68

    if is_takeout:
        center(d, y, table_no, F_BIG)
    else:
        center(d, y, f"桌號：{table_no}", F_BIG)
    y += 58

    if order.get("order_id") != "":
        d.text((MARGIN, y), f"訂單：{order['order_id']}", font=F_NORMAL, fill=0)
        y += 42

    d.text((MARGIN, y), f"時間：{order.get('created_at', '')}", font=F_SMALL, fill=0)
    y += 42

    d.line((MARGIN, y, PAPER_WIDTH - MARGIN, y), fill=0, width=2)
    y += 18

    for item in order.get("items", []):
        name = str(item.get("name") or "")
        qty = item.get("qty", 1)

        # Item name on left, quantity aligned right.
        qty_text = f"×{qty}"
        d.text((MARGIN, y), name, font=F_BOLD, fill=0)
        qx = PAPER_WIDTH - MARGIN - text_width(d, qty_text, F_BOLD)
        d.text((qx, y), qty_text, font=F_BOLD, fill=0)
        y += 43

        options = item.get("options") or ""
        note = item.get("note") or ""

        if options:
            for line in wrap_text(d, f"選項：{options}", F_SMALL, PAPER_WIDTH - MARGIN * 3):
                d.text((MARGIN + 18, y), line, font=F_SMALL, fill=0)
                y += 34

        if note:
            for line in wrap_text(d, f"備註：{note}", F_SMALL, PAPER_WIDTH - MARGIN * 3):
                d.text((MARGIN + 18, y), line, font=F_SMALL, fill=0)
                y += 34

        y += 10

    d.line((MARGIN, y, PAPER_WIDTH - MARGIN, y), fill=0, width=2)
    y += 18
    center(d, y, "訂單結束", F_NORMAL)
    y += 58

    # Extra feed before cutter.
    y += 40
    return img.crop((0, 0, PAPER_WIDTH, min(y, img.height)))


def print_bitmap(img: Image.Image):
    printer = win32print.OpenPrinter(PRINTER_NAME)
    try:
        props = win32print.GetPrinter(printer, 2)
        pdc = win32ui.CreateDC()
        pdc.CreatePrinterDC(PRINTER_NAME)

        printable_w = pdc.GetDeviceCaps(win32con.HORZRES)
        printable_h = pdc.GetDeviceCaps(win32con.VERTRES)

        # Scale receipt to printer's printable width.
        scale = printable_w / img.width
        target_w = printable_w
        target_h = max(1, int(img.height * scale))

        # GDI works reliably with RGB.
        rgb = img.convert("RGB")
        dib = None
        try:
            from PIL import ImageWin
            dib = ImageWin.Dib(rgb)

            pdc.StartDoc("QR POS Kitchen Order")
            pdc.StartPage()

            # Keep aspect ratio. Thermal driver handles the page/feed.
            dib.draw(pdc.GetHandleOutput(), (0, 0, target_w, target_h))

            pdc.EndPage()
            pdc.EndDoc()
        finally:
            pdc.DeleteDC()
    finally:
        win32print.ClosePrinter(printer)

    # Cutter command after graphics job. If driver/printer ignores it,
    # printing still succeeds; cutter can be enabled in the Windows driver.
    if CONFIG.get("auto_cut", True):
        try:
            h = win32print.OpenPrinter(PRINTER_NAME)
            try:
                job = win32print.StartDocPrinter(h, 1, ("QR POS Cut", None, "RAW"))
                win32print.StartPagePrinter(h)
                win32print.WritePrinter(h, b"\x1d\x56\x00")
                win32print.EndPagePrinter(h)
                win32print.EndDocPrinter(h)
            finally:
                win32print.ClosePrinter(h)
        except Exception as e:
            print("自動切紙指令未執行：", e)


def mark_printed(order: dict):
    order_id = order.get("order_id")
    if order_id == "":
        return

    # New whole-order endpoint first.
    r = requests.post(
        f"{SERVER}/api/print-marked/{order_id}",
        headers=HEADERS,
        timeout=10,
    )
    r.raise_for_status()


def run():
    print("Kitchen Print Agent 啟動")
    print(f"伺服器：{SERVER}")
    print(f"印表機：{PRINTER_NAME}")
    print("列印模式：Windows GDI 圖形列印（繁體中文）")

    while True:
        try:
            r = requests.get(
                f"{SERVER}/api/print-pending",
                headers=HEADERS,
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()

            # Whole-order API: {"orders":[...]}
            jobs = data.get("orders")
            if jobs is None:
                # Compatibility with {"items":[...]}.
                jobs = data.get("items", [])

            for payload in jobs:
                order = normalize_order(payload)
                print(f"收到待列印訂單：{order.get('order_id')} / {order.get('table_no')}")
                receipt = render_receipt(order)
                print_bitmap(receipt)

                # If the server still returns item IDs rather than order IDs,
                # mark that returned ID after successful printing.
                mark_id = payload.get("order_id") or payload.get("id") or order.get("order_id")
                if mark_id != "":
                    rr = requests.post(
                        f"{SERVER}/api/print-marked/{mark_id}",
                        headers=HEADERS,
                        timeout=10,
                    )
                    rr.raise_for_status()

                print("列印成功")

        except KeyboardInterrupt:
            print("\nPrint Agent 已停止")
            break
        except Exception as e:
            print("列印代理錯誤：", repr(e))

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
