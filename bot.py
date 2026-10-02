"""Small Telegram order bot using only Python's standard library."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import shop_updates as shop
import admin_panel as admin
import channel_posts


ROOT = Path(__file__).resolve().parent


def load_local_env() -> None:
    """Read the local .env file so PowerShell does not need to export it each time."""
    path = ROOT / ".env"
    if not path.is_file():
        return
    allowed = {"BOT_TOKEN", "ADMIN_CHAT_ID", "CHANNEL_ID", "BOT_DB_PATH", "PRODUCTS_PATH"}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key in allowed:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ[key] = value


load_local_env()
DB_PATH = Path(os.environ.get("BOT_DB_PATH", ROOT / "orders.sqlite3"))
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
PRODUCTS_PATH = Path(os.environ.get("PRODUCTS_PATH", ROOT / "products.json"))
SOURCE_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,24}$")
MIN_QUANTITY = 10
WHATSAPP_URL = "https://wa.me/212781209365"
WHATSAPP_LABEL = "💬 تقدر حتى تتاصل بينا فالواتساب"
DETAIL_FIELDS = {"name": "السميّة كاملة", "phone": "نمرة التيليفون", "city": "المدينة",
                 "address": "العنوان كامل باش نوصلو ليك"}
PRICE_PATTERN = re.compile(
    r"(?<![\d.,])([0-9]{1,6})(?:[.,]([0-9]{1,2}))?\s*"
    r"(?:dhs?|mad|درهم|دراهم|د\.?م\.?)\b", re.IGNORECASE,
)


def telegram_request(request: urllib.request.Request, timeout: int) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=timeout + 5) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            failure = json.load(exc)
        except (ValueError, OSError):
            failure = {}
        finally:
            exc.close()
        raise shop.TelegramError(failure.get("description", f"HTTP {exc.code}"), exc.code,
                                 failure.get("parameters", {}).get("retry_after", 0)) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        # The request URL contains the token; show only the underlying network reason.
        reason = getattr(exc, "reason", exc)
        detail = str(reason).replace(TOKEN, "[redacted]")[:180]
        raise RuntimeError(f"Telegram connection error ({type(exc).__name__}: {detail})") from exc
    if not result.get("ok"):
        raise shop.TelegramError(result.get("description", "unknown error"), result.get("error_code", 0),
                                 result.get("parameters", {}).get("retry_after", 0))
    return result["result"]


def api(method: str, data: dict | None = None, timeout: int = 15) -> dict:
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    encoded = urllib.parse.urlencode(data or {}).encode("utf-8")
    return telegram_request(urllib.request.Request(url, encoded, method="POST"), timeout)


def keyboard(rows: list[list[tuple[str, str] | dict]]) -> str:
    return json.dumps(
        {"inline_keyboard": [[button if isinstance(button, dict) else
                              {"text": button[0], "callback_data": button[1]} for button in row] for row in rows]},
        ensure_ascii=False,
    )


def whatsapp_button(product=None, data=None, order=None):
    """Prepare a draft only; opening the link never submits an order."""
    data = dict(data or {})
    lines = ["سلام، بغيت نتاصل بيكم على الطلب ديالي من LuxeVista."]
    if order is not None:
        lines.append(f"رقم الطلب: #{order['id']}")
        data = {"items": admin.order_items(order), "name": order["customer_name"],
                "phone": order["phone"], "city": order["city"], "address": order["address"],
                "description": order["description"], "note_audio": bool(order["note_audio_json"])}
    else:
        lines.append("بغيت نسولكم قبل ما نأكد الطلب.")
    if data.get("items"):
        lines.append(admin.item_summary(data["items"]))
        total = sum(item["quantity"] * item["price_dh"] for item in data["items"])
        lines.append(f"المجموع بلا التوصيل: {total} درهم")
    elif product:
        lines += [f"المنتج: {product['name']} ({product['sku']})",
                  f"الثمن للقطعة: {product['price_dh']} درهم"]
        if data.get("quantity"):
            lines += [f"الكمية: {data['quantity']}",
                      f"المجموع بلا التوصيل: {product['price_dh'] * data['quantity']} درهم"]
    elif data.get("sku"):
        lines.append(f"رمز المنتج: {data['sku']}")
        if data.get("quantity"):
            lines.append(f"الكمية: {data['quantity']}")
    for field, label in DETAIL_FIELDS.items():
        if data.get(field):
            lines.append(f"{label}: {data[field]}")
    if data.get("description"):
        lines.append("التفاصيل: " + data["description"])
    if data.get("note_audio"):
        lines.append("صيفطت ڤوكال فتيليجرام. نقدر نعاود نصيفطو هنا إلا احتاجيتوه.")
    return {"text": WHATSAPP_LABEL,
            "url": WHATSAPP_URL + "?" + urllib.parse.urlencode({"text": "\n".join(lines)})}


def order_link_keyboard(bot_username: str, sku: str, source: str) -> str:
    url = f"https://t.me/{bot_username}?start=p_{sku}_{source}"
    return json.dumps({"inline_keyboard": [[{"text": "🛒 طلب دابا", "url": url}]]}, ensure_ascii=False)


def product_post_caption(product: dict, bot_username: str, source: str,
                         album: bool = False) -> tuple[str, list[dict]]:
    """One caption design for previews and publications; media stays untouched."""
    caption = ""
    entities = []

    def append(text, kind=None, **extra):
        nonlocal caption
        if kind:
            entities.append({"type": kind, "offset": len(caption.encode("utf-16-le")) // 2,
                             "length": len(text.encode("utf-16-le")) // 2, **extra})
        caption += text

    description = product.get("description", "").strip()
    if description.startswith(product["name"]):
        description = description[len(product["name"]):].lstrip(" \n—–|-:")
    # Imported descriptions include their price; the catalog price is authoritative.
    description = PRICE_PATTERN.sub("", description)
    description = "\n".join(line.strip(" —–|-:") for line in description.splitlines()
                            if line.strip(" —–|-:"))
    append("LUXEVISTA", "bold")
    append("\n────────────────\n\n")
    append(product["name"], "bold")
    if description:
        append("\n\n" + description)
    append("\n\n")
    append("🟨  ")
    append(f"{product['price_dh']} DH", "bold")
    entities.append({**entities[-1], "type": "text_link",
                     "url": f"https://t.me/{bot_username}?start=p_{product['sku']}_{source}"})
    append("  ·  للقطعة\n")
    append("الطلب كيبدا من 10 قطع", "italic")
    append("\n\n────────────────\n")
    append("🛍 طلب دابا", "bold")
    entities.append({**entities[-1], "type": "text_link",
                     "url": f"https://t.me/{bot_username}?start=p_{product['sku']}_{source}"})
    append("\n\n")
    append(WHATSAPP_LABEL, "text_link", url=whatsapp_button(product)["url"])
    if len(caption.encode("utf-16-le")) // 2 > 1024:
        raise RuntimeError("الوصف ديال المنتج طويل بزاف. نقص منو فـ /products وعاود شوف المعاينة.")
    return caption, entities


def send(chat_id: int | str, text: str, rows=None) -> dict:
    payload = {"chat_id": chat_id, "text": text}
    if rows:
        payload["reply_markup"] = keyboard(rows)
    return api("sendMessage", payload)


def remember_order_message(db: sqlite3.Connection, chat_id: int, message_id: int) -> None:
    db.execute("INSERT OR IGNORE INTO order_messages VALUES (?, ?)", (chat_id, message_id))
    db.commit()


def send_order(db: sqlite3.Connection, chat_id: int, text: str, rows=None) -> dict:
    message = send(chat_id, text, rows)
    remember_order_message(db, chat_id, message["message_id"])
    return message


def clean_order_chat(db: sqlite3.Connection, chat_id: int) -> None:
    """Delete only tracked form messages, never imported photos or admin alerts."""
    ids = [row["message_id"] for row in db.execute(
        "SELECT message_id FROM order_messages WHERE chat_id=? ORDER BY message_id", (chat_id,))]
    for offset in range(0, len(ids), 100):
        batch = ids[offset:offset + 100]
        try:
            api("deleteMessages", {"chat_id": chat_id, "message_ids": json.dumps(batch)})
        except RuntimeError:
            # Old/undeletable messages must not prevent cleaning the rest of the form.
            for message_id in batch:
                try:
                    api("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
                except RuntimeError:
                    pass
    db.execute("DELETE FROM order_messages WHERE chat_id=?", (chat_id,))
    db.commit()


def load_products() -> dict:
    data = json.loads(PRODUCTS_PATH.read_text(encoding="utf-8"))
    products = {}
    for product in data:
        sku = product["sku"]
        if not re.fullmatch(r"[a-z0-9_-]{1,24}", sku):
            raise ValueError(f"Invalid product SKU: {sku}")
        if sku in products or not isinstance(product["price_dh"], int) or product["price_dh"] < 0:
            raise ValueError(f"Duplicate SKU or invalid price: {sku}")
        products[sku] = product
    return products


def parse_product_caption(caption: str) -> tuple[str, str, int]:
    caption = caption.strip()
    all_matches = list(PRICE_PATTERN.finditer(caption))
    prices = {int(match.group(1)) for match in all_matches}
    if (len(caption) > 950 or not all_matches or len(prices) != 1
            or any(match.group(2) and int(match.group(2)) != 0 for match in all_matches)):
        raise ValueError("خاص الوصف يكون فيه ثمن واحد واضح بالدرهم بلا فاصلة (بحال 110 درهم).")
    first_line = caption.splitlines()[0].strip()
    name = re.split(r"\s*(?:—|–|\|+|--+)\s*", first_line, maxsplit=1)[0].strip(" -:…")
    if not name or PRICE_PATTERN.search(name) or len(name) > 100:
        raise ValueError("بدا الوصف بسمية قصيرة ديال المنتج، ومن بعد زيد الثمن بالدرهم.")
    return name, caption, prices.pop()


def save_products(products: dict) -> None:
    temporary = PRODUCTS_PATH.with_name(PRODUCTS_PATH.name + ".tmp")
    temporary.write_text(json.dumps(list(products.values()), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, PRODUCTS_PATH)


def next_sku(products: dict, db: sqlite3.Connection) -> str:
    # A published or ordered SKU must never point to a different product.
    reserved = {row["sku"] for row in db.execute("""
        SELECT sku FROM published_posts UNION SELECT sku FROM published_albums
        UNION SELECT sku FROM orders UNION SELECT sku FROM issued_links
        UNION SELECT sku FROM retired_order_skus UNION SELECT sku FROM notification_skus""")}
    number = 1
    while f"p{number:04d}" in products or f"p{number:04d}" in reserved:
        number += 1
    return f"p{number:04d}"


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS sessions (
        chat_id INTEGER PRIMARY KEY, step TEXT NOT NULL, data TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS order_messages (
        chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
        PRIMARY KEY (chat_id, message_id)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL, sku TEXT NOT NULL, product_name TEXT NOT NULL,
        unit_price_dh INTEGER NOT NULL, quantity INTEGER NOT NULL,
        customer_name TEXT NOT NULL, phone TEXT NOT NULL, city TEXT NOT NULL,
        address TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS published_posts (
        source_chat_id INTEGER NOT NULL, source_message_id INTEGER NOT NULL,
        sku TEXT NOT NULL, target_chat_id TEXT NOT NULL, channel_message_id INTEGER NOT NULL,
        PRIMARY KEY (source_chat_id, source_message_id, sku, target_chat_id)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS album_items (
        source_chat_id INTEGER NOT NULL, media_group_id TEXT NOT NULL,
        source_message_id INTEGER NOT NULL, file_id TEXT,
        caption TEXT, caption_entities TEXT,
        PRIMARY KEY (source_chat_id, source_message_id)
    )""")
    existing_columns = {row["name"] for row in db.execute("PRAGMA table_info(album_items)")}
    for column in ("file_id", "caption", "caption_entities"):
        if column not in existing_columns:
            db.execute(f"ALTER TABLE album_items ADD COLUMN {column} TEXT")
    db.execute("""CREATE INDEX IF NOT EXISTS album_items_group
        ON album_items(source_chat_id, media_group_id, source_message_id)""")
    db.execute("""CREATE TABLE IF NOT EXISTS published_albums (
        source_chat_id INTEGER NOT NULL, media_group_id TEXT NOT NULL,
        sku TEXT NOT NULL, target_chat_id TEXT NOT NULL,
        copied_message_ids TEXT NOT NULL, cta_message_id INTEGER,
        PRIMARY KEY (source_chat_id, media_group_id, sku, target_chat_id)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS product_imports (
        origin_key TEXT PRIMARY KEY, sku TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS source_imports (
        source_chat_id INTEGER NOT NULL, source_message_id INTEGER NOT NULL,
        sku TEXT NOT NULL, PRIMARY KEY (source_chat_id, source_message_id)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS album_imports (
        source_chat_id INTEGER NOT NULL, media_group_id TEXT NOT NULL,
        sku TEXT NOT NULL, PRIMARY KEY (source_chat_id, media_group_id)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS issued_links (
        sku TEXT PRIMARY KEY
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS retired_order_skus (
        sku TEXT PRIMARY KEY
    )""")
    db.commit()
    shop.init_schema(db)
    admin.init_schema(db)
    channel_posts.init_schema(db)
    db.execute("""CREATE TABLE IF NOT EXISTS basket_items (
        chat_id INTEGER NOT NULL, sku TEXT NOT NULL, quantity INTEGER NOT NULL,
        source TEXT NOT NULL, PRIMARY KEY (chat_id, sku)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS post_caption_overrides (
        target_chat_id TEXT NOT NULL, original_message_id INTEGER NOT NULL,
        caption_message_id INTEGER NOT NULL,
        PRIMARY KEY (target_chat_id, original_message_id)
    )""")
    db.commit()
    return db


