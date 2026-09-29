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


def keyboard(rows: list[list[tuple[str, str]]]) -> str:
    return json.dumps(
        {"inline_keyboard": [[{"text": label, "callback_data": callback} for label, callback in row] for row in rows]},
        ensure_ascii=False,
    )


def order_link_keyboard(bot_username: str, sku: str, source: str) -> str:
    url = f"https://t.me/{bot_username}?start=p_{sku}_{source}"
    return json.dumps({"inline_keyboard": [[{"text": "🛒 Order now | اطلب الآن", "url": url}]]}, ensure_ascii=False)


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
    append(f"{product['price_dh']} DH", "bold")
    append("  ·  للوحدة | per item\n\n")
    append("رسوم التوصيل تُؤكد عند الطلب\nDelivery fee confirmed when ordering", "italic")
    append("\n\n────────────────\n")
    label = "🛍 اطلب الآن | Order now"
    append(label, "bold")
    if album:
        entities.append({**entities[-1], "type": "text_link",
                         "url": f"https://t.me/{bot_username}?start=p_{product['sku']}_{source}"})
    if len(caption.encode("utf-16-le")) // 2 > 1024:
        raise RuntimeError("Product caption is too long for the new layout. Shorten its description in /products and preview again.")
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
        raise ValueError("Caption must contain one unambiguous whole-DH price (for example, 110 DH or 110 درهم).")
    first_line = caption.splitlines()[0].strip()
    name = re.split(r"\s*(?:—|–|\|+|--+)\s*", first_line, maxsplit=1)[0].strip(" -:…")
    if not name or PRICE_PATTERN.search(name) or len(name) > 100:
        raise ValueError("Start the caption with a short product name, then put the price in DH.")
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


def show_product(chat_id: int, product: dict, db: sqlite3.Connection, source: str = "channel") -> None:
    sku = product["sku"]
    unavailable = shop.stock(db, sku) == 0
    buttons = [[("🔔 Notify me when available | أخبرني عند التوفر", f"restock:{sku}")]] if unavailable else [
        [("🛒 اطلب الآن", f"order:{sku}:{source}")]]
    send_order(db, chat_id,
         f"{product['name']}\n{product.get('description', '')}\n\n"
         f"السعر: {product['price_dh']} درهم للوحدة\n"
         + ("غير متوفر حاليًا. اطلب تنبيهًا عند التوفر." if unavailable else "التوصيل يُحدد عند تأكيد الطلب.")
         + "\nتنبيهات المنتجات الجديدة: /notifications", buttons)


def start(chat_id: int, payload: str, products: dict, db: sqlite3.Connection) -> None:
    cancel(db, chat_id)
    # Deep link: ?start=p_SKU_SOURCE ; source defaults to "channel".
    if payload.startswith("p_"):
        remainder = payload[2:]
        for sku in sorted(products, key=len, reverse=True):
            if remainder == sku or remainder.startswith(sku + "_"):
                source = remainder[len(sku) + 1:] if remainder != sku else "channel"
                if SOURCE_PATTERN.fullmatch(source):
                    show_product(chat_id, products[sku], db, source)
                    return
    send_order(db, chat_id, "مرحبا 👋 اختر المنتج:\nللاشتراك في تنبيهات الجديد: /notifications",
         [[(f"{p['name']} — {p['price_dh']} DH", f"view:{p['sku']}")] for p in products.values()])


