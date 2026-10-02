"""Import channel posts by link and format them in place."""
import json
import re

from shop_updates import TelegramError, error_message


def init_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS channel_products (
        channel_id TEXT NOT NULL, message_id INTEGER NOT NULL, sku TEXT NOT NULL,
        target_chat_id TEXT NOT NULL, kind TEXT NOT NULL, album_id TEXT,
        file_id TEXT, cta_message_id INTEGER, product_json TEXT NOT NULL,
        catalog_ready INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(channel_id, message_id))""")
    db.commit()


def apply_edit(api, method, payload):
    try:
        api(method, payload)
    except TelegramError as exc:
        if exc.code != 400 or "message is not modified" not in str(exc).lower():
            raise


def update(db, row, product, username, api, caption, keyboard):
    text, entities = caption(product, username, "channel")
    markup = json.dumps({"inline_keyboard": []})
    payload = {"chat_id": row["channel_id"], "message_id": row["message_id"]}
    if row["kind"] == "text":
        payload.update(text=text, entities=json.dumps(entities, ensure_ascii=False), reply_markup=markup,
                       link_preview_options=json.dumps({"is_disabled": True}))
        method = "editMessageText"
    else:
        payload.update(caption=text, caption_entities=json.dumps(entities, ensure_ascii=False))
        if not row["album_id"]:
            payload["reply_markup"] = markup
        method = "editMessageCaption"
    apply_edit(api, method, payload)
    if row["album_id"] and row["cta_message_id"]:
        apply_edit(api, "editMessageText", {
            "chat_id": row["channel_id"], "message_id": row["cta_message_id"],
            "text": text, "entities": json.dumps(entities, ensure_ascii=False),
            "reply_markup": markup, "link_preview_options": json.dumps({"is_disabled": True})})


def known_product(db, channel_id, target, mid):
    candidates = set()
    cta = None
    album_id = None
    for row in db.execute("SELECT sku, channel_message_id FROM published_posts WHERE target_chat_id IN (?, ?)",
                          (channel_id, target)):
        override = db.execute("SELECT caption_message_id FROM post_caption_overrides WHERE target_chat_id=? AND original_message_id=?",
                              (target, row["channel_message_id"])).fetchone()
        if mid == (override[0] if override else row["channel_message_id"]):
            candidates.add(row["sku"])
    for row in db.execute("SELECT * FROM published_albums WHERE target_chat_id IN (?, ?)", (channel_id, target)):
        try:
            mids = json.loads(row["copied_message_ids"])
        except ValueError:
            continue
        if not isinstance(mids, list) or not mids:
            continue
        override = db.execute("SELECT caption_message_id FROM post_caption_overrides WHERE target_chat_id=? AND original_message_id=?",
                              (target, mids[0])).fetchone()
        if mid in mids or (override and mid == override[0]):
            candidates.add(row["sku"])
            cta, album_id = row["cta_message_id"], row["media_group_id"]
    imported = db.execute("SELECT sku FROM product_imports WHERE origin_key=?", (f"channel:{channel_id}:{mid}",)).fetchone()
    if imported:
        candidates.add(imported[0])
    if len(candidates) > 1:
        raise ValueError("هذا المنشور مرتبط بسجلات منتجات متعارضة. صحح ارتباط رمز المنتج قبل التعديل.")
    return next(iter(candidates), None), cta, album_id


def edit_link(db, products, chat_id, target, username, url, *, api, save_products,
              next_sku, parse_caption, caption, keyboard):
    link = re.fullmatch(r"https://t\.me/(?:(c)/([0-9]+)|([A-Za-z0-9_]+))/([1-9][0-9]*)(?:\?single)?", url)
    if not link or not target:
        raise ValueError("اضبط CHANNEL_ID أولًا، ثم استخدم /edit متبوعًا برابط المنشور مثل https://t.me/yourchannel/123.")
    channel = api("getChat", {"chat_id": target})
    matches = (str(channel["id"]) == "-100" + link[2] if link[1]
               else str(channel.get("username", "")).lower() == link[3].lower())
    if not matches or channel.get("type") != "channel":
        raise ValueError("يجب أن يكون الرابط من قناة تيليجرام المحددة.")
    me = api("getMe")
    member = api("getChatMember", {"chat_id": channel["id"], "user_id": me["id"]})
    if member.get("status") != "creator" and not (member.get("status") == "administrator" and member.get("can_edit_messages")):
        raise ValueError("فعّل صلاحية تعديل الرسائل للبوت في إدارة القناة، ثم أعد المحاولة.")
    channel_id, mid = str(channel["id"]), int(link[4])
    row = db.execute("SELECT * FROM channel_products WHERE channel_id=? AND message_id=?", (channel_id, mid)).fetchone()
    if not row:
        # Bot API has no arbitrary getMessage method. Forward to the requesting admin only.
        try:
            original = api("forwardMessage", {"chat_id": chat_id, "from_chat_id": channel_id,
                           "message_id": mid, "disable_notification": True})
        except RuntimeError as exc:
            raise RuntimeError(f"تعذّرت قراءة المنشور. قد يكون محذوفًا أو محميًا من إعادة التوجيه أو غير متاح: {error_message(exc)}") from exc
        kind = "photo" if original.get("photo") else "video" if original.get("video") else "text" if original.get("text") else None
        if not kind:
            raise ValueError("نوع المنشور غير مدعوم. استخدم صورة أو فيديو أو منشورًا نصيًا للمنتج.")
        raw = original.get("caption") if kind != "text" else original.get("text")
        if not raw:
            raise ValueError("هذه الصورة لا تحتوي وصف المنتج. انسخ رابط صورة الألبوم التي تحمل الاسم والسعر.")
        sku, cta, known_album = known_product(db, channel_id, target, mid)
        album_id = original.get("media_group_id") or known_album
        if not sku and album_id:
            sibling = db.execute("SELECT sku FROM channel_products WHERE channel_id=? AND album_id=?",
                                 (channel_id, str(album_id))).fetchone()
            if sibling:
                raise ValueError("سبق استيراد هذا الألبوم. استخدم /edit " + sibling[0] + " لتحديثه.")
        if sku:
            if sku not in products:
                raise ValueError(f"حُذف المنتج المرتبط {sku} من الكتالوج. أعده قبل التعديل.")
            product = products[sku]
        else:
            # Strip only the known LuxeVista wrapper, so already-formatted posts can be adopted.
            lines = raw.splitlines()
            if lines and lines[0].strip() == "LUXEVISTA":
                lines = [line for line in lines[1:] if line.strip() and not set(line.strip()) <= {'─'}
                         and line.strip() not in {"🛍 اطلب الآن | Order now", "الحد الأدنى: 10 قطع | Minimum: 10 pieces",
                                                  "🛍 اطلب الآن", "الحد الأدنى: 10 قطع"}]
                lines = [line.replace("🟨  ", "").replace("  ·  للوحدة | per item", "").replace("  ·  للوحدة", "") for line in lines]
            name, description, price = parse_caption("\n".join(lines))
            sku = next_sku(products, db)
            product = {"sku": sku, "name": name, "description": description, "price_dh": price}
        caption(product, username, "channel")  # Validate length before reserving a product.
        file_id = original["photo"][-1]["file_id"] if kind == "photo" else original["video"]["file_id"] if kind == "video" else None
        with db:
            db.execute("""INSERT INTO channel_products
                (channel_id, message_id, sku, target_chat_id, kind, album_id, file_id, cta_message_id, product_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (channel_id, mid, sku, target, kind, str(album_id) if album_id else None, file_id, cta,
                 json.dumps(product, ensure_ascii=False)))
            db.execute("INSERT OR IGNORE INTO issued_links VALUES (?)", (sku,))
        row = db.execute("SELECT * FROM channel_products WHERE channel_id=? AND message_id=?", (channel_id, mid)).fetchone()
    sku = row["sku"]
    if not row["catalog_ready"]:
        if sku not in products:
            product = json.loads(row["product_json"])
            save_products({**products, sku: product})
            products[sku] = product
        with db:
            db.execute("UPDATE channel_products SET catalog_ready=1 WHERE channel_id=? AND message_id=?", (channel_id, mid))
    if sku not in products:
        raise ValueError(f"حُذف المنتج {sku} من الكتالوج. أعده قبل التعديل.")
    try:
        update(db, row, products[sku], username, api, caption, keyboard)
    except RuntimeError as exc:
        raise RuntimeError(f"تم حفظ المنتج {sku}، لكن تعذّر تحديث منشور القناة أو زره: {error_message(exc)}. أعد إرسال نفس رابط /edit.") from exc
    return sku
