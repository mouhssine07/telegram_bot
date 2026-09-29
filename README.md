# LuxeVista Telegram shop bot

This bot supports a small product catalog, a private order form, SQLite order storage, an admin alert, `/orders`, tracked product links, and channel posts with an **Order now** button. It uses Python 3.10+ and the standard library. No hosting is needed to try it on your own computer; keep the process running to receive orders.

## Set up (Windows PowerShell)

For a **new clone only**, create the local files if they do not already exist:

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.template -Destination .env
}
if (-not (Test-Path -LiteralPath products.json)) {
    Copy-Item -LiteralPath products.example.json -Destination products.json
}
```

Fill in the placeholders in your local `.env` privately before starting. The sample catalog is empty; import your products through the bot. No pip installation is needed. `.env`, legacy `.env.example` files, the live catalog, and SQLite files are deliberately ignored by Git. The bot creates its database on first start; keep your existing database when updating an installation.

1. In Telegram, open [@BotFather](https://t.me/BotFather), send `/newbot`, and save the new bot's token privately.
2. Keep your existing `.env` beside `bot.py`, with `BOT_TOKEN`, `ADMIN_CHAT_ID`, and `CHANNEL_ID`. For a new installation only, create this file privately. **Never overwrite existing configuration, `products.json`, or `orders.sqlite3` with examples. Do not send your token in chat or commit `.env`.**
3. Run these PowerShell commands:

   ```powershell
   python bot.py
   ```

4. Open your new bot in Telegram and send `/id` in a private chat. Copy the numeric chat ID into `.env` as `ADMIN_CHAT_ID`. Stop the bot with Ctrl+C.
5. Start or restart it yourself; the bot automatically reads `.env`:

   ```powershell
   python bot.py
   ```

6. In your private chat with the bot, send `/link bv001 channel`. Put the returned link in the caption of the corresponding product post in your channel. Test the entire flow yourself before posting the link publicly. For an ad, create a separate source, e.g. `/link bv001 tgad1`.

## Publish a product with an Order now button

1. Add `CHANNEL_ID=@YourChannelUsername` to `.env` (for a private channel, use its numeric chat ID instead). Restart the bot after changing `.env`.
2. Add your bot as an **administrator** of the channel with **Post Messages** permission. This is necessary for it to publish posts.
3. From the configured admin's private chat, send a photo or several photos together as one album. Start its caption with the product name and include one clear whole-DH price. The bot imports the product and returns an automatic SKU such as `p0001`.
4. **Reply to the original photo or any photo in its album** with `/preview`. The SKU is inferred from the import; an explicit SKU is also supported, e.g. `/preview p0001`.
5. Reply to the original photo or album with `/publish`. Single photos have a native **Order now | اطلب الآن** button. Albums remain separate photos in one album, with a bold bilingual order link in the caption. There is no collage or separate CTA post. Duplicate publishing of the same original and SKU is prevented.
6. Tap the button on the new channel post and complete a test order. The admin notification should show `source: channel`.

New previews and publications use a consistent LuxeVista caption: brand header, bold product name, description, bold linked catalog price with a gold accent, a bilingual 10-piece minimum, and order action. The delivery-fee note is omitted. Telegram captions do not support arbitrary font colors; the linked price uses the viewer's Telegram link styling. Imported name/price repetitions are removed from the description. Photos and album order stay unchanged; the album caption remains on its first photo. Captions use Telegram text entities, including correct Arabic/emoji offsets. If the layout exceeds the caption limit, shorten the description in `/products` and preview again. Custom formatting from the original caption is replaced by this layout.

The bot creates a new channel post. It cannot reliably add a button to an existing post you published manually. If the new post replaces an old one, review it first and then remove the old post yourself if you want to avoid duplicates. Publishing is only triggered by your explicit `/publish` command.

You can also edit `products.json` to set the SKU, name, description, and **unit price**. The bot reloads the catalog before processing private messages, callbacks, and queued notifications, removing stale import mappings. Deleted SKU gaps can be reused unless reserved by published posts, saved/deleted orders, generated links, or stock/notification setup. Replace `bv001` with your actual SKU in `/link`. Delivery charges need manual confirmation. Stock is managed using `/stock`, as described below.

## Commands

| Command | Use |
| --- | --- |
| `/start` | Open catalog. |
| `/id` | Show your own chat ID for configuration. |
| `/cancel` | Cancel the active form. |
| `/admin` | Owner/team: open the private admin panel. |
| `/products` | Owner/team: browse and edit products, stock and categories. |
| `/addproduct` | Owner/team: instructions for adding a product by photo or album. |
| `/sales` | Owner/team: all-time counts and product values by current order status. |
| `/sales today` or `/sales month` | Owner/team: report for orders placed during this UTC day/month. |
| `/team` | Main admin only: list team members and access instructions. |
| `/team add CHAT_ID` or `/team remove CHAT_ID` | Main admin only: grant or revoke teammate access. |
| `/link SKU source` | Admin: create a product link with a source label. |
| `/orders` | Customer: show their own 10 most recent orders. Admin: show the latest 10 orders across customers. |
| `/orders 2` | Show the next page of older orders; the same ownership restrictions apply on every page. |
| `/confirm 2` | Owner/team: confirm order #2 and send its customer an Arabic and English confirmation. |
| `/delete 2` | Main admin only: permanently delete order #2 and renumber remaining orders from 1. |
| `/preview SKU` | Admin: reply to a prepared product post to preview it with an order button. |
| `/publish SKU` | Admin: reply to a prepared product post to publish it in the channel. |
| `/stock SKU` | Admin: see the current stock quantity, or whether stock is not tracked yet. |
| `/stock SKU 5` | Admin: set the available quantity to 5 (not add 5). Use 0 for unavailable. |
| `/category SKU women collections` | Admin: replace the product's categories. Set these before first publication. |
| `/category SKU` | Admin: see categories. `/category SKU none` clears them. |
| `/announce SKU` | Admin: queue a new-arrival alert without publishing a channel post; once per SKU. |
| `/notifications` or `/arrivals` | Customer: choose new-arrival categories, All arrivals, and manage restock requests. |
| `/notifications off` | Stop all arrival/restock alerts, including pending deliveries, for your own account. |

Orders are stored in `orders.sqlite3` in this folder unless `BOT_DB_PATH` is set. Back up this file because it contains customer names, addresses, and phone numbers. Keep `.env` and `orders.sqlite3` private. A buyer must open the bot and press Start before it can take an order. Only one running copy of this polling bot should use the token at a time.

## Customer history and conversation cleanup

Opening a product's Order now link or selecting it in the catalog shows its recorded photo or complete photo album in the customer's private chat, followed by current product details and the order button. Album photos retain their original order and grouping. Existing publication/import records are reused, including older channel posts, without republishing anything. Photos remain in the chat after checkout cleanup so the customer can refer back to the product. If media cannot be loaded, the bot explains this and still allows checkout; catalog-only products without recorded media continue to show text details.

Each order requires **10 to 1000 pieces of one product**. This minimum is checked when starting checkout, entering quantity, and confirming, including unfinished sessions created before the update. If tracked stock is below 10, ordering is blocked with an explanation.

After quantity selection, returning customers see their name, phone, city, and address from their latest saved order for the same Telegram account. They can continue with these details or edit just one field at a time. Missing or invalid details are requested before the final review. New customers complete the usual form, and everyone can edit delivery details from the review screen. Arabic guidance explains that reuse does not submit an order: final confirmation is still required. Edited details become available for future orders only after the new order is submitted; cancellation leaves previous orders unchanged. No extra profile database is created, and historical order details are preserved. If all of a customer's orders are permanently deleted, they enter their details again.

After the customer confirms, the order is saved and the admin is notified. A confirmation remains in the customer's chat with the order number and an instruction to send `/orders`. The bot then removes the tracked product-selection and form messages, including the customer's text answers and validation prompts. Saved orders, admin notifications, imported photos, and unrelated chat history are preserved. Cancellation does not delete the conversation.

Ownership is checked using the private Telegram chat ID, never a customer name, phone number, or ID supplied in the command. Existing orders are included. Customers can only see orders placed from their own Telegram account; the configured admin retains access to all orders.

Cleanup tracking persists in an additional table created automatically on your next start. No existing order or product is replaced. Messages from before this update were not tracked. [Telegram deletion limits](https://core.telegram.org/bots/api#deletemessage), including the 48-hour limit, can leave some messages visible. Cleanup errors do not undo saved orders.

## Delete an order (admin only)

In your private admin chat, send `/orders` to check the current numbers, then `/delete 2` to permanently remove order #2. It disappears from both admin and customer order history. Customers cannot delete orders.

After each successful deletion, all remaining orders are renumbered consecutively from 1 in their existing order. For example, deleting #2 from #1, #2, #3 leaves #1 and #2; the next new order is #3. Deleting all orders makes the next new order #1. Customer ownership and all other details stay with their original orders. Invalid or missing order numbers leave everything unchanged.

Previously sent Telegram confirmations and admin alerts retain their old numbers. **Check `/orders` again before each deletion**: repeating `/delete 2` can delete a different order after renumbering. Deleting an order does not remove its product or release its SKU for reuse. Deletion, SKU reservation, renumbering, and the next-number reset happen in one database transaction.

## Telegram admin panel and team access

Start the bot yourself as usual with `python bot.py`, then send `/admin` in your private chat. The panel offers Orders, New orders, Products & stock, Add product, and Sales totals. The main admin also has Team access. It is unavailable in group chats and to ordinary customers; every action checks current access, including buttons and unfinished edits.

`ADMIN_CHAT_ID` in your existing `.env` remains the main admin. To add a teammate, have them open the bot and send `/id`, then send `/team add THEIR_NUMERIC_ID` from your main admin account. `/team remove THEIR_NUMERIC_ID` immediately revokes access, including existing buttons and unfinished edits. Team membership survives restarts without changing `.env`. Only the main admin can manage access or use permanent `/delete`.

Teammates can view all customer orders, update statuses, import/edit products, manage stock/categories, create links, publish products, and queue arrival announcements. New order alerts continue to go to the main admin; teammates open `/admin` or `/orders` to manage them. Ordinary customers retain access only to their own `/orders`, without admin buttons.

**Order status:** new order alerts and admin/team `/orders` results include Confirmed, Shipped, Delivered, Cancelled, and Reopen buttons. The panel also filters and pages through orders by status. Cancellation changes the status and keeps the order in history; it does not delete it. You can correct a status using the latest card. Status changes never adjust stock. Customers see their current status through `/orders`. Confirming through the Confirmed button or `/confirm ORDER_NUMBER` also sends the customer an Arabic and English confirmation with their order number, product, quantity, saved product total, and `/orders` instructions. Repeated confirmation of an already confirmed order and outdated buttons do not resend it; changing away from confirmed and confirming again sends a fresh message. Other statuses do not send customer messages. If delivery fails, the admin sees a warning and the order remains confirmed; there is no automatic retry for this message. Check `/orders` for current numbers before using `/confirm`, especially after permanent deletion renumbers orders.

Buttons use an internal permanent reference, so `/delete` renumbering cannot redirect them to another order. A deleted order's buttons stop working. An outdated button cannot overwrite a newer status change; the bot returns a fresh card instead. Status changes record the acting admin and time in SQLite. Existing orders are upgraded automatically on your next start without changing their original details. Old alerts sent before this update have no buttons; open `/orders` to get new cards.

**Products:** open Products & stock, choose a product, then tap Edit name, Edit price, Edit description, Set stock, or Set categories. Send the new value as your next text message. `/cancel`, `/start`, or navigating elsewhere in the panel stops the pending edit. Names allow 1–100 characters, descriptions up to 950, and prices/stock whole numbers from 0 to 999999. Use `-` to clear a description or `none` to clear categories. A conflicting catalog/stock change while editing requires reopening the editor, protecting edits made by another teammate or in `products.json`.

Add product uses the same photo/album import flow and automatic SKU protection as before. Catalog edits keep the SKU and extra product fields, and preserve other products and saved order prices. They do **not** rewrite original photo captions or existing channel posts; update existing posts separately when needed. Future previews/publications use the latest catalog name, price, and description. If the name or price changes during checkout, the customer must review and confirm the latest summary before the order is saved. Setting stock from zero to a positive quantity through the panel triggers the existing opt-in restock alerts.

**Sales totals:** `/sales` and the panel show order counts, unit counts, and product value for each current status. Delivered order value is shown separately from new, confirmed, shipped, and cancelled orders. Reports use the price saved on each order, not today's catalog price. Today/month filter by the order's placement date in UTC, not its delivery date. Delivery fees, costs, payment collection and profit are not tracked. Permanently deleted orders are excluded from totals.

Keep `admin_panel.py` and `shop_updates.py` beside `bot.py`. Both use only Python's standard library.

## Stock and opt-in alerts

Stock changes **only when the admin sets it**. Confirming, cancelling, or deleting orders does not change the quantity. Set the actual remaining quantity regularly; several customers can each order up to the last quantity you entered. There is no automatic stock reservation.

From your private admin chat:

```text
/stock p0001 0
/stock p0001 5
/stock p0001
/category p0001 women collections
```

The first command marks the product unavailable; the second sets its total available quantity to 5. Quantities must be whole numbers from 0 to 999999. Stock is stored in `orders.sqlite3`, alongside orders, subscriptions, categories, and the delivery queue. `products.json` remains the catalog for names, descriptions, and prices. No stock quantity is guessed or added to your existing products: until you set a quantity, a product keeps its previous ordering behavior. After stock is configured, zero stock blocks ordering and requests above the configured quantity are rejected; stock is checked again at order confirmation.

When an unavailable product is opened from a channel link or the catalog, the bot offers **Notify me when available**. Tapping it registers one restock request for that product and account. Changing stock from **0 to a positive number** queues an alert only for its waiting customers. The alert contains the product name, price, and a native order-link button. After successful delivery, that request is fulfilled; a future outage requires a new request. Customers can cancel an individual request from `/notifications`.

For arrivals, customers open `/notifications` and explicitly subscribe to **All arrivals**, **women's watches** (`women`), **men's watches** (`men`), or **new collections** (`collections`). You can also create category keys with `/category SKU summer_2026`; these become choices in the menu. A product can have up to five categories. Keys use lowercase letters, digits, underscores or hyphens, start with a letter, and have at most 24 characters. `all` is reserved for All arrivals; `none` clears a product's categories.

Import the product as usual, set its stock and categories, then reply to its original photo or album with `/publish`. The first successful publication queues arrivals for customers already subscribed to a matching category or All arrivals. Overlapping subscriptions produce just one arrival alert per product/customer. Imports and previews never announce a product. Later category changes, repeat publishing, repeated `/announce`, and new subscriptions do not resend the original announcement. Existing recorded publications are treated as already announced on upgrade. `/announce SKU` is an alternative for a product that has not yet been announced; it does not post to the channel. Single-photo buttons and the album's caption link remain unchanged.

Subscriptions are off by default. Placing an order or opening `/notifications` does not opt a customer in. Every alert offers an **all notifications off** button and `/notifications` instructions. Opting out also cancels queued alerts. The sender rechecks consent and product availability just before delivery. Alerts for an unavailable product wait until it is available; removed products are skipped. SKUs used for stock, categories, or notification requests are reserved against automatic reuse, so old requests and links cannot move to a different watch.

Alerts are delivered while `python bot.py` is running. The queue survives restarts, sends at most one alert per second, retries temporary errors, respects Telegram's retry delay, and stops notifications to accounts that block the bot. A network timeout after Telegram accepted a message, or a crash before its delivery was recorded, can rarely cause a duplicate on retry. See [Telegram's rate-limit guidance](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this). Keep `shop_updates.py` beside `bot.py`; it uses only the standard library.

## Test this update (Windows PowerShell)

1. Run the offline tests from this folder. They use temporary files and a simulated Telegram API, without reading your real `.env`, changing your real catalog/database, or starting the bot:

   ```powershell
   python -B -m unittest -v test_bot test_admin_panel
   ```

2. Stop your running bot yourself with Ctrl+C, then start it yourself:

   ```powershell
   python bot.py
   ```

3. Using a customer account, tap **Order now** on an existing channel post and complete and confirm a test order. Check that the form messages disappear while the success message remains, and that the admin receives the order.
4. Send `/orders` from that customer account. It should show that customer's order. Use a second non-admin account and verify it cannot see the first customer's order; complete a separate test order and check each account again.
5. Send `/orders` from the admin account to verify both orders are visible. If a customer has more than 10 orders, use `/orders 2` for older ones. These live test orders are saved normally.
6. To test deletion, choose a disposable test order using the admin's `/orders`, then send `/delete NUMBER` with that order's current number. Check that it is gone, remaining numbers are consecutive, and each customer still sees only their own orders. Create another test order to verify its number continues the sequence. A customer trying `/delete NUMBER` must be refused.
7. For restock testing, choose a test product and note its current `/stock SKU`. Set `/stock SKU 0`; from customer account A open its product link and tap **Notify me when available**. Leave account B unsubscribed. Set `/stock SKU 3` as admin and wait for delivery: only A should receive an alert. Setting stock to 4 afterward should not send another restock alert. Restore the actual quantity when finished.
8. For arrivals, subscribe account A to `women` and B to `men` through `/notifications`. Import a new test product, then set `/category SKU women` and `/stock SKU 3`. Preview it first (no alert); publish it, or use `/announce SKU` to test without a channel post. Only A should receive the arrival alert. Repeating `/announce SKU` must not send it again. An All arrivals subscriber should receive one alert as well, even if also subscribed to women.
9. Send `/notifications off` from A and test another matching product: A should receive nothing. Complete an order for a product with configured stock, then check `/stock SKU` as admin: the quantity must stay unchanged. Update it yourself when needed.
10. As the main admin, open `/admin`. Complete a disposable test order from a customer account, then tap Confirmed, Shipped, Delivered, Cancelled, or Reopen on the latest admin order card. Verify the customer's `/orders` shows the new status and `/stock SKU` stays unchanged. Retry an old status button: it should show the current order without changing its status.
11. Open Products & stock, choose a test product, edit its name/price/description, and verify the catalog. Restore its real values afterward. Test `/cancel` during an edit. Check `/sales` and its Today/This month buttons against your saved orders.
12. Test `/admin` from an ordinary customer account: access must be denied. To test team access, grant access to a trusted test account with `/team add ID`, verify operational access, then revoke it with `/team remove ID`. Previously sent admin buttons must stop working for that account. Teammates must not be able to grant access or permanently delete orders.

The bot works while `python bot.py` is running. A website dashboard can be added later if managing the shop in Telegram becomes difficult.