def publish_album(chat_id: int, media_group_id: str, sku: str, is_publish: bool,
                  products: dict, db: sqlite3.Connection, bot_username: str) -> None:
    destination = CHANNEL_ID if is_publish else chat_id
    rows = db.execute("""SELECT source_message_id, file_id, caption, caption_entities FROM album_items
        WHERE source_chat_id=? AND media_group_id=? ORDER BY source_message_id""",
        (chat_id, media_group_id)).fetchall()
    if not 2 <= len(rows) <= 10 or not all(row["file_id"] for row in rows):
        send(chat_id, "Send all product photos together as one album, then reply to any photo with /preview " + sku)
        return
    caption_row = next((row for row in rows if row["caption"]), None)
    if not caption_row:
        send(chat_id, "This album has no description. Add a caption to one photo and send the album again.")
        return
    if is_publish:
        prior = db.execute("""SELECT cta_message_id FROM published_albums
            WHERE source_chat_id=? AND media_group_id=? AND sku=? AND target_chat_id=?""",
            (chat_id, media_group_id, sku, CHANNEL_ID)).fetchone()
        if prior:
            send(chat_id, "This album was already published. Check the channel before posting it again.")
            return
    caption, entities = product_post_caption(
        products[sku], bot_username, "channel" if is_publish else "preview", album=True)
    media = [{"type": "photo", "media": row["file_id"]} for row in rows]
    media[0]["caption"] = caption
    media[0]["caption_entities"] = entities
    result = api("sendMediaGroup", {"chat_id": destination, "media": json.dumps(media, ensure_ascii=False)}, timeout=35)
    message_ids = [message["message_id"] for message in result]
    if is_publish:
        with db:
            db.execute("""INSERT INTO published_albums VALUES (?, ?, ?, ?, ?, ?)""",
                       (chat_id, media_group_id, sku, CHANNEL_ID,
                        json.dumps(message_ids), None))
            shop.queue_arrival(db, sku)
        send(chat_id, f"✅ Published {len(rows)} separate photos as one album to {CHANNEL_ID}, with one tappable order link in its description.")
    else:
        send(chat_id, f"Album preview above ({len(rows)} separate photos). Reply to any photo in the original album with /publish {sku} when ready.")


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
            send(chat_id, "Add a caption with the product name and one price (e.g. 110 DH) to the photo, then send it again. For several photos, send them together as one album.")
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
        status = "Already imported"
    else:
        try:
            name, description, price_dh = parse_product_caption(caption)
        except ValueError as exc:
            send(chat_id, f"Could not import this product: {exc}\nEdit its caption and send it again.")
            return
        sku = next_sku(products, db)
        product = {"sku": sku, "name": name, "description": description, "price_dh": price_dh}
        save_products({**products, sku: product})
        products[sku] = product
        db.execute("INSERT OR REPLACE INTO product_imports VALUES (?, ?)", (key, sku))
        status = "Imported"
    db.execute("INSERT OR REPLACE INTO source_imports VALUES (?, ?, ?)",
               (chat_id, message["message_id"], sku))
    if message.get("media_group_id"):
        db.execute("INSERT OR REPLACE INTO album_imports VALUES (?, ?, ?)",
                   (chat_id, str(message["media_group_id"]), sku))
    db.commit()
    p = products[sku]
    send(chat_id, f"✅ {status}: {sku}\n{p['name']} — {p['price_dh']} DH\n"
         f"Reply to the product photo (or any photo in its album) with /preview or /publish. You can also add the SKU: /publish {sku}.")


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
    if raw.startswith("/start"):
        admin.clear_editor(db, chat_id)
        parts = raw.split(maxsplit=1)
        start(chat_id, parts[1].strip() if len(parts) > 1 else "", products, db)
        remember_order_message(db, chat_id, message["message_id"])
        return
    if raw == "/id":
        send(chat_id, f"Your chat ID: {chat_id}")
        return
    if raw == "/cancel":
        admin.clear_editor(db, chat_id)
        cancel(db, chat_id)
        send(chat_id, "تم إلغاء العملية. اكتب /start للعودة إلى المنتجات.")
        return
    if panel(products, db).command(chat_id, raw):
        if admin.is_admin(db, ADMIN_CHAT_ID, chat_id):
            cancel(db, chat_id)
        return
    if raw.split(maxsplit=1)[:1] in (["/notifications"], ["/arrivals"]):
        if raw.split()[1:] == ["off"]:
            shop.stop_notifications(db, chat_id)
            send(chat_id, "تم إيقاف جميع التنبيهات. يمكنك الاشتراك مجددًا عبر /notifications")
        elif len(raw.split()) == 1:
            shop.preferences(db, products, chat_id, send)
        else:
            send(chat_id, "لإدارة التنبيهات: /notifications\nلإيقاف الجميع: /notifications off")
        return
    if raw.split(maxsplit=1)[:1] in (["/stock"], ["/category"], ["/announce"]):
        if not admin.is_admin(db, ADMIN_CHAT_ID, chat_id):
            send(chat_id, "هذا الأمر مخصص للمسؤول.")
            return
        shop.admin_command(db, products, chat_id, raw, send)
        return
    if raw.startswith("/preview") or raw.startswith("/publish"):
        if not admin.is_admin(db, ADMIN_CHAT_ID, chat_id):
            send(chat_id, "هذا الأمر مخصص للمسؤول.")
            return
        parts = raw.split()
        original = message.get("reply_to_message")
        if not original or len(parts) not in (1, 2):
            send(chat_id, "Send the product photo with its caption first. Then reply to its photo with /preview or /publish. You can also add a SKU, e.g. /preview bv001.")
            return
        imported_sku = imported_sku_for_reply(db, chat_id, original)
        sku = parts[1] if len(parts) == 2 else imported_sku
        if not sku or sku not in products:
            send(chat_id, "No product found. Send the photo with a name and price in its caption first, or add its SKU to products.json.")
            return
        if imported_sku and sku != imported_sku:
            send(chat_id, f"This product was imported as {imported_sku}. Use /preview {imported_sku} or /publish {imported_sku}.")
            return
        source_id = original["message_id"]
        is_publish = parts[0] == "/publish"
        if not is_publish and parts[0] != "/preview":
            return
        if is_publish and not CHANNEL_ID:
            send(chat_id, "Set CHANNEL_ID in .env, restart the bot, then try again.")
            return
        if original.get("media_group_id"):
            try:
                publish_album(chat_id, str(original["media_group_id"]), sku, is_publish,
                              products, db, bot_username)
            except RuntimeError as exc:
                send(chat_id, f"Could not {'publish' if is_publish else 'preview'} album: {exc}")
            return
        destination = CHANNEL_ID if is_publish else chat_id
        if is_publish:
            prior = db.execute("""SELECT channel_message_id FROM published_posts
                WHERE source_chat_id=? AND source_message_id=? AND sku=? AND target_chat_id=?""",
                (chat_id, source_id, sku, CHANNEL_ID)).fetchone()
            if prior:
                send(chat_id, f"Already published this post as channel message #{prior['channel_message_id']}.")
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
                "reply_markup": order_link_keyboard(bot_username, sku, "channel" if is_publish else "preview"),
            })
        except RuntimeError as exc:
            send(chat_id, f"Could not {'publish' if is_publish else 'preview'}: {exc}")
            return
        if is_publish:
            with db:
                db.execute("""INSERT INTO published_posts VALUES (?, ?, ?, ?, ?)""",
                           (chat_id, source_id, sku, CHANNEL_ID, result["message_id"]))
                shop.queue_arrival(db, sku)
            send(chat_id, f"✅ Published to {CHANNEL_ID} with an Order now button (message #{result['message_id']}).")
        else:
            send(chat_id, "Preview above. Reply to the same original post with /publish " + sku + " when ready.")
        return
    if raw.split(maxsplit=1)[:1] == ["/orders"]:
        show_orders(chat_id, raw, db)
        return
    if raw.split(maxsplit=1)[:1] == ["/delete"]:
        delete_order(chat_id, raw, db)
        return
    if raw.startswith("/link"):
        if not admin.is_admin(db, ADMIN_CHAT_ID, chat_id):
            send(chat_id, "هذا الأمر مخصص للمسؤول.")
            return
        parts = raw.split()
        if len(parts) != 3 or parts[1] not in products or not SOURCE_PATTERN.fullmatch(parts[2]):
            send(chat_id, "Usage: /link SKU source\nExample: /link bv001 channel")
            return
        payload = f"p_{parts[1]}_{parts[2]}"
        if len(payload) > 64:
            send(chat_id, "SKU and source are too long for a Telegram deep link.")
            return
        db.execute("INSERT OR IGNORE INTO issued_links (sku) VALUES (?)", (parts[1],))
        db.commit()
        send(chat_id, f"Product link:\nhttps://t.me/{bot_username}?start={payload}")
        return
    if raw.startswith("/"):
        send(chat_id, "اكتب /start لعرض المنتجات أو /cancel لإلغاء الطلب.")
        return

    if panel(products, db).editor_text(chat_id, raw):
        return
    current = session(db, chat_id)
    if not current:
        send(chat_id, "اكتب /start لعرض المنتجات.")
        return
    step, data = current
    remember_order_message(db, chat_id, message["message_id"])
    value = " ".join(raw.split())
    if not value or len(value) > 150:
        send_order(db, chat_id, "أدخل قيمة صحيحة (حتى 150 حرفًا). أو اكتب /cancel.")
        return
    if step == "quantity":
        if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 1000:
            send_order(db, chat_id, "أدخل الكمية بالأرقام من 1 إلى 1000.")
            return
        if not check_stock(chat_id, data["sku"], int(value), products, db):
            return
        data["quantity"] = int(value)
        put_session(db, chat_id, "name", data)
        send_order(db, chat_id, "الاسم الكامل؟")
    elif step == "name":
        data["name"] = value
        put_session(db, chat_id, "phone", data)
        send_order(db, chat_id, "رقم الهاتف؟")
    elif step == "phone":
        phone = re.sub(r"[\s().-]", "", value)
        if not re.fullmatch(r"\+?[0-9]{9,15}", phone):
            send_order(db, chat_id, "أدخل رقم هاتف صحيحًا، مثل 0612345678 أو +212612345678.")
            return
        data["phone"] = phone
        put_session(db, chat_id, "city", data)
        send_order(db, chat_id, "المدينة؟")
    elif step == "city":
        data["city"] = value
        put_session(db, chat_id, "address", data)
        send_order(db, chat_id, "العنوان الكامل للتوصيل؟")
    elif step == "address":
        p = products.get(data["sku"])
        if not p:
            cancel(db, chat_id)
            send_order(db, chat_id, "هذا المنتج لم يعد متاحًا. اكتب /start لاختيار منتج آخر.")
            return
        data["address"] = value
        review_order(chat_id, p, data, db)
    else:
        send_order(db, chat_id, "اضغط على زر تأكيد الطلب أو اكتب /cancel.")