def reconcile_imports(db: sqlite3.Connection, products: dict) -> None:
    """Remove old photo-to-SKU mappings when a product was deleted from JSON."""
    missing = {row["sku"] for row in db.execute("""
        SELECT sku FROM product_imports UNION SELECT sku FROM source_imports
        UNION SELECT sku FROM album_imports""") if row["sku"] not in products}
    if missing:
        with db:
            for table in ("product_imports", "source_imports", "album_imports"):
                db.executemany(f"DELETE FROM {table} WHERE sku=?", ((sku,) for sku in missing))


def sync_products(products: dict, db: sqlite3.Connection) -> None:
    """Honor admin edits immediately, before importing or processing an order."""
    current = load_products()
    if current != products:
        reconcile_imports(db, current)
        products.clear()
        products.update(current)


def put_session(db: sqlite3.Connection, chat_id: int, step: str, data: dict) -> None:
    db.execute(
        "INSERT INTO sessions VALUES (?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET step=excluded.step, data=excluded.data",
        (chat_id, step, json.dumps(data, ensure_ascii=False)),
    )
    db.commit()


def session(db: sqlite3.Connection, chat_id: int):
    row = db.execute("SELECT step, data FROM sessions WHERE chat_id=?", (chat_id,)).fetchone()
    return (row["step"], json.loads(row["data"])) if row else None


def cancel(db: sqlite3.Connection, chat_id: int) -> None:
    db.execute("DELETE FROM sessions WHERE chat_id=?", (chat_id,))
    db.execute("DELETE FROM order_messages WHERE chat_id=?", (chat_id,))
    db.commit()


def panel(products: dict, db: sqlite3.Connection) -> admin.Panel:
    return admin.Panel(db, products, ADMIN_CHAT_ID, send, order_message, save_products)


def send_product_photos(chat_id: int, product: dict, db: sqlite3.Connection) -> None:
    """Reuse recorded product media, keeping albums intact and photos in chat history."""
    sku = product["sku"]
    album = db.execute("SELECT * FROM published_albums WHERE sku=? ORDER BY rowid DESC LIMIT 1", (sku,)).fetchone()
    if not album:
        album = db.execute("SELECT * FROM album_imports WHERE sku=? ORDER BY rowid DESC LIMIT 1", (sku,)).fetchone()
    if album:
        photos = db.execute("""SELECT file_id FROM album_items
            WHERE source_chat_id=? AND media_group_id=? ORDER BY source_message_id""",
            (album["source_chat_id"], album["media_group_id"])).fetchall()
        try:
            if 2 <= len(photos) <= 10 and all(row["file_id"] for row in photos):
                media = [{"type": "photo", "media": row["file_id"]} for row in photos]
                media[0]["caption"] = product["name"]
                api("sendMediaGroup", {"chat_id": chat_id, "media": json.dumps(media, ensure_ascii=False)}, timeout=35)
                return
            if "copied_message_ids" in album.keys():
                result = api("copyMessages", {"chat_id": chat_id, "from_chat_id": album["target_chat_id"],
                    "message_ids": json.dumps(sorted(json.loads(album["copied_message_ids"]))), "remove_caption": "true"})
                if result:
                    return
        except RuntimeError:
            # A media failure must not prevent viewing the product or ordering it.
            send(chat_id, "ما قدرناش نبيّنو التصاور دابا. تقدر تشوفهم فالقناة وتكمّل الطلب من هنا.")
            return
    post = db.execute("SELECT * FROM published_posts WHERE sku=? ORDER BY rowid DESC LIMIT 1", (sku,)).fetchone()
    original = db.execute("""SELECT s.* FROM source_imports s WHERE s.sku=? AND NOT EXISTS (
        SELECT 1 FROM album_items a WHERE a.source_chat_id=s.source_chat_id
        AND a.source_message_id=s.source_message_id) ORDER BY s.rowid DESC LIMIT 1""", (sku,)).fetchone()
    if not post and not original:
        linked = db.execute("SELECT kind, file_id FROM channel_products WHERE sku=? AND file_id IS NOT NULL LIMIT 1", (sku,)).fetchone()
        if linked:
            try:
                api("sendPhoto" if linked["kind"] == "photo" else "sendVideo",
                    {"chat_id": chat_id, linked["kind"]: linked["file_id"], "caption": product["name"]})
            except RuntimeError:
                send(chat_id, "التصويرة ما بانتش دابا. تقدر تكمّل الطلب من هنا.")
        return
    try:
        api("copyMessage", {"chat_id": chat_id,
            "from_chat_id": post["target_chat_id"] if post else original["source_chat_id"],
            "message_id": post["channel_message_id"] if post else original["source_message_id"],
            "caption": product["name"], "reply_markup": json.dumps({"inline_keyboard": []})})
    except RuntimeError:
        send(chat_id, "ما قدرناش نبيّنو التصويرة دابا. تقدر تشوفها فالقناة وتكمّل الطلب من هنا.")


