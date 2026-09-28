"""Stock and opt-in notifications. Standard library only; no configuration reads."""

import json
import re
import time


TOPICS = {"all": "كل المنتجات الجديدة", "women": "ساعات نسائية",
          "men": "ساعات رجالية", "collections": "مجموعات جديدة"}
TOPIC_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,23}")


class TelegramError(RuntimeError):
    def __init__(self, description, code=0, retry_after=0):
        super().__init__(f"Telegram API: {description}")
        self.code = code
        self.retry_after = retry_after


def init_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS inventory (
            sku TEXT PRIMARY KEY, quantity INTEGER NOT NULL CHECK(quantity >= 0)
        );
        CREATE TABLE IF NOT EXISTS product_topics (
            sku TEXT NOT NULL, topic TEXT NOT NULL, PRIMARY KEY(sku, topic)
        );
        CREATE TABLE IF NOT EXISTS arrival_subscriptions (
            chat_id INTEGER NOT NULL, topic TEXT NOT NULL, PRIMARY KEY(chat_id, topic)
        );
        CREATE TABLE IF NOT EXISTS restock_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
            sku TEXT NOT NULL, UNIQUE(chat_id, sku)
        );
        CREATE TABLE IF NOT EXISTS arrival_announcements (sku TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS notification_skus (sku TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS notification_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
            sku TEXT NOT NULL, kind TEXT NOT NULL, topics TEXT NOT NULL DEFAULT '[]',
            request_id INTEGER, status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS pending_notification
            ON notification_jobs(chat_id, sku, kind) WHERE status='pending';
        CREATE INDEX IF NOT EXISTS notification_due
            ON notification_jobs(status, next_attempt);
        CREATE TABLE IF NOT EXISTS notification_clock (
            id INTEGER PRIMARY KEY CHECK(id=1), next_send REAL NOT NULL
        );
        INSERT OR IGNORE INTO notification_clock VALUES (1, 0);
        INSERT OR IGNORE INTO arrival_announcements SELECT sku FROM published_posts;
        INSERT OR IGNORE INTO arrival_announcements SELECT sku FROM published_albums;
    """)


def stock(db, sku):
    row = db.execute("SELECT quantity FROM inventory WHERE sku=?", (sku,)).fetchone()
    return row[0] if row else None


def set_stock(db, sku, quantity):
    with db:
        old = stock(db, sku)
        db.execute("INSERT OR IGNORE INTO notification_skus VALUES (?)", (sku,))
        db.execute("INSERT INTO inventory VALUES (?, ?) ON CONFLICT(sku) DO UPDATE SET quantity=excluded.quantity",
                   (sku, quantity))
        if old == 0 and quantity > 0:
            db.execute("""INSERT OR IGNORE INTO notification_jobs(chat_id, sku, kind, request_id)
                SELECT chat_id, sku, 'restock', id FROM restock_requests WHERE sku=?""", (sku,))


def topics(db):
    return {**TOPICS, **{row[0]: TOPICS.get(row[0], row[0])
                        for row in db.execute("SELECT DISTINCT topic FROM product_topics")}}


def eligible(db, job):
    if job["kind"] == "restock":
        return db.execute("SELECT 1 FROM restock_requests WHERE id=? AND chat_id=? AND sku=?",
                          (job["request_id"], job["chat_id"], job["sku"])).fetchone() is not None
    selected = {row[0] for row in db.execute(
        "SELECT topic FROM arrival_subscriptions WHERE chat_id=?", (job["chat_id"],))}
    return bool(selected.intersection(json.loads(job["topics"])))


def stop_notifications(db, chat_id):
    with db:
        db.execute("DELETE FROM arrival_subscriptions WHERE chat_id=?", (chat_id,))
        db.execute("DELETE FROM restock_requests WHERE chat_id=?", (chat_id,))
        db.execute("UPDATE notification_jobs SET status='cancelled' WHERE chat_id=? AND status='pending'", (chat_id,))


def preferences(db, products, chat_id, send):
    selected = {row[0] for row in db.execute(
        "SELECT topic FROM arrival_subscriptions WHERE chat_id=?", (chat_id,))}
    choices = topics(db)
    choices.update({topic: TOPICS.get(topic, topic) for topic in selected})
    rows = [[(("🔕 إيقاف: " if topic in selected else "🔔 اشتراك: ") + label,
              f"arrival:{'off' if topic in selected else 'on'}:{topic}")]
            for topic, label in choices.items()]
    rows += [[("إلغاء تنبيه: " + products.get(row[0], {}).get("name", row[0]), f"unrestock:{row[0]}")]
             for row in db.execute("SELECT sku FROM restock_requests WHERE chat_id=?", (chat_id,))]
    rows.append([("🔕 إيقاف جميع التنبيهات", "notifications:off")])
    # Keep unusually large catalogs within Telegram's keyboard/message limits.
    for offset in range(0, len(rows), 40):
        send(chat_id, "🔔 تنبيهات LuxeVista\nاختر ما تريد الاشتراك فيه. لن نرسل تنبيهات دون طلبك.\n"
             "تنبيه التوفر يُرسل مرة واحدة لكل طلب. لإيقاف الجميع: /notifications off", rows[offset:offset + 40])


def notification_callback(db, products, chat_id, command, send):
    if command == "notifications:off":
        stop_notifications(db, chat_id)
        send(chat_id, "تم إيقاف جميع التنبيهات. يمكنك الاشتراك مجددًا عبر /notifications")
    elif command.startswith("arrival:"):
        parts = command.split(":")
        if len(parts) != 3 or parts[1] not in ("on", "off"):
            return True
        action, topic = parts[1:]
        if not TOPIC_PATTERN.fullmatch(topic) or (action == "on" and topic not in topics(db)):
            send(chat_id, "هذه الفئة غير متاحة. افتح /notifications")
            return True
        with db:
            if action == "on":
                db.execute("INSERT OR IGNORE INTO arrival_subscriptions VALUES (?, ?)", (chat_id, topic))
            else:
                db.execute("DELETE FROM arrival_subscriptions WHERE chat_id=? AND topic=?", (chat_id, topic))
                for job in db.execute("SELECT * FROM notification_jobs WHERE chat_id=? AND kind='arrival' AND status='pending'",
                                      (chat_id,)).fetchall():
                    if not eligible(db, job):
                        db.execute("UPDATE notification_jobs SET status='cancelled' WHERE id=?", (job["id"],))
        preferences(db, products, chat_id, send)
    elif command.startswith("restock:"):
        sku = command[len("restock:"):]
        if sku not in products:
            send(chat_id, "هذا المنتج لم يعد متاحًا.")
        elif stock(db, sku) != 0:
            send(chat_id, "هذا المنتج متاح الآن. افتح /start لطلبه.")
        else:
            with db:
                db.execute("INSERT OR IGNORE INTO notification_skus VALUES (?)", (sku,))
                db.execute("INSERT OR IGNORE INTO restock_requests(chat_id, sku) VALUES (?, ?)", (chat_id, sku))
            send(chat_id, "✅ سنخبرك مرة واحدة عند توفر هذا المنتج. لإدارة التنبيهات: /notifications",
                 [[("إلغاء هذا التنبيه", f"unrestock:{sku}")]])
    elif command.startswith("unrestock:"):
        sku = command[len("unrestock:"):]
        with db:
            db.execute("DELETE FROM restock_requests WHERE chat_id=? AND sku=?", (chat_id, sku))
            db.execute("UPDATE notification_jobs SET status='cancelled' WHERE chat_id=? AND sku=? AND kind='restock' AND status='pending'",
                       (chat_id, sku))
        send(chat_id, "تم إلغاء تنبيه التوفر لهذا المنتج.")
    else:
        return False
    return True


def queue_arrival(db, sku):
    """Caller commits together with publication; snapshot only existing opt-ins."""
    if not db.execute("INSERT OR IGNORE INTO arrival_announcements VALUES (?)", (sku,)).rowcount:
        return False
    db.execute("INSERT OR IGNORE INTO notification_skus VALUES (?)", (sku,))
    product_topics = ["all"] + [row[0] for row in db.execute("SELECT topic FROM product_topics WHERE sku=?", (sku,))]
    subscribers = db.execute("""SELECT DISTINCT chat_id FROM arrival_subscriptions
        WHERE topic='all' OR topic IN (SELECT topic FROM product_topics WHERE sku=?)""", (sku,)).fetchall()
    db.executemany("INSERT OR IGNORE INTO notification_jobs(chat_id, sku, kind, topics) VALUES (?, ?, 'arrival', ?)",
                   ((row[0], sku, json.dumps(product_topics)) for row in subscribers))
    return True


def admin_command(db, products, chat_id, command, send):
    """Call only after verifying the configured admin and private chat."""
    parts = command.split()
    if len(parts) < 2 or parts[1] not in products:
        send(chat_id, "Use /stock SKU [quantity], /category SKU women collections, or /announce SKU.\nChoose an existing SKU.")
        return
    action, sku = parts[:2]
    if action == "/stock":
        if len(parts) == 2:
            quantity = stock(db, sku)
            send(chat_id, f"{sku}: " + (f"{quantity} available." if quantity is not None else "Stock is not tracked yet. Set it with /stock SKU quantity."))
        elif len(parts) == 3 and re.fullmatch(r"[0-9]{1,6}", parts[2]):
            set_stock(db, sku, int(parts[2]))
            send(chat_id, f"Stock for {sku}: {int(parts[2])}. Eligible restock alerts are queued automatically.")
        else:
            send(chat_id, "Usage: /stock SKU quantity (whole number from 0 to 999999). This sets the total available quantity.")
    elif action == "/category":
        if len(parts) == 2:
            labels = [row[0] for row in db.execute("SELECT topic FROM product_topics WHERE sku=? ORDER BY topic", (sku,))]
            send(chat_id, f"{sku}: " + (", ".join(labels) or "No categories. Use /category SKU women collections"))
        elif (len(parts) <= 7 and all(TOPIC_PATTERN.fullmatch(topic) and topic not in ("all", "none") for topic in parts[2:])) or parts[2:] == ["none"]:
            with db:
                db.execute("INSERT OR IGNORE INTO notification_skus VALUES (?)", (sku,))
                db.execute("DELETE FROM product_topics WHERE sku=?", (sku,))
                if parts[2:] != ["none"]:
                    db.executemany("INSERT OR IGNORE INTO product_topics VALUES (?, ?)", ((sku, topic) for topic in parts[2:]))
            send(chat_id, f"Categories updated for {sku}. Set them before the first /publish or /announce.")
        else:
            send(chat_id, "Use up to 5 category keys (lowercase letters, digits, _ or -; max 24 characters). Example: /category SKU women collections. Use none to clear.")
    elif action == "/announce":
        if len(parts) != 2:
            send(chat_id, "Usage: /announce SKU")
            return
        with db:
            queued = queue_arrival(db, sku)
        send(chat_id, "New-arrival alerts queued for existing subscribers." if queued else "This product was already announced; no duplicate alerts queued.")


def process_notifications(db, products, username, api, now=None):
    """Deliver at most one due alert per tick; persisted retries survive restarts."""
    now = time.time() if now is None else now
    if db.execute("SELECT next_send FROM notification_clock WHERE id=1").fetchone()[0] > now:
        return
    job = db.execute("SELECT * FROM notification_jobs WHERE status='pending' AND next_attempt<=? ORDER BY next_attempt, id LIMIT 1",
                     (now,)).fetchone()
    if not job:
        return
    product = products.get(job["sku"])
    if not product or not eligible(db, job):
        with db:
            db.execute("UPDATE notification_jobs SET status='cancelled' WHERE id=?", (job["id"],))
            if not product and job["kind"] == "restock":
                db.execute("DELETE FROM restock_requests WHERE id=?", (job["request_id"],))
        return
    if stock(db, job["sku"]) == 0:
        with db:
            db.execute("UPDATE notification_jobs SET next_attempt=? WHERE id=?", (now + 60, job["id"]))
        return
    title = "🔔 متوفر من جديد" if job["kind"] == "restock" else "✨ جديد LuxeVista"
    markup = {"inline_keyboard": [[{"text": "🛒 Order now | اطلب الآن",
               "url": f"https://t.me/{username}?start=p_{job['sku']}_{job['kind']}"}],
               [{"text": "🔕 إيقاف جميع التنبيهات", "callback_data": "notifications:off"}]]}
    with db:
        # At most one notification per second, including across restarts.
        db.execute("UPDATE notification_clock SET next_send=? WHERE id=1", (now + 1,))
    try:
        api("sendMessage", {"chat_id": job["chat_id"],
            "text": f"{title}\n{product['name']}\n{product['price_dh']} DH\n\nلإدارة التنبيهات: /notifications",
            "reply_markup": json.dumps(markup, ensure_ascii=False)})
    except RuntimeError as exc:
        if getattr(exc, "code", 0) == 403:
            stop_notifications(db, job["chat_id"])
            return
        delay = max(getattr(exc, "retry_after", 0), min(30 * 2 ** min(job["attempts"], 7), 3600))
        with db:
            db.execute("UPDATE notification_jobs SET attempts=attempts+1, next_attempt=? WHERE id=?", (now + delay, job["id"]))
            if getattr(exc, "code", 0) == 429:
                db.execute("UPDATE notification_clock SET next_send=? WHERE id=1", (now + delay,))
        print(f"Notification #{job['id']} deferred; retry in {delay}s.")
        return
    with db:
        db.execute("UPDATE notification_jobs SET status='sent' WHERE id=?", (job["id"],))
        if job["kind"] == "restock":
            db.execute("DELETE FROM restock_requests WHERE id=?", (job["request_id"],))