def review_order(chat_id: int, p: dict, data: dict, db: sqlite3.Connection) -> None:
    data["quoted_price_dh"] = p["price_dh"]
    data["quoted_name"] = p["name"]
    put_session(db, chat_id, "confirm", data)
    message = send_order(db, chat_id, f"راجع طلبك:\n\n{p['name']} × {data['quantity']}\n"
         f"المجموع: {p['price_dh'] * data['quantity']} درهم (بدون التوصيل)\n"
         f"الاسم: {data['name']}\nالهاتف: {data['phone']}\n"
         f"المدينة: {data['city']}\nالعنوان: {data['address']}\n\n"
         "بالضغط على تأكيد، سترسل بياناتك إلى البائع لمتابعة الطلب.",
         [[("✅ تأكيد الطلب", "confirm"), ("❌ إلغاء", "cancel")]])
    data["confirm_message_id"] = message["message_id"]
    put_session(db, chat_id, "confirm", data)


def check_stock(chat_id: int, sku: str, quantity: int, products: dict, db: sqlite3.Connection) -> bool:
    if sku not in products:
        cancel(db, chat_id)
        send(chat_id, "هذا المنتج لم يعد متاحًا. افتح /start لاختيار منتج آخر.")
        return False
    available = shop.stock(db, sku)
    if available == 0:
        show_product(chat_id, products[sku], db)
        return False
    if available is not None and quantity > available:
        send_order(db, chat_id, f"الكمية المتاحة حاليًا: {available}. أدخل كمية أقل.")
        return False
    return True


