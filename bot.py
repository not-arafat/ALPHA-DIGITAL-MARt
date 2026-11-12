import os
import html
import asyncio
import logging
from dotenv import load_dotenv
from aiohttp import web
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.error import RetryAfter
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from supabase import create_client, Client

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
PORT = int(os.getenv("PORT", "8080"))
SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "@support")
CURRENCY = os.getenv("CURRENCY", "BDT")
PAYMENT_INFO = os.getenv("PAYMENT_INFO", "bKash: 01XXXXXXXXX (Personal)")

HISTORY_PER_PAGE = 5
HISTORY_MAX = 50

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("AlphaSupply")

if not (BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY):
    raise SystemExit("❌ Missing required env vars: BOT_TOKEN, SUPABASE_URL, SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ---------------- helpers ----------------
async def sb(q):
    return await asyncio.to_thread(q.execute)


def is_admin(uid):
    return uid in ADMIN_IDS


def btn(text, callback_data=None, url=None, style=None, copy_text=None):
    b = {"text": str(text)}
    if callback_data:
        b["callback_data"] = callback_data
    if url:
        b["url"] = url
    if style:
        b["style"] = style
    if copy_text:
        b["copy_text"] = {"text": str(copy_text)}
    return b


def fmt_price(val):
    try:
        val = float(val)
    except (TypeError, ValueError):
        return "0.00"
    return f"{val:.2f}"


def extract_support_username() -> str:
    raw = (SUPPORT_CONTACT or "").strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    return raw.lstrip("@").strip()


def parse_proxy_item(content: str):
    content = (content or "").strip()
    if not content:
        return None
    parts = content.split(":")
    if len(parts) < 4:
        return None
    try:
        port_num = int(parts[1])
    except (ValueError, TypeError):
        return None
    if port_num < 1 or port_num > 65535:
        return None
    server = parts[0].strip()
    port = parts[1].strip()
    password = parts[-1].strip()
    username = ":".join(parts[2:-1]).strip()
    if not server or not username or not password:
        return None
    return server, port, username, password


def format_item_block(idx: int, content: str, total: int = 1, category: str = "") -> str:
    content = (content or "").strip()
    if not content:
        return ""

    cat_lower = (category or "").lower()
    is_mail = any(k in cat_lower for k in ["mail", "email", "gmail", "outlook", "hotmail", "yahoo"])

    # ---------- 1) PROXY ----------
    if not is_mail:
        parsed = parse_proxy_item(content)
        if parsed:
            server, port, username, password = parsed
            label = "🏷 Proxy Details" if total == 1 else f"🏷 Proxy {idx} Details"
            return (
                f"{label}\n\n"
                f"Server : <code>{html.escape(server)}</code>\n\n"
                f"Port : <code>{html.escape(port)}</code>\n\n"
                f"Username : <code>{html.escape(username)}</code>\n\n"
                f"Password : <code>{html.escape(password)}</code>\n\n"
                f"⛔️One-line :\n"
                f"<code>{html.escape(content)}</code>"
            )

    # ---------- 3) EMAIL ----------
    if is_mail:
        parts = content.split(":", 1)
        if len(parts) == 2:
            addr = parts[0].strip()
            pwd = parts[1].strip()
            label = "📧 Email Details" if total == 1 else f"📧 Email {idx} Details"
            return (
                f"{label}\n\n"
                f"Address : <code>{html.escape(addr)}</code>\n\n"
                f"Password : <code>{html.escape(pwd)}</code>\n\n"
                f"⛔️One-line :\n"
                f"<code>{html.escape(content)}</code>"
            )

    # ---------- 2) VPN (with or without key) ----------
    parts = content.split(":")
    if len(parts) >= 3:
        username = parts[0].strip()
        password = parts[1].strip()
        key = ":".join(parts[2:]).strip()
        label = "🔐 VPN Details" if total == 1 else f"🔐 VPN {idx} Details"
        return (
            f"{label}\n\n"
            f"Username / Email : <code>{html.escape(username)}</code>\n\n"
            f"Password : <code>{html.escape(password)}</code>\n\n"
            f"Key : <code>{html.escape(key)}</code>\n\n"
            f"⛔️One-line :\n"
            f"<code>{html.escape(content)}</code>"
        )
    if len(parts) == 2:
        username = parts[0].strip()
        password = parts[1].strip()
        label = "🔐 VPN Details" if total == 1 else f"🔐 VPN {idx} Details"
        return (
            f"{label}\n\n"
            f"Username / Email : <code>{html.escape(username)}</code>\n\n"
            f"Password : <code>{html.escape(password)}</code>\n\n"
            f"⛔️One-line :\n"
            f"<code>{html.escape(content)}</code>"
        )

    # ---------- 4) FALLBACK ----------
    label = "🎁 Item" if total == 1 else f"🎁 Item {idx}"
    return f"{label}\n\n<code>{html.escape(content)}</code>"


async def get_user(uid):
    r = await sb(supabase.table("users").select("*").eq("id", uid).limit(1))
    return r.data[0] if r.data else None


async def ensure_user(tg_user):
    u = await get_user(tg_user.id)
    if u:
        await sb(supabase.table("users").update({
            "username": tg_user.username,
            "first_name": tg_user.first_name,
        }).eq("id", tg_user.id))
        return u
    r = await sb(supabase.table("users").insert({
        "id": tg_user.id,
        "username": tg_user.username,
        "first_name": tg_user.first_name,
        "balance": 0,
        "is_admin": is_admin(tg_user.id),
    }))
    return r.data[0]


async def get_usernames_batch(uids):
    if not uids:
        return {}
    try:
        r = await sb(supabase.table("users").select("id,username,first_name").in_("id", list(uids)))
        data = r.data or []
        return {
            int(u["id"]): (u.get("username") or u.get("first_name") or f"User {u['id']}")
            for u in data
        }
    except Exception as e:
        log.warning(f"get_usernames_batch error: {e}")
        return {}


# -------- Transaction trim helper (max 50 per user) --------
async def trim_user_transactions(uid):
    """Keep only the most recent HISTORY_MAX transactions for a user."""
    try:
        r = await sb(
            supabase.table("transactions").select("id")
            .eq("user_id", uid).order("created_at", desc=True)
        )
        rows = r.data or []
        if len(rows) <= HISTORY_MAX:
            return
        to_delete = [row["id"] for row in rows[HISTORY_MAX:]]
        for i in range(0, len(to_delete), 100):
            chunk = to_delete[i:i + 100]
            try:
                await sb(supabase.table("transactions").delete().in_("id", chunk))
            except Exception as e:
                log.warning(f"trim chunk error: {e}")
    except Exception as e:
        log.warning(f"trim_user_transactions error: {e}")


# -------- Payment Methods helpers --------
async def get_active_payment_methods():
    try:
        r = await sb(supabase.table("payment_methods").select("*").eq("is_active", True).order("id"))
        return r.data or []
    except Exception as e:
        log.error(f"get_active_payment_methods error: {e}")
        return []


async def get_all_payment_methods():
    try:
        r = await sb(supabase.table("payment_methods").select("*").order("id"))
        return r.data or []
    except Exception as e:
        log.error(f"get_all_payment_methods error: {e}")
        return []


async def get_payment_method(pmid):
    try:
        r = await sb(supabase.table("payment_methods").select("*").eq("id", pmid).limit(1))
        return r.data[0] if r.data else None
    except Exception as e:
        log.error(f"get_payment_method error: {e}")
        return None


# -------- Pending orders helpers --------
async def create_pending_order(user_id, product_id, qty, unit_price):
    total = float(unit_price) * qty
    r = await sb(supabase.table("orders").insert({
        "user_id": user_id,
        "product_id": product_id,
        "quantity": qty,
        "total_price": total,
        "status": "pending",
        "delivered_items": "",
    }))
    return r.data[0] if r.data else None


async def deduct_balance_atomic(user_id, amount):
    user = await get_user(user_id)
    if not user:
        return False, 0.0
    balance = float(user["balance"])
    if balance < amount:
        return False, balance
    new_balance = balance - amount
    await sb(supabase.table("users").update({"balance": new_balance}).eq("id", user_id))
    return True, new_balance


async def refund_balance(user_id, amount):
    user = await get_user(user_id)
    if not user:
        return 0.0
    new_balance = float(user["balance"]) + amount
    await sb(supabase.table("users").update({"balance": new_balance}).eq("id", user_id))
    return new_balance


async def complete_pending_order(order_id, items):
    r = await sb(supabase.table("orders").select("*").eq("id", order_id).limit(1))
    if not r.data:
        return False, "Order not found"
    o = r.data[0]
    if o["status"] != "pending":
        return False, f"Order is already {o['status']}"

    user = await get_user(int(o["user_id"]))
    if not user:
        return False, "User not found"

    current_balance = float(user["balance"])

    items_text = "\n".join(items)
    await sb(supabase.table("orders").update({
        "status": "completed",
        "delivered_items": items_text,
    }).eq("id", order_id))

    return True, {"order": o, "items": items, "new_balance": current_balance}


# -------- Menu labels --------
MENU_BUY = "🛒 Buy Items"
MENU_BALANCE = "💰 Add Balance"
MENU_PROFILE = "👤 Profile"
MENU_HISTORY = "📜 History"
MENU_SUPPORT = "🆘 Support"
MENU_ADMIN = "⚙️ Admin Panel"
MENU_BUTTONS = {MENU_BUY, MENU_BALANCE, MENU_PROFILE, MENU_HISTORY, MENU_SUPPORT, MENU_ADMIN}

ADM_PRODUCTS = "📦 Product"
ADM_BROADCAST = "📢 Broadcast"
ADM_TOPUPS = "💳 Pending Topups"
ADM_ORDERS = "⏳ Pending Orders"
ADM_STATS = "📊 Stats"
ADM_PAYMENTS = "🏦 Payment Methods"
ADM_BACK = "⬅️ Back to Main Menu"
ADMIN_BUTTONS = {ADM_PRODUCTS, ADM_BROADCAST, ADM_TOPUPS, ADM_ORDERS, ADM_STATS, ADM_PAYMENTS, ADM_BACK}


def main_menu(admin: bool = False):
    kb = [
        [{"text": MENU_BUY, "style": "success"}, {"text": MENU_BALANCE, "style": "success"}],
        [{"text": MENU_PROFILE, "style": "primary"}, {"text": MENU_HISTORY, "style": "primary"}],
        [{"text": MENU_SUPPORT, "style": "danger"}],
    ]
    if admin:
        kb.append([{"text": MENU_ADMIN, "style": "danger"}])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


def admin_menu():
    kb = [
        [{"text": ADM_PRODUCTS, "style": "success"}, {"text": ADM_BROADCAST, "style": "primary"}],
        [{"text": ADM_TOPUPS, "style": "primary"}, {"text": ADM_ORDERS, "style": "primary"}],
        [{"text": ADM_STATS, "style": "primary"}, {"text": ADM_PAYMENTS, "style": "success"}],
        [{"text": ADM_BACK, "style": "danger"}],
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


# ============================================================
# USER HANDLERS
# ============================================================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update.effective_user)
    ctx.user_data["in_admin"] = False
    await update.message.reply_text(
        "👋 <b>Welcome to AlphaSupply!</b>\n\n"
        "Buy VPN, Proxy, Mail accounts with instant delivery.\n\n"
        "Use the menu below to get started.",
        parse_mode="HTML",
        reply_markup=main_menu(is_admin(update.effective_user.id)),
    )


async def cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    ctx.user_data["in_admin"] = False
    await update.message.reply_text(
        "Cancelled.",
        reply_markup=main_menu(is_admin(update.effective_user.id)),
    )


async def show_categories(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    r = await sb(supabase.table("products").select("category").eq("is_active", True))
    cats = sorted({row["category"] for row in (r.data or [])})
    if not cats:
        await update.message.reply_text("❌ No products available right now.")
        return
    # 2 per line
    kb = []
    row = []
    for c in cats:
        row.append(btn(f"📦 {c.title()}", callback_data=f"cat:{c}", style="primary"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([btn("❌ Cancel", callback_data="cancel_browse", style="danger")])
    await update.message.reply_text(
        "🛒 <b>Select a category you want to buy:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def cancel_browse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        await q.edit_message_text("❌ Browse cancelled.")
    except Exception:
        pass


async def cat_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cat = q.data.split(":", 1)[1]
    r = await sb(supabase.table("products").select("*").eq("category", cat).eq("is_active", True))
    products = r.data or []
    if not products:
        await q.edit_message_text(f"No products in {html.escape(cat)}.")
        return
    kb = []
    for p in products:
        in_stock = p["stock"] > 0
        emoji = "" if in_stock else ""
        kb.append([btn(
            f"{emoji} {p['name']} — {p['price']} {CURRENCY}",
            callback_data=f"prod:{p['id']}",
            style="success" if in_stock else "primary",
        )])
    kb.append([btn("⬅️ Back", callback_data="back_cats", style="danger")])
    await q.edit_message_text(
        f"📦 Please choose a <b>{html.escape(cat.title())}</b> to buy",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def back_cats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    r = await sb(supabase.table("products").select("category").eq("is_active", True))
    cats = sorted({row["category"] for row in (r.data or [])})
    # 2 per line
    kb = []
    row = []
    for c in cats:
        row.append(btn(f"📦 {c.title()}", callback_data=f"cat:{c}", style="primary"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([btn("❌ Cancel", callback_data="cancel_browse", style="danger")])
    await q.edit_message_text(
        "🛒 <b>Select a category:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ============================================================
# QUANTITY SELECTOR
# ============================================================
def build_quantity_view(product, qty):
    max_q = max(1, int(product["stock"]))
    if product["stock"] <= 0:
        qty = max(1, qty)
    else:
        qty = max(1, min(qty, max_q)) if qty <= max_q else qty
    total = float(product["price"]) * qty

    stock_line = f"📦 Stock: {product['stock']}"

    text = (
        f"🛒 <b>{html.escape(product['name'])}</b>\n\n"
        f"💰 Price: {fmt_price(product['price'])} {CURRENCY}\n"
        f"{stock_line}\n"
        f"📝 {html.escape(product.get('description') or '—')}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🔢 Quantity: <b>{qty}</b>\n"
        f"💵 Total: <b>{fmt_price(total)} {CURRENCY}</b>\n"
        f"━━━━━━━━━━━━━━━"
    )

    kb = [
        [
            btn("-", callback_data=f"qty:dec:{product['id']}:{qty}", style="danger"),
            btn(f"🛒 {qty}", callback_data=f"qty:info:{product['id']}", style="success"),
            btn("+", callback_data=f"qty:inc:{product['id']}:{qty}", style="primary"),
        ],
        [btn("✏️ Enter Quantity", callback_data=f"qty:manual:{product['id']}", style="primary")],
        [btn("✅ Confirm & Buy", callback_data=f"qty:buy:{product['id']}:{qty}", style="success")],
        [btn("⬅️ Back", callback_data=f"cat:{product['category']}", style="danger")],
    ]
    return text, InlineKeyboardMarkup(kb)


async def render_quantity_view(q, product, qty):
    text, kb = build_quantity_view(product, qty)
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass


async def prod_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(":", 1)[1])
    r = await sb(supabase.table("products").select("*").eq("id", pid).limit(1))
    if not r.data:
        await q.edit_message_text("Product not found.")
        return
    p = r.data[0]
    await render_quantity_view(q, p, 1)


async def qty_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    action = parts[1]
    pid = int(parts[2])

    if action == "info":
        await q.answer()
        return

    r = await sb(supabase.table("products").select("*").eq("id", pid).limit(1))
    if not r.data:
        await q.answer("Product not found.", show_alert=True)
        return
    p = r.data[0]

    if action == "inc":
        qty = int(parts[3]) + 1
        if qty > 99:
            await q.answer("Max 99 per order.", show_alert=True)
            return
        if p["stock"] > 0 and qty > p["stock"]:
            await q.answer(f"Note: {p['stock']} in stock, rest will be pending.", show_alert=False)
        else:
            await q.answer()
        await render_quantity_view(q, p, qty)

    elif action == "dec":
        qty = max(1, int(parts[3]) - 1)
        await q.answer()
        await render_quantity_view(q, p, qty)

    elif action == "manual":
        await q.answer()
        ctx.user_data["qty_manual_pid"] = pid
        ctx.user_data["state"] = "qty_manual"
        await q.edit_message_text(
            f"🔢 <b>Enter Quantity</b>\n\n"
            f"Product: <b>{html.escape(p['name'])}</b>\n"
            f"Price: {fmt_price(p['price'])} {CURRENCY}\n"
            f"Stock: {p['stock']}\n\n"
            f"Send a number between <b>1</b> and <b>99</b>.\n\n"
            f"💡 If stock is less, extra items become <b>pending</b> for admin.",
            parse_mode="HTML",
        )

    elif action == "buy":
        qty = int(parts[3])
        await do_purchase(q, ctx, p, qty)


async def qty_manual_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pid = ctx.user_data.pop("qty_manual_pid", None)
    ctx.user_data.pop("state", None)
    if not pid:
        await update.message.reply_text("Session expired. Use /start.")
        return
    txt = update.message.text.strip()
    try:
        qty = int(txt)
    except ValueError:
        await update.message.reply_text("❌ Invalid. Send a whole number:")
        ctx.user_data["qty_manual_pid"] = pid
        ctx.user_data["state"] = "qty_manual"
        return

    r = await sb(supabase.table("products").select("*").eq("id", pid).limit(1))
    if not r.data:
        await update.message.reply_text("Product not found.")
        return
    p = r.data[0]

    if qty < 1:
        qty = 1
    if qty > 99:
        await update.message.reply_text("❌ Max 99 per order. Send a smaller number:")
        ctx.user_data["qty_manual_pid"] = pid
        ctx.user_data["state"] = "qty_manual"
        return

    text, kb = build_quantity_view(p, qty)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


# ============================================================
# PURCHASE EXECUTION
# ============================================================
def build_purchase_header(product, qty, total_cost, new_balance):
    header = (
        "✅ <b>Purchase Successful</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"📦Product Name :- 🛍 <b>{html.escape(product['name'])}</b>\n"
    )
    if qty > 1:
        header += f"🔢Quantity :- <b>{qty}</b>\n"
    header += (
        f"💵Price :- {fmt_price(total_cost)} {CURRENCY}\n"
        f"💳Your Balance :- {fmt_price(new_balance)} {CURRENCY}\n"
    )
    return header


async def send_purchase_result(q, ctx, product, items, qty, total_cost, new_balance):
    header = build_purchase_header(product, qty, total_cost, new_balance)
    total_items = len(items)
    category = (product.get("category") or "").lower()
    blocks = [format_item_block(i, c, total=total_items, category=category) for i, c in enumerate(items, 1)]

    full_parts = [header]
    for block in blocks:
        full_parts.append("━━━━━━━━━━━━━━━━━━━━━━\n" + block)
    full = "\n\n".join(full_parts)

    if len(full) <= 4000:
        try:
            await q.edit_message_text(full, parse_mode="HTML")
            return
        except Exception:
            pass

    try:
        await q.edit_message_text(header, parse_mode="HTML")
    except Exception:
        try:
            await ctx.bot.send_message(q.from_user.id, header, parse_mode="HTML")
        except Exception:
            pass

    current = ""
    for block in blocks:
        seg = "━━━━━━━━━━━━━━━━━━━━━━\n" + block + "\n\n"
        if len(current) + len(seg) > 3800:
            if current.strip():
                try:
                    await ctx.bot.send_message(q.from_user.id, current.rstrip(), parse_mode="HTML")
                except Exception:
                    pass
            current = seg
        else:
            current += seg
    if current.strip():
        try:
            await ctx.bot.send_message(q.from_user.id, current.rstrip(), parse_mode="HTML")
        except Exception:
            pass


async def do_purchase(q, ctx, product, qty):
    uid = q.from_user.id
    unit_price = float(product["price"])
    total_needed = unit_price * qty
    avail = int(product.get("stock", 0))

    user = await get_user(uid)
    if not user:
        await q.answer("User not found.", show_alert=True)
        return
    balance = float(user["balance"])
    if balance < total_needed:
        await q.answer(
            f"❌ Insufficient balance. Need {fmt_price(total_needed)} {CURRENCY}. "
            f"Your balance: {fmt_price(balance)} {CURRENCY}.",
            show_alert=True,
        )
        await render_quantity_view(q, product, qty)
        return

    # ---------- FULL DELIVERY ----------
    if avail >= qty:
        try:
            r = await sb(supabase.rpc("purchase_product_batch", {
                "p_user_id": uid,
                "p_product_id": product["id"],
                "p_quantity": qty,
            }))
            result = r.data
        except Exception as e:
            log.exception("purchase failed")
            msg = str(e).lower()
            if "insufficient_balance" in msg:
                await q.answer("❌ Insufficient balance.", show_alert=True)
                await render_quantity_view(q, product, qty)
            elif "out_of_stock" in msg:
                await q.answer("❌ Not enough stock. Try smaller qty.", show_alert=True)
                await render_quantity_view(q, product, qty)
            else:
                await q.answer("❌ Purchase failed. Try again.", show_alert=True)
            return

        await q.answer("✅ Purchase successful!")
        items = result.get("items", []) or []
        if isinstance(items, str):
            import json as _json
            try:
                items = _json.loads(items)
            except Exception:
                items = [items]
        new_balance = result.get("new_balance", 0)
        total_cost = result.get("total_cost", 0)
        # Trim after RPC-inserted transaction
        asyncio.create_task(trim_user_transactions(uid))
        await send_purchase_result(q, ctx, product, items, qty, total_cost, new_balance)
        return

    # ---------- PARTIAL / FULLY PENDING ----------
    deliver_qty = min(avail, qty) if avail > 0 else 0
    pending_qty = qty - deliver_qty

    delivered_items = []
    delivered_cost = 0.0
    delivered_balance = balance

    if deliver_qty > 0:
        try:
            r = await sb(supabase.rpc("purchase_product_batch", {
                "p_user_id": uid,
                "p_product_id": product["id"],
                "p_quantity": deliver_qty,
            }))
            result = r.data
            raw_items = result.get("items", []) or []
            if isinstance(raw_items, str):
                import json as _json
                try:
                    raw_items = _json.loads(raw_items)
                except Exception:
                    raw_items = [raw_items]
            delivered_items = raw_items
            delivered_cost = float(result.get("total_cost", 0))
            delivered_balance = float(result.get("new_balance", balance))
        except Exception as e:
            log.exception("partial delivery failed")
            deliver_qty = 0
            pending_qty = qty
            delivered_items = []
            delivered_cost = 0.0
            delivered_balance = balance

    pending_cost = pending_qty * unit_price
    pending_order = None
    final_balance = delivered_balance

    if pending_qty > 0 and pending_cost > 0:
        ok, final_balance = await deduct_balance_atomic(uid, pending_cost)
        if not ok:
            log.error(f"Pending deduction failed for user {uid}, need {pending_cost}")
            if deliver_qty > 0:
                await refund_balance(uid, delivered_cost)
            await q.answer(
                "❌ Insufficient balance to place order. Please try again.",
                show_alert=True,
            )
            await render_quantity_view(q, product, qty)
            return

        try:
            pending_order = await create_pending_order(uid, product["id"], pending_qty, unit_price)
        except Exception as e:
            log.error(f"Pending order create error: {e}")
            await refund_balance(uid, pending_cost)
            final_balance = final_balance + pending_cost
            await q.answer("❌ Order failed. Please try again.", show_alert=True)
            await render_quantity_view(q, product, qty)
            return

        if pending_order:
            try:
                await sb(supabase.table("transactions").insert({
                    "user_id": uid,
                    "type": "purchase",
                    "amount": -pending_cost,
                    "balance_after": final_balance,
                    "reference": f"pending_{pending_order['id']}",
                    "note": f"Pending order placed ({pending_qty} qty)",
                }))
            except Exception as e:
                log.warning(f"pending txn insert failed: {e}")
            # Trim after manual insert
            await trim_user_transactions(uid)

    await q.answer("⏳ Order processed")

    category = (product.get("category") or "").lower()

    if deliver_qty > 0 and pending_qty > 0:
        header = (
            f"⚠️ <b>Partial Delivery</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 Product: <b>{html.escape(product['name'])}</b>\n"
            f"🔢 Requested: <b>{qty}</b>\n"
            f"✅ Delivered: <b>{deliver_qty}</b>\n"
            f"⏳ Pending: <b>{pending_qty}</b>\n\n"
            f"💵 Delivered Cost: {fmt_price(delivered_cost)} {CURRENCY}\n"
            f"💵 Pending Cost: {fmt_price(pending_cost)} {CURRENCY} (already charged)\n"
            f"💳 Your Balance: {fmt_price(final_balance)} {CURRENCY}\n"
        )
        total_items = len(delivered_items)
        blocks = [format_item_block(i, c, total=total_items, category=category) for i, c in enumerate(delivered_items, 1)]
        body = "\n\n".join(["━━━━━━━━━━━━━━━━━━━━━━\n" + b for b in blocks])
        tail = (
            f"\n\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ <b>{pending_qty} item(s) pending</b>\n"
            f"Admin has been notified. You'll receive them shortly."
        )
        full = header + "\n" + body + tail
        if len(full) <= 4000:
            try:
                await q.edit_message_text(full, parse_mode="HTML")
            except Exception:
                try:
                    await ctx.bot.send_message(uid, full, parse_mode="HTML")
                except Exception:
                    pass
        else:
            try:
                await q.edit_message_text(header, parse_mode="HTML")
            except Exception:
                try:
                    await ctx.bot.send_message(uid, header, parse_mode="HTML")
                except Exception:
                    pass
            current = ""
            for block in blocks:
                seg = "━━━━━━━━━━━━━━━━━━━━━━\n" + block + "\n\n"
                if len(current) + len(seg) > 3800:
                    if current.strip():
                        try:
                            await ctx.bot.send_message(uid, current.rstrip(), parse_mode="HTML")
                        except Exception:
                            pass
                    current = seg
                else:
                    current += seg
            if current.strip():
                try:
                    await ctx.bot.send_message(uid, current.rstrip(), parse_mode="HTML")
                except Exception:
                    pass
            try:
                await ctx.bot.send_message(uid, tail, parse_mode="HTML")
            except Exception:
                pass
    else:
        try:
            await q.edit_message_text(
                f"⏳ <b>Order Pending</b>\n\n"
                f"📦 Product: <b>{html.escape(product['name'])}</b>\n"
                f"🔢 Quantity: <b>{pending_qty}</b>\n"
                f"💵 Amount: <b>{fmt_price(pending_cost)} {CURRENCY}</b>\n\n"
                f"❌ Out of stock right now.\n"
                f"Your order has been forwarded to admin.",
                parse_mode="HTML",
            )
        except Exception:
            pass

    if pending_order:
        kb_view = InlineKeyboardMarkup([[
            btn(
                f"👁 View & Manage Order #{pending_order['id']}",
                callback_data=f"ord:view:{pending_order['id']}",
                style="success",
            ),
        ]])
        for aid in ADMIN_IDS:
            try:
                await ctx.bot.send_message(
                    aid,
                    f"⏳ <b>New Pending Order</b>\n\n"
                    f"🆔 Order: <code>#{pending_order['id']}</code>\n"
                    f"👤 User: <code>{uid}</code>\n"
                    f"📦 Product: <b>{html.escape(product['name'])}</b>\n"
                    f"🔢 Quantity: <b>{pending_qty}</b>\n"
                    f"💵 Amount: <b>{fmt_price(pending_cost)} {CURRENCY}</b> (already charged)",
                    parse_mode="HTML",
                    reply_markup=kb_view,
                )
            except Exception:
                pass


async def noop_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("Not available")


# ============================================================
# PROFILE / HISTORY / SUPPORT
# ============================================================
async def show_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = await get_user(uid)
    if not u:
        await update.message.reply_text("Send /start first.")
        return
    r = await sb(supabase.table("orders").select("id", count="exact").eq("user_id", uid))
    count = r.count or 0
    joined = (u.get("created_at") or "")[:10]
    uname = u.get("username")
    uname_text = f"@{html.escape(uname)}" if uname else "—"
    fname = html.escape(u.get("first_name") or "-")
    await update.message.reply_text(
        f"👤 <b>Profile</b>\n\n"
        f"🆔 <code>{uid}</code>\n"
        f"👤 {fname}\n"
        f"🔗 {uname_text}\n"
        f"💰 Balance: <b>{u['balance']} {CURRENCY}</b>\n"
        f"🧾 Orders: {count}\n"
        f"📅 Joined: {joined}",
        parse_mode="HTML",
    )


async def show_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    username = extract_support_username()
    if not username:
        await update.message.reply_text("🆘 Support contact is not configured yet.")
        return
    kb = InlineKeyboardMarkup([[
        btn("💬 Contact Support", url=f"https://t.me/{username}", style="success"),
    ]])
    await update.message.reply_text(
        "🆘 <b>Support</b>\n\nClick the button below to contact our support team:",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ============================================================
# HISTORY (paginated, 5 per page, max 50 stored)
# ============================================================
async def build_history_view(uid, page: int = 1):
    await trim_user_transactions(uid)

    r = await sb(
        supabase.table("transactions").select("id")
        .eq("user_id", uid).order("created_at", desc=True).limit(HISTORY_MAX)
    )
    all_ids = [row["id"] for row in (r.data or [])]
    total = len(all_ids)

    if total == 0:
        return "📜 <b>Transaction History</b>\n\n<i>No transactions yet.</i>", None

    total_pages = max(1, (total + HISTORY_PER_PAGE - 1) // HISTORY_PER_PAGE)
    page = max(1, min(page, total_pages))

    start = (page - 1) * HISTORY_PER_PAGE
    page_ids = all_ids[start:start + HISTORY_PER_PAGE]

    r2 = await sb(supabase.table("transactions").select("*").in_("id", page_ids))
    rows = r2.data or []
    id_to_row = {row["id"]: row for row in rows}
    ordered = [id_to_row[i] for i in page_ids if i in id_to_row]

    blocks = []
    for t in ordered:
        sign = "+" if t["amount"] >= 0 else ""
        emoji = {"topup": "💰", "purchase": "🛒", "refund": "↩️", "admin_adjust": "⚙️"}.get(t["type"], "•")
        dt = (t.get("created_at") or "")[:16].replace("T", " ")
        blocks.append(
            f"{emoji} <code>{sign}{t['amount']}</code> {CURRENCY} • {html.escape(t['type'])}\n"
            f"<i>{dt}</i> • Bal: {t['balance_after']}"
        )

    body = "\n\n".join(blocks)

    header = (
        f"📜 <b>Transaction History</b>\n\n"
        f"📊 Total: <b>{total}</b>  •  Page {page}/{total_pages}\n\n"
    )
    text = header + body

    nav = []
    if page > 1:
        nav.append(btn("⬅️ Prev", callback_data=f"hist:page:{page-1}", style="primary"))
    if total_pages > 1:
        nav.append(btn(f"{page}/{total_pages}", callback_data="noop", style="primary"))
    if page < total_pages:
        nav.append(btn("Next ➡️", callback_data=f"hist:page:{page+1}", style="primary"))
    kb = InlineKeyboardMarkup([nav]) if nav else None
    return text, kb


async def show_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text, kb = await build_history_view(uid, 1)
    if kb:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode="HTML")


async def hist_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        page = int(q.data.split(":")[2])
    except Exception:
        page = 1
    uid = q.from_user.id
    text, kb = await build_history_view(uid, page)
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass


# ============================================================
# USER TOPUP FLOW (NEW UI + AUTO VERIFY)
# ============================================================
async def start_topup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    for k in ("topup_pmid", "topup_pm_name", "topup_min", "topup_amount"):
        ctx.user_data.pop(k, None)
    ctx.user_data.pop("state", None)

    methods = await get_active_payment_methods()
    if not methods:
        await update.message.reply_text(
            "💰 <b>Add Balance</b>\n\n"
            "❌ No payment methods are available right now.\n\n"
            "Please contact support.",
            parse_mode="HTML",
        )
        return

    ctx.user_data["state"] = "topup_amount_first"
    await update.message.reply_text(
        "💰 <b>Add Balance</b>\n\n"
        "💵 How much do you want to add?\n\n"
        "Send the <b>amount</b> in numbers (e.g., 100):",
        parse_mode="HTML",
    )


async def topup_amount_first_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(update.message.text.strip())
        if amt <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("❌ Invalid amount. Send a positive number:")
        return

    methods = await get_active_payment_methods()
    eligible = [m for m in methods if float(m.get("min_amount") or 0) <= amt]

    if not eligible:
        if methods:
            min_possible = min(float(m.get("min_amount") or 0) for m in methods)
            await update.message.reply_text(
                f"❌ No payment method available for <b>{fmt_price(amt)} {CURRENCY}</b>.\n\n"
                f"💵 Minimum deposit is <b>{fmt_price(min_possible)} {CURRENCY}</b>.\n\n"
                f"Please send a higher amount:",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text("❌ No payment methods available right now.")
            ctx.user_data.pop("state", None)
        return

    ctx.user_data["topup_amount"] = amt
    ctx.user_data.pop("state", None)

    # 2 per line + cancel button
    kb = []
    row = []
    for m in eligible:
        row.append(btn(
            f"{m['name']}",
            callback_data=f"tp:pick:{m['id']}",
            style="primary",
        ))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([btn("❌ Cancel", callback_data="tup:cancel", style="danger")])

    await update.message.reply_text(
        f"💰 <b>Add Balance</b>\n\n"
        f"💵 Amount: <b>{fmt_price(amt)} {CURRENCY}</b>\n\n"
        f"🏦 Select a payment method to continue:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def topup_pick_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        pmid = int(q.data.split(":")[2])
    except Exception:
        await q.answer("Invalid.", show_alert=True)
        return

    amt = ctx.user_data.get("topup_amount")
    if not amt:
        await q.answer("Session expired. Start again.", show_alert=True)
        try:
            await q.edit_message_text("⚠️ Session expired. Please tap 💰 Add Balance again.")
        except Exception:
            pass
        return

    m = await get_payment_method(pmid)
    if not m or not m.get("is_active"):
        await q.answer("This payment method is no longer available.", show_alert=True)
        return

    min_amt = float(m.get("min_amount") or 0)
    if amt < min_amt:
        await q.answer(
            f"Minimum for {m['name']} is {fmt_price(min_amt)} {CURRENCY}.",
            show_alert=True,
        )
        return

    await q.answer()
    ctx.user_data["topup_pmid"] = pmid
    ctx.user_data["topup_pm_name"] = m["name"]
    ctx.user_data["topup_min"] = min_amt
    ctx.user_data.pop("state", None) # Not asking for txn yet

    number_str = str(m["number"]).strip()

    # New Copy, Cancel, I've Paid buttons
    kb = InlineKeyboardMarkup([
        [btn(f"{number_str}", copy_text=number_str, style="primary")],
        [
            btn("❌ Cancel", callback_data="tup:cancel", style="danger"),
            btn("✅ I've Paid", callback_data="tup:paid", style="success")
        ]
    ])

    await q.edit_message_text(
        f"💳 Pay <b>exactly {fmt_price(amt)} {CURRENCY}</b> via <b>{html.escape(m['name'])}</b> to:\n"
        f"<code>{html.escape(number_str)}</code>\n\n"
        f"👇 Tap the button below to <b>copy the number</b>.\n\n"
        f"✅ After payment, tap <b>I've Paid</b>.",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def topup_cancel_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data.clear()
    try:
        await q.edit_message_text("❌ Topup cancelled.")
    except Exception:
        pass


async def topup_paid_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    ctx.user_data["state"] = "topup_txn"
    await q.edit_message_text(
        "✅ <b>Please send your Transaction ID (TrxID)</b>\n\n"
        "You can find this in your bKash/Nagad confirmation SMS.",
        parse_mode="HTML"
    )


# ---------------- SMS VERIFICATION LOGIC ----------------
async def try_verify_payment(trx_id, amount, sms_sender):
    """Search payment_sms table for matching trx_id."""
    try:
        r = await sb(
            supabase.table("payment_sms").select("*")
            .eq("trx_id", trx_id).eq("is_used", False).limit(1)
        )
        if not r.data:
            return False
        
        sms = r.data[0]
        # Verify amount (with small tolerance)
        if abs(float(sms["amount"]) - float(amount)) > 0.01:
            return False
            
        # Verify provider (bKash/Nagad) if configured in payment method
        if sms_sender:
            if sms.get("provider", "").lower() != sms_sender.lower():
                return False
                
        return sms["id"]
    except Exception as e:
        log.error(f"verify_payment error: {e}")
        return False


async def topup_txn_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txn = update.message.text.strip()
    amt = ctx.user_data.get("topup_amount")
    pm_id = ctx.user_data.get("topup_pmid")
    pm_name = ctx.user_data.get("topup_pm_name", "")
    uid = update.effective_user.id

    if not amt or not pm_id:
        await update.message.reply_text("Session expired. Please tap 💰 Add Balance again.", reply_markup=main_menu(is_admin(uid)))
        ctx.user_data.clear()
        return

    m = await get_payment_method(pm_id)
    sms_sender = m.get("sms_sender") if m else ""

    # Prevent overlapping messages
    if ctx.user_data.get("is_verifying"):
        await update.message.reply_text("⏳ Please wait, still verifying...")
        return
    ctx.user_data["is_verifying"] = True

    msg = await update.message.reply_text(
        "⏳ <b>Verifying your payment, please wait...</b>",
        parse_mode="HTML"
    )

    # --- Try 1: 15 seconds ---
    await asyncio.sleep(15)
    sms_id = await try_verify_payment(txn, amt, sms_sender)
    
    if sms_id:
        return await _complete_auto_topup(update, ctx, msg, sms_id, txn, amt, pm_id, pm_name, uid)

    # --- Failed ---
    ctx.user_data["is_verifying"] = False
    
    # New Button Layout: Request Manual Review on top, Cancel and Try Again side-by-side
    kb = InlineKeyboardMarkup([
        [btn("📞 Request for Manual Review", callback_data=f"tup:mreview:{txn}", style="primary")],
        [
            btn("❌ Cancel", callback_data="tup:cancel", style="danger"),
            btn("🔄 Try Again", callback_data="tup:try_again", style="success")
        ]
    ])
    
    await msg.edit_text(
        "❌ <b>Payment not found.</b>\n\n"
        "Please check your SMS and ensure you entered the correct <b>TrxID</b>.\n"
        "If you are sure you paid, you can request a manual review.",
        parse_mode="HTML",
        reply_markup=kb
    )


async def _complete_auto_topup(update, ctx, msg, sms_id, txn, amt, pm_id, pm_name, uid):
    ctx.user_data["is_verifying"] = False
    ctx.user_data.pop("state", None)
    ctx.user_data.pop("topup_amount", None)
    ctx.user_data.pop("topup_pmid", None)
    ctx.user_data.pop("topup_pm_name", None)

    # 1. Mark SMS as used
    try:
        await sb(supabase.table("payment_sms").update({"is_used": True}).eq("id", sms_id))
    except Exception as e:
        log.error(f"Failed to mark sms used: {e}")

    # 2. Insert Topup Request as approved
    r = await sb(supabase.table("topup_requests").insert({
        "user_id": uid, "amount": amt, "txn_id": txn, "status": "approved",
        "payment_method_id": pm_id, "payment_method_name": pm_name
    }))
    tid = r.data[0]["id"] if r.data else None

    # 3. Add Balance
    user = await get_user(uid)
    new_balance = float(user["balance"]) + float(amt)
    await sb(supabase.table("users").update({"balance": new_balance}).eq("id", uid))

    # 4. Add Transaction history
    await sb(supabase.table("transactions").insert({
        "user_id": uid, "type": "topup", "amount": amt,
        "balance_after": new_balance, "reference": str(tid), "note": "Auto-verified topup"
    }))
    asyncio.create_task(trim_user_transactions(uid))

    # 5. Success Message
    await msg.edit_text(
        f"✅ <b>Topup Successful!</b>\n\n"
        f"Amount: <b>{fmt_price(amt)} {CURRENCY}</b>\n"
        f"TrxID: <code>{html.escape(txn)}</code>\n"
        f"💳 New Balance: <b>{fmt_price(new_balance)} {CURRENCY}</b>\n\n"
        f"Thank you for your payment!",
        parse_mode="HTML"
    )
    await update.effective_chat.send_message(
        "Use the menu below to continue.",
        reply_markup=main_menu(is_admin(uid))
    )
    
    # Notify Admin (Optional)
    for aid in ADMIN_IDS:
        try:
            await ctx.bot.send_message(
                aid,
                f"✅ <b>Auto-Verified Topup</b>\n"
                f"User: <code>{uid}</code>\n"
                f"Amount: {fmt_price(amt)} {CURRENCY}\n"
                f"TrxID: <code>{html.escape(txn)}</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass


async def tup_try_again_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["state"] = "topup_txn"
    try:
        await q.edit_message_text(
            "🔄 <b>Please send your Transaction ID (TrxID) again:</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass


async def tup_manual_review_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    txn = q.data.split(":")[2]
    uid = q.from_user.id
    
    amt = ctx.user_data.get("topup_amount")
    pm_id = ctx.user_data.get("topup_pmid")
    pm_name = ctx.user_data.get("topup_pm_name", "")
    
    ctx.user_data.clear()

    if not amt or not pm_id:
        await q.edit_message_text("⚠️ Session expired. Please start again.")
        return

    # Save to DB as pending
    try:
        r = await sb(supabase.table("topup_requests").insert({
            "user_id": uid, "amount": amt, "txn_id": txn, "status": "pending",
            "payment_method_id": pm_id, "payment_method_name": pm_name
        }))
        tid = r.data[0]["id"]
    except Exception as e:
        log.error(f"Manual review insert error: {e}")
        await q.edit_message_text("❌ Something went wrong. Try again later.")
        return

    await q.edit_message_text(
        f"✅ <b>Manual Review Requested</b>\n\n"
        f"Amount: {fmt_price(amt)} {CURRENCY}\n"
        f"TrxID: <code>{html.escape(txn)}</code>\n\n"
        f"Admin will verify and credit your account shortly.",
        parse_mode="HTML"
    )
    await q.message.reply_text("Main Menu:", reply_markup=main_menu(is_admin(uid)))

    # Notify Admin
    kb_view = InlineKeyboardMarkup([[btn(f"👁 View & Manage Topup #{tid}", callback_data=f"tup:view:{tid}", style="success")]])
    for aid in ADMIN_IDS:
        try:
            await ctx.bot.send_message(
                aid,
                f"⚠️ <b>Manual Review Requested</b>\n"
                f"User: <code>{uid}</code>\n"
                f"Amount: {fmt_price(amt)} {CURRENCY}\n"
                f"TrxID: <code>{html.escape(txn)}</code>\n\n"
                f"Payment could not be auto-verified.",
                parse_mode="HTML",
                reply_markup=kb_view
            )
        except Exception:
            pass


# ============================================================
# ADMIN — PRODUCT MANAGEMENT (CATEGORY-WISE UI)
# ============================================================
async def build_products_view():
    """Show categories view (Admin)."""
    r = await sb(supabase.table("products").select("category").order("category"))
    rows = r.data or []
    if not rows:
        kb = [
            [btn("➕ Add New Product", callback_data="adm:newprod", style="success")],
            [btn("⬅️ Back to Admin Panel", callback_data="adm:back", style="danger")],
        ]
        return "📦 <b>Product Categories</b>\n\nNo products yet.\n\nAdd your first product:", InlineKeyboardMarkup(kb)

    counts = {}
    for row in rows:
        cat = row.get("category") or "uncategorized"
        counts[cat] = counts.get(cat, 0) + 1

    kb = []
    for cat in sorted(counts.keys()):
        kb.append([btn(
            f"📁 {cat.title()} ({counts[cat]})",
            callback_data=f"adm:prodcat:{cat}",
            style="primary",
        )])
    kb.append([btn("➕ Add New Product", callback_data="adm:newprod", style="success")])
    kb.append([btn("⬅️ Back to Admin Panel", callback_data="adm:back", style="danger")])
    return (
        f"📦 <b>Product Categories</b> ({len(counts)})\n\n"
        f"Select a category to view its products:",
        InlineKeyboardMarkup(kb),
    )


async def build_category_products_view(category: str):
    """Show products within a specific category (Admin)."""
    if not category:
        return await build_products_view()

    r = await sb(supabase.table("products").select("*").eq("category", category).order("id"))
    products = r.data or []

    if not products:
        kb = [
            [btn("➕ Add New Product", callback_data="adm:newprod", style="success")],
            [btn("⬅️ Back to Categories", callback_data="adm:prods", style="danger")],
        ]
        return (
            f"📁 <b>{html.escape(category.title())}</b>\n\n"
            f"No products in this category.",
            InlineKeyboardMarkup(kb),
        )

    kb = []
    for p in products:
        emoji = "🟢" if p.get("is_active") else "🔴"
        stock = p.get("stock", 0)
        kb.append([btn(
            f"{emoji} {p['name']} • {p['price']} {CURRENCY} • stock: {stock}",
            callback_data=f"adm:prod:{p['id']}",
            style="primary" if p.get("is_active") else "danger",
        )])
    kb.append([btn("➕ Add New Product", callback_data="adm:newprod", style="success")])
    kb.append([btn("⬅️ Back to Categories", callback_data="adm:prods", style="danger")])
    return (
        f"📁 <b>{html.escape(category.title())}</b> ({len(products)})\n\n"
        f"Click a product to manage:",
        InlineKeyboardMarkup(kb),
    )


async def show_admin_product_detail(target, pid: int, is_callback: bool = True):
    r = await sb(supabase.table("products").select("*").eq("id", pid).limit(1))
    if not r.data:
        if is_callback:
            await target.edit_message_text("Product not found.")
        else:
            await target.reply_text("Product not found.")
        return
    p = r.data[0]

    unsold_r = await sb(supabase.table("product_items").select("id", count="exact")
                        .eq("product_id", pid).eq("is_sold", False))
    total_r = await sb(supabase.table("product_items").select("id", count="exact")
                       .eq("product_id", pid))
    unsold = unsold_r.count or 0
    total = total_r.count or 0
    sold = total - unsold

    pending_r = await sb(supabase.table("orders").select("id", count="exact")
                         .eq("product_id", pid).eq("status", "pending"))
    pending_cnt = pending_r.count or 0

    text = (
        f"📦 <b>{html.escape(p['name'])}</b>\n\n"
        f"🏷️ Category: {html.escape(p['category'])}\n"
        f"💰 Price: {fmt_price(p['price'])} {CURRENCY}\n"
        f"📝 {html.escape(p.get('description') or '—')}\n\n"
        f"📊 <b>Stock Info:</b>\n"
        f"🟢 Available: <b>{unsold}</b>\n"
        f"🔴 Sold: {sold}\n"
        f"📦 Total: {total}\n"
        f"⏳ Pending Orders: {pending_cnt}\n"
        f"Status: {'✅ Active' if p.get('is_active') else '❌ Inactive'}"
    )

    # Back to category
    cat = p.get("category") or ""
    back_cb = f"adm:prodcat:{cat}" if cat else "adm:prods"

    if p.get("is_active"):
        kb = [
            [btn("➕ Add Stock", callback_data=f"adm:stock:{pid}", style="success"),
             btn("🗑️ Clear Unsold", callback_data=f"adm:clrstock:{pid}", style="danger")],
            [btn("✏️ Edit Price", callback_data=f"adm:eprice:{pid}", style="primary"),
             btn("🗑️ Delete Product", callback_data=f"adm:delconf:{pid}", style="danger")],
            [btn(f"⬅️ Back to {cat.title() if cat else 'Categories'}", callback_data=back_cb, style="primary")],
        ]
    else:
        kb = [
            [btn("✅ Reactivate Product", callback_data=f"adm:reactivate:{pid}", style="success")],
            [btn("🗑️ Delete Permanently", callback_data=f"adm:delconf:{pid}", style="danger")],
            [btn(f"⬅️ Back to {cat.title() if cat else 'Categories'}", callback_data=back_cb, style="primary")],
        ]
    markup = InlineKeyboardMarkup(kb)
    if is_callback:
        await target.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await target.reply_text(text, parse_mode="HTML", reply_markup=markup)


# ============================================================
# ADMIN — PENDING ORDERS
# ============================================================
PENDING_ORDERS_PER_PAGE = 10


async def build_pending_orders_list(page: int = 1):
    r = await sb(supabase.table("orders").select("*").eq("status", "pending").order("id"))
    orders = r.data or []

    if not orders:
        kb = [[btn("⬅️ Back to Admin Panel", callback_data="adm:back", style="danger")]]
        return (
            "⏳ <b>Pending Orders</b>\n\n✅ No pending orders right now.",
            InlineKeyboardMarkup(kb),
        )

    total = len(orders)
    total_pages = max(1, (total + PENDING_ORDERS_PER_PAGE - 1) // PENDING_ORDERS_PER_PAGE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * PENDING_ORDERS_PER_PAGE
    page_orders = orders[start:start + PENDING_ORDERS_PER_PAGE]

    uids = list({int(o["user_id"]) for o in page_orders})
    pids = list({int(o["product_id"]) for o in page_orders})
    uname_map = await get_usernames_batch(uids)

    product_map = {}
    if pids:
        try:
            pr = await sb(supabase.table("products").select("id,name").in_("id", pids))
            for p in (pr.data or []):
                product_map[int(p["id"])] = p["name"]
        except Exception as e:
            log.warning(f"product names fetch error: {e}")

    kb = []
    for o in page_orders:
        uname = (uname_map.get(int(o["user_id"])) or f"User {o['user_id']}").lstrip("@")
        if len(uname) > 12:
            uname = uname[:10] + "…"
        pname = product_map.get(int(o["product_id"]), "Product")
        if len(pname) > 12:
            pname = pname[:10] + "…"
        kb.append([btn(
            f"#{o['id']} @{uname} • {pname} x{o['quantity']} • {fmt_price(o['total_price'])} {CURRENCY}",
            callback_data=f"ord:view:{o['id']}",
            style="primary",
        )])

    nav = []
    if page > 1:
        nav.append(btn("⬅️ Prev", callback_data=f"ord:list:{page-1}", style="primary"))
    if total_pages > 1:
        nav.append(btn(f"{page}/{total_pages}", callback_data="noop", style="primary"))
    if page < total_pages:
        nav.append(btn("Next ➡️", callback_data=f"ord:list:{page+1}", style="primary"))
    if nav:
        kb.append(nav)
    kb.append([btn("⬅️ Back to Admin Panel", callback_data="adm:back", style="danger")])

    text = (
        f"⏳ <b>Pending Orders</b>\n\n"
        f"📊 Total: <b>{total}</b>  •  Page {page}/{total_pages}\n\n"
        f"Click an order to manage:"
    )
    return text, InlineKeyboardMarkup(kb)


async def build_pending_order_detail(order_id: int):
    r = await sb(supabase.table("orders").select("*").eq("id", order_id).limit(1))
    if not r.data:
        return "Order not found.", InlineKeyboardMarkup([[btn("⬅️ Back", callback_data="ord:list:1", style="danger")]])
    o = r.data[0]

    if o["status"] != "pending":
        return (
            f"⚠️ Order #{order_id} is already <b>{html.escape(o['status'])}</b>.",
            InlineKeyboardMarkup([[btn("⬅️ Back", callback_data="ord:list:1", style="primary")]])
        )

    uinfo = await get_usernames_batch([int(o["user_id"])])
    uname = (uinfo.get(int(o["user_id"])) or f"User {o['user_id']}").lstrip("@")
    user = await get_user(int(o["user_id"]))
    balance = fmt_price(user["balance"]) if user else "?"

    pr = await sb(supabase.table("products").select("name").eq("id", o["product_id"]).limit(1))
    pname = pr.data[0]["name"] if pr.data else "Product"

    text = (
        f"⏳ <b>Pending Order #{o['id']}</b>\n\n"
        f"👤 User: <a href=\"tg://user?id={o['user_id']}\">@{html.escape(uname)}</a>\n"
        f"🆔 ID: <code>{o['user_id']}</code>\n"
        f"💰 Balance: <code>{balance} {CURRENCY}</code>\n"
        f"📦 Product: <b>{html.escape(pname)}</b>\n"
        f"🔢 Quantity: <b>{o['quantity']}</b>\n"
        f"💵 Amount: <b>{fmt_price(o['total_price'])} {CURRENCY}</b> (already charged)\n"
        f"📅 Created: {(o.get('created_at') or '')[:16].replace('T', ' ')}\n\n"
        f"Provide items to complete this order:"
    )
    kb = [
        [btn("✅ Fulfill Order", callback_data=f"ord:fulfill:{order_id}", style="success")],
        [btn("❌ Cancel & Refund", callback_data=f"ord:cancel:{order_id}", style="danger")],
        [btn("⬅️ Back to List", callback_data="ord:list:1", style="primary")],
    ]
    return text, InlineKeyboardMarkup(kb)


async def ord_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Admin only.", show_alert=True)
        return
    data = q.data

    if data.startswith("ord:list:"):
        try:
            page = int(data.split(":")[2])
        except Exception:
            page = 1
        await q.answer()
        text, kb = await build_pending_orders_list(page)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass

    elif data.startswith("ord:view:"):
        oid = int(data.split(":")[2])
        await q.answer()
        text, kb = await build_pending_order_detail(oid)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass

    elif data.startswith("ord:fulfill:"):
        oid = int(data.split(":")[2])
        r = await sb(supabase.table("orders").select("*").eq("id", oid).limit(1))
        if not r.data:
            await q.answer("Order not found.", show_alert=True)
            return
        o = r.data[0]
        if o["status"] != "pending":
            await q.answer("Already processed.", show_alert=True)
            return
        ctx.user_data["fulfill_order_id"] = oid
        ctx.user_data["fulfill_qty"] = int(o["quantity"])
        ctx.user_data["state"] = "adm_fulfill_order"
        await q.answer()
        try:
            await q.edit_message_text(
                f"✅ <b>Fulfill Order #{oid}</b>\n\n"
                f"Send <b>{o['quantity']}</b> item(s), one per line.\n\n"
                f"Each line = one deliverable item.\n"
                f"Count must match exactly.\n\n"
                f"📝 <b>Format examples:</b>\n"
                f"• Proxy: <code>server:port:user:pass</code>\n"
                f"• VPN: <code>user:pass:key</code> or <code>user:pass</code>\n"
                f"• Email: <code>address:password</code>\n\n"
                f"Type /cancel to abort.",
                parse_mode="HTML",
            )
        except Exception:
            pass

    elif data.startswith("ord:cancel:"):
        oid = int(data.split(":")[2])
        await q.answer()
        r = await sb(supabase.table("orders").select("*").eq("id", oid).limit(1))
        if not r.data:
            await q.answer("Not found.", show_alert=True)
            return
        o = r.data[0]
        kb = InlineKeyboardMarkup([[
            btn("✅ Confirm Cancel & Refund", callback_data=f"ord:rejcancel:{oid}", style="danger"),
            btn("❌ Keep", callback_data=f"ord:view:{oid}", style="primary"),
        ]])
        try:
            await q.edit_message_text(
                f"⚠️ <b>Cancel Pending Order #{oid}?</b>\n\n"
                f"💰 Refund amount: <b>{fmt_price(o['total_price'])} {CURRENCY}</b>\n\n"
                f"The refund will be added back to the user's balance.\n"
                f"This cannot be undone.",
                parse_mode="HTML",
                reply_markup=kb,
            )
        except Exception:
            pass

    elif data.startswith("ord:rejcancel:"):
        oid = int(data.split(":")[2])
        await q.answer("Cancelling...")
        r = await sb(supabase.table("orders").select("*").eq("id", oid).limit(1))
        if not r.data:
            await q.answer("Not found.", show_alert=True)
            return
        o = r.data[0]
        if o["status"] != "pending":
            await q.answer("Already processed.", show_alert=True)
            return

        try:
            await sb(supabase.table("orders").update({"status": "cancelled"}).eq("id", oid))
        except Exception as e:
            await q.edit_message_text(f"❌ Error: {html.escape(str(e))}")
            return

        refund_amount = float(o["total_price"])
        new_balance = await refund_balance(int(o["user_id"]), refund_amount)

        try:
            await sb(supabase.table("transactions").insert({
                "user_id": o["user_id"],
                "type": "refund",
                "amount": refund_amount,
                "balance_after": new_balance,
                "reference": f"cancel_order_{oid}",
                "note": f"Pending order #{oid} cancelled - refund",
            }))
        except Exception as e:
            log.warning(f"refund txn insert failed: {e}")

        await trim_user_transactions(int(o["user_id"]))

        try:
            pr = await sb(supabase.table("products").select("name").eq("id", o["product_id"]).limit(1))
            pname = pr.data[0]["name"] if pr.data else "Product"
            await ctx.bot.send_message(
                o["user_id"],
                f"❌ <b>Pending Order Cancelled</b>\n\n"
                f"📦 Product: <b>{html.escape(pname)}</b>\n"
                f"🔢 Quantity: {o['quantity']}\n"
                f"💰 Refund: <b>{fmt_price(refund_amount)} {CURRENCY}</b>\n"
                f"💳 New Balance: <b>{fmt_price(new_balance)} {CURRENCY}</b>\n\n"
                f"Amount has been refunded to your wallet.",
                parse_mode="HTML",
            )
        except Exception:
            pass

        await q.answer(f"Refunded {fmt_price(refund_amount)} {CURRENCY}.", show_alert=True)
        text, kb = await build_pending_orders_list(1)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass

    else:
        await q.answer()


async def adm_btn_orders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text, kbd = await build_pending_orders_list(1)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kbd)


async def adm_fulfill_order_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    oid = ctx.user_data.pop("fulfill_order_id", None)
    expected_qty = ctx.user_data.pop("fulfill_qty", 0)
    ctx.user_data.pop("state", None)

    if not oid:
        await update.message.reply_text("Session expired.", reply_markup=admin_menu())
        return

    lines = [l.strip() for l in update.message.text.splitlines() if l.strip()]
    if len(lines) != expected_qty:
        await update.message.reply_text(
            f"❌ You provided <b>{len(lines)}</b> items, but this order needs <b>{expected_qty}</b>.\n\n"
            f"Please send exactly <b>{expected_qty}</b> items (one per line):",
            parse_mode="HTML",
        )
        ctx.user_data["fulfill_order_id"] = oid
        ctx.user_data["fulfill_qty"] = expected_qty
        ctx.user_data["state"] = "adm_fulfill_order"
        return

    ok, result = await complete_pending_order(oid, lines)
    if not ok:
        await update.message.reply_text(
            f"❌ Cannot complete order:\n\n<code>{html.escape(str(result))}</code>",
            parse_mode="HTML",
            reply_markup=admin_menu(),
        )
        return

    o = result["order"]
    new_balance = result["new_balance"]
    items = result["items"]

    pr = await sb(supabase.table("products").select("name,category").eq("id", o["product_id"]).limit(1))
    if pr.data:
        pname = pr.data[0].get("name", "Product")
        pcat = (pr.data[0].get("category") or "").lower()
    else:
        pname = "Product"
        pcat = ""

    total_items = len(items)
    blocks = [format_item_block(i, c, total=total_items, category=pcat) for i, c in enumerate(items, 1)]
    body = "\n\n".join(["━━━━━━━━━━━━━━━━━━━━━━\n" + b for b in blocks])

    header = (
        f"✅ <b>Pending Order Completed</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 Order: <code>#{o['id']}</code>\n"
        f"🛍 Product: <b>{html.escape(pname)}</b>\n"
        f"🔢 Quantity: <b>{o['quantity']}</b>\n"
        f"💵 Charged: {fmt_price(o['total_price'])} {CURRENCY} (already paid)\n"
        f"💳 Your Balance: {fmt_price(new_balance)} {CURRENCY}\n"
    )
    full = header + "\n" + body
    try:
        if len(full) <= 4000:
            await ctx.bot.send_message(o["user_id"], full, parse_mode="HTML")
        else:
            await ctx.bot.send_message(o["user_id"], header, parse_mode="HTML")
            current = ""
            for block in blocks:
                seg = "━━━━━━━━━━━━━━━━━━━━━━\n" + block + "\n\n"
                if len(current) + len(seg) > 3800:
                    if current.strip():
                        try:
                            await ctx.bot.send_message(o["user_id"], current.rstrip(), parse_mode="HTML")
                        except Exception:
                            pass
                    current = seg
                else:
                    current += seg
            if current.strip():
                try:
                    await ctx.bot.send_message(o["user_id"], current.rstrip(), parse_mode="HTML")
                except Exception:
                    pass
    except Exception as e:
        log.error(f"Failed to send fulfilled order: {e}")

    await update.message.reply_text(
        f"✅ Order <b>#{oid}</b> fulfilled and delivered to user.",
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )


# ============================================================
# ADMIN — PENDING TOPUPS
# ============================================================
TOPUPS_PER_PAGE = 10


async def build_topups_list_view(page: int = 1):
    r = await sb(supabase.table("topup_requests").select("*").eq("status", "pending").order("id"))
    reqs = r.data or []

    if not reqs:
        kb = [[btn("⬅️ Back to Admin Panel", callback_data="adm:back", style="danger")]]
        return (
            "💳 <b>Pending Topups</b>\n\n✅ No pending topups right now.",
            InlineKeyboardMarkup(kb),
        )

    total = len(reqs)
    total_pages = max(1, (total + TOPUPS_PER_PAGE - 1) // TOPUPS_PER_PAGE)
    page = max(1, min(page, total_pages))

    start = (page - 1) * TOPUPS_PER_PAGE
    page_reqs = reqs[start:start + TOPUPS_PER_PAGE]

    uids = list({int(t["user_id"]) for t in page_reqs})
    uname_map = await get_usernames_batch(uids)

    kb = []
    for t in page_reqs:
        uname = uname_map.get(int(t["user_id"])) or f"User {t['user_id']}"
        uname = uname.lstrip("@")
        if len(uname) > 16:
            uname = uname[:14] + "…"
        amount_str = fmt_price(t["amount"])
        kb.append([btn(
            f"👤 @{uname} • {amount_str} {CURRENCY}",
            callback_data=f"tup:view:{t['id']}",
            style="primary",
        )])

    nav = []
    if page > 1:
        nav.append(btn("⬅️ Prev", callback_data=f"tup:page:{page-1}", style="primary"))
    if total_pages > 1:
        nav.append(btn(f"{page}/{total_pages}", callback_data="noop", style="primary"))
    if page < total_pages:
        nav.append(btn("Next ➡️", callback_data=f"tup:page:{page+1}", style="primary"))
    if nav:
        kb.append(nav)

    kb.append([btn("⬅️ Back to Admin Panel", callback_data="adm:back", style="danger")])

    text = (
        f"💳 <b>Pending Topups</b>\n\n"
        f"📊 Total: <b>{total}</b>  •  Page {page}/{total_pages}\n\n"
        f"Click a topup to open:"
    )
    return text, InlineKeyboardMarkup(kb)


async def build_topup_detail_view(tid: int, page_hint: int = 1):
    r = await sb(supabase.table("topup_requests").select("*").eq("id", tid).limit(1))
    if not r.data:
        return "Topup not found.", InlineKeyboardMarkup([[btn("⬅️ Back", callback_data=f"tup:page:{page_hint}", style="primary")]])

    t = r.data[0]
    status = (t.get("status") or "").lower()
    pm_name = t.get("payment_method_name") or ""
    pm_line = f"🏦 Method: <b>{html.escape(pm_name)}</b>\n" if pm_name else ""

    if status != "pending":
        uname_map = await get_usernames_batch([int(t["user_id"])])
        uname = (uname_map.get(int(t["user_id"])) or f"User {t['user_id']}").lstrip("@")
        reason = t.get("reject_reason") or ""
        text = (
            f"💳 <b>Topup #{t['id']}</b>\n\n"
            f"👤 User: @{html.escape(uname)}\n"
            f"{pm_line}"
            f"💰 Amount: {fmt_price(t['amount'])} {CURRENCY}\n"
            f"📌 Status: <b>{status.upper()}</b>\n"
        )
        if reason:
            text += f"📝 Reason: <i>{html.escape(reason)}</i>\n"
        kb = [[btn("⬅️ Back to List", callback_data=f"tup:page:{page_hint}", style="primary")]]
        return text, InlineKeyboardMarkup(kb)

    uname_map = await get_usernames_batch([int(t["user_id"])])
    uname = (uname_map.get(int(t["user_id"])) or f"User {t['user_id']}").lstrip("@")

    text = (
        f"💳 <b>Topup #{t['id']}</b>\n\n"
        f"👤 User: <a href=\"tg://user?id={t['user_id']}\">@{html.escape(uname)}</a>\n"
        f"🆔 ID: <code>{t['user_id']}</code>\n"
        f"{pm_line}"
        f"💰 Amount: <b>{fmt_price(t['amount'])} {CURRENCY}</b>\n"
        f"📝 TrxID: <code>{html.escape(str(t.get('txn_id') or '—'))}</code>\n"
        f"📅 Date: {(t.get('created_at') or '')[:16].replace('T', ' ')}\n\n"
        f"Choose an action:"
    )
    kb = [
        [
            btn("✅ Approve", callback_data=f"tup:ok:{tid}", style="success"),
            btn("❌ Reject", callback_data=f"tup:rejstart:{tid}", style="danger"),
        ],
        [btn("⬅️ Back to List", callback_data=f"tup:page:{page_hint}", style="primary")],
    ]
    return text, InlineKeyboardMarkup(kb)


async def _do_reject_topup(ctx, tid: int, reason: str):
    reason = (reason or "No reason provided").strip()
    try:
        await sb(supabase.table("topup_requests").update({
            "status": "rejected", "reject_reason": reason,
        }).eq("id", tid))
    except Exception:
        try:
            await sb(supabase.table("topup_requests").update({
                "status": "rejected",
            }).eq("id", tid))
        except Exception as e:
            log.error(f"reject topup error: {e}")
            return False

    r = await sb(supabase.table("topup_requests").select("user_id,amount").eq("id", tid).limit(1))
    if r.data:
        u = r.data[0]
        try:
            await ctx.bot.send_message(
                u["user_id"],
                f"❌ <b>Topup Rejected</b>\n\n"
                f"Amount: {fmt_price(u['amount'])} {CURRENCY}\n"
                f"Reason: <i>{html.escape(reason)}</i>",
                parse_mode="HTML",
            )
        except Exception:
            pass
    return True


async def adm_btn_topups(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text, kbd = await build_topups_list_view(1)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kbd)


async def adm_topup_reject_reason_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = ctx.user_data.pop("reject_topup_id", None)
    ctx.user_data.pop("state", None)
    if not tid:
        await update.message.reply_text("Session expired. Try again.")
        return
    reason = update.message.text.strip() or "No reason provided"
    await _do_reject_topup(ctx, int(tid), reason)
    text, kbd = await build_topups_list_view(1)
    await update.message.reply_text(
        f"❌ Topup #{tid} rejected.\n\n{text}",
        parse_mode="HTML", reply_markup=kbd,
    )


async def adm_topup_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    data = q.data

    if data == "tup:rejcancel":
        tid = ctx.user_data.pop("reject_topup_id", None)
        ctx.user_data.pop("state", None)
        if tid:
            text, kb = await build_topup_detail_view(int(tid))
        else:
            text, kb = await build_topups_list_view(1)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
        return

    parts = data.split(":")
    if len(parts) < 3:
        return
    action = parts[1]

    if action == "page":
        page = int(parts[2])
        text, kb = await build_topups_list_view(page)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass

    elif action == "view":
        tid = int(parts[2])
        text, kb = await build_topup_detail_view(tid)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass

    elif action == "ok":
        tid = int(parts[2])
        try:
            await sb(supabase.rpc("approve_topup", {"p_topup_id": tid}))
        except Exception as e:
            await q.answer(f"Error: {str(e)[:100]}", show_alert=True)
            return
        r = await sb(supabase.table("topup_requests").select("user_id,amount").eq("id", tid).limit(1))
        if r.data:
            u = r.data[0]
            try:
                await ctx.bot.send_message(
                    u["user_id"],
                    f"✅ Topup of {fmt_price(u['amount'])} {CURRENCY} approved!",
                )
            except Exception:
                pass
            asyncio.create_task(trim_user_transactions(int(u["user_id"])))
        await q.answer("✅ Approved!", show_alert=False)
        text, kb = await build_topups_list_view(1)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass

    elif action == "rejstart":
        tid = int(parts[2])
        ctx.user_data["reject_topup_id"] = tid
        ctx.user_data["state"] = "adm_topup_reject"
        kb = [
            [btn("⏭️ Skip (Reject Without Reason)", callback_data=f"tup:rejskip:{tid}", style="danger")],
            [btn("❌ Cancel", callback_data="tup:rejcancel", style="primary")],
        ]
        try:
            await q.edit_message_text(
                f"❌ <b>Reject Topup #{tid}</b>\n\n"
                f"Send the rejection reason as a text message.\n\n"
                f"Or tap <b>Skip</b> to reject without a reason.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(kb),
            )
        except Exception:
            pass

    elif action == "rejskip":
        tid = int(parts[2])
        ctx.user_data.pop("reject_topup_id", None)
        ctx.user_data.pop("state", None)
        await _do_reject_topup(ctx, tid, "No reason provided")
        await q.answer("❌ Rejected.", show_alert=False)
        text, kb = await build_topups_list_view(1)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass


# ============================================================
# ADMIN — PAYMENT METHODS (Updated for SMS Sender)
# ============================================================
async def build_payment_methods_view():
    methods = await get_all_payment_methods()
    if not methods:
        kb = [
            [btn("➕ Add Payment Method", callback_data="pm:new", style="success")],
            [btn("⬅️ Back to Admin Panel", callback_data="adm:back", style="danger")],
        ]
        return "🏦 <b>Payment Methods</b>\n\nNo methods yet.\n\nAdd your first method:", InlineKeyboardMarkup(kb)

    kb = []
    for m in methods:
        emoji = "🟢" if m.get("is_active") else "🔴"
        kb.append([btn(
            f"{emoji} {m['name']} • {m['number']} • min {fmt_price(m['min_amount'])} {CURRENCY}",
            callback_data=f"pm:view:{m['id']}",
            style="success" if m.get("is_active") else "danger",
        )])
    kb.append([btn("➕ Add Payment Method", callback_data="pm:new", style="success")])
    kb.append([btn("⬅️ Back to Admin Panel", callback_data="adm:back", style="danger")])
    return f"🏦 <b>Payment Methods</b> ({len(methods)})\n\nClick a method to manage:", InlineKeyboardMarkup(kb)


async def show_payment_method_detail(target, pmid, is_callback=True):
    m = await get_payment_method(pmid)
    if not m:
        text = "Method not found."
        kb = InlineKeyboardMarkup([[btn("⬅️ Back", callback_data="pm:list", style="primary")]])
    else:
        status = "🟢 Active" if m.get("is_active") else "🔴 Paused"
        sms_sender = m.get("sms_sender") or "Not Set"
        text = (
            f"🏦 <b>{html.escape(m['name'])}</b>\n\n"
            f"📱 Number: <code>{html.escape(m['number'])}</code>\n"
            f"📨 SMS Sender: <code>{html.escape(sms_sender)}</code>\n"
            f"💰 Min Amount: <b>{fmt_price(m['min_amount'])} {CURRENCY}</b>\n"
            f"📌 Status: {status}\n\n"
            f"Manage this method:"
        )
        toggle_text = "⏸️ Pause (Hide from Users)" if m.get("is_active") else "▶️ Resume (Show to Users)"
        toggle_style = "danger" if m.get("is_active") else "success"
        kb = InlineKeyboardMarkup([
            [btn(toggle_text, callback_data=f"pm:toggle:{pmid}", style=toggle_style)],
            [btn("✏️ Edit Name", callback_data=f"pm:ename:{pmid}", style="primary"),
             btn("📱 Edit Number", callback_data=f"pm:enum:{pmid}", style="primary")],
            [btn("📨 Edit SMS Sender", callback_data=f"pm:esender:{pmid}", style="primary")],
            [btn("💰 Edit Min Amount", callback_data=f"pm:emin:{pmid}", style="primary")],
            [btn("🗑️ Delete Method", callback_data=f"pm:delconf:{pmid}", style="danger")],
            [btn("⬅️ Back to List", callback_data="pm:list", style="primary")],
        ])
    if is_callback:
        await target.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await target.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def adm_btn_payments(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text, kbd = await build_payment_methods_view()
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kbd)


async def pm_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Admin only.", show_alert=True)
        return
    data = q.data

    if data == "pm:list":
        await q.answer()
        text, kb = await build_payment_methods_view()
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass

    elif data == "pm:new":
        await q.answer()
        ctx.user_data["state"] = "pm_new_name"
        await q.edit_message_text(
            "➕ <b>Add Payment Method</b>\n\n"
            "Step 1/4 — Send the method <b>name</b> (e.g., bKash, Nagad):",
            parse_mode="HTML",
        )

    elif data.startswith("pm:view:"):
        await q.answer()
        pmid = int(data.split(":")[2])
        await show_payment_method_detail(q, pmid, is_callback=True)

    elif data.startswith("pm:toggle:"):
        pmid = int(data.split(":")[2])
        m = await get_payment_method(pmid)
        if not m:
            await q.answer("Method not found.", show_alert=True)
            return
        new_active = not m.get("is_active", True)
        await sb(supabase.table("payment_methods").update({"is_active": new_active}).eq("id", pmid))
        await q.answer("✅ Resumed! Visible to users." if new_active else "⏸️ Paused! Hidden from users.", show_alert=False)
        await show_payment_method_detail(q, pmid, is_callback=True)

    elif data.startswith("pm:ename:"):
        await q.answer()
        pmid = int(data.split(":")[2])
        ctx.user_data["pm_edit_id"] = pmid
        ctx.user_data["state"] = "pm_edit_name"
        await q.edit_message_text("✏️ Send the new <b>method name</b>:", parse_mode="HTML")

    elif data.startswith("pm:enum:"):
        await q.answer()
        pmid = int(data.split(":")[2])
        ctx.user_data["pm_edit_id"] = pmid
        ctx.user_data["state"] = "pm_edit_number"
        await q.edit_message_text("📱 Send the new <b>payment number</b>:", parse_mode="HTML")

    elif data.startswith("pm:esender:"):
        await q.answer()
        pmid = int(data.split(":")[2])
        ctx.user_data["pm_edit_id"] = pmid
        ctx.user_data["state"] = "pm_edit_sms_sender"
        await q.edit_message_text("📨 Send the new <b>SMS Sender</b> (e.g., bKash, Nagad):", parse_mode="HTML")

    elif data.startswith("pm:emin:"):
        await q.answer()
        pmid = int(data.split(":")[2])
        ctx.user_data["pm_edit_id"] = pmid
        ctx.user_data["state"] = "pm_edit_min"
        await q.edit_message_text("💰 Send the new <b>minimum deposit amount</b>:", parse_mode="HTML")

    elif data.startswith("pm:delconf:"):
        await q.answer()
        pmid = int(data.split(":")[2])
        m = await get_payment_method(pmid)
        if not m:
            await q.answer("Not found.", show_alert=True)
            return
        kb = InlineKeyboardMarkup([[
            btn("✅ Yes, Delete", callback_data=f"pm:delyes:{pmid}", style="danger"),
            btn("❌ Cancel", callback_data=f"pm:view:{pmid}", style="primary"),
        ]])
        await q.edit_message_text(
            f"⚠️ <b>Delete Payment Method?</b>\n\n"
            f"<b>{html.escape(m['name'])}</b>\n"
            f"Number: <code>{html.escape(m['number'])}</code>\n\n"
            f"This cannot be undone.",
            parse_mode="HTML",
            reply_markup=kb,
        )

    elif data.startswith("pm:delyes:"):
        await q.answer("Deleting...")
        pmid = int(data.split(":")[2])
        try:
            await sb(supabase.table("payment_methods").delete().eq("id", pmid))
        except Exception as e:
            await q.edit_message_text(f"❌ Error: {html.escape(str(e))}")
            return
        text, kb = await build_payment_methods_view()
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass

    else:
        await q.answer()


# ---- Add Payment Method state handlers ----
async def pm_new_name_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("❌ Invalid name. Try again:")
        return
    ctx.user_data["pm_name"] = name
    ctx.user_data["state"] = "pm_new_number"
    await update.message.reply_text(
        "Step 2/4 — Send the <b>payment number</b> (e.g., 01XXXXXXXXX):",
        parse_mode="HTML",
    )


async def pm_new_number_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    number = update.message.text.strip()
    if not number:
        await update.message.reply_text("❌ Invalid number. Try again:")
        return
    ctx.user_data["pm_number"] = number
    ctx.user_data["state"] = "pm_new_sms_sender"
    await update.message.reply_text(
        "Step 3/4 — Send the <b>SMS Sender</b> (e.g., bKash, Nagad) exact name.\n"
        "This will be used to match the incoming SMS.",
        parse_mode="HTML",
    )


async def pm_new_sms_sender_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sender = update.message.text.strip()
    if not sender:
        await update.message.reply_text("❌ Invalid sender. Try again:")
        return
    ctx.user_data["pm_sms_sender"] = sender
    ctx.user_data["state"] = "pm_new_min"
    await update.message.reply_text(
        "Step 4/4 — Send the <b>minimum deposit amount</b> (e.g., 50):",
        parse_mode="HTML",
    )


async def pm_new_min_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        min_amt = float(update.message.text.strip())
        if min_amt < 0:
            min_amt = 0.0
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Send a number:")
        return

    name = ctx.user_data.pop("pm_name", None)
    number = ctx.user_data.pop("pm_number", None)
    sender = ctx.user_data.pop("pm_sms_sender", "")
    ctx.user_data.pop("state", None)

    if not name or not number:
        await update.message.reply_text("Session lost. Try again.", reply_markup=admin_menu())
        return

    try:
        await sb(supabase.table("payment_methods").insert({
            "name": name, "number": number, "sms_sender": sender, "min_amount": min_amt, "is_active": True,
        }))
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {html.escape(str(e))}", reply_markup=admin_menu())
        return

    await update.message.reply_text(
        f"✅ Payment method <b>{html.escape(name)}</b> added!",
        parse_mode="HTML",
    )
    text, kb = await build_payment_methods_view()
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


# ---- Edit Payment Method state handlers ----
async def pm_edit_name_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pmid = ctx.user_data.pop("pm_edit_id", None)
    ctx.user_data.pop("state", None)
    if not pmid:
        await update.message.reply_text("Session expired.")
        return
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("❌ Invalid name. Try again:")
        ctx.user_data["pm_edit_id"] = pmid
        ctx.user_data["state"] = "pm_edit_name"
        return
    await sb(supabase.table("payment_methods").update({"name": name}).eq("id", pmid))
    await update.message.reply_text(f"✅ Name updated to <b>{html.escape(name)}</b>", parse_mode="HTML")
    await show_payment_method_detail(update.message, pmid, is_callback=False)


async def pm_edit_number_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pmid = ctx.user_data.pop("pm_edit_id", None)
    ctx.user_data.pop("state", None)
    if not pmid:
        await update.message.reply_text("Session expired.")
        return
    number = update.message.text.strip()
    if not number:
        await update.message.reply_text("❌ Invalid number. Try again:")
        ctx.user_data["pm_edit_id"] = pmid
        ctx.user_data["state"] = "pm_edit_number"
        return
    await sb(supabase.table("payment_methods").update({"number": number}).eq("id", pmid))
    await update.message.reply_text(f"✅ Number updated to <code>{html.escape(number)}</code>", parse_mode="HTML")
    await show_payment_method_detail(update.message, pmid, is_callback=False)


async def pm_edit_sms_sender_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pmid = ctx.user_data.pop("pm_edit_id", None)
    ctx.user_data.pop("state", None)
    if not pmid:
        await update.message.reply_text("Session expired.")
        return
    sender = update.message.text.strip()
    if not sender:
        await update.message.reply_text("❌ Invalid sender. Try again:")
        ctx.user_data["pm_edit_id"] = pmid
        ctx.user_data["state"] = "pm_edit_sms_sender"
        return
    await sb(supabase.table("payment_methods").update({"sms_sender": sender}).eq("id", pmid))
    await update.message.reply_text(f"✅ SMS Sender updated to <code>{html.escape(sender)}</code>", parse_mode="HTML")
    await show_payment_method_detail(update.message, pmid, is_callback=False)


async def pm_edit_min_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pmid = ctx.user_data.pop("pm_edit_id", None)
    ctx.user_data.pop("state", None)
    if not pmid:
        await update.message.reply_text("Session expired.")
        return
    try:
        min_amt = float(update.message.text.strip())
        if min_amt < 0:
            min_amt = 0.0
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Try again:")
        ctx.user_data["pm_edit_id"] = pmid
        ctx.user_data["state"] = "pm_edit_min"
        return
    await sb(supabase.table("payment_methods").update({"min_amount": min_amt}).eq("id", pmid))
    await update.message.reply_text(
        f"✅ Min amount updated to <b>{fmt_price(min_amt)} {CURRENCY}</b>",
        parse_mode="HTML",
    )
    await show_payment_method_detail(update.message, pmid, is_callback=False)


# ============================================================
# ADMIN — text-menu entry points
# ============================================================
async def admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    ctx.user_data["in_admin"] = True
    await update.message.reply_text(
        "⚙️ <b>Admin Panel</b>\n\nChoose an option from the menu below:",
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )


async def adm_btn_products(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, kbd = await build_products_view()
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kbd)


async def adm_btn_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["state"] = "adm_broadcast"
    await update.message.reply_text(
        "📢 <b>Broadcast</b>\n\nSend the message you want to broadcast to all users.\n\n"
        "It supports text, photos, videos, forwards — anything.\n\n/cancel to abort.",
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )


async def adm_btn_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    u = await sb(supabase.table("users").select("id", count="exact"))
    p = await sb(supabase.table("products").select("id", count="exact").eq("is_active", True))
    o = await sb(supabase.table("orders").select("id", count="exact"))
    po = await sb(supabase.table("orders").select("id", count="exact").eq("status", "pending"))
    tp = await sb(supabase.table("topup_requests").select("id", count="exact").eq("status", "pending"))
    pm = await sb(supabase.table("payment_methods").select("id", count="exact").eq("is_active", True))
    await update.message.reply_text(
        f"📊 <b>Stats</b>\n\n"
        f"👥 Users: {u.count or 0}\n"
        f"📦 Products: {p.count or 0}\n"
        f"🛒 Orders: {o.count or 0}\n"
        f"⏳ Pending Orders: {po.count or 0}\n"
        f"💳 Pending Topups: {tp.count or 0}\n"
        f"🏦 Active Payment Methods: {pm.count or 0}",
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )


# ============================================================
# ADMIN — add-product state flow
# ============================================================
async def adm_addprod_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["p_name"] = update.message.text.strip()
    ctx.user_data["state"] = "adm_prod_cat"
    await update.message.reply_text(
        "Step 2/4 — Send <b>category</b> (e.g., vpn, proxy, mail):",
        parse_mode="HTML",
    )


async def adm_addprod_cat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["p_cat"] = update.message.text.strip().lower()
    ctx.user_data["state"] = "adm_prod_price"
    await update.message.reply_text("Step 3/4 — Send <b>price</b> (number):", parse_mode="HTML")


async def adm_addprod_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Invalid price. Send a number:")
        return
    ctx.user_data["p_price"] = price
    ctx.user_data["state"] = "adm_prod_desc"
    await update.message.reply_text("Step 4/4 — Send <b>description</b> (or /skip):", parse_mode="HTML")


async def adm_addprod_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    desc = "" if update.message.text.startswith("/skip") else update.message.text.strip()
    data = {
        "name": ctx.user_data.pop("p_name"),
        "category": ctx.user_data.pop("p_cat"),
        "price": ctx.user_data.pop("p_price"),
        "description": desc, "stock": 0, "is_active": True,
    }
    ctx.user_data.pop("state", None)
    await sb(supabase.table("products").insert(data))
    await update.message.reply_text(
        f"✅ Product added: <b>{html.escape(data['name'])}</b>",
        parse_mode="HTML",
    )
    # Show the category view of newly added product
    text, kbd = await build_category_products_view(data["category"])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kbd)


# ============================================================
# ADMIN — add-stock state flow
# ============================================================
async def adm_addstock_items(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pid = ctx.user_data.pop("stock_pid", None)
    ctx.user_data.pop("state", None)
    if not pid:
        await update.message.reply_text("Session lost. Try again.", reply_markup=admin_menu())
        return
    lines = [l.strip() for l in update.message.text.splitlines() if l.strip()]
    if not lines:
        await update.message.reply_text("No items received.", reply_markup=admin_menu())
        return
    await sb(supabase.table("product_items").insert([{"product_id": pid, "content": l} for l in lines]))
    r = await sb(supabase.table("product_items").select("id", count="exact")
                 .eq("product_id", pid).eq("is_sold", False))
    new_stock = r.count or 0
    await sb(supabase.table("products").update({"stock": new_stock}).eq("id", pid))
    await update.message.reply_text(
        f"✅ Added <b>{len(lines)}</b> items. New stock: <b>{new_stock}</b>",
        parse_mode="HTML",
    )
    await show_admin_product_detail(update.message, pid, is_callback=False)


# ============================================================
# ADMIN — edit-price state flow
# ============================================================
async def adm_edit_price_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pid = ctx.user_data.pop("edit_price_pid", None)
    ctx.user_data.pop("state", None)
    if not pid:
        await update.message.reply_text("Session expired. Try again.", reply_markup=admin_menu())
        return
    try:
        new_price = float(update.message.text.strip())
        if new_price < 0:
            new_price = 0.0
    except ValueError:
        await update.message.reply_text("❌ Invalid price. Send a number:")
        ctx.user_data["edit_price_pid"] = pid
        ctx.user_data["state"] = "adm_edit_price"
        return
    await sb(supabase.table("products").update({"price": new_price}).eq("id", pid))
    await update.message.reply_text(
        f"✅ Price updated to <b>{fmt_price(new_price)} {CURRENCY}</b>",
        parse_mode="HTML",
    )
    await show_admin_product_detail(update.message, pid, is_callback=False)


# ============================================================
# ADMIN — broadcast
# ============================================================
async def adm_broadcast_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("state", None)
    r = await sb(supabase.table("users").select("id"))
    users = r.data or []
    if not users:
        await update.message.reply_text("No users to broadcast to.", reply_markup=admin_menu())
        return

    status = await update.message.reply_text(f"📤 Broadcasting to {len(users)} users...")

    from_chat = update.message.chat_id
    msg_id = update.message.message_id
    sem = asyncio.Semaphore(20)
    counters = {"sent": 0, "failed": 0}

    async def send_to(uid: int):
        async with sem:
            for _attempt in range(3):
                try:
                    await ctx.bot.copy_message(
                        chat_id=uid, from_chat_id=from_chat, message_id=msg_id,
                    )
                    counters["sent"] += 1
                    break
                except RetryAfter as e:
                    wait = e.retry_after
                    if hasattr(wait, "total_seconds"):
                        wait = wait.total_seconds()
                    await asyncio.sleep(float(wait) + 1)
                except Exception:
                    counters["failed"] += 1
                    break
            else:
                counters["failed"] += 1
            await asyncio.sleep(0.05)

    await asyncio.gather(*[send_to(u["id"]) for u in users], return_exceptions=True)

    report = (
        f"✅ <b>Broadcast complete</b>\n\n"
        f"✅ Sent: {counters['sent']}\n"
        f"❌ Failed: {counters['failed']}\n"
        f"📊 Total: {len(users)}"
    )
    try:
        await status.edit_text(report, parse_mode="HTML")
    except Exception:
        await update.message.reply_text(report, parse_mode="HTML")
    await update.message.reply_text("Admin Menu:", reply_markup=admin_menu())


# ============================================================
# ADMIN — inline callbacks (adm:*)
# ============================================================
async def adm_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Admin only.", show_alert=True)
        return
    data = q.data

    if data == "adm:prods":
        await q.answer()
        text, kbd = await build_products_view()
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kbd)

    elif data.startswith("adm:prodcat:"):
        await q.answer()
        cat = data.split(":", 2)[2]
        text, kbd = await build_category_products_view(cat)
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kbd)

    elif data == "adm:back":
        await q.answer()
        await q.message.reply_text(
            "⚙️ <b>Admin Panel</b>\n\nChoose an option from the menu below:",
            parse_mode="HTML",
            reply_markup=admin_menu(),
        )
        try:
            await q.delete_message()
        except Exception:
            pass

    elif data == "adm:newprod":
        await q.answer()
        ctx.user_data["state"] = "adm_prod_name"
        await q.edit_message_text(
            "➕ <b>New Product</b>\n\nStep 1/4 — Send product <b>name</b>:",
            parse_mode="HTML",
        )

    elif data.startswith("adm:prod:"):
        await q.answer()
        pid = int(data.split(":")[2])
        await show_admin_product_detail(q, pid, is_callback=True)

    elif data.startswith("adm:stock:"):
        await q.answer()
        pid = int(data.split(":")[2])
        ctx.user_data["stock_pid"] = pid
        ctx.user_data["state"] = "adm_stock_items"
        await q.edit_message_text(
            "📦 <b>Add Stock</b>\n\nSend items, <b>one per line</b>.\nEach line becomes one deliverable.\n\n"
            "Format examples:\n"
            "• Proxy: <code>server:port:user:pass</code>\n"
            "• VPN: <code>user:pass:key</code> or <code>user:pass</code>\n"
            "• Email: <code>address:password</code>",
            parse_mode="HTML",
        )

    elif data.startswith("adm:clrstock:"):
        await q.answer()
        pid = int(data.split(":")[2])
        try:
            await sb(supabase.table("product_items").delete().eq("product_id", pid).eq("is_sold", False))
            await sb(supabase.table("products").update({"stock": 0}).eq("id", pid))
        except Exception as e:
            await q.answer(f"Error: {str(e)[:100]}", show_alert=True)
            return
        await q.answer("Unsold stock cleared!", show_alert=True)
        await show_admin_product_detail(q, pid, is_callback=True)

    elif data.startswith("adm:eprice:"):
        await q.answer()
        pid = int(data.split(":")[2])
        r = await sb(supabase.table("products").select("name,price").eq("id", pid).limit(1))
        if not r.data:
            await q.edit_message_text("Product not found.")
            return
        p = r.data[0]
        ctx.user_data["edit_price_pid"] = pid
        ctx.user_data["state"] = "adm_edit_price"
        await q.edit_message_text(
            f"✏️ <b>Edit Price</b>\n\n"
            f"Product: <b>{html.escape(p['name'])}</b>\n"
            f"Current price: {fmt_price(p['price'])} {CURRENCY}\n\n"
            f"Send the new price (number):",
            parse_mode="HTML",
        )

    elif data.startswith("adm:reactivate:"):
        await q.answer()
        pid = int(data.split(":")[2])
        await sb(supabase.table("products").update({"is_active": True}).eq("id", pid))
        await q.answer("Product reactivated!", show_alert=True)
        await show_admin_product_detail(q, pid, is_callback=True)

    elif data.startswith("adm:delconf:"):
        await q.answer()
        pid = int(data.split(":")[2])
        r = await sb(supabase.table("products").select("*").eq("id", pid).limit(1))
        if not r.data:
            await q.edit_message_text("Product not found.")
            return
        p = r.data[0]
        kb = [[
            btn("✅ Yes, Delete", callback_data=f"adm:delyes:{pid}", style="danger"),
            btn("❌ Cancel", callback_data=f"adm:prod:{pid}", style="primary"),
        ]]
        await q.edit_message_text(
            f"⚠️ <b>Delete Product?</b>\n\n"
            f"<b>{html.escape(p['name'])}</b>\n\n"
            f"If this product has order history, it will be <b>deactivated</b> instead.\n"
            f"Otherwise it will be permanently removed.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    elif data.startswith("adm:delyes:"):
        await q.answer("Processing...")
        pid = int(data.split(":")[2])

        # Get category before deletion
        cat = None
        try:
            pr = await sb(supabase.table("products").select("category").eq("id", pid).limit(1))
            if pr.data:
                cat = pr.data[0].get("category")
        except Exception:
            pass

        try:
            order_check = await sb(supabase.table("orders").select("id", count="exact").eq("product_id", pid))
            has_orders = (order_check.count or 0) > 0

            if has_orders:
                await sb(supabase.table("products").update({"is_active": False}).eq("id", pid))
                note = "✅ Product <b>deactivated</b> (has order history)."
            else:
                await sb(supabase.table("product_items").delete().eq("product_id", pid))
                await sb(supabase.table("products").delete().eq("id", pid))
                note = "✅ Product <b>deleted permanently</b>."
        except Exception as e:
            await q.edit_message_text(f"❌ Error: {html.escape(str(e))}")
            return

        if cat:
            text, kbd = await build_category_products_view(cat)
        else:
            text, kbd = await build_products_view()
        try:
            await q.edit_message_text(f"{note}\n\n{text}", parse_mode="HTML", reply_markup=kbd)
        except Exception:
            await ctx.bot.send_message(q.from_user.id, text, parse_mode="HTML", reply_markup=kbd)

    else:
        await q.answer()


# ============================================================
# ROUTER
# ============================================================
async def menu_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    state = ctx.user_data.get("state")
    user_is_admin = is_admin(update.effective_user.id)

    if text in ADMIN_BUTTONS and user_is_admin:
        ctx.user_data.pop("state", None)
        if text == ADM_PRODUCTS:
            return await adm_btn_products(update, ctx)
        if text == ADM_BROADCAST:
            return await adm_btn_broadcast(update, ctx)
        if text == ADM_TOPUPS:
            return await adm_btn_topups(update, ctx)
        if text == ADM_ORDERS:
            return await adm_btn_orders(update, ctx)
        if text == ADM_STATS:
            return await adm_btn_stats(update, ctx)
        if text == ADM_PAYMENTS:
            return await adm_btn_payments(update, ctx)
        if text == ADM_BACK:
            ctx.user_data["in_admin"] = False
            await update.message.reply_text(
                "⬅️ Returned to Main Menu.",
                reply_markup=main_menu(user_is_admin),
            )
            return
        return

    if text in MENU_BUTTONS:
        ctx.user_data.pop("state", None)
        if text == MENU_ADMIN:
            return await admin_panel(update, ctx)
        ctx.user_data["in_admin"] = False
        if text == MENU_BUY:
            return await show_categories(update, ctx)
        if text == MENU_BALANCE:
            return await start_topup(update, ctx)
        if text == MENU_PROFILE:
            return await show_profile(update, ctx)
        if text == MENU_HISTORY:
            return await show_history(update, ctx)
        if text == MENU_SUPPORT:
            return await show_support(update, ctx)
        return

    if state == "topup_amount_first":
        return await topup_amount_first_handler(update, ctx)
    if state == "topup_txn":
        return await topup_txn_handler(update, ctx)
    if state == "qty_manual":
        return await qty_manual_handler(update, ctx)
    if state == "adm_prod_name":
        return await adm_addprod_name(update, ctx)
    if state == "adm_prod_cat":
        return await adm_addprod_cat(update, ctx)
    if state == "adm_prod_price":
        return await adm_addprod_price(update, ctx)
    if state == "adm_prod_desc":
        return await adm_addprod_desc(update, ctx)
    if state == "adm_stock_items":
        return await adm_addstock_items(update, ctx)
    if state == "adm_edit_price":
        return await adm_edit_price_handler(update, ctx)
    if state == "adm_broadcast":
        return await adm_broadcast_handler(update, ctx)
    if state == "adm_topup_reject":
        return await adm_topup_reject_reason_handler(update, ctx)
    if state == "adm_fulfill_order":
        return await adm_fulfill_order_handler(update, ctx)
    if state == "pm_new_name":
        return await pm_new_name_handler(update, ctx)
    if state == "pm_new_number":
        return await pm_new_number_handler(update, ctx)
    if state == "pm_new_sms_sender":
        return await pm_new_sms_sender_handler(update, ctx)
    if state == "pm_new_min":
        return await pm_new_min_handler(update, ctx)
    if state == "pm_edit_name":
        return await pm_edit_name_handler(update, ctx)
    if state == "pm_edit_number":
        return await pm_edit_number_handler(update, ctx)
    if state == "pm_edit_sms_sender":
        return await pm_edit_sms_sender_handler(update, ctx)
    if state == "pm_edit_min":
        return await pm_edit_min_handler(update, ctx)

    kb = admin_menu() if (user_is_admin and ctx.user_data.get("in_admin")) else main_menu(user_is_admin)
    await update.message.reply_text("Unknown option. Use the menu.", reply_markup=kb)


# ============================================================
# HEALTHCHECK / MAIN
# ============================================================
async def health(request):
    return web.Response(text="OK")


async def start_health():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"✅ Healthcheck listening on :{PORT}")


async def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    app.add_handler(CallbackQueryHandler(cat_callback, pattern="^cat:"))
    app.add_handler(CallbackQueryHandler(prod_callback, pattern="^prod:"))
    app.add_handler(CallbackQueryHandler(qty_callback, pattern="^qty:"))
    app.add_handler(CallbackQueryHandler(back_cats, pattern="^back_cats$"))
    app.add_handler(CallbackQueryHandler(cancel_browse, pattern="^cancel_browse$"))
    app.add_handler(CallbackQueryHandler(noop_cb, pattern="^noop$"))

    app.add_handler(CallbackQueryHandler(hist_callback, pattern="^hist:"))
    app.add_handler(CallbackQueryHandler(topup_pick_callback, pattern="^tp:pick:"))
    app.add_handler(CallbackQueryHandler(topup_cancel_callback, pattern="^tup:cancel$"))
    app.add_handler(CallbackQueryHandler(topup_paid_callback, pattern="^tup:paid$"))
    app.add_handler(CallbackQueryHandler(tup_try_again_callback, pattern="^tup:try_again$"))
    app.add_handler(CallbackQueryHandler(tup_manual_review_callback, pattern="^tup:mreview:"))
    app.add_handler(CallbackQueryHandler(adm_topup_action, pattern="^tup:"))
    app.add_handler(CallbackQueryHandler(ord_callback, pattern="^ord:"))
    app.add_handler(CallbackQueryHandler(pm_callback, pattern="^pm:"))
    app.add_handler(CallbackQueryHandler(adm_callback, pattern="^adm:"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))

    asyncio.create_task(start_health())
    log.info("🚀 Starting polling...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