def show_product(chat_id: int, product: dict, db: sqlite3.Connection, source: str = "channel",
                 include_photos: bool = False) -> None:
    sku = product["sku"]
    if include_photos:
        send_product_photos(chat_id, product, db)
    unavailable = shop.stock(db, sku) == 0
    buttons = [[("🔔 خبرني ملي يتوفر", f"restock:{sku}")]] if unavailable else [
        [("🛍 طلب دابا", f"order:{sku}:{source}")]]
    buttons.append([whatsapp_button(product)])
    send_order(db, chat_id,
         f"الثمن: {product['price_dh']} درهم للقطعة\n"
         "ثمن التوصيل غادي نتافقو عليه ملي نأكدو الطلب."
         + ("\nسالَا دابا. كليكي باش نخبروك ملي يرجع." if unavailable else ""), buttons)


def start(chat_id: int, payload: str, products: dict, db: sqlite3.Connection) -> None:
    cancel(db, chat_id)
    # Deep link: ?start=p_SKU_SOURCE ; source defaults to "channel".
    if payload.startswith("p_"):
        remainder = payload[2:]
        for sku in sorted(products, key=len, reverse=True):
            if remainder == sku or remainder.startswith(sku + "_"):
                source = remainder[len(sku) + 1:] if remainder != sku else "channel"
                if SOURCE_PATTERN.fullmatch(source):
                    show_product(chat_id, products[sku], db, source, include_photos=True)
                    return
    send_order(db, chat_id, "مرحبا بيك 👋 اختار شنو عجبك:\nباش نخبروك بالجديد: /notifications",
         [[(f"{p['name']} — {p['price_dh']} DH", f"view:{p['sku']}")] for p in products.values()])


def publish_album(chat_id: int, media_group_id: str, sku: str, is_publish: bool,
                  products: dict, db: sqlite3.Connection, bot_username: str) -> None:
    destination = CHANNEL_ID if is_publish else chat_id
    rows = db.execute("""SELECT source_message_id, file_id, caption, caption_entities FROM album_items
        WHERE source_chat_id=? AND media_group_id=? ORDER BY source_message_id""",
        (chat_id, media_group_id)).fetchall()
    if not 2 <= len(rows) <= 10 or not all(row["file_id"] for row in rows):
        send(chat_id, "أرسل صور المنتج معًا كألبوم، ثم رد على إحدى الصور بالأمر /preview " + sku)
        return
    caption_row = next((row for row in rows if row["caption"]), None)
    if not caption_row:
        send(chat_id, "هذا الألبوم بدون وصف. أضف وصفًا لإحدى الصور وأعد إرساله.")
        return
    prior = None
    if is_publish:
        prior = db.execute("""SELECT cta_message_id FROM published_albums
            WHERE source_chat_id=? AND media_group_id=? AND sku=? AND target_chat_id=?""",
            (chat_id, media_group_id, sku, CHANNEL_ID)).fetchone()
        if prior:
            send(chat_id, "سبق نشر هذا الألبوم. تحقق من القناة قبل إعادة نشره.")
            return
    if not prior:
        caption, entities = product_post_caption(
            products[sku], bot_username, "channel" if is_publish else "preview", album=True)
        media = [{"type": "photo", "media": row["file_id"]} for row in rows]
        media[0]["caption"] = caption
        media[0]["caption_entities"] = entities
        result = api("sendMediaGroup", {"chat_id": destination, "media": json.dumps(media, ensure_ascii=False)}, timeout=35)
        message_ids = [message["message_id"] for message in result]
        if is_publish:
            # Record publication so retrying cannot repost photos.
            with db:
                db.execute("""INSERT INTO published_albums VALUES (?, ?, ?, ?, ?, ?)""",
                           (chat_id, media_group_id, sku, CHANNEL_ID, json.dumps(message_ids), None))
    if is_publish:
        with db:
            shop.queue_arrival(db, sku)
        send(chat_id, f"✅ نُشر ألبوم المنتج في {CHANNEL_ID} مع رابط الطلب في الوصف.")
    else:
        send(chat_id, f"معاينة الألبوم أعلاه ({len(rows)} صور منفصلة). للنشر، رد على الألبوم الأصلي بالأمر /publish {sku}.")