def order_message(order: sqlite3.Row) -> str:
    return (f"🛒 طلب #{order['id']} ({order['status']})\n"
            f"{order['product_name']} [{order['sku']}] × {order['quantity']}\n"
            f"المجموع بدون التوصيل: {order['unit_price_dh'] * order['quantity']} DH\n"
            f"الاسم: {order['customer_name']}\nالهاتف: {order['phone']}\n"
            f"المدينة: {order['city']}\nالعنوان: {order['address']}\n"
            f"المصدر: {order['source']}\nالتاريخ (UTC): {order['created_at']}")


def delete_order(chat_id: int, command: str, db: sqlite3.Connection) -> None:
    if not admin.is_owner(ADMIN_CHAT_ID, chat_id):
        send(chat_id, "هذا الأمر مخصص للمسؤول.")
        return
    parts = command.split()
    if (len(parts) != 2 or not re.fullmatch(r"[1-9][0-9]{0,18}", parts[1])
            or int(parts[1]) > 9223372036854775807):
        send(chat_id, "Usage: /delete ORDER_NUMBER\nExample: /delete 2")
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
        send(chat_id, f"Order #{order_id} was not found. Use /orders to check the order number.")
        return
    send(chat_id, f"Deleted order #{order_id}. Remaining orders have been renumbered from 1.\n"
         f"Next new order: #{len(remaining_ids) + 1}. Use /orders to check current numbers before deleting another order.\n"
         "Previously sent confirmations still show their old numbers.")


