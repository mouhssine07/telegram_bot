"""Private Telegram admin controls; no config reads or network calls on import."""

import json
import re
import uuid

import shop_updates as shop


STATUSES = {"new": "🆕 New / Reopen", "confirmed": "✅ Confirmed", "shipped": "🚚 Shipped",
            "delivered": "📦 Delivered", "cancelled": "❌ Cancelled"}
FIELDS = {"name": "name", "price_dh": "price in whole DH", "description": "description",
          "stock": "available stock (0–999999)", "categories": "categories, e.g. women collections (or none)"}


def init_schema(db):
    with db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(orders)")}
        if "description" not in columns:
            db.execute("ALTER TABLE orders ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        if "note_audio_json" not in columns:
            db.execute("ALTER TABLE orders ADD COLUMN note_audio_json TEXT")
        if "admin_key" not in columns:
            db.execute("ALTER TABLE orders ADD COLUMN admin_key TEXT")
        if "status_version" not in columns:
            db.execute("ALTER TABLE orders ADD COLUMN status_version INTEGER NOT NULL DEFAULT 0")
        if "items_json" not in columns:
            db.execute("ALTER TABLE orders ADD COLUMN items_json TEXT")
        if "total_dh" not in columns:
            db.execute("ALTER TABLE orders ADD COLUMN total_dh INTEGER")
        for row in db.execute("SELECT id FROM orders WHERE admin_key IS NULL").fetchall():
            db.execute("UPDATE orders SET admin_key=? WHERE id=?", (uuid.uuid4().hex, row["id"]))
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS order_admin_key ON orders(admin_key)")
        db.execute("""CREATE TABLE IF NOT EXISTS admin_team (
            chat_id INTEGER PRIMARY KEY, added_by INTEGER NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS admin_sessions (
            chat_id INTEGER PRIMARY KEY, data TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS order_status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, admin_key TEXT NOT NULL,
            actor_chat_id INTEGER NOT NULL, old_status TEXT NOT NULL, new_status TEXT NOT NULL,
            changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")


def order_items(order):
    if order["items_json"]:
        return json.loads(order["items_json"])
    return [{"sku": order["sku"], "name": order["product_name"],
             "price_dh": order["unit_price_dh"], "quantity": order["quantity"],
             "source": order["source"]}]


def item_summary(items):
    return "\n".join(f"{item['name'][:60]} [{item['sku']}] × {item['quantity']} — "
                     f"{item['price_dh'] * item['quantity']} DH" for item in items)


def order_total(order):
    return sum(item["price_dh"] * item["quantity"] for item in order_items(order))


def is_owner(owner, chat_id):
    return bool(owner) and str(chat_id) == owner


def is_admin(db, owner, chat_id):
    return bool(owner) and (is_owner(owner, chat_id) or db.execute(
        "SELECT 1 FROM admin_team WHERE chat_id=?", (chat_id,)).fetchone() is not None)


def order_buttons(db, order):
    key = order["admin_key"]
    if not key:
        with db:
            db.execute("UPDATE orders SET admin_key=? WHERE id=? AND admin_key IS NULL",
                       (uuid.uuid4().hex, order["id"]))
        key = db.execute("SELECT admin_key FROM orders WHERE id=?", (order["id"],)).fetchone()[0]
    choices = [(label, f"adm:s:{key}:{order['status_version']}:{status}")
               for status, label in STATUSES.items() if status != order["status"]]
    rows = [choices[index:index + 2] for index in range(0, len(choices), 2)]
    if order["note_audio_json"]:
        rows.append([("🎤 Play order audio", f"audio:{key}")])
    return rows


def clear_editor(db, chat_id):
    with db:
        db.execute("DELETE FROM admin_sessions WHERE chat_id=?", (chat_id,))


class Panel:
    def __init__(self, db, products, owner, send, order_message, save_products):
        self.db, self.products, self.owner = db, products, owner
        self.send, self.order_message, self.save_products = send, order_message, save_products

    def allowed(self, chat_id):
        if is_admin(self.db, self.owner, chat_id):
            return True
        self.send(chat_id, "هذا القسم مخصص لفريق LuxeVista فقط.")
        return False

    def home(self, chat_id):
        clear_editor(self.db, chat_id)
        self.send(chat_id, "LuxeVista — Admin panel\nChoose what to manage. Stock changes only when you set it.", [
            [("📋 Orders", "adm:orders:0:all"), ("🆕 New orders", "adm:orders:0:new")],
            [("⌚ Products & stock", "adm:products:0"), ("➕ Add product", "adm:add")],
            [("📊 Sales totals", "adm:sales:all")],
            *([[("👥 Team access", "adm:team")]] if is_owner(self.owner, chat_id) else [])])

    def orders(self, chat_id, page=0, status="all"):
        where, params = ("", ()) if status == "all" else (" WHERE status=?", (status,))
        rows = self.db.execute("SELECT * FROM orders" + where + " ORDER BY id DESC LIMIT 6 OFFSET ?",
                               (*params, page * 5)).fetchall()
        if not rows:
            self.send(chat_id, "No orders on this page.", [[("Admin panel", "adm:home")]])
            return
        for order in rows[:5]:
            self.send(chat_id, self.order_message(order), order_buttons(self.db, order))
        nav = []
        if page:
            nav.append(("← Previous", f"adm:orders:{page - 1}:{status}"))
        if len(rows) > 5:
            nav.append(("Next →", f"adm:orders:{page + 1}:{status}"))
        self.send(chat_id, f"Orders — {status}, page {page + 1}", ([nav] if nav else []) + [
            [("New", "adm:orders:0:new"), ("Confirmed", "adm:orders:0:confirmed")],
            [("Shipped", "adm:orders:0:shipped"), ("Delivered", "adm:orders:0:delivered")],
            [("Cancelled", "adm:orders:0:cancelled"), ("All", "adm:orders:0:all")],
            [("Admin panel", "adm:home")]])

    def change_status(self, chat_id, key, version, status):
        with self.db:
            order = self.db.execute("SELECT * FROM orders WHERE admin_key=?", (key,)).fetchone()
            if order and order["status_version"] == version and order["status"] != status:
                changed = self.db.execute("""UPDATE orders SET status=?, status_version=status_version+1
                    WHERE admin_key=? AND status_version=?""", (status, key, version)).rowcount
                if changed:
                    self.db.execute("""INSERT INTO order_status_history
                        (admin_key, actor_chat_id, old_status, new_status) VALUES (?, ?, ?, ?)""",
                                    (key, chat_id, order["status"], status))
            else:
                changed = False
        if not order:
            self.send(chat_id, "This order was deleted. Open /admin for current orders.")
            return
        current = self.db.execute("SELECT * FROM orders WHERE admin_key=?", (key,)).fetchone()
        notice = "Status updated.\n\n" if changed else "This button is outdated or already applied. Current order:\n\n"
        if changed and status == "confirmed":
            try:
                self.send(current["chat_id"],
                          f"✅ تم تأكيد طلبك #{current['id']}\n"
                          "شكرًا لاختيارك LuxeVista. سنتواصل معك بخصوص التوصيل.\n\n"
                          f"✅ Your order #{current['id']} is confirmed\n"
                          "Thank you for choosing LuxeVista. We will contact you about delivery.\n\n"
                          f"{item_summary(order_items(current))}\n"
                          f"المجموع بدون التوصيل | Total excluding delivery: "
                          f"{order_total(current)} DH\n\n"
                          "لمتابعة طلبك | View your order: /orders")
                notice = "Status updated. Arabic/English confirmation sent to the customer.\n\n"
            except RuntimeError:
                notice = ("Status updated, but the customer confirmation could not be delivered. "
                          "Contact the customer directly; the order remains confirmed.\n\n")
        self.send(chat_id, notice
                  + self.order_message(current), order_buttons(self.db, current))

    def confirm_order(self, chat_id, parts):
        if (len(parts) != 2 or not re.fullmatch(r"[1-9][0-9]{0,18}", parts[1])
                or int(parts[1]) > 9223372036854775807):
            self.send(chat_id, "Usage: /confirm ORDER_NUMBER\nExample: /confirm 2\nUse /orders to check current order numbers.")
            return
        order = self.db.execute("SELECT * FROM orders WHERE id=?", (int(parts[1]),)).fetchone()
        if not order:
            self.send(chat_id, "Order not found. Use /orders to check current order numbers.")
            return
        # Legacy rows may not have acquired their permanent button reference yet.
        order_buttons(self.db, order)
        order = self.db.execute("SELECT * FROM orders WHERE id=?", (order["id"],)).fetchone()
        self.change_status(chat_id, order["admin_key"], order["status_version"], "confirmed")

    def sales(self, chat_id, period="all"):
        where = {"all": "", "today": " WHERE created_at>=datetime('now','start of day')",
                 "month": " WHERE created_at>=datetime('now','start of month')"}[period]
        rows = self.db.execute("""SELECT status, count(*) AS orders, sum(quantity) AS units,
            sum(COALESCE(total_dh, unit_price_dh * quantity)) AS value FROM orders""" + where + " GROUP BY status").fetchall()
        by_status = {row["status"]: row for row in rows}
        lines = [f"📊 Orders placed: {period} (UTC)", "Current status / orders / units / value (DH)"]
        for status in dict.fromkeys([*STATUSES, *by_status]):
            row = by_status.get(status)
            lines.append(f"{status}: {row['orders'] if row else 0} / {row['units'] if row else 0} / {row['value'] if row else 0} DH")
        delivered = by_status.get("delivered")
        lines += [f"\nDelivered order value: {delivered['value'] if delivered else 0} DH",
                  "Product totals only; delivery fees, costs and payment collection are not tracked.",
                  "Cancelled orders are excluded from delivered value. Deleted orders are excluded from this report."]
        self.send(chat_id, "\n".join(lines), [[("Today", "adm:sales:today"), ("This month", "adm:sales:month"),
                  ("All time", "adm:sales:all")], [("Admin panel", "adm:home")]])

    def product_list(self, chat_id, page=0):
        products = list(self.products.values())
        rows = [[(f"{p['sku']} — {p['name'][:50]}", f"adm:p:{p['sku']}")] for p in products[page * 10:page * 10 + 10]]
        nav = []
        if page:
            nav.append(("← Previous", f"adm:products:{page - 1}"))
        if len(products) > (page + 1) * 10:
            nav.append(("Next →", f"adm:products:{page + 1}"))
        self.send(chat_id, f"Products — page {page + 1}", rows + ([nav] if nav else []) + [
            [("➕ Add product", "adm:add"), ("Admin panel", "adm:home")]])

    def product(self, chat_id, sku):
        p = self.products.get(sku)
        if not p:
            self.send(chat_id, "Product no longer exists. Open /admin for the current catalog.")
            return
        stock = shop.stock(self.db, sku)
        categories = [row[0] for row in self.db.execute("SELECT topic FROM product_topics WHERE sku=? ORDER BY topic", (sku,))]
        self.send(chat_id, f"{sku} — {p['name']}\n{p['price_dh']} DH\n"
                  f"Stock: {stock if stock is not None else 'not tracked'}\nCategories: {', '.join(categories) or 'none'}\n\n"
                  + p.get("description", "")[:1500], [
            [("Edit name", f"adm:e:{sku}:name"), ("Edit price", f"adm:e:{sku}:price_dh")],
            [("Edit description", f"adm:e:{sku}:description")],
            [("Set stock", f"adm:e:{sku}:stock"), ("Set categories", f"adm:e:{sku}:categories")],
            [("Products", "adm:products:0"), ("Admin panel", "adm:home")]])

    def edit(self, chat_id, sku, field):
        if sku not in self.products:
            self.product(chat_id, sku)
            return
        data = {"sku": sku, "field": field, "product": self.products[sku], "stock": shop.stock(self.db, sku),
                "categories": [row[0] for row in self.db.execute("SELECT topic FROM product_topics WHERE sku=? ORDER BY topic", (sku,))]}
        with self.db:
            self.db.execute("INSERT INTO admin_sessions VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET data=excluded.data",
                            (chat_id, json.dumps(data, ensure_ascii=False)))
        self.send(chat_id, f"Editing {sku} — {self.products[sku]['name']}\nSend the new {FIELDS[field]}.\n"
                  "Use /cancel to stop. Use - for an empty description.")

    def editor_text(self, chat_id, raw):
        row = self.db.execute("SELECT data FROM admin_sessions WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            return False
        if not self.allowed(chat_id):
            clear_editor(self.db, chat_id)
            return True
        data = json.loads(row[0])
        sku, field = data["sku"], data["field"]
        current = self.products.get(sku)
        categories = [row[0] for row in self.db.execute("SELECT topic FROM product_topics WHERE sku=? ORDER BY topic", (sku,))]
        if (current != data["product"] or (field == "stock" and shop.stock(self.db, sku) != data["stock"])
                or (field == "categories" and categories != data["categories"])):
            clear_editor(self.db, chat_id)
            self.send(chat_id, "This product changed or was removed while you were editing. Reopen it in /admin before editing again.")
            return True
        value = raw.strip()
        if field in ("price_dh", "stock"):
            if not re.fullmatch(r"[0-9]{1,6}", value):
                self.send(chat_id, "Enter a whole number from 0 to 999999, or /cancel.")
                return True
            value = int(value)
        elif field == "name":
            value = " ".join(value.split())
            if not 1 <= len(value) <= 100:
                self.send(chat_id, "Use a product name of 1–100 characters, or /cancel.")
                return True
        elif field == "description":
            value = "" if value == "-" else value
            if len(value) > 950:
                self.send(chat_id, "Description must be at most 950 characters, or /cancel.")
                return True
        elif field == "categories":
            parts = value.split()
            if parts != ["none"] and (not 1 <= len(parts) <= 5 or not all(
                    shop.TOPIC_PATTERN.fullmatch(topic) and topic not in ("all", "none") for topic in parts)):
                self.send(chat_id, "Use up to five category keys, e.g. women collections, or none to clear.")
                return True
        if field == "stock":
            shop.set_stock(self.db, sku, value)
        elif field == "categories":
            shop.admin_command(self.db, self.products, chat_id, f"/category {sku} {value}", self.send)
        else:
            updated = {**self.products, sku: {**current, field: value}}
            self.save_products(updated)
            self.products.clear()
            self.products.update(updated)
        clear_editor(self.db, chat_id)
        self.send(chat_id, "Saved." + (f" Send /edit {sku} to update existing channel posts with these details." if field in ("name", "price_dh", "description") else ""))
        self.product(chat_id, sku)
        return True

    def team(self, chat_id, parts):
        if not is_owner(self.owner, chat_id):
            self.send(chat_id, "Only the main admin can manage team access.")
            return
        if len(parts) == 1:
            members = [str(row[0]) for row in self.db.execute("SELECT chat_id FROM admin_team ORDER BY chat_id")]
            self.send(chat_id, "Team: " + (", ".join(members) or "no teammates yet") + "\n"
                      "Ask a teammate to open the bot and send /id.\n/team add CHAT_ID\n/team remove CHAT_ID\n"
                      "Teammates can see all customer orders, change statuses, edit products/stock, and publish. Only you can manage access or permanently delete orders.")
            return
        if len(parts) != 3 or parts[1] not in ("add", "remove") or not re.fullmatch(r"[1-9][0-9]{0,15}", parts[2]):
            self.send(chat_id, "Usage: /team add CHAT_ID or /team remove CHAT_ID (positive numeric private chat ID).")
            return
        target = int(parts[2])
        if is_owner(self.owner, target):
            self.send(chat_id, "The main admin is configured in .env and cannot be changed here.")
            return
        with self.db:
            if parts[1] == "add":
                self.db.execute("INSERT OR IGNORE INTO admin_team VALUES (?, ?)", (target, chat_id))
            else:
                self.db.execute("DELETE FROM admin_team WHERE chat_id=?", (target,))
                self.db.execute("DELETE FROM admin_sessions WHERE chat_id=?", (target,))
        self.send(chat_id, f"Team access {'granted' if parts[1] == 'add' else 'revoked'} for {target}.")

    def add_product(self, chat_id):
        clear_editor(self.db, chat_id)
        self.send(chat_id, "Send a product photo or album here. Start the caption with its name and include one whole-DH price.\n"
                  "Example:\nLuxeVista watch\n120 DH\n\nThe bot assigns its SKU. Then open Products to edit it or set stock. "
                  "Reply to the original photo with /preview, then /publish when ready.")

    def command(self, chat_id, raw):
        parts = raw.split()
        if not parts or parts[0] not in ("/admin", "/sales", "/products", "/addproduct", "/team", "/confirm"):
            return False
        if not self.allowed(chat_id):
            return True
        clear_editor(self.db, chat_id)
        if parts[0] == "/confirm":
            self.confirm_order(chat_id, parts)
        elif parts[0] == "/team":
            self.team(chat_id, parts)
        elif parts[0] == "/admin" and len(parts) == 1:
            self.home(chat_id)
        elif parts[0] == "/sales" and (len(parts) == 1 or len(parts) == 2 and parts[1] in ("all", "today", "month")):
            self.sales(chat_id, parts[1] if len(parts) == 2 else "all")
        elif parts[0] == "/products" and len(parts) == 1:
            self.product_list(chat_id)
        elif parts[0] == "/addproduct" and len(parts) == 1:
            self.add_product(chat_id)
        else:
            self.send(chat_id, "Use /admin, /products, /addproduct, or /sales [all|today|month].")
        return True

    def callback(self, chat_id, command):
        if not command.startswith("adm:"):
            return False
        if not self.allowed(chat_id):
            return True
        clear_editor(self.db, chat_id)
        parts = command.split(":")
        if command == "adm:home":
            self.home(chat_id)
        elif command == "adm:add":
            self.add_product(chat_id)
        elif command == "adm:team":
            self.team(chat_id, ["/team"])
        elif len(parts) == 4 and parts[1] == "orders" and re.fullmatch(r"[0-9]{1,8}", parts[2]) and parts[3] in ("all", *STATUSES):
            self.orders(chat_id, int(parts[2]), parts[3])
        elif len(parts) == 3 and parts[1] == "sales" and parts[2] in ("all", "today", "month"):
            self.sales(chat_id, parts[2])
        elif len(parts) == 3 and parts[1] == "products" and re.fullmatch(r"[0-9]{1,8}", parts[2]):
            self.product_list(chat_id, int(parts[2]))
        elif len(parts) == 3 and parts[1] == "p":
            self.product(chat_id, parts[2])
        elif len(parts) == 4 and parts[1] == "e" and parts[3] in FIELDS:
            self.edit(chat_id, parts[2], parts[3])
        elif (len(parts) == 5 and parts[1] == "s" and re.fullmatch(r"[a-f0-9]{32}", parts[2])
              and re.fullmatch(r"[0-9]{1,8}", parts[3]) and parts[4] in STATUSES):
            self.change_status(chat_id, parts[2], int(parts[3]), parts[4])
        else:
            self.send(chat_id, "This action is unavailable. Open /admin again.")
        return True