def edit_published_product(chat_id, command, products, db, bot_username):
    if not admin.is_admin(db, ADMIN_CHAT_ID, chat_id):
        send(chat_id, "هاد الأمر غير للمسؤول.")
        return
    parts = command.split()
    if len(parts) == 2 and parts[1].startswith("https://"):
        try:
            sku = channel_posts.edit_link(db, products, chat_id, CHANNEL_ID, bot_username, parts[1],
                api=api, save_products=save_products, next_sku=next_sku, parse_caption=parse_product_caption,
                caption=product_post_caption, keyboard=order_link_keyboard)
        except (RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
            send(chat_id, f"تعذّر إكمال تحديث المنشور: {shop.error_message(exc)}")
            return
        send(chat_id, f"✅ تم تحديث المنشور. المنتج: {sku}\n{products[sku]['name']} — {products[sku]['price_dh']} DH\n"
             f"لإدارة المنتج: /products. للتحديث لاحقًا: /edit {sku} أو نفس رابط المنشور.")
        return
    if len(parts) not in (2, 3):
        send(chat_id, "لاستيراد منشور وتنسيقه: أرسل /edit متبوعًا برابطه. لتطبيق تفاصيل المنتج المحفوظة: /edit متبوعًا برمز المنتج.\n"
             "مثال: /edit https://t.me/yourchannel/123\nلإصلاح مرجع المنشور: أرسل /edit ثم رمز المنتج ثم رابط المنشور.")
        return
    sku = parts[1]
    if sku not in products:
        send(chat_id, "المنتج غير موجود. اختر رمزه من /products.")
        return
    if not CHANNEL_ID:
        send(chat_id, "اضبط CHANNEL_ID في ملف .env وأعد تشغيل البوت أولًا.")
        return
    posts = db.execute("SELECT channel_message_id FROM published_posts WHERE sku=? AND target_chat_id=?",
                       (sku, CHANNEL_ID)).fetchall()
    albums = db.execute("SELECT copied_message_ids, cta_message_id FROM published_albums WHERE sku=? AND target_chat_id=?",
                        (sku, CHANNEL_ID)).fetchall()
    linked = db.execute("SELECT * FROM channel_products WHERE sku=? AND target_chat_id=?", (sku, CHANNEL_ID)).fetchall()
    if linked and len(parts) == 2:
        # Link-adopted posts include text messages and album button state.
        for row in linked:
            try:
                channel_posts.update(db, row, products[sku], bot_username, api, product_post_caption, order_link_keyboard)
                send(chat_id, f"✅ {sku}: تم تحديث المنشور المرتبط #{row['message_id']}.")
            except RuntimeError as exc:
                send(chat_id, f"تعذّر تحديث المنشور المرتبط #{row['message_id']}: {shop.error_message(exc)}")
        if not posts and not albums:
            return
    if not posts and not albums:
        send(chat_id, f"لا توجد منشورات مسجلة للمنتج {sku} في {CHANNEL_ID}. لم يُنشر أو يُستبدل أي شيء.")
        return
    repair_mid = None
    if len(parts) == 3:
        link = re.fullmatch(r"https://t\.me/(?:(c)/([0-9]+)|([A-Za-z0-9_]+))/([1-9][0-9]*)(?:\?single)?", parts[2])
        if not link:
            send(chat_id, "انسخ رابط منشور تيليجرام، مثل: /edit p0003 https://t.me/yourchannel/123")
            return
        if len(posts) + len(albums) != 1:
            send(chat_id, "لهذا المنتج عدة منشورات مسجلة. لا يمكن تحديد السجل المراد إصلاحه بهذا الرابط.")
            return
        try:
            channel = api("getChat", {"chat_id": CHANNEL_ID})
        except RuntimeError as exc:
            send(chat_id, f"تعذّر التحقق من القناة: {shop.error_message(exc)}")
            return
        matches = (str(channel["id"]) == "-100" + link[2] if link[1]
                   else str(channel.get("username", "")).lower() == link[3].lower())
        if not matches:
            send(chat_id, "الرابط ليس من القناة المحددة. لم يتم إجراء أي تغيير.")
            return
        repair_mid = int(link[4])
    try:
        caption, entities = product_post_caption(products[sku], bot_username, "channel")
    except RuntimeError as exc:
        send(chat_id, f"تعذّر تحديث المنشورات: {shop.error_message(exc)}")
        return
    markup = json.dumps({"inline_keyboard": []})
    operations = {}
    invalid_records = 0
    for post in posts:
        mid = post["channel_message_id"]
        operations[("editMessageCaption", mid)] = {
            "caption": caption, "caption_entities": json.dumps(entities, ensure_ascii=False),
            "reply_markup": markup}
    for album in albums:
        try:
            mids = json.loads(album["copied_message_ids"])
            if not isinstance(mids, list) or not mids or not all(type(mid) is int and mid > 0 for mid in mids):
                raise ValueError("Invalid message IDs")
        except (ValueError, TypeError):
            invalid_records += 1
            continue
        operations[("editMessageCaption", mids[0])] = {
            "caption": caption, "caption_entities": json.dumps(entities, ensure_ascii=False)}
        if album["cta_message_id"]:
            operations[("editMessageText", album["cta_message_id"])] = {
                "text": caption, "entities": json.dumps(entities, ensure_ascii=False),
                "link_preview_options": json.dumps({"is_disabled": True}),
                "reply_markup": markup}
    updated = unchanged = 0
    missing_message = False
    failures = []
    repaired = False
    for (method, mid), payload in operations.items():
        original_mid = mid
        if method == "editMessageCaption":
            override = db.execute("SELECT caption_message_id FROM post_caption_overrides WHERE target_chat_id=? AND original_message_id=?",
                                  (CHANNEL_ID, mid)).fetchone()
            mid = repair_mid if repair_mid is not None else override[0] if override else mid
        try:
            api(method, {"chat_id": CHANNEL_ID, "message_id": mid, **payload})
            updated += 1
        except RuntimeError as exc:
            if isinstance(exc, shop.TelegramError) and exc.code == 400 and "message is not modified" in str(exc).lower():
                unchanged += 1
            else:
                missing_message |= "message to edit not found" in str(exc).lower()
                failures.append(f"#{mid}: {shop.error_message(exc)}")
                continue
        if method == "editMessageCaption" and repair_mid is not None:
            with db:
                db.execute("INSERT OR REPLACE INTO post_caption_overrides VALUES (?, ?, ?)",
                           (CHANNEL_ID, original_mid, mid))
            repaired = True
    message = f"{sku}: تم تحديث {updated} رسالة في القناة؛ و{unchanged} رسالة محدثة مسبقًا."
    if failures:
        message += f"\nفشل تحديث {len(failures)} رسالة. تحقق من صلاحيات البوت ووجود المنشورات، ثم أعد /edit {sku}."
        message += "\n" + "\n".join(failures[:5])[:1000]
        if missing_message:
            message += (f"\nالرسالة المحفوظة غير متاحة في {CHANNEL_ID}. إذا كان المنشور موجودًا، "
                        f"انسخ رابطه وأرسل /edit {sku} متبوعًا بالرابط لإصلاح منشور واحد. "
                        "للألبوم، انسخ رابط الصورة التي تحمل الوصف. لا يمكن تعديل المنشورات المحذوفة.")
    if repaired:
        message += f"\nتم حفظ مرجع الوصف المصحح. استخدم لاحقًا /edit {sku}."
    if invalid_records:
        message += f"\nتم تجاوز {invalid_records} سجل ألبوم بسبب معرّفات رسائل غير صالحة."
    send(chat_id, message)


def imported_sku_for_reply(db: sqlite3.Connection, chat_id: int, original: dict) -> str | None:
    group = original.get("media_group_id")
    if group:
        row = db.execute("SELECT sku FROM album_imports WHERE source_chat_id=? AND media_group_id=?",
                         (chat_id, str(group))).fetchone()
    else:
        row = db.execute("SELECT sku FROM source_imports WHERE source_chat_id=? AND source_message_id=?",
                         (chat_id, original["message_id"])).fetchone()
    return row["sku"] if row else None


def import_product_photo(message: dict, chat_id: int, products: dict, db: sqlite3.Connection) -> None:
    caption = message.get("caption", "").strip()
    if not admin.is_admin(db, ADMIN_CHAT_ID, chat_id):
        return
    admin.clear_editor(db, chat_id)
    if not caption:
        if message.get("photo") and not message.get("media_group_id"):
            send(chat_id, "أضف اسم المنتج وسعرًا واحدًا (مثل ١١٠ درهم) إلى وصف الصورة وأعد إرسالها. أرسل الصور المتعددة معًا كألبوم.")
        return
    origin = message.get("forward_origin")
    if origin:
        if origin.get("type") != "channel":
            return
        origin_chat = origin.get("chat", {})
        expected = CHANNEL_ID.lower().lstrip("@")
        actual = (str(origin_chat.get("id")) if CHANNEL_ID.lstrip("-").isdigit()
                  else str(origin_chat.get("username", "")).lower())
        if not CHANNEL_ID or actual != expected:
            return
        key = f"channel:{origin_chat['id']}:{origin['message_id']}"
    elif message.get("media_group_id"):
        key = f"direct:{chat_id}:album:{message['media_group_id']}"
    else:
        key = f"direct:{chat_id}:photo:{message['message_id']}"
    existing = db.execute("SELECT sku FROM product_imports WHERE origin_key=?", (key,)).fetchone()
    if existing and existing["sku"] in products:
        sku = existing["sku"]
        status = "مستورد مسبقًا"
    else:
        try:
            name, description, price_dh = parse_product_caption(caption)
        except ValueError as exc:
            send(chat_id, f"تعذّر استيراد المنتج: {shop.error_message(exc)}\nعدّل الوصف وأعد الإرسال.")
            return
        sku = next_sku(products, db)
        product = {"sku": sku, "name": name, "description": description, "price_dh": price_dh}
        save_products({**products, sku: product})
        products[sku] = product
        db.execute("INSERT OR REPLACE INTO product_imports VALUES (?, ?)", (key, sku))
        status = "تم الاستيراد"
    db.execute("INSERT OR REPLACE INTO source_imports VALUES (?, ?, ?)",
               (chat_id, message["message_id"], sku))
    if message.get("media_group_id"):
        db.execute("INSERT OR REPLACE INTO album_imports VALUES (?, ?, ?)",
                   (chat_id, str(message["media_group_id"]), sku))
    db.commit()
    p = products[sku]
    send(chat_id, f"✅ {status}: {sku}\n{p['name']} — {p['price_dh']} DH\n"
         f"رد على صورة المنتج أو إحدى صور الألبوم بالأمر /preview أو /publish. يمكنك إضافة رمز المنتج: /publish {sku}.")


def basket_rows(db, chat_id):
    return [dict(row) for row in db.execute(
        "SELECT sku, quantity, source FROM basket_items WHERE chat_id=? ORDER BY sku", (chat_id,))]


def basket_quote(chat_id, products, db):
    rows = basket_rows(db, chat_id)
    quantity = sum(row["quantity"] for row in rows)
    if not MIN_QUANTITY <= quantity <= 1000:
        send(chat_id, "خاص السلة يكون فيها من 10 حتى لـ 1000 قطعة فالمجموع. تقدر تخلط الموديلات.\n"
             "باش تبدّل السلة: /basket")
        return None
    items = []
    for row in rows:
        product = products.get(row["sku"])
        available = shop.stock(db, row["sku"])
        if not product or (available is not None and row["quantity"] > available):
            send(chat_id, f"{row['sku']}: المنتج ما متوفرش ولا الكمية ما كافياش.\n"
                 "باش تبدّل السلة: /basket")
            return None
        items.append({**row, "name": product["name"], "price_dh": product["price_dh"]})
    return items


def show_basket(chat_id, products, db):
    rows = basket_rows(db, chat_id)
    lines = ["🧺 السلة", "خاص 10 قطع على الأقل فالمجموع، من الموديلات اللي بغيتي."]
    buttons = []
    total = 0
    for row in rows:
        p = products.get(row["sku"])
        label = p["name"][:60] if p else row["sku"] + " (غير متاح)"
        subtotal = p["price_dh"] * row["quantity"] if p else 0
        total += subtotal
        lines.append(f"{label} × {row['quantity']} — {subtotal} DH")
        buttons.append([(f"✏️ {label}", f"basket:edit:{row['sku']}"),
                        ("✖ حذف", f"basket:remove:{row['sku']}")])
    lines.append(f"\n{sum(row['quantity'] for row in rows)} قطعة — {total} DH (بلا التوصيل)")
    if rows:
        buttons.append([("✅ نكمل الطلب", "basket:checkout")])
    else:
        lines.append("السلة خاوية")
    buttons.append([("🛍 نكمل نتقدّى", "basket:shop")])
    send_order(db, chat_id, "\n".join(lines), buttons)


def basket_callback(chat_id, command, products, db):
    if not (command.startswith("add:") or command.startswith("basket:")):
        return False
    admin.clear_editor(db, chat_id)
    # Navigating/editing invalidates any previous checkout confirmation.
    db.execute("DELETE FROM sessions WHERE chat_id=?", (chat_id,))
    db.commit()
    if command == "basket:shop":
        start(chat_id, "", products, db)
    elif command == "basket:show":
        show_basket(chat_id, products, db)
    elif command == "basket:checkout":
        if not ADMIN_CHAT_ID:
            send(chat_id, "ما يمكنش تدير طلب دابا. عاود جرّب من بعد.")
            return True
        items = basket_quote(chat_id, products, db)
        if items:
            data = {"items": items}
            previous = db.execute("SELECT * FROM orders WHERE chat_id=? ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
            if previous:
                for field in DETAIL_FIELDS:
                    data[field] = previous["customer_name" if field == "name" else field]
                show_saved_details(chat_id, data, db)
            else:
                next_detail(chat_id, data, products, db)
    elif command.startswith("basket:remove:"):
        with db:
            db.execute("DELETE FROM basket_items WHERE chat_id=? AND sku=?", (chat_id, command[len('basket:remove:'):]))
        show_basket(chat_id, products, db)
    elif command.startswith("add:") or command.startswith("basket:edit:"):
        if command.startswith("add:"):
            parts = command.split(":", 2)
            if len(parts) != 3 or not SOURCE_PATTERN.fullmatch(parts[2]):
                return True
            _, sku, source = parts
        else:
            sku = command[len("basket:edit:"):]
            source = "catalog"
        existing = db.execute("SELECT * FROM basket_items WHERE chat_id=? AND sku=?", (chat_id, sku)).fetchone()
        if sku not in products:
            send(chat_id, "المنتج ما متوفرش. حيدو من /basket.")
            return True
        if not existing and len(basket_rows(db, chat_id)) >= 20:
            send(chat_id, "تقدر تزيد حتى لـ 20 موديل فالسلة. بدّل /basket الأول.")
            return True
        put_session(db, chat_id, "basket_quantity", {"sku": sku, "source": existing["source"] if existing else source})
        send_order(db, chat_id, f"{products[sku]['name']}\n"
                   f"الكمية دابا: {existing['quantity'] if existing else 0}\n"
                   "كتب شحال بغيتي من هاد الموديل (1–1000)، ولا 0 باش تحيدو.\n"
                   "\n"
                   "تقدر تخلط الموديلات باش تجمع 10 قطع.")
    return True


def confirm_basket(chat_id, data, products, db):
    items = basket_quote(chat_id, products, db)
    if not items:
        return
    if data["items"] != items:
        data["items"] = items
        send_order(db, chat_id, "تبدلات السلة. شوفها وعاود أكد الطلب.")
        review_order(chat_id, None, data, db)
        return
    quantity = sum(item["quantity"] for item in items)
    total = sum(item["price_dh"] * item["quantity"] for item in items)
    with db:
        cursor = db.execute("""INSERT INTO orders
            (chat_id, sku, product_name, unit_price_dh, quantity, customer_name, phone,
             city, address, source, admin_key, items_json, total_dh)
            VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, items[0]["sku"], "Mixed basket", quantity, data["name"], data["phone"],
             data["city"], data["address"], ", ".join(dict.fromkeys(item["source"] for item in items)),
             admin.uuid.uuid4().hex, json.dumps(items, ensure_ascii=False), total))
        db.execute("DELETE FROM basket_items WHERE chat_id=?", (chat_id,))
        db.execute("DELETE FROM sessions WHERE chat_id=?", (chat_id,))
        db.executemany("INSERT OR IGNORE INTO retired_order_skus VALUES (?)", ((item["sku"],) for item in items))
    order = db.execute("SELECT * FROM orders WHERE id=?", (cursor.lastrowid,)).fetchone()
    try:
        send(ADMIN_CHAT_ID, order_message(order), admin.order_buttons(db, order))
        send_order_audio(ADMIN_CHAT_ID, order)
    except RuntimeError as exc:
        print(f"Admin notification failed for order #{order['id']}: {exc}", file=sys.stderr)
    send(chat_id, f"✅ تسجّل الطلب ديالك #{order['id']}.\nباش تشوف الطلب ديالك: /orders",
         [[whatsapp_button(order=order)]])
    clean_order_chat(db, chat_id)


def handle_text(message: dict, products: dict, db: sqlite3.Connection, bot_username: str) -> None:
    chat_id = message["chat"]["id"]
    if message["chat"]["type"] != "private":
        return
    sync_products(products, db)
    if message.get("media_group_id") and (message.get("photo") or message.get("video")):
        if not admin.is_admin(db, ADMIN_CHAT_ID, chat_id):
            return
        photo = message.get("photo") or []
        db.execute("""INSERT OR REPLACE INTO album_items
            (source_chat_id, media_group_id, source_message_id, file_id, caption, caption_entities)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (chat_id, str(message["media_group_id"]), message["message_id"],
             photo[-1]["file_id"] if photo else None,
             message.get("caption", ""),
             json.dumps(message["caption_entities"], ensure_ascii=False) if message.get("caption_entities") else None))
        db.commit()
        import_product_photo(message, chat_id, products, db)
        return
    if message.get("photo") or message.get("video"):
        import_product_photo(message, chat_id, products, db)
        return
    raw = message.get("text", "").strip()
    if raw.split(maxsplit=1)[:1] == ["/edit"]:
        admin.clear_editor(db, chat_id)
        edit_published_product(chat_id, raw, products, db, bot_username)
        return
    if raw == "/basket":
        start(chat_id, "", products, db)
        return
    if raw.startswith("/start"):
        admin.clear_editor(db, chat_id)
        parts = raw.split(maxsplit=1)
        start(chat_id, parts[1].strip() if len(parts) > 1 else "", products, db)
        remember_order_message(db, chat_id, message["message_id"])
        return
    if raw == "/id":
        send(chat_id, f"معرّف محادثتك: {chat_id}")
        return
    if raw == "/cancel":
        admin.clear_editor(db, chat_id)
        cancel(db, chat_id)
        send(chat_id, "صافي، لغينا العملية. كتب /start باش ترجع للمنتجات.")
        return
    if panel(products, db).command(chat_id, raw):
        if admin.is_admin(db, ADMIN_CHAT_ID, chat_id):
            cancel(db, chat_id)
        return
    if raw.split(maxsplit=1)[:1] in (["/notifications"], ["/arrivals"]):
        if raw.split()[1:] == ["off"]:
            shop.stop_notifications(db, chat_id)
            send(chat_id, "وقفنا كاع التنبيهات. إلا بغيتي ترجع تشترك، كتب /notifications")
        elif len(raw.split()) == 1:
            shop.preferences(db, products, chat_id, send)
        else:
            send(chat_id, "باش تبدّل التنبيهات: /notifications\nباش توقف كلشي: /notifications off")
        return
    if raw.split(maxsplit=1)[:1] in (["/stock"], ["/category"], ["/announce"]):
        if not admin.is_admin(db, ADMIN_CHAT_ID, chat_id):
            send(chat_id, "هاد الأمر غير للمسؤول.")
            return
        shop.admin_command(db, products, chat_id, raw, send)
        return
    if raw.startswith("/preview") or raw.startswith("/publish"):
        if not admin.is_admin(db, ADMIN_CHAT_ID, chat_id):
            send(chat_id, "هاد الأمر غير للمسؤول.")
            return
        parts = raw.split()
        original = message.get("reply_to_message")
        if not original or len(parts) not in (1, 2):
            send(chat_id, "أرسل صورة المنتج مع وصفها أولًا. ثم رد عليها بالأمر /preview أو /publish. يمكنك إضافة رمز المنتج، مثل /preview bv001.")
            return
        imported_sku = imported_sku_for_reply(db, chat_id, original)
        sku = parts[1] if len(parts) == 2 else imported_sku
        if not sku or sku not in products:
            send(chat_id, "المنتج غير موجود. أرسل الصورة مع الاسم والسعر في وصفها، أو أضف المنتج إلى products.json.")
            return
        if imported_sku and sku != imported_sku:
            send(chat_id, f"تم استيراد المنتج بالرمز {imported_sku}. استخدم /preview {imported_sku} أو /publish {imported_sku}.")
            return
        source_id = original["message_id"]
        is_publish = parts[0] == "/publish"
        if not is_publish and parts[0] != "/preview":
            return
        if is_publish and not CHANNEL_ID:
            send(chat_id, "اضبط CHANNEL_ID في .env وأعد تشغيل البوت ثم حاول مجددًا.")
            return
        if original.get("media_group_id"):
            try:
                publish_album(chat_id, str(original["media_group_id"]), sku, is_publish,
                              products, db, bot_username)
            except RuntimeError as exc:
                send(chat_id, f"تعذّر {'نشر' if is_publish else 'عرض'} الألبوم: {shop.error_message(exc)}")
            return
        destination = CHANNEL_ID if is_publish else chat_id
        if is_publish:
            prior = db.execute("""SELECT channel_message_id FROM published_posts
                WHERE source_chat_id=? AND source_message_id=? AND sku=? AND target_chat_id=?""",
                (chat_id, source_id, sku, CHANNEL_ID)).fetchone()
            if prior:
                send(chat_id, f"سبق نشر هذا المنشور في القناة برقم #{prior['channel_message_id']}.")
                return
        try:
            caption, entities = product_post_caption(
                products[sku], bot_username, "channel" if is_publish else "preview")
            result = api("copyMessage", {
                "chat_id": destination,
                "from_chat_id": chat_id,
                "message_id": source_id,
                "caption": caption,
                "caption_entities": json.dumps(entities, ensure_ascii=False),
                "reply_markup": json.dumps({"inline_keyboard": []}),
            })
        except RuntimeError as exc:
            send(chat_id, f"تعذّر {'النشر' if is_publish else 'العرض'}: {shop.error_message(exc)}")
            return
        if is_publish:
            with db:
                db.execute("""INSERT INTO published_posts VALUES (?, ?, ?, ?, ?)""",
                           (chat_id, source_id, sku, CHANNEL_ID, result["message_id"]))
                shop.queue_arrival(db, sku)
            send(chat_id, f"✅ نُشر في {CHANNEL_ID} مع رابط الطلب (الرسالة #{result['message_id']}).")
        else:
            send(chat_id, "المعاينة أعلاه. رد على المنشور الأصلي بالأمر /publish " + sku + " عندما تكون جاهزًا.")
        return
    if raw.split(maxsplit=1)[:1] == ["/orders"]:
        show_orders(chat_id, raw, db)
        return
    if raw.split(maxsplit=1)[:1] == ["/delete"]:
        delete_order(chat_id, raw, db)
        return
    if raw.startswith("/link"):
        if not admin.is_admin(db, ADMIN_CHAT_ID, chat_id):
            send(chat_id, "هاد الأمر غير للمسؤول.")
            return
        parts = raw.split()
        if len(parts) != 3 or parts[1] not in products or not SOURCE_PATTERN.fullmatch(parts[2]):
            send(chat_id, "أرسل /link ثم رمز المنتج ثم رمز المصدر\nمثال: /link bv001 channel")
            return
        payload = f"p_{parts[1]}_{parts[2]}"
        if len(payload) > 64:
            send(chat_id, "رمز المنتج والمصدر أطول من الحد المسموح لرابط تيليجرام.")
            return
        db.execute("INSERT OR IGNORE INTO issued_links (sku) VALUES (?)", (parts[1],))
        db.commit()
        send(chat_id, f"رابط المنتج:\nhttps://t.me/{bot_username}?start={payload}")
        return
    if raw.startswith("/"):
        send(chat_id, "كتب /start باش تشوف المنتجات، ولا /cancel باش تلغي الطلب.")
        return

    if panel(products, db).editor_text(chat_id, raw):
        return
    current = session(db, chat_id)
    if not current:
        send(chat_id, "كتب /start باش تشوف المنتجات.")
        return
    step, data = current
    remember_order_message(db, chat_id, message["message_id"])
    if step == "description":
        media = message.get("voice") or message.get("audio")
        if media:
            data["note_audio"] = {"kind": "voice" if message.get("voice") else "audio", "file_id": media["file_id"]}
        elif raw and len(raw) <= 1000:
            data["description"] = raw
        else:
            send_order(db, chat_id, "صيفط لينا شنو بغيتي فـ 1000 حرف ولا أقل، ولا صيفط ڤوكال.")
            return
        ask_order_note(chat_id, data, db, product=products.get(data.get("sku")))
        return
    value = " ".join(raw.split())
    if not value or len(value) > 150:
        send_order(db, chat_id, "كتب المعلومة اللي طلبنا منك، حتى لـ 150 حرف. ولا كتب /cancel باش تلغي.")
        return
    if step == "basket_quantity":
        if not value.isascii() or not value.isdigit() or not 0 <= int(value) <= 1000:
            send_order(db, chat_id, "كتب كمية من 1 حتى لـ 1000، ولا 0 باش تحيد المنتج.")
            return
        quantity = int(value)
        sku = data["sku"]
        available = shop.stock(db, sku)
        rows = basket_rows(db, chat_id)
        if quantity and (sku not in products or (available is not None and quantity > available)):
            send_order(db, chat_id, "المنتج ما متوفرش ولا المخزون ما كافيش. كتب كمية أخرى ولا 0 باش تحيدو.")
            return
        if quantity + sum(row["quantity"] for row in rows if row["sku"] != sku) > 1000:
            send_order(db, chat_id, "السلة كتقبل حتى لـ 1000 قطعة.")
            return
        with db:
            if quantity:
                db.execute("INSERT OR REPLACE INTO basket_items VALUES (?, ?, ?, ?)", (chat_id, sku, quantity, data["source"]))
                db.execute("INSERT OR IGNORE INTO issued_links VALUES (?)", (sku,))
            else:
                db.execute("DELETE FROM basket_items WHERE chat_id=? AND sku=?", (chat_id, sku))
            db.execute("DELETE FROM sessions WHERE chat_id=?", (chat_id,))
        show_basket(chat_id, products, db)
    elif step == "quantity":
        if not value.isascii() or not value.isdigit() or not MIN_QUANTITY <= int(value) <= 1000:
            send_order(db, chat_id, "الطلب كيبدا من 10 قطع. كتب شحال بغيتي، من 10 حتى لـ 1000.")
            return
        if not check_stock(chat_id, data["sku"], int(value), products, db):
            return
        data["quantity"] = int(value)
        previous = db.execute("SELECT customer_name, phone, city, address FROM orders WHERE chat_id=? ORDER BY id DESC LIMIT 1",
                              (chat_id,)).fetchone()
        if previous:
            for field in DETAIL_FIELDS:
                data.setdefault(field, previous["customer_name" if field == "name" else field])
            show_saved_details(chat_id, data, db)
        else:
            next_detail(chat_id, data, products, db)
    elif step in DETAIL_FIELDS:
        if step == "phone":
            value = re.sub(r"[\s().-]", "", value)
            if not re.fullmatch(r"\+?[0-9]{9,15}", value):
                send_order(db, chat_id, "كتب نمرة صحيحة، بحال 0612345678 ولا +212612345678.")
                return
        data[step] = value
        if data.pop("editing_detail", False):
            show_saved_details(chat_id, data, db)
        else:
            next_detail(chat_id, data, products, db)
    elif step == "details":
        send_order(db, chat_id, "كليكي على «نكملو بهاد المعلومات» ولا اختار المعلومة اللي بغيتي تبدّل فالميساج لفوق.")
    else:
        send_order(db, chat_id, "كليكي على «نأكد الطلب» ولا كتب /cancel باش تلغي.")


def next_detail(chat_id: int, data: dict, products: dict, db: sqlite3.Connection) -> None:
    for field, label in DETAIL_FIELDS.items():
        value = data.get(field, "")
        if (not isinstance(value, str) or not value.strip() or len(value) > 150
                or (field == "phone" and not re.fullmatch(r"\+?[0-9]{9,15}", value))):
            put_session(db, chat_id, field, data)
            send_order(db, chat_id, label + "؟")
            return
    if not data.get("note_complete") and "items" not in data:
        ask_order_note(chat_id, data, db, product=products.get(data.get("sku")))
        return
    if "items" in data:
        items = basket_quote(chat_id, products, db)
        if not items:
            return
        data["items"] = items
        review_order(chat_id, None, data, db)
        return
    if data["sku"] not in products:
        cancel(db, chat_id)
        send(chat_id, "هاد المنتج ما بقاش متوفر. كتب /start واختار شي واحد آخر.")
        return
    review_order(chat_id, products[data["sku"]], data, db)


def note_summary(data):
    return ("التفاصيل ديال الطلب: " + (data.get("description") or "—")
            + ("\n🎤 الڤوكال ديالك تسجّل" if data.get("note_audio") else ""))


def ask_order_note(chat_id, data, db, mode=None, product=None):
    has_note = bool(data.get("description") or data.get("note_audio"))
    mode = mode or ("saved" if has_note else "choose")
    done = ("✅ نكملو" if has_note else "⏭ ندوز بلا تفاصيل", "note:done")
    rows = [[done], [("↩️ نبدّل الطريقة", "note:choose")]]
    if mode == "voice":
        prompt = ("🎤 صيفط لينا ڤوكال فيه شنو بغيتي\n\n"
                  "شدّ على الميكرو لتحت حدا بلاصة الكتابة، هضر على اللوان والتفاصيل وصيفط الڤوكال.\n"
                  "إلا بانت ليك الكاميرا بلاصة الميكرو، كليكي عليها مرة باش تبدّل للصوت.")
    elif mode == "text":
        prompt = ("✍️ كتب لينا هنا اللوان ولا التفاصيل اللي بغيتي\n\n"
                  "مثلا: بغيت 5 كحلين و5 ذهبيين.\n"
                  "جملة قصيرة كافية، حتى لـ 1000 حرف.")
    elif mode == "saved":
        prompt = ("✅ وصلاتنا التفاصيل ديالك\n\n" + note_summary(data)
                  + "\n\nكليكي على «نكملو» باش تشوف الطلب ديالك قبل ما تأكدو.")
        rows = [[done], [("✏️ نبدّل ولا نزيد تفاصيل", "note:choose")],
                [("🗑 نمسح التفاصيل والڤوكال", "note:clear")]]
    else:
        prompt = ("🎨 بغيتي شي لون ولا عندك شي تفاصيل؟\n\n"
                  "🎤 تقدر تصيفط ڤوكال بلا ما تكتب\n"
                  "✍️ ولا تكتب لينا شنو بغيتي\n\n"
                  "ما عندك ما تزيد؟ كليكي على «ندوز بلا تفاصيل» 👇")
        if has_note:
            prompt = ("✏️ كيفاش بغيتي تبدّل ولا تزيد التفاصيل؟\n\n"
                      "تقدر تجمع الكتابة والڤوكال. الكتابة الجديدة كتبدّل القديمة، والڤوكال الجديد كيبدّل القديم.")
        rows = [[("🎤 نصيفط ڤوكال", "note:voice")],
                [("✍️ نكتب التفاصيل", "note:text")], [done]]
    rows.append([whatsapp_button(product, data)])
    clear_note_buttons(chat_id, data)
    message = send_order(db, chat_id, prompt, rows)
    data["note_message_id"] = message["message_id"]
    data["note_mode"] = mode
    put_session(db, chat_id, "description", data)


def clear_note_buttons(chat_id, data):
    if data.get("note_message_id"):
        try:
            api("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": data["note_message_id"],
                                           "reply_markup": json.dumps({"inline_keyboard": []})})
        except RuntimeError:
            # The session still rejects callbacks from old/deleted messages.
            pass


def send_order_audio(chat_id, order):
    if order["note_audio_json"]:
        audio = json.loads(order["note_audio_json"])
        api("sendVoice" if audio["kind"] == "voice" else "sendAudio",
            {"chat_id": chat_id, audio["kind"]: audio["file_id"],
             "caption": f"🎤 طلب #{order['id']} — {order['customer_name']}"})


def show_saved_details(chat_id: int, data: dict, db: sqlite3.Connection) -> None:
    message = send_order(db, chat_id,
        "مرحبا بيك فـ LuxeVista ✨\nها المعلومات ديال التوصيل اللي عطيتينا من قبل.\n"
        "شوف واش باقي صحيحة، وتقدر تبدّل اللي بغيتي.\n\n"
        + "\n".join(f"{label}: {data.get(field) or 'ما كاملش'}" for field, label in DETAIL_FIELDS.items())
        + "\n\nالطلب ما غاديش يتصيفط حتى تشوف الملخص وتأكدو. التغييرات غادي تتحفظ مع الطلب الجديد.",
        [[("✅ نكملو بهاد المعلومات", "details:use")],
         [("✏️ الاسم", "details:name"), ("✏️ التيليفون", "details:phone")],
         [("✏️ المدينة", "details:city"), ("✏️ العنوان", "details:address")],
         [("❌ نلغي", "cancel")]])
    data["details_message_id"] = message["message_id"]
    put_session(db, chat_id, "details", data)


def review_order(chat_id: int, p: dict, data: dict, db: sqlite3.Connection) -> None:
    if "items" in data:
        total = sum(item["price_dh"] * item["quantity"] for item in data["items"])
        message = send_order(db, chat_id, "شوف الطلب ديالك واش كلشي صحيح:\n\n"
            + admin.item_summary(data["items"])
            + f"\n\nالمجموع بلا التوصيل: {total} DH\n"
            + "\n".join(f"{label}: {data[field]}" for field, label in DETAIL_FIELDS.items())
            + "\n\nملي تأكد الطلب، معلومات التوصيل ديالك غادي توصل للبائع.",
            [[("✅ نأكد الطلب", "confirm"), ("❌ نلغي", "cancel")],
             [("✏️ نبدّل معلومات التوصيل", "details:edit"), ("🧺 تعديل السلة", "basket:show")],
             [whatsapp_button(data=data)]])
        data["confirm_message_id"] = message["message_id"]
        put_session(db, chat_id, "confirm", data)
        return
    data["quoted_price_dh"] = p["price_dh"]
    data["quoted_name"] = p["name"]
    put_session(db, chat_id, "confirm", data)
    message = send_order(db, chat_id, f"شوف الطلب ديالك واش كلشي صحيح:\n\n{p['name']} × {data['quantity']}\n"
         f"المجموع: {p['price_dh'] * data['quantity']} درهم (بلا التوصيل)\n"
         f"الاسم: {data['name']}\nالتيليفون: {data['phone']}\n"
         f"المدينة: {data['city']}\nالعنوان: {data['address']}\n\n"
         + note_summary(data) + "\n\nملي تكليكي على «نأكد الطلب»، غادي نصيفطو المعلومات ديالك للبائع باش يكمل معاك.",
         [[("✅ نأكد الطلب", "confirm"), ("❌ نلغي", "cancel")],
          [("✏️ نبدّل معلومات التوصيل", "details:edit"), ("✏️ التفاصيل ديال الطلب", "note:edit")],
          [whatsapp_button(p, data)]])
    data["confirm_message_id"] = message["message_id"]
    put_session(db, chat_id, "confirm", data)


def check_stock(chat_id: int, sku: str, quantity: int, products: dict, db: sqlite3.Connection) -> bool:
    if sku not in products:
        cancel(db, chat_id)
        send(chat_id, "هاد المنتج ما بقاش متوفر. كتب /start واختار شي واحد آخر.")
        return False
    available = shop.stock(db, sku)
    if available == 0:
        show_product(chat_id, products[sku], db)
        return False
    if not MIN_QUANTITY <= quantity <= 1000:
        send_order(db, chat_id, "الطلب كيبدا من 10 قطع. كتب شحال بغيتي، من 10 حتى لـ 1000.")
        return False
    if available is not None and available < MIN_QUANTITY:
        send_order(db, chat_id, f"باقي غير {available} قطع دابا، والطلب كيبدا من 10. عاود جرّب ملي يتزاد المخزون.")
        return False
    if available is not None and quantity > available:
        send_order(db, chat_id, f"باقي {available} قطع دابا. كتب كمية أقل.")
        return False
    return True


def order_message(order: sqlite3.Row) -> str:
    return (f"🛒 طلب #{order['id']} ({admin.STATUSES.get(order['status'], 'غير معروف')})\n"
            f"{admin.item_summary(admin.order_items(order))}\n"
            f"المجموع بلا التوصيل: {admin.order_total(order)} DH\n"
            f"الاسم: {order['customer_name']}\nالتيليفون: {order['phone']}\n"
            f"المدينة: {order['city']}\nالعنوان: {order['address']}\n"
            f"التفاصيل ديال الطلب: {order['description'] or '—'}\n"
            + ("🎤 الڤوكال ديالك تسجّل\n" if order["note_audio_json"] else "")
            + f"المصدر: { {'channel': 'القناة', 'catalog': 'الكتالوج', 'direct': 'مباشر', 'arrival': 'تنبيه الجديد', 'restock': 'تنبيه التوفر'}.get(order['source'], order['source'])}\nالتاريخ (التوقيت العالمي): {order['created_at']}")


def delete_order(chat_id: int, command: str, db: sqlite3.Connection) -> None:
    if not admin.is_owner(ADMIN_CHAT_ID, chat_id):
        send(chat_id, "هاد الأمر غير للمسؤول.")
        return
    parts = command.split()
    if (len(parts) != 2 or not re.fullmatch(r"[1-9][0-9]{0,18}", parts[1])
            or int(parts[1]) > 9223372036854775807):
        send(chat_id, "لحذف طلب: أرسل /delete ثم رقمه\nمثال: /delete 2")
        return
    order_id = int(parts[1])
    with db:
        # Keep the SKU reserved even when its last saved order is removed.
        db.execute("INSERT OR IGNORE INTO retired_order_skus (sku) SELECT sku FROM orders WHERE id=?",
                   (order_id,))
        removed = db.execute("DELETE FROM orders WHERE id=?", (order_id,)).rowcount
        if removed:
            remaining_ids = [row["id"] for row in db.execute("SELECT id FROM orders ORDER BY id")]
            # Fill gaps in ascending order so each lower destination ID is free.
            for new_id, old_id in enumerate(remaining_ids, 1):
                if new_id != old_id:
                    db.execute("UPDATE orders SET id=? WHERE id=?", (new_id, old_id))
            db.execute("UPDATE sqlite_sequence SET seq=? WHERE name='orders'", (len(remaining_ids),))
    if not removed:
        send(chat_id, f"الطلب #{order_id} غير موجود. تحقق من رقمه عبر /orders.")
        return
    send(chat_id, f"تم حذف الطلب #{order_id}. أُعيد ترقيم الطلبات المتبقية من ١.\n"
         f"رقم الطلب القادم: #{len(remaining_ids) + 1}. تحقق من الأرقام الحالية عبر /orders قبل حذف طلب آخر.\n"
         "رسائل التأكيد السابقة ما زالت تحمل الأرقام القديمة.")


def show_orders(chat_id: int, command: str, db: sqlite3.Connection) -> None:
    parts = command.split()
    if len(parts) > 2 or (len(parts) == 2 and not re.fullmatch(r"[1-9][0-9]{0,8}", parts[1])):
        send(chat_id, "باش تشوف الطلبات ديالك: /orders\nباش تشوف الصفحة اللي من بعد: /orders 2")
        return
    page = int(parts[1]) if len(parts) == 2 else 1
    is_admin = admin.is_admin(db, ADMIN_CHAT_ID, chat_id)
    # Ownership comes from Telegram's private chat, never from command arguments.
    where = "" if is_admin else " WHERE chat_id=?"
    params = () if is_admin else (chat_id,)
    recent = db.execute("SELECT * FROM orders" + where + " ORDER BY id DESC LIMIT 11 OFFSET ?",
                        (*params, (page - 1) * 10)).fetchall()
    if not recent:
        send(chat_id, "ما كاين حتى طلب فهاد الصفحة." if page > 1 else "ما عندك حتى طلب دابا.")
        return
    for order in recent[:10]:
        send(chat_id, order_message(order), admin.order_buttons(db, order) if is_admin else None)
    if len(recent) > 10:
        send(chat_id, f"باش تشوف الطلبات القديمة، صيفط: /orders {page + 1}")


def handle_callback(callback: dict, products: dict, db: sqlite3.Connection) -> None:
    chat_id = callback["message"]["chat"]["id"]
    api("answerCallbackQuery", {"callback_query_id": callback["id"]})
    if callback["message"]["chat"]["type"] != "private":
        return
    sync_products(products, db)
    command = callback.get("data", "")
    if panel(products, db).callback(chat_id, command):
        if admin.is_admin(db, ADMIN_CHAT_ID, chat_id):
            cancel(db, chat_id)
        return
    if shop.notification_callback(db, products, chat_id, command, send):
        return
    if command.startswith("add:"):
        command = "order:" + command[4:]
    elif command.startswith("basket:"):
        start(chat_id, "", products, db)
        return
    if command.startswith("audio:"):
        if admin.is_admin(db, ADMIN_CHAT_ID, chat_id):
            order = db.execute("SELECT * FROM orders WHERE admin_key=?", (command[6:],)).fetchone()
            if order:
                send_order_audio(chat_id, order)
        return
    if command in {"note:done", "note:edit", "note:clear", "note:voice", "note:text", "note:choose"}:
        current = session(db, chat_id)
        if not current:
            return
        step, data = current
        expected = data.get("note_message_id" if step == "description" else "confirm_message_id")
        if callback["message"]["message_id"] != expected:
            return
        if command == "note:done" and step == "description":
            clear_note_buttons(chat_id, data)
            data["note_complete"] = True
            next_detail(chat_id, data, products, db)
        elif command == "note:edit" and step == "confirm":
            data.pop("note_complete", None)
            ask_order_note(chat_id, data, db, mode="choose", product=products.get(data.get("sku")))
        elif command == "note:clear" and step == "description":
            data.pop("description", None)
            data.pop("note_audio", None)
            ask_order_note(chat_id, data, db, product=products.get(data.get("sku")))
        elif command == "note:choose" and step == "description":
            ask_order_note(chat_id, data, db, mode="choose", product=products.get(data.get("sku")))
        elif (command in {"note:voice", "note:text"} and step == "description"
              and data.get("note_mode", "choose") == "choose"):
            ask_order_note(chat_id, data, db, mode=command.split(":")[1], product=products.get(data.get("sku")))
        return
    if command.startswith("view:"):
        sku = command[5:]
        if sku in products:
            remember_order_message(db, chat_id, callback["message"]["message_id"])
            show_product(chat_id, products[sku], db, "catalog", include_photos=True)
        return
    if command.startswith("order:"):
        admin.clear_editor(db, chat_id)
        parts = command.split(":", 2)
        if len(parts) == 3 and parts[1] in products and SOURCE_PATTERN.fullmatch(parts[2]):
            if not check_stock(chat_id, parts[1], MIN_QUANTITY, products, db):
                return
            if not ADMIN_CHAT_ID:
                send(chat_id, "ما يمكنش تدير طلب دابا. عاود جرّب من بعد.")
                return
            put_session(db, chat_id, "quantity", {"sku": parts[1], "source": parts[2]})
            remember_order_message(db, chat_id, callback["message"]["message_id"])
            send_order(db, chat_id, "شحال من قطعة بغيتي؟\nالطلب كيبدا من 10 قطع، وحتى لـ 1000.\nإلا بغيتي تلغي، كتب /cancel")
        return
    if command.startswith("details:"):
        current = session(db, chat_id)
        if not current:
            send(chat_id, "هاد المرحلة سالات. كتب /start باش تدير طلب جديد.")
            return
        step, data = current
        action = command.split(":", 1)[1]
        expected = data.get("confirm_message_id") if step == "confirm" and action == "edit" else data.get("details_message_id")
        if (callback["message"]["message_id"] != expected
                or not (step == "details" and (action == "use" or action in DETAIL_FIELDS)
                        or step == "confirm" and action == "edit")):
            send(chat_id, "استعمل الأزرار ديال آخر ميساج فالطلب ديالك.")
            return
        if action == "edit":
            show_saved_details(chat_id, data, db)
        elif action == "use":
            next_detail(chat_id, data, products, db)
        else:
            data["editing_detail"] = True
            put_session(db, chat_id, action, data)
            send_order(db, chat_id, f"كتب لينا {DETAIL_FIELDS[action]} من جديد:")
        return
    if command == "cancel":
        cancel(db, chat_id)
        send(chat_id, "صافي، لغينا الطلب. كتب /start باش تشوف المنتجات.")
        return
    if command != "confirm":
        return
    current = session(db, chat_id)
    if not current or current[0] != "confirm":
        send(chat_id, "هاد المرحلة سالات. كتب /start باش تدير طلب جديد.")
        return
    data = current[1]
    if data.get("confirm_message_id") and data["confirm_message_id"] != callback["message"]["message_id"]:
        send(chat_id, "كليكي على زر التأكيد فآخر ملخص ديال الطلب.")
        return
    remember_order_message(db, chat_id, callback["message"]["message_id"])
    if "items" in data:
        confirm_basket(chat_id, data, products, db)
        return
    p = products.get(data["sku"])
    if not p:
        cancel(db, chat_id)
        send(chat_id, "هاد المنتج ما متوفرش دابا.")
        return
    if not check_stock(chat_id, data["sku"], data["quantity"], products, db):
        if data["sku"] in products:
            put_session(db, chat_id, "quantity", data)
        return
    if data.get("quoted_price_dh") != p["price_dh"] or data.get("quoted_name") != p["name"]:
        send_order(db, chat_id, "تبدلات معلومات المنتج. شوف الثمن دابا وعاود أكد الطلب.")
        review_order(chat_id, p, data, db)
        return
    with db:
        cursor = db.execute("""INSERT INTO orders
            (chat_id, sku, product_name, unit_price_dh, quantity,
             customer_name, phone, city, address, source, admin_key, description, note_audio_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, p["sku"], p["name"], p["price_dh"], data["quantity"],
             data["name"], data["phone"], data["city"], data["address"], data["source"], admin.uuid.uuid4().hex,
             data.get("description", ""), json.dumps(data["note_audio"]) if data.get("note_audio") else None))
        db.execute("DELETE FROM sessions WHERE chat_id=?", (chat_id,))
    order = db.execute("SELECT * FROM orders WHERE id=?", (cursor.lastrowid,)).fetchone()
    try:
        send(ADMIN_CHAT_ID, order_message(order), admin.order_buttons(db, order))
        send_order_audio(ADMIN_CHAT_ID, order)
    except RuntimeError as exc:
        print(f"Admin notification failed for order #{order['id']}: {exc}", file=sys.stderr)
    send(chat_id, f"✅ تسجّل الطلب ديالك #{order['id']}. غادي نتاصلو بيك باش نأكدو التوصيل.\n\n"
         "📦 باش تشوف الطلبات ديالك فوقتما بغيتي، كتب /orders", [[whatsapp_button(order=order)]])
    clean_order_chat(db, chat_id)


def main() -> None:
    if not TOKEN:
        raise SystemExit("Put BOT_TOKEN in the .env file next to bot.py (check that it is named .env, not .env.txt).")
    products = load_products()
    db = connect()
    reconcile_imports(db, products)
    me = api("getMe")
    username = me["username"]
    print(f"Bot @{username} running. Open a private chat and send /id for your admin chat ID.")
    if not ADMIN_CHAT_ID:
        print("ADMIN_CHAT_ID missing: order capture is disabled until configured and restarted.")
    offset = None
    consecutive_failures = 0
    while True:
        try:
            # Shorter long polling tolerates home routers and proxies with low idle timeouts.
            due = db.execute("SELECT 1 FROM notification_jobs WHERE status='pending' AND next_attempt<=? LIMIT 1",
                             (time.time(),)).fetchone()
            updates = api("getUpdates", {"timeout": 1 if due else 10, **({"offset": offset} if offset else {})}, timeout=15)
            consecutive_failures = 0
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    if "message" in update:
                        handle_text(update["message"], products, db, username)
                    elif "callback_query" in update:
                        handle_callback(update["callback_query"], products, db)
                except Exception as exc:
                    print(f"Update {update['update_id']} failed: {exc}", file=sys.stderr)
            try:
                sync_products(products, db)
                shop.process_notifications(db, products, username, api)
            except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
                print(f"Notification processing deferred: {type(exc).__name__}", file=sys.stderr)
        except (RuntimeError, json.JSONDecodeError) as exc:
            consecutive_failures += 1
            delay = min(2 ** min(consecutive_failures, 5), 30)
            print(f"Polling error: {exc}. Retrying in {delay}s.", file=sys.stderr)
            time.sleep(delay)


if __name__ == "__main__":
    main()