def show_orders(chat_id: int, command: str, db: sqlite3.Connection) -> None:
    parts = command.split()
    if len(parts) > 2 or (len(parts) == 2 and not re.fullmatch(r"[1-9][0-9]{0,8}", parts[1])):
        send(chat_id, "لعرض طلباتك: /orders\nللصفحة التالية: /orders 2")
        return
    page = int(parts[1]) if len(parts) == 2 else 1
    is_admin = admin.is_admin(db, ADMIN_CHAT_ID, chat_id)
    # Ownership comes from Telegram's private chat, never from command arguments.
    where = "" if is_admin else " WHERE chat_id=?"
    params = () if is_admin else (chat_id,)
    recent = db.execute("SELECT * FROM orders" + where + " ORDER BY id DESC LIMIT 11 OFFSET ?",
                        (*params, (page - 1) * 10)).fetchall()
    if not recent:
        send(chat_id, "لا توجد طلبات في هذه الصفحة." if page > 1 else "لا توجد طلبات بعد.")
        return
    for order in recent[:10]:
        send(chat_id, order_message(order), admin.order_buttons(db, order) if is_admin else None)
    if len(recent) > 10:
        send(chat_id, f"لعرض الطلبات الأقدم أرسل: /orders {page + 1}")


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
    if command.startswith("view:"):
        sku = command[5:]
        if sku in products:
            remember_order_message(db, chat_id, callback["message"]["message_id"])
            show_product(chat_id, products[sku], db, "catalog")
        return
    if command.startswith("order:"):
        admin.clear_editor(db, chat_id)
        parts = command.split(":", 2)
        if len(parts) == 3 and parts[1] in products and SOURCE_PATTERN.fullmatch(parts[2]):
            if not check_stock(chat_id, parts[1], 1, products, db):
                return
            if not ADMIN_CHAT_ID:
                send(chat_id, "الطلب غير متاح الآن. يرجى المحاولة لاحقًا.")
                return
            put_session(db, chat_id, "quantity", {"sku": parts[1], "source": parts[2]})
            remember_order_message(db, chat_id, callback["message"]["message_id"])
            send_order(db, chat_id, "كم قطعة تريد؟ (من 1 إلى 1000)\nيمكنك الإلغاء بـ /cancel")
        return
    if command == "cancel":
        cancel(db, chat_id)
        send(chat_id, "تم الإلغاء. اكتب /start لعرض المنتجات.")
        return
    if command != "confirm":
        return
    current = session(db, chat_id)
    if not current or current[0] != "confirm":
        send(chat_id, "انتهت هذه الخطوة. اكتب /start لإنشاء طلب جديد.")
        return
    data = current[1]
    if data.get("confirm_message_id") and data["confirm_message_id"] != callback["message"]["message_id"]:
        send(chat_id, "استخدم زر التأكيد في آخر ملخص لطلبك.")
        return
    remember_order_message(db, chat_id, callback["message"]["message_id"])
    p = products.get(data["sku"])
    if not p:
        cancel(db, chat_id)
        send(chat_id, "هذا المنتج غير متاح حاليًا.")
        return
    if not check_stock(chat_id, data["sku"], data["quantity"], products, db):
        if data["sku"] in products:
            put_session(db, chat_id, "quantity", data)
        return
    if data.get("quoted_price_dh") != p["price_dh"] or data.get("quoted_name") != p["name"]:
        send_order(db, chat_id, "Product details changed. Please review the current price and confirm again.")
        review_order(chat_id, p, data, db)
        return
    with db:
        cursor = db.execute("""INSERT INTO orders
            (chat_id, sku, product_name, unit_price_dh, quantity,
             customer_name, phone, city, address, source, admin_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, p["sku"], p["name"], p["price_dh"], data["quantity"],
             data["name"], data["phone"], data["city"], data["address"], data["source"], admin.uuid.uuid4().hex))
        db.execute("DELETE FROM sessions WHERE chat_id=?", (chat_id,))
    order = db.execute("SELECT * FROM orders WHERE id=?", (cursor.lastrowid,)).fetchone()
    try:
        send(ADMIN_CHAT_ID, order_message(order), admin.order_buttons(db, order))
    except RuntimeError as exc:
        print(f"Admin notification failed for order #{order['id']}: {exc}", file=sys.stderr)
    send(chat_id, f"✅ تم تسجيل طلبك #{order['id']}. سنتواصل معك لتأكيد التوصيل.\n\n"
         "📦 يمكنك مشاهدة طلباتك في أي وقت بإرسال /orders")
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
