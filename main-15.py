import os
import html
import sqlite3
import logging
from datetime import datetime

import httpx

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    LabeledPrice,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

# =========================
# CONFIG
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
STORE_NAME_ENV = os.getenv("STORE_NAME", "ZENTRO STORE")
SUPPORT_USERNAME_ENV = os.getenv("SUPPORT_USERNAME", "support")
TELEGRAM_PAYMENT_TOKEN = os.getenv("TELEGRAM_PAYMENT_TOKEN", "").strip()
PAYMENT_CURRENCY = os.getenv("PAYMENT_CURRENCY", "USD").strip().upper()

DB_PATH = os.getenv(
    "DB_PATH",
    "/data/zentro_store.db" if os.path.isdir("/data") else "zentro_store.db",
)

SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL_SECONDS", "900"))
ORDER_CHECK_INTERVAL = int(os.getenv("ORDER_CHECK_INTERVAL_SECONDS", "120"))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "25"))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("zentro-auto-store")


def now_iso():
    return datetime.utcnow().isoformat(timespec="seconds")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                balance REAL NOT NULL DEFAULT 0,
                banned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                api_url TEXT NOT NULL,
                api_key TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                profit_percent REAL NOT NULL DEFAULT 15,
                auto_sync INTEGER NOT NULL DEFAULT 1,
                last_balance REAL,
                last_currency TEXT DEFAULT '',
                last_error TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_id INTEGER NOT NULL,
                external_name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(provider_id, external_name),
                FOREIGN KEY(provider_id) REFERENCES providers(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                external_service_id TEXT NOT NULL,
                name TEXT NOT NULL,
                service_type TEXT DEFAULT '',
                cost_rate REAL NOT NULL DEFAULT 0,
                sell_rate REAL NOT NULL DEFAULT 0,
                min_qty INTEGER NOT NULL DEFAULT 1,
                max_qty INTEGER NOT NULL DEFAULT 1000000,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(provider_id, external_service_id),
                FOREIGN KEY(provider_id) REFERENCES providers(id) ON DELETE CASCADE,
                FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                provider_id INTEGER NOT NULL,
                provider_order_id TEXT DEFAULT '',
                target TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                sell_price REAL NOT NULL,
                cost_estimate REAL NOT NULL DEFAULT 0,
                provider_charge REAL,
                provider_status TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'sending',
                refunded_amount REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL,
                payload TEXT NOT NULL,
                telegram_charge_id TEXT UNIQUE,
                provider_charge_id TEXT,
                status TEXT NOT NULL DEFAULT 'created',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

        defaults = {
            "store_name": STORE_NAME_ENV,
            "support_username": SUPPORT_USERNAME_ENV,
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                (key, str(value)),
            )

        if OWNER_ID:
            conn.execute(
                "INSERT OR IGNORE INTO admins(user_id) VALUES(?)",
                (OWNER_ID,),
            )


def ensure_user(user):
    if not user:
        return
    now = now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO users
            (user_id, username, first_name, balance, banned, created_at, updated_at)
            VALUES (?, ?, ?, 0, 0, ?, ?)
            """,
            (user.id, user.username or "", user.first_name or "", now, now),
        )
        conn.execute(
            """
            UPDATE users
            SET username = ?, first_name = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (user.username or "", user.first_name or "", now, user.id),
        )


def get_user(user_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()


def is_admin(user_id):
    if user_id == OWNER_ID:
        return True
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM admins WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row is not None


def is_banned(user_id):
    row = get_user(user_id)
    return bool(row and row["banned"])


def get_setting(key, default=""):
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,),
        ).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value)
            VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )


def store_name():
    return get_setting("store_name", STORE_NAME_ENV)


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(str(value).replace(",", "."))
    except Exception:
        return default


def safe_int(value, default=0):
    try:
        if value is None:
            return default
        return int(float(str(value)))
    except Exception:
        return default


def blue(text, data):
    return InlineKeyboardButton(text=text, callback_data=data, style="primary")


def red(text, data):
    return InlineKeyboardButton(text=text, callback_data=data, style="danger")


def back_home():
    return InlineKeyboardMarkup([[red("⬅️ العودة للرئيسية", "home")]])


def back_admin():
    return InlineKeyboardMarkup([[red("⬅️ لوحة الإدارة", "admin")]])


def status_ar(status):
    status = (status or "").strip().lower()
    mapping = {
        "sending": "يتم الإرسال ⏳",
        "pending": "قيد التنفيذ ⏳",
        "processing": "قيد التنفيذ 🔄",
        "in progress": "قيد التنفيذ 🔄",
        "completed": "مكتمل ✅",
        "complete": "مكتمل ✅",
        "partial": "جزئي ⚠️",
        "canceled": "ملغي ❌",
        "cancelled": "ملغي ❌",
        "refunded": "مسترد 💸",
        "failed": "فشل ❌",
    }
    return mapping.get(status, status or "غير معروف")


# =========================
# STANDARD SMM API ADAPTER
# =========================

class SMMProvider:
    def __init__(self, api_url, api_key):
        self.api_url = api_url.strip()
        self.api_key = api_key.strip()

    async def request(self, action, **params):
        payload = {"key": self.api_key, "action": action}
        payload.update(params)

        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
        ) as client:
            response = await client.post(
                self.api_url,
                data=payload,
                headers={
                    "User-Agent": "ZentroStoreBot/2.0",
                    "Accept": "application/json,text/plain,*/*",
                },
            )

        if response.status_code >= 400:
            raise RuntimeError(
                f"HTTP {response.status_code}: {response.text[:250]}"
            )

        try:
            data = response.json()
        except ValueError:
            raise RuntimeError(f"الرد ليس JSON: {response.text[:250]}")

        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(str(data["error"]))

        return data

    async def balance(self):
        data = await self.request("balance")
        if not isinstance(data, dict):
            raise RuntimeError("تعذر قراءة الرصيد.")
        return {
            "balance": safe_float(data.get("balance"), 0.0),
            "currency": str(data.get("currency") or ""),
        }

    async def services(self):
        data = await self.request("services")

        if isinstance(data, dict):
            for key in ("services", "data", "items"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break

        if not isinstance(data, list):
            raise RuntimeError("الخدمة لم ترجع قائمة خدمات.")

        result = []

        for item in data:
            if not isinstance(item, dict):
                continue

            sid = item.get("service") or item.get("id") or item.get("service_id")
            if sid is None:
                continue

            min_qty = max(
                1,
                safe_int(item.get("min") or item.get("minimum"), 1),
            )
            max_qty = max(
                min_qty,
                safe_int(item.get("max") or item.get("maximum"), 1000000),
            )

            result.append(
                {
                    "service_id": str(sid),
                    "name": str(
                        item.get("name")
                        or item.get("service_name")
                        or f"Service {sid}"
                    ).strip(),
                    "category": str(
                        item.get("category")
                        or item.get("category_name")
                        or "عام"
                    ).strip(),
                    "rate": safe_float(
                        item.get("rate") or item.get("price") or item.get("cost"),
                        0.0,
                    ),
                    "min": min_qty,
                    "max": max_qty,
                    "type": str(item.get("type") or ""),
                }
            )

        return result

    async def add_order(self, service_id, target, quantity):
        data = await self.request(
            "add",
            service=str(service_id),
            link=target,
            quantity=int(quantity),
        )

        if not isinstance(data, dict):
            raise RuntimeError("رد إنشاء الطلب غير متوقع.")

        order_id = data.get("order") or data.get("id") or data.get("order_id")
        if order_id is None:
            raise RuntimeError(f"الخدمة لم ترجع رقم طلب: {str(data)[:250]}")

        return str(order_id)

    async def status(self, order_id):
        data = await self.request("status", order=str(order_id))

        if not isinstance(data, dict):
            raise RuntimeError("رد حالة الطلب غير متوقع.")

        return {
            "status": str(data.get("status") or "").strip(),
            "charge": safe_float(data.get("charge"), None),
            "remains": safe_int(data.get("remains"), None),
        }


def get_provider(provider_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM providers WHERE id = ?",
            (provider_id,),
        ).fetchone()


def provider_client(provider):
    return SMMProvider(provider["api_url"], provider["api_key"])


async def test_provider(provider_id):
    provider = get_provider(provider_id)
    if not provider:
        raise RuntimeError("إعداد API غير موجود.")

    result = await provider_client(provider).balance()

    with db() as conn:
        conn.execute(
            """
            UPDATE providers
            SET last_balance = ?, last_currency = ?, last_error = '', updated_at = ?
            WHERE id = ?
            """,
            (result["balance"], result["currency"], now_iso(), provider_id),
        )

    return result


async def sync_provider(provider_id):
    provider = get_provider(provider_id)
    if not provider:
        raise RuntimeError("إعداد API غير موجود.")

    services = await provider_client(provider).services()
    profit = float(provider["profit_percent"])

    created = 0
    updated = 0
    seen = set()

    with db() as conn:
        for service in services:
            seen.add(service["service_id"])

            category = conn.execute(
                """
                SELECT id FROM categories
                WHERE provider_id = ? AND external_name = ?
                """,
                (provider_id, service["category"]),
            ).fetchone()

            if category:
                category_id = category["id"]
            else:
                cur = conn.execute(
                    """
                    INSERT INTO categories
                    (provider_id, external_name, display_name, active, created_at, updated_at)
                    VALUES (?, ?, ?, 1, ?, ?)
                    """,
                    (
                        provider_id,
                        service["category"],
                        service["category"],
                        now_iso(),
                        now_iso(),
                    ),
                )
                category_id = cur.lastrowid

            sell_rate = round(service["rate"] * (1 + profit / 100.0), 6)

            existing = conn.execute(
                """
                SELECT id FROM products
                WHERE provider_id = ? AND external_service_id = ?
                """,
                (provider_id, service["service_id"]),
            ).fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE products
                    SET category_id = ?, name = ?, service_type = ?,
                        cost_rate = ?, sell_rate = ?, min_qty = ?, max_qty = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        category_id,
                        service["name"],
                        service["type"],
                        service["rate"],
                        sell_rate,
                        service["min"],
                        service["max"],
                        now_iso(),
                        existing["id"],
                    ),
                )
                updated += 1
            else:
                conn.execute(
                    """
                    INSERT INTO products
                    (
                        provider_id, category_id, external_service_id, name,
                        service_type, cost_rate, sell_rate, min_qty, max_qty,
                        active, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        provider_id,
                        category_id,
                        service["service_id"],
                        service["name"],
                        service["type"],
                        service["rate"],
                        sell_rate,
                        service["min"],
                        service["max"],
                        now_iso(),
                        now_iso(),
                    ),
                )
                created += 1

        old_services = conn.execute(
            """
            SELECT id, external_service_id
            FROM products
            WHERE provider_id = ?
            """,
            (provider_id,),
        ).fetchall()

        for row in old_services:
            if row["external_service_id"] not in seen:
                conn.execute(
                    "UPDATE products SET active = 0, updated_at = ? WHERE id = ?",
                    (now_iso(), row["id"]),
                )

        conn.execute(
            "UPDATE providers SET last_error = '', updated_at = ? WHERE id = ?",
            (now_iso(), provider_id),
        )

    return len(services), created, updated


# =========================
# MENUS
# =========================

def main_menu(user_id):
    rows = [
        [blue("🛍️ المتجر", "store:0"), red("💳 شحن الرصيد", "deposit")],
        [blue("👤 حسابي", "account"), red("📦 طلباتي", "orders:0")],
        [blue("🎧 الدعم الفني", "support"), red("ℹ️ معلومات المتجر", "about")],
    ]
    if is_admin(user_id):
        rows.append([blue("⚙️ مركز إدارة المتجر", "admin")])
    return InlineKeyboardMarkup(rows)


def admin_menu():
    return InlineKeyboardMarkup(
        [
            [blue("📊 الإحصائيات", "admin_stats"), red("🔗 إعدادات API", "providers:0")],
            [blue("🛍️ المنتجات", "admin_products:0"), red("🗂️ الأقسام", "admin_categories:0")],
            [blue("📦 الطلبات", "admin_orders:0"), red("👥 المستخدمون", "admin_users:0")],
            [blue("📢 الإذاعة", "admin_broadcast"), red("💵 الرصيد والأسعار", "admin_money")],
            [blue("🛠 إعدادات المتجر", "admin_settings"), red("👮 المشرفون", "admin_admins")],
            [red("⬅️ العودة للمتجر", "home")],
        ]
    )


def nav_row(prefix, page, total, per_page):
    row = []
    if page > 0:
        row.append(blue("⬅️ السابق", f"{prefix}:{page - 1}"))
    if (page + 1) * per_page < total:
        row.append(blue("التالي ➡️", f"{prefix}:{page + 1}"))
    return row


async def post_init(app):
    try:
        await app.bot.set_my_description(
            description=(
                f"مرحباً بك في {store_name()} 🛍️\n"
                "متجر للخدمات والمنتجات الرقمية.\n"
                "🎮 ألعاب وشحن\n"
                "📱 تطبيقات واشتراكات\n"
                "💳 بطاقات وحسابات\n"
                "🎧 دعم فني"
            )
        )
        await app.bot.set_my_short_description(
            short_description="متجر خدمات رقمية • ألعاب • تطبيقات • بطاقات • حسابات"
        )
        await app.bot.set_my_commands(
            [
                BotCommand("start", "🏠 فتح المتجر"),
                BotCommand("id", "🆔 معرف حسابك"),
                BotCommand("admin", "⚙️ لوحة الإدارة"),
                BotCommand("ping", "🏓 فحص البوت"),
            ]
        )
    except TelegramError as exc:
        logger.warning("Profile setup failed: %s", exc)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    user = update.effective_user

    if is_banned(user.id):
        await update.message.reply_text("⛔ حسابك محظور.")
        return

    context.user_data.clear()
    await update.message.reply_text(
        "👋 <b>أهلاً وسهلاً بك</b>\n\n"
        f"🔵🔴 <b>{html.escape(store_name())}</b>\n\n"
        "🛍️ متجر متكامل للخدمات والمنتجات الرقمية\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎮 ألعاب وشحن\n"
        "📱 تطبيقات واشتراكات\n"
        "💳 بطاقات وخدمات رقمية\n"
        "👤 حسابات جاهزة\n"
        "🤖 خدمات الذكاء الاصطناعي\n"
        "🌐 VPN و Proxy\n"
        "🎨 برامج وأدوات التصميم\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "⚡ خدمة سريعة ومنظمة\n"
        "🎧 دعم فني عند الحاجة\n\n"
        "اختر القسم المطلوب 👇",
        parse_mode="HTML",
        reply_markup=main_menu(user.id),
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    await update.message.reply_text(
        f"🆔 <b>معرف حسابك:</b>\n\n<code>{update.effective_user.id}</code>",
        parse_mode="HTML",
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Pong!\n✅ البوت يعمل.")


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ هذا الأمر للإدارة فقط.")
        return

    context.user_data.clear()
    await update.message.reply_text(
        "⚙️ <b>مركز إدارة المتجر</b>\n\nكل الخيارات مربوطة بوظائف فعلية 👇",
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )


# =========================
# USER PAGES
# =========================

async def show_home(query, context):
    context.user_data.clear()
    await query.edit_message_text(
        f"🔵🔴 <b>{html.escape(store_name())}</b>\n\n🏠 الصفحة الرئيسية\n\nاختر الخدمة 👇",
        parse_mode="HTML",
        reply_markup=main_menu(query.from_user.id),
    )


async def show_store(query, page=0):
    per_page = 8
    offset = page * per_page

    with db() as conn:
        total = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM categories c
            JOIN providers p ON p.id = c.provider_id
            WHERE c.active = 1 AND p.active = 1
            """
        ).fetchone()["c"]

        categories = conn.execute(
            """
            SELECT c.*
            FROM categories c
            JOIN providers p ON p.id = c.provider_id
            WHERE c.active = 1 AND p.active = 1
            ORDER BY c.display_name
            LIMIT ? OFFSET ?
            """,
            (per_page, offset),
        ).fetchall()

    rows = [
        [blue(f"📁 {c['display_name']}", f"category:{c['id']}:0")]
        for c in categories
    ]

    if not rows:
        rows.append([blue("📭 ما في خدمات بعد", "noop")])

    nav = nav_row("store", page, total, per_page)
    if nav:
        rows.append(nav)

    rows.append([red("⬅️ الرئيسية", "home")])

    await query.edit_message_text(
        "🛍️ <b>أقسام المتجر</b>\n\nاختر القسم المطلوب 👇",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def show_category(query, category_id, page=0):
    per_page = 8
    offset = page * per_page

    with db() as conn:
        category = conn.execute(
            """
            SELECT c.*, p.active AS provider_active
            FROM categories c
            JOIN providers p ON p.id = c.provider_id
            WHERE c.id = ?
            """,
            (category_id,),
        ).fetchone()

        total = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM products
            WHERE category_id = ? AND active = 1
            """,
            (category_id,),
        ).fetchone()["c"]

        products = conn.execute(
            """
            SELECT *
            FROM products
            WHERE category_id = ? AND active = 1
            ORDER BY name
            LIMIT ? OFFSET ?
            """,
            (category_id, per_page, offset),
        ).fetchall()

    if not category or not category["active"] or not category["provider_active"]:
        await query.answer("القسم غير متوفر.", show_alert=True)
        return

    rows = [
        [blue(f"🛒 {p['name'][:42]}", f"product:{p['id']}")]
        for p in products
    ]

    if not rows:
        rows.append([blue("📭 لا توجد خدمات", "noop")])

    nav = []
    if page > 0:
        nav.append(blue("⬅️ السابق", f"category:{category_id}:{page - 1}"))
    if (page + 1) * per_page < total:
        nav.append(blue("التالي ➡️", f"category:{category_id}:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([red("⬅️ الأقسام", "store:0")])

    await query.edit_message_text(
        f"📁 <b>{html.escape(category['display_name'])}</b>\n\nاختر الخدمة 👇",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def show_product(query, product_id):
    with db() as conn:
        product = conn.execute(
            """
            SELECT pr.*, p.active AS provider_active
            FROM products pr
            JOIN providers p ON p.id = pr.provider_id
            WHERE pr.id = ?
            """,
            (product_id,),
        ).fetchone()

    if not product or not product["active"] or not product["provider_active"]:
        await query.answer("الخدمة غير متوفرة.", show_alert=True)
        return

    await query.edit_message_text(
        f"🛍️ <b>{html.escape(product['name'])}</b>\n\n"
        f"💵 السعر لكل 1000: <b>{product['sell_rate']:.4f}$</b>\n"
        f"📉 الحد الأدنى: <b>{product['min_qty']}</b>\n"
        f"📈 الحد الأعلى: <b>{product['max_qty']}</b>\n\n"
        "اضغط شراء، والبوت يرسل الطلب للمصدر تلقائياً.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [blue("✅ شراء أوتوماتيكي", f"buy:{product_id}")],
                [red("⬅️ رجوع", f"category:{product['category_id']}:0")],
            ]
        ),
    )


async def show_account(query):
    user = get_user(query.from_user.id)

    with db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE user_id = ?",
            (query.from_user.id,),
        ).fetchone()["c"]

    username = (
        f"@{user['username']}"
        if user and user["username"]
        else "بدون يوزر"
    )

    await query.edit_message_text(
        "👤 <b>حسابي</b>\n\n"
        f"🆔 <code>{query.from_user.id}</code>\n"
        f"👤 {html.escape(username)}\n"
        f"💰 الرصيد: <b>{user['balance']:.4f}$</b>\n"
        f"📦 الطلبات: <b>{count}</b>",
        parse_mode="HTML",
        reply_markup=back_home(),
    )


async def show_orders(query, page=0):
    per_page = 8
    offset = page * per_page

    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE user_id = ?",
            (query.from_user.id,),
        ).fetchone()["c"]

        orders = conn.execute(
            """
            SELECT *
            FROM orders
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (query.from_user.id, per_page, offset),
        ).fetchall()

    rows = [
        [blue(f"📦 #{o['id']} | {status_ar(o['status'])}", f"myorder:{o['id']}")]
        for o in orders
    ]

    if not rows:
        rows.append([blue("📭 ما عندك طلبات", "noop")])

    nav = nav_row("orders", page, total, per_page)
    if nav:
        rows.append(nav)

    rows.append([red("⬅️ الرئيسية", "home")])

    await query.edit_message_text(
        "📦 <b>طلباتي</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def show_order(query, order_id):
    with db() as conn:
        order = conn.execute(
            """
            SELECT o.*, pr.name AS product_name
            FROM orders o
            JOIN products pr ON pr.id = o.product_id
            WHERE o.id = ? AND o.user_id = ?
            """,
            (order_id, query.from_user.id),
        ).fetchone()

    if not order:
        await query.answer("الطلب غير موجود.", show_alert=True)
        return

    await query.edit_message_text(
        f"📦 <b>طلب #{order['id']}</b>\n\n"
        f"🛍️ {html.escape(order['product_name'])}\n"
        f"🔢 الكمية: <b>{order['quantity']}</b>\n"
        f"💵 السعر: <b>{order['sell_price']:.4f}$</b>\n"
        f"📌 الحالة: <b>{html.escape(status_ar(order['status']))}</b>\n"
        f"🔗 رقم التنفيذ: <code>{html.escape(order['provider_order_id'] or '-')}</code>\n"
        f"💸 المسترد: <b>{order['refunded_amount']:.4f}$</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[red("⬅️ طلباتي", "orders:0")]]),
    )


async def show_support(query):
    support = get_setting(
        "support_username",
        SUPPORT_USERNAME_ENV,
    ).replace("@", "").strip()

    rows = []
    if support and support != "support":
        rows.append(
            [
                InlineKeyboardButton(
                    "🎧 مراسلة الدعم",
                    url=f"https://t.me/{support}",
                    style="primary",
                )
            ]
        )

    rows.append([red("⬅️ الرئيسية", "home")])

    await query.edit_message_text(
        "🎧 <b>الدعم الفني</b>\n\nإذا واجهتك مشكلة تواصل معنا.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def show_about(query):
    with db() as conn:
        providers = conn.execute(
            "SELECT COUNT(*) AS c FROM providers WHERE active = 1"
        ).fetchone()["c"]
        products = conn.execute(
            "SELECT COUNT(*) AS c FROM products WHERE active = 1"
        ).fetchone()["c"]

    await query.edit_message_text(
        f"ℹ️ <b>{html.escape(store_name())}</b>\n\n"
        "🛍️ متجر للخدمات والمنتجات الرقمية\n"
        "⚡ خدمة سريعة ومنظمة\n"
        "🔐 متابعة آمنة للطلبات\n"
        "🎧 دعم فني عند الحاجة",
        parse_mode="HTML",
        reply_markup=back_home(),
    )


async def show_deposit(query, context):
    if not TELEGRAM_PAYMENT_TOKEN:
        await query.edit_message_text(
            "💳 <b>شحن الرصيد</b>\n\n"
            "طرق الشحن غير متاحة حالياً.\n"
            "للمساعدة تواصل مع الدعم الفني.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [blue("🎧 الدعم الفني", "support")],
                [red("⬅️ العودة للرئيسية", "home")],
            ]),
        )
        return

    context.user_data.clear()
    context.user_data["state"] = "deposit_amount"

    await query.edit_message_text(
        f"💳 <b>شحن الرصيد أوتوماتيكياً</b>\n\n"
        f"أرسل المبلغ بعملة {PAYMENT_CURRENCY}.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[red("❌ إلغاء", "home")]]),
    )


# =========================
# ADMIN PAGES
# =========================

async def admin_home(query, context):
    if not is_admin(query.from_user.id):
        await query.answer("❌ غير مصرح لك.", show_alert=True)
        return

    context.user_data.clear()
    await query.edit_message_text(
        "⚙️ <b>مركز إدارة المتجر</b>\n\nكل الخيارات شغالة 👇",
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )


async def admin_stats(query):
    with db() as conn:
        users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        balance = conn.execute("SELECT COALESCE(SUM(balance),0) AS s FROM users").fetchone()["s"]
        providers = conn.execute("SELECT COUNT(*) AS c FROM providers WHERE active = 1").fetchone()["c"]
        products = conn.execute("SELECT COUNT(*) AS c FROM products WHERE active = 1").fetchone()["c"]
        pending = conn.execute(
            """
            SELECT COUNT(*) AS c FROM orders
            WHERE LOWER(status) IN ('sending','pending','processing','in progress','partial')
            """
        ).fetchone()["c"]

    await query.edit_message_text(
        "📊 <b>إحصائيات المتجر</b>\n\n"
        f"👥 المستخدمون: <b>{users}</b>\n"
        f"💰 أرصدة العملاء: <b>{float(balance):.4f}$</b>\n"
        f"🔗 روابط API الفعالة: <b>{providers}</b>\n"
        f"🛍️ الخدمات الفعالة: <b>{products}</b>\n"
        f"⏳ الطلبات الجارية: <b>{pending}</b>",
        parse_mode="HTML",
        reply_markup=back_admin(),
    )


async def providers_page(query, page=0):
    per_page = 8
    offset = page * per_page

    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM providers").fetchone()["c"]
        providers = conn.execute(
            "SELECT * FROM providers ORDER BY id DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        ).fetchall()

    rows = [[blue("➕ إضافة API جديد", "provider_add")]]

    for p in providers:
        rows.append(
            [
                blue(
                    f"{'✅' if p['active'] else '⛔'} {p['name']}",
                    f"provider:{p['id']}",
                )
            ]
        )

    nav = nav_row("providers", page, total, per_page)
    if nav:
        rows.append(nav)

    rows.append([red("⬅️ لوحة الإدارة", "admin")])

    await query.edit_message_text(
        "🔗 <b>إعدادات API</b>\n\n"
        "من هنا تقدر تضيف الربط وتفحص الاتصال وتستورد الخدمات وتفعّل المزامنة.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def provider_page(query, provider_id):
    provider = get_provider(provider_id)
    if not provider:
        await query.answer("إعداد API غير موجود.", show_alert=True)
        return

    with db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM products WHERE provider_id = ?",
            (provider_id,),
        ).fetchone()["c"]

    masked = (
        provider["api_key"][:3]
        + "*" * max(4, len(provider["api_key"]) - 3)
    )

    await query.edit_message_text(
        f"🔗 <b>{html.escape(provider['name'])}</b>\n\n"
        f"🌐 <code>{html.escape(provider['api_url'])}</code>\n"
        f"🔑 <code>{html.escape(masked)}</code>\n"
        f"📈 الربح: <b>{provider['profit_percent']:.2f}%</b>\n"
        f"🛍️ الخدمات: <b>{count}</b>\n"
        f"💰 رصيد API: <b>{provider['last_balance'] if provider['last_balance'] is not None else '-'} {html.escape(provider['last_currency'] or '')}</b>\n"
        f"📌 {'فعال ✅' if provider['active'] else 'متوقف ⛔'}\n"
        f"🔄 Auto Sync: {'فعال ✅' if provider['auto_sync'] else 'متوقف ⛔'}\n\n"
        f"آخر خطأ: <code>{html.escape((provider['last_error'] or '-')[:300])}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    blue("🧪 فحص الاتصال", f"provider_test:{provider_id}"),
                    red("🔄 سحب الخدمات", f"provider_sync:{provider_id}"),
                ],
                [
                    blue("📈 تعديل الربح", f"provider_profit:{provider_id}"),
                    red("⏯ تفعيل/إيقاف", f"provider_toggle:{provider_id}"),
                ],
                [
                    blue("🔁 Auto Sync", f"provider_autosync:{provider_id}"),
                    red("🗑 حذف API", f"provider_delete:{provider_id}"),
                ],
                [red("⬅️ إعدادات API", "providers:0")],
            ]
        ),
    )


async def admin_products(query, page=0):
    per_page = 8
    offset = page * per_page

    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
        products = conn.execute(
            """
            SELECT * FROM products
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (per_page, offset),
        ).fetchall()

    rows = [
        [
            blue(
                f"{'✅' if p['active'] else '⛔'} {p['name'][:38]}",
                f"admin_product:{p['id']}",
            )
        ]
        for p in products
    ]

    if not rows:
        rows.append([blue("📭 لا توجد خدمات مضافة", "providers:0")])

    nav = nav_row("admin_products", page, total, per_page)
    if nav:
        rows.append(nav)

    rows.append([red("⬅️ لوحة الإدارة", "admin")])

    await query.edit_message_text(
        "🛍️ <b>إدارة المنتجات</b>\n\nيمكنك عرض الخدمات وتفعيلها أو إيقافها.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def admin_product_page(query, product_id):
    with db() as conn:
        product = conn.execute(
            """
            SELECT pr.*, p.name AS provider_name
            FROM products pr
            JOIN providers p ON p.id = pr.provider_id
            WHERE pr.id = ?
            """,
            (product_id,),
        ).fetchone()

    if not product:
        await query.answer("المنتج غير موجود.", show_alert=True)
        return

    await query.edit_message_text(
        f"🛍️ <b>{html.escape(product['name'])}</b>\n\n"
        f"🔗 الربط: {html.escape(product['provider_name'])}\n"
        f"🆔 Service ID: <code>{html.escape(product['external_service_id'])}</code>\n"
        f"💲 تكلفة/1000: <b>{product['cost_rate']:.6f}</b>\n"
        f"💵 بيع/1000: <b>{product['sell_rate']:.6f}</b>\n"
        f"📉 {product['min_qty']} | 📈 {product['max_qty']}\n"
        f"📌 {'فعال ✅' if product['active'] else 'متوقف ⛔'}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [blue("⏯ تفعيل/إيقاف", f"product_toggle:{product_id}")],
                [red("⬅️ المنتجات", "admin_products:0")],
            ]
        ),
    )


async def admin_categories(query, page=0):
    per_page = 8
    offset = page * per_page

    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM categories").fetchone()["c"]
        categories = conn.execute(
            "SELECT * FROM categories ORDER BY id DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        ).fetchall()

    rows = [
        [
            blue(
                f"{'✅' if c['active'] else '⛔'} {c['display_name'][:40]}",
                f"admin_category:{c['id']}",
            )
        ]
        for c in categories
    ]

    nav = nav_row("admin_categories", page, total, per_page)
    if nav:
        rows.append(nav)

    rows.append([red("⬅️ لوحة الإدارة", "admin")])

    await query.edit_message_text(
        "🗂️ <b>الأقسام</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def admin_category_page(query, category_id):
    with db() as conn:
        category = conn.execute(
            "SELECT * FROM categories WHERE id = ?",
            (category_id,),
        ).fetchone()

    if not category:
        await query.answer("القسم غير موجود.", show_alert=True)
        return

    await query.edit_message_text(
        f"🗂️ <b>{html.escape(category['display_name'])}</b>\n\n"
        f"📌 {'فعال ✅' if category['active'] else 'متوقف ⛔'}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [blue("✏️ تغيير الاسم", f"category_rename:{category_id}")],
                [red("⏯ تفعيل/إيقاف", f"category_toggle:{category_id}")],
                [red("⬅️ الأقسام", "admin_categories:0")],
            ]
        ),
    )


async def admin_orders(query, page=0):
    per_page = 8
    offset = page * per_page

    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
        orders = conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        ).fetchall()

    rows = [
        [
            blue(
                f"📦 #{o['id']} | {status_ar(o['status'])}",
                f"admin_order:{o['id']}",
            )
        ]
        for o in orders
    ]

    if not rows:
        rows.append([blue("📭 لا توجد طلبات", "noop")])

    nav = nav_row("admin_orders", page, total, per_page)
    if nav:
        rows.append(nav)

    rows.append([red("⬅️ لوحة الإدارة", "admin")])

    await query.edit_message_text(
        "📦 <b>إدارة الطلبات</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def admin_order_page(query, order_id):
    with db() as conn:
        order = conn.execute(
            """
            SELECT o.*, pr.name AS product_name, p.name AS provider_name
            FROM orders o
            JOIN products pr ON pr.id = o.product_id
            JOIN providers p ON p.id = o.provider_id
            WHERE o.id = ?
            """,
            (order_id,),
        ).fetchone()

    if not order:
        await query.answer("الطلب غير موجود.", show_alert=True)
        return

    await query.edit_message_text(
        f"📦 <b>طلب #{order['id']}</b>\n\n"
        f"👤 <code>{order['user_id']}</code>\n"
        f"🛍️ {html.escape(order['product_name'])}\n"
        f"🔗 API: {html.escape(order['provider_name'])}\n"
        f"🧾 رقم التنفيذ: <code>{html.escape(order['provider_order_id'] or '-')}</code>\n"
        f"🔢 الكمية: <b>{order['quantity']}</b>\n"
        f"💵 البيع: <b>{order['sell_price']:.4f}$</b>\n"
        f"📌 {html.escape(status_ar(order['status']))}\n"
        f"💸 المسترد: <b>{order['refunded_amount']:.4f}$</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [blue("🔄 تحديث الحالة الآن", f"order_refresh:{order_id}")],
                [red("⬅️ الطلبات", "admin_orders:0")],
            ]
        ),
    )


async def admin_users(query, page=0):
    per_page = 8
    offset = page * per_page

    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        users = conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        ).fetchall()

    rows = [[blue("🔎 بحث بالـ ID", "user_search")]]

    for u in users:
        name = u["first_name"] or u["username"] or str(u["user_id"])
        rows.append(
            [
                blue(
                    f"{'⛔' if u['banned'] else '👤'} {name[:24]} | {u['balance']:.2f}$",
                    f"admin_user:{u['user_id']}",
                )
            ]
        )

    nav = nav_row("admin_users", page, total, per_page)
    if nav:
        rows.append(nav)

    rows.append([red("⬅️ لوحة الإدارة", "admin")])

    await query.edit_message_text(
        "👥 <b>إدارة المستخدمين</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def admin_user_page(query, user_id):
    user = get_user(user_id)

    if not user:
        await query.answer("المستخدم غير موجود.", show_alert=True)
        return

    username = f"@{user['username']}" if user["username"] else "بدون يوزر"

    await query.edit_message_text(
        "👤 <b>المستخدم</b>\n\n"
        f"الاسم: {html.escape(user['first_name'] or '')}\n"
        f"اليوزر: {html.escape(username)}\n"
        f"🆔 <code>{user_id}</code>\n"
        f"💰 الرصيد: <b>{user['balance']:.4f}$</b>\n"
        f"📌 {'محظور ⛔' if user['banned'] else 'فعال ✅'}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    blue("➕ إضافة رصيد", f"user_add:{user_id}"),
                    red("➖ خصم رصيد", f"user_sub:{user_id}"),
                ],
                [red("⛔/✅ حظر أو فك الحظر", f"user_ban:{user_id}")],
                [red("⬅️ المستخدمون", "admin_users:0")],
            ]
        ),
    )


async def admin_money(query):
    with db() as conn:
        providers = conn.execute(
            "SELECT * FROM providers ORDER BY id"
        ).fetchall()

    lines = ["💵 <b>الرصيد والأسعار</b>\n"]
    for p in providers:
        lines.append(
            f"\n🔗 {html.escape(p['name'])}\n"
            f"💰 {p['last_balance'] if p['last_balance'] is not None else '-'} "
            f"{html.escape(p['last_currency'] or '')}\n"
            f"📈 الربح: {p['profit_percent']:.2f}%\n"
        )

    if not providers:
        lines.append("\nلا يوجد مصادر.")

    await query.edit_message_text(
        "".join(lines),
        parse_mode="HTML",
        reply_markup=back_admin(),
    )


async def admin_settings(query):
    await query.edit_message_text(
        "🛠 <b>إعدادات المتجر</b>\n\n"
        f"🏪 الاسم: <b>{html.escape(store_name())}</b>\n"
        f"🎧 الدعم: <b>@{html.escape(get_setting('support_username', SUPPORT_USERNAME_ENV).replace('@',''))}</b>\n"
        f"💳 الشحن: {'مفعّل ✅' if TELEGRAM_PAYMENT_TOKEN else 'غير مفعّل ⛔'}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [blue("🏪 تغيير اسم المتجر", "setting_name")],
                [blue("🎧 تغيير يوزر الدعم", "setting_support")],
                [red("⬅️ لوحة الإدارة", "admin")],
            ]
        ),
    )


async def admin_admins(query):
    with db() as conn:
        admins = conn.execute("SELECT user_id FROM admins ORDER BY user_id").fetchall()

    lines = ["👮 <b>المشرفون</b>\n", f"\n👑 المالك: <code>{OWNER_ID}</code>"]

    for row in admins:
        if row["user_id"] != OWNER_ID:
            lines.append(f"\n👮 <code>{row['user_id']}</code>")

    await query.edit_message_text(
        "".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [blue("➕ إضافة مشرف", "admin_add_admin"), red("➖ حذف مشرف", "admin_remove_admin")],
                [red("⬅️ لوحة الإدارة", "admin")],
            ]
        ),
    )


# =========================
# CALLBACK ROUTER
# =========================

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ensure_user(query.from_user)

    if is_banned(query.from_user.id) and not is_admin(query.from_user.id):
        await query.answer("⛔ حسابك محظور.", show_alert=True)
        return

    data = query.data
    await query.answer()

    # USER
    if data == "home":
        await show_home(query, context)

    elif data.startswith("store:"):
        await show_store(query, int(data.split(":")[1]))

    elif data.startswith("category:"):
        _, category_id, page = data.split(":")
        await show_category(query, int(category_id), int(page))

    elif data.startswith("product:"):
        await show_product(query, int(data.split(":")[1]))

    elif data.startswith("buy:"):
        product_id = int(data.split(":")[1])

        with db() as conn:
            product = conn.execute(
                """
                SELECT pr.*, p.active AS provider_active
                FROM products pr
                JOIN providers p ON p.id = pr.provider_id
                WHERE pr.id = ?
                """,
                (product_id,),
            ).fetchone()

        if not product or not product["active"] or not product["provider_active"]:
            await query.answer("الخدمة غير متوفرة.", show_alert=True)
            return

        context.user_data.clear()
        context.user_data["state"] = "buy_target"
        context.user_data["product_id"] = product_id

        await query.edit_message_text(
            f"🛒 <b>{html.escape(product['name'])}</b>\n\n"
            "1️⃣ أرسل الرابط / اليوزر / الـ ID المطلوب للخدمة.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[red("❌ إلغاء", "home")]]),
        )

    elif data == "buy_confirm":
        await confirm_buy(query, context)

    elif data == "account":
        await show_account(query)

    elif data.startswith("orders:"):
        await show_orders(query, int(data.split(":")[1]))

    elif data.startswith("myorder:"):
        await show_order(query, int(data.split(":")[1]))

    elif data == "deposit":
        await show_deposit(query, context)

    elif data == "support":
        await show_support(query)

    elif data == "about":
        await show_about(query)

    elif data == "noop":
        await query.answer("لا يوجد شيء هنا حالياً.", show_alert=True)

    # ADMIN
    elif data == "admin":
        await admin_home(query, context)

    elif data.startswith("admin") or data.startswith("provider") or data.startswith("product_toggle") or data.startswith("category_") or data.startswith("order_refresh") or data.startswith("user_") or data.startswith("setting_"):
        if not is_admin(query.from_user.id):
            await query.answer("❌ غير مصرح لك.", show_alert=True)
            return

        if data == "admin_stats":
            await admin_stats(query)

        elif data.startswith("providers:"):
            await providers_page(query, int(data.split(":")[1]))

        elif data == "provider_add":
            context.user_data.clear()
            context.user_data["state"] = "provider_name"
            await query.edit_message_text(
                "➕ <b>إضافة API جديد</b>\n\n"
                "أرسل اسم الربط الذي تريد أن يظهر لك داخل لوحة الإدارة.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[red("❌ إلغاء", "providers:0")]]),
            )

        elif data.startswith("provider:"):
            await provider_page(query, int(data.split(":")[1]))

        elif data.startswith("provider_test:"):
            provider_id = int(data.split(":")[1])

            try:
                result = await test_provider(provider_id)
                await provider_page(query, provider_id)
                await query.answer(
                    f"✅ الاتصال ناجح\nالرصيد: {result['balance']} {result['currency']}",
                    show_alert=True,
                )
            except Exception as exc:
                with db() as conn:
                    conn.execute(
                        """
                        UPDATE providers
                        SET last_error = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (str(exc)[:500], now_iso(), provider_id),
                    )
                await provider_page(query, provider_id)
                await query.answer(
                    f"❌ فشل الاتصال:\n{str(exc)[:170]}",
                    show_alert=True,
                )

        elif data.startswith("provider_sync:"):
            provider_id = int(data.split(":")[1])

            await query.edit_message_text("🔄 جاري تحديث الخدمات والأسعار...")

            try:
                total, created, updated = await sync_provider(provider_id)
                await provider_page(query, provider_id)
                await query.answer(
                    f"✅ تم تحديث {total} خدمة\nجديد: {created} | تحديث: {updated}",
                    show_alert=True,
                )
            except Exception as exc:
                with db() as conn:
                    conn.execute(
                        """
                        UPDATE providers
                        SET last_error = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (str(exc)[:500], now_iso(), provider_id),
                    )
                await provider_page(query, provider_id)
                await query.answer(
                    f"❌ فشل السحب:\n{str(exc)[:170]}",
                    show_alert=True,
                )

        elif data.startswith("provider_profit:"):
            provider_id = int(data.split(":")[1])
            context.user_data.clear()
            context.user_data["state"] = "provider_profit"
            context.user_data["provider_id"] = provider_id
            await query.edit_message_text(
                "📈 أرسل نسبة الربح الجديدة.\nمثال: <code>15</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[red("❌ إلغاء", f"provider:{provider_id}")]]),
            )

        elif data.startswith("provider_toggle:"):
            provider_id = int(data.split(":")[1])

            with db() as conn:
                row = conn.execute(
                    "SELECT active FROM providers WHERE id = ?",
                    (provider_id,),
                ).fetchone()

                if row:
                    conn.execute(
                        """
                        UPDATE providers
                        SET active = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (0 if row["active"] else 1, now_iso(), provider_id),
                    )

            await provider_page(query, provider_id)

        elif data.startswith("provider_autosync:"):
            provider_id = int(data.split(":")[1])

            with db() as conn:
                row = conn.execute(
                    "SELECT auto_sync FROM providers WHERE id = ?",
                    (provider_id,),
                ).fetchone()

                if row:
                    conn.execute(
                        """
                        UPDATE providers
                        SET auto_sync = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (0 if row["auto_sync"] else 1, now_iso(), provider_id),
                    )

            await provider_page(query, provider_id)

        elif data.startswith("provider_delete:"):
            provider_id = int(data.split(":")[1])

            with db() as conn:
                active_orders = conn.execute(
                    """
                    SELECT COUNT(*) AS c
                    FROM orders
                    WHERE provider_id = ?
                    AND LOWER(status) IN ('sending','pending','processing','in progress','partial')
                    """,
                    (provider_id,),
                ).fetchone()["c"]

                if active_orders:
                    await query.answer(
                        "❌ لا يمكن حذف مصدر فيه طلبات جارية.",
                        show_alert=True,
                    )
                    return

                conn.execute("DELETE FROM providers WHERE id = ?", (provider_id,))

            await providers_page(query, 0)

        elif data.startswith("admin_products:"):
            await admin_products(query, int(data.split(":")[1]))

        elif data.startswith("admin_product:"):
            await admin_product_page(query, int(data.split(":")[1]))

        elif data.startswith("product_toggle:"):
            product_id = int(data.split(":")[1])

            with db() as conn:
                row = conn.execute(
                    "SELECT active FROM products WHERE id = ?",
                    (product_id,),
                ).fetchone()

                if row:
                    conn.execute(
                        """
                        UPDATE products
                        SET active = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (0 if row["active"] else 1, now_iso(), product_id),
                    )

            await admin_product_page(query, product_id)

        elif data.startswith("admin_categories:"):
            await admin_categories(query, int(data.split(":")[1]))

        elif data.startswith("admin_category:"):
            await admin_category_page(query, int(data.split(":")[1]))

        elif data.startswith("category_toggle:"):
            category_id = int(data.split(":")[1])

            with db() as conn:
                row = conn.execute(
                    "SELECT active FROM categories WHERE id = ?",
                    (category_id,),
                ).fetchone()

                if row:
                    conn.execute(
                        """
                        UPDATE categories
                        SET active = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (0 if row["active"] else 1, now_iso(), category_id),
                    )

            await admin_category_page(query, category_id)

        elif data.startswith("category_rename:"):
            category_id = int(data.split(":")[1])
            context.user_data.clear()
            context.user_data["state"] = "category_rename"
            context.user_data["category_id"] = category_id
            await query.edit_message_text(
                "✏️ أرسل الاسم الجديد للقسم.",
                reply_markup=InlineKeyboardMarkup([[red("❌ إلغاء", f"admin_category:{category_id}")]]),
            )

        elif data.startswith("admin_orders:"):
            await admin_orders(query, int(data.split(":")[1]))

        elif data.startswith("admin_order:"):
            await admin_order_page(query, int(data.split(":")[1]))

        elif data.startswith("order_refresh:"):
            order_id = int(data.split(":")[1])
            await refresh_order(order_id, context.application)
            await admin_order_page(query, order_id)

        elif data.startswith("admin_users:"):
            await admin_users(query, int(data.split(":")[1]))

        elif data.startswith("admin_user:"):
            await admin_user_page(query, int(data.split(":")[1]))

        elif data == "user_search":
            context.user_data.clear()
            context.user_data["state"] = "user_search"
            await query.edit_message_text(
                "🔎 أرسل Telegram ID للمستخدم.",
                reply_markup=InlineKeyboardMarkup([[red("❌ إلغاء", "admin_users:0")]]),
            )

        elif data.startswith("user_add:"):
            target = int(data.split(":")[1])
            context.user_data.clear()
            context.user_data["state"] = "user_add"
            context.user_data["target_user_id"] = target
            await query.edit_message_text(
                "➕ أرسل المبلغ الذي تريد إضافته.",
                reply_markup=InlineKeyboardMarkup([[red("❌ إلغاء", f"admin_user:{target}")]]),
            )

        elif data.startswith("user_sub:"):
            target = int(data.split(":")[1])
            context.user_data.clear()
            context.user_data["state"] = "user_sub"
            context.user_data["target_user_id"] = target
            await query.edit_message_text(
                "➖ أرسل المبلغ الذي تريد خصمه.",
                reply_markup=InlineKeyboardMarkup([[red("❌ إلغاء", f"admin_user:{target}")]]),
            )

        elif data.startswith("user_ban:"):
            target = int(data.split(":")[1])

            if target == OWNER_ID:
                await query.answer("❌ لا يمكن حظر المالك.", show_alert=True)
                return

            with db() as conn:
                row = conn.execute(
                    "SELECT banned FROM users WHERE user_id = ?",
                    (target,),
                ).fetchone()

                if row:
                    conn.execute(
                        """
                        UPDATE users
                        SET banned = ?, updated_at = ?
                        WHERE user_id = ?
                        """,
                        (0 if row["banned"] else 1, now_iso(), target),
                    )

            await admin_user_page(query, target)

        elif data == "admin_broadcast":
            context.user_data.clear()
            context.user_data["state"] = "broadcast"
            await query.edit_message_text(
                "📢 <b>الإذاعة</b>\n\nأرسل الرسالة الآن.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[red("❌ إلغاء", "admin")]]),
            )

        elif data == "admin_money":
            await admin_money(query)

        elif data == "admin_settings":
            await admin_settings(query)

        elif data == "setting_name":
            context.user_data.clear()
            context.user_data["state"] = "setting_name"
            await query.edit_message_text(
                "🏪 أرسل اسم المتجر الجديد.",
                reply_markup=InlineKeyboardMarkup([[red("❌ إلغاء", "admin_settings")]]),
            )

        elif data == "setting_support":
            context.user_data.clear()
            context.user_data["state"] = "setting_support"
            await query.edit_message_text(
                "🎧 أرسل يوزر الدعم بدون @.",
                reply_markup=InlineKeyboardMarkup([[red("❌ إلغاء", "admin_settings")]]),
            )

        elif data == "admin_admins":
            await admin_admins(query)

        elif data == "admin_add_admin":
            context.user_data.clear()
            context.user_data["state"] = "admin_add_admin"
            await query.edit_message_text(
                "➕ أرسل Telegram ID للمشرف الجديد.",
                reply_markup=InlineKeyboardMarkup([[red("❌ إلغاء", "admin_admins")]]),
            )

        elif data == "admin_remove_admin":
            context.user_data.clear()
            context.user_data["state"] = "admin_remove_admin"
            await query.edit_message_text(
                "➖ أرسل Telegram ID للمشرف المراد حذفه.",
                reply_markup=InlineKeyboardMarkup([[red("❌ إلغاء", "admin_admins")]]),
            )


# =========================
# AUTO BUY
# =========================

async def confirm_buy(query, context):
    product_id = context.user_data.get("product_id")
    target = context.user_data.get("target")
    quantity = context.user_data.get("quantity")

    if not product_id or not target or quantity is None:
        await query.answer("انتهت جلسة الطلب. ابدأ من جديد.", show_alert=True)
        return

    with db() as conn:
        product = conn.execute(
            """
            SELECT pr.*, p.active AS provider_active
            FROM products pr
            JOIN providers p ON p.id = pr.provider_id
            WHERE pr.id = ?
            """,
            (product_id,),
        ).fetchone()

        user = conn.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (query.from_user.id,),
        ).fetchone()

    if not product or not product["active"] or not product["provider_active"]:
        await query.answer("الخدمة غير متوفرة.", show_alert=True)
        return

    price = round(product["sell_rate"] * int(quantity) / 1000.0, 6)

    if float(user["balance"]) < price:
        await query.answer(
            f"❌ رصيدك غير كافٍ. المطلوب {price:.4f}$",
            show_alert=True,
        )
        return

    with db() as conn:
        cur = conn.execute(
            """
            UPDATE users
            SET balance = balance - ?, updated_at = ?
            WHERE user_id = ? AND balance >= ?
            """,
            (price, now_iso(), query.from_user.id, price),
        )

        if cur.rowcount != 1:
            await query.answer("❌ الرصيد غير كافٍ.", show_alert=True)
            return

    provider = get_provider(product["provider_id"])

    try:
        provider_order_id = await provider_client(provider).add_order(
            product["external_service_id"],
            target,
            quantity,
        )
    except Exception as exc:
        with db() as conn:
            conn.execute(
                """
                UPDATE users
                SET balance = balance + ?, updated_at = ?
                WHERE user_id = ?
                """,
                (price, now_iso(), query.from_user.id),
            )

        context.user_data.clear()

        await query.edit_message_text(
            "❌ <b>تعذر تنفيذ الطلب</b>\n\n"
            "تمت إعادة رصيدك تلقائياً.\n\n"
            f"<code>{html.escape(str(exc)[:350])}</code>",
            parse_mode="HTML",
            reply_markup=back_home(),
        )
        return

    cost = round(product["cost_rate"] * int(quantity) / 1000.0, 6)

    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO orders
            (
                user_id, product_id, provider_id, provider_order_id,
                target, quantity, sell_price, cost_estimate,
                status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                query.from_user.id,
                product_id,
                product["provider_id"],
                provider_order_id,
                target,
                quantity,
                price,
                cost,
                now_iso(),
                now_iso(),
            ),
        )
        order_id = cur.lastrowid

    context.user_data.clear()

    await query.edit_message_text(
        f"✅ <b>تم إرسال الطلب للمصدر تلقائياً</b>\n\n"
        f"🧾 رقم طلبك: <code>#{order_id}</code>\n"
        f"🔗 رقم التنفيذ: <code>{html.escape(provider_order_id)}</code>\n"
        f"💵 السعر: <b>{price:.4f}$</b>\n"
        "⏳ الحالة: قيد التنفيذ\n\n"
        "البوت سيتابع الحالة تلقائياً.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [blue("📦 طلباتي", "orders:0")],
                [red("⬅️ الرئيسية", "home")],
            ]
        ),
    )


# =========================
# TEXT STATES
# =========================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    user = update.effective_user
    text = (update.message.text or "").strip()
    state = context.user_data.get("state")

    if is_banned(user.id) and not is_admin(user.id):
        await update.message.reply_text("⛔ حسابك محظور.")
        return

    if not state:
        return

    if state == "buy_target":
        product_id = context.user_data["product_id"]

        with db() as conn:
            product = conn.execute(
                "SELECT * FROM products WHERE id = ?",
                (product_id,),
            ).fetchone()

        context.user_data["target"] = text
        context.user_data["state"] = "buy_quantity"

        await update.message.reply_text(
            f"2️⃣ أرسل الكمية.\n\n"
            f"📉 الحد الأدنى: <b>{product['min_qty']}</b>\n"
            f"📈 الحد الأعلى: <b>{product['max_qty']}</b>",
            parse_mode="HTML",
        )
        return

    if state == "buy_quantity":
        product_id = context.user_data["product_id"]

        with db() as conn:
            product = conn.execute(
                "SELECT * FROM products WHERE id = ?",
                (product_id,),
            ).fetchone()

        try:
            quantity = int(text)
        except ValueError:
            await update.message.reply_text("❌ أرسل كمية صحيحة.")
            return

        if quantity < product["min_qty"] or quantity > product["max_qty"]:
            await update.message.reply_text(
                f"❌ الكمية لازم تكون بين {product['min_qty']} و {product['max_qty']}."
            )
            return

        price = round(product["sell_rate"] * quantity / 1000.0, 6)

        context.user_data["quantity"] = quantity
        context.user_data["state"] = "buy_confirm"

        await update.message.reply_text(
            "🧾 <b>تأكيد الطلب</b>\n\n"
            f"🛍️ {html.escape(product['name'])}\n"
            f"🔢 الكمية: <b>{quantity}</b>\n"
            f"💵 السعر: <b>{price:.4f}$</b>\n"
            f"🎯 <code>{html.escape(context.user_data['target'])}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [blue("✅ تأكيد وإرسال للمصدر", "buy_confirm")],
                    [red("❌ إلغاء", "home")],
                ]
            ),
        )
        return

    if state == "deposit_amount":
        try:
            amount = float(text.replace(",", "."))
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ أرسل مبلغ صحيح.")
            return

        context.user_data.clear()
        payload = f"wallet:{user.id}:{amount:.2f}:{now_iso()}"

        with db() as conn:
            conn.execute(
                """
                INSERT INTO payments
                (user_id, amount, currency, payload, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'created', ?, ?)
                """,
                (user.id, amount, PAYMENT_CURRENCY, payload, now_iso(), now_iso()),
            )

        await context.bot.send_invoice(
            chat_id=user.id,
            title=f"شحن رصيد {store_name()}",
            description=f"شحن المحفظة بمبلغ {amount:.2f} {PAYMENT_CURRENCY}",
            payload=payload,
            provider_token=TELEGRAM_PAYMENT_TOKEN,
            currency=PAYMENT_CURRENCY,
            prices=[
                LabeledPrice(
                    "شحن المحفظة",
                    int(round(amount * 100)),
                )
            ],
        )
        return

    if not is_admin(user.id):
        return

    if state == "provider_name":
        context.user_data["provider_name"] = text
        context.user_data["state"] = "provider_url"
        await update.message.reply_text(
            "🌐 أرسل رابط API الكامل.",
            parse_mode="HTML",
        )
        return

    if state == "provider_url":
        if not text.startswith(("http://", "https://")):
            await update.message.reply_text("❌ الرابط غير صحيح.")
            return

        context.user_data["provider_url"] = text
        context.user_data["state"] = "provider_key"
        await update.message.reply_text("🔑 أرسل مفتاح API.")
        return

    if state == "provider_key":
        context.user_data["provider_key"] = text
        context.user_data["state"] = "provider_profit_new"
        await update.message.reply_text(
            "📈 أرسل نسبة الربح.\nمثال: <code>15</code>",
            parse_mode="HTML",
        )
        return

    if state == "provider_profit_new":
        try:
            profit = float(text.replace(",", "."))
            if profit < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ أرسل نسبة صحيحة.")
            return

        name = context.user_data["provider_name"]
        url = context.user_data["provider_url"]
        key = context.user_data["provider_key"]

        with db() as conn:
            cur = conn.execute(
                """
                INSERT INTO providers
                (name, api_url, api_key, active, profit_percent, auto_sync, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, 1, ?, ?)
                """,
                (name, url, key, profit, now_iso(), now_iso()),
            )
            provider_id = cur.lastrowid

        context.user_data.clear()

        await update.message.reply_text(
            f"✅ تم حفظ الربط <b>{html.escape(name)}</b>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [blue("🧪 فحص الاتصال", f"provider_test:{provider_id}")],
                    [red("🔗 فتح إعدادات API", f"provider:{provider_id}")],
                ]
            ),
        )
        return

    if state == "provider_profit":
        provider_id = context.user_data["provider_id"]

        try:
            profit = float(text.replace(",", "."))
            if profit < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ أرسل نسبة صحيحة.")
            return

        with db() as conn:
            conn.execute(
                """
                UPDATE providers
                SET profit_percent = ?, updated_at = ?
                WHERE id = ?
                """,
                (profit, now_iso(), provider_id),
            )

            products = conn.execute(
                "SELECT id, cost_rate FROM products WHERE provider_id = ?",
                (provider_id,),
            ).fetchall()

            for product in products:
                sell = round(product["cost_rate"] * (1 + profit / 100.0), 6)
                conn.execute(
                    "UPDATE products SET sell_rate = ?, updated_at = ? WHERE id = ?",
                    (sell, now_iso(), product["id"]),
                )

        context.user_data.clear()

        await update.message.reply_text(
            "✅ تم تحديث نسبة الربح وأسعار البيع.",
            reply_markup=InlineKeyboardMarkup([[blue("🔗 إعدادات API", f"provider:{provider_id}")]]),
        )
        return

    if state == "category_rename":
        category_id = context.user_data["category_id"]

        with db() as conn:
            conn.execute(
                """
                UPDATE categories
                SET display_name = ?, updated_at = ?
                WHERE id = ?
                """,
                (text, now_iso(), category_id),
            )

        context.user_data.clear()

        await update.message.reply_text(
            "✅ تم تغيير اسم القسم.",
            reply_markup=InlineKeyboardMarkup([[blue("🗂️ فتح القسم", f"admin_category:{category_id}")]]),
        )
        return

    if state == "user_search":
        try:
            target = int(text)
        except ValueError:
            await update.message.reply_text("❌ أرسل Telegram ID صحيح.")
            return

        context.user_data.clear()

        if not get_user(target):
            await update.message.reply_text("❌ المستخدم ما دخل البوت من قبل.")
            return

        await update.message.reply_text(
            "✅ تم العثور عليه.",
            reply_markup=InlineKeyboardMarkup([[blue("👤 فتح المستخدم", f"admin_user:{target}")]]),
        )
        return

    if state in ("user_add", "user_sub"):
        try:
            amount = float(text.replace(",", "."))
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ أرسل مبلغ صحيح.")
            return

        target = context.user_data["target_user_id"]
        sign = 1 if state == "user_add" else -1

        with db() as conn:
            row = conn.execute(
                "SELECT balance FROM users WHERE user_id = ?",
                (target,),
            ).fetchone()

            if not row:
                context.user_data.clear()
                await update.message.reply_text("❌ المستخدم غير موجود.")
                return

            new_balance = max(0.0, float(row["balance"]) + sign * amount)

            conn.execute(
                """
                UPDATE users
                SET balance = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (new_balance, now_iso(), target),
            )

        context.user_data.clear()

        await update.message.reply_text(
            f"✅ الرصيد الجديد: {new_balance:.4f}$",
            reply_markup=InlineKeyboardMarkup([[blue("👤 فتح المستخدم", f"admin_user:{target}")]]),
        )
        return

    if state == "broadcast":
        with db() as conn:
            users = conn.execute(
                "SELECT user_id FROM users WHERE banned = 0"
            ).fetchall()

        sent = 0
        failed = 0

        for row in users:
            try:
                await context.bot.send_message(row["user_id"], text)
                sent += 1
            except TelegramError:
                failed += 1

        context.user_data.clear()

        await update.message.reply_text(
            f"📢 انتهت الإذاعة.\n✅ تم: {sent}\n❌ فشل: {failed}",
            reply_markup=InlineKeyboardMarkup([[blue("⚙️ لوحة الإدارة", "admin")]]),
        )
        return

    if state == "setting_name":
        set_setting("store_name", text)
        context.user_data.clear()
        await update.message.reply_text(
            "✅ تم تغيير اسم المتجر.",
            reply_markup=InlineKeyboardMarkup([[blue("🛠 الإعدادات", "admin_settings")]]),
        )
        return

    if state == "setting_support":
        support = text.replace("@", "").strip()
        set_setting("support_username", support)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ يوزر الدعم: @{support}",
            reply_markup=InlineKeyboardMarkup([[blue("🛠 الإعدادات", "admin_settings")]]),
        )
        return

    if state == "admin_add_admin":
        try:
            admin_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ أرسل ID صحيح.")
            return

        with db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO admins(user_id) VALUES(?)",
                (admin_id,),
            )

        context.user_data.clear()

        await update.message.reply_text(
            "✅ تم إضافة المشرف.",
            reply_markup=InlineKeyboardMarkup([[blue("👮 المشرفون", "admin_admins")]]),
        )
        return

    if state == "admin_remove_admin":
        try:
            admin_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ أرسل ID صحيح.")
            return

        if admin_id == OWNER_ID:
            await update.message.reply_text("❌ لا يمكن حذف المالك.")
            return

        with db() as conn:
            conn.execute("DELETE FROM admins WHERE user_id = ?", (admin_id,))

        context.user_data.clear()

        await update.message.reply_text(
            "✅ تم حذف المشرف.",
            reply_markup=InlineKeyboardMarkup([[blue("👮 المشرفون", "admin_admins")]]),
        )


# =========================
# PAYMENT HANDLERS
# =========================

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    payment = update.message.successful_payment
    user_id = update.effective_user.id
    charge_id = payment.telegram_payment_charge_id
    amount = payment.total_amount / 100.0

    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM payments WHERE telegram_charge_id = ?",
            (charge_id,),
        ).fetchone()

        if existing:
            return

        conn.execute(
            """
            INSERT INTO payments
            (
                user_id, amount, currency, payload,
                telegram_charge_id, provider_charge_id,
                status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'paid', ?, ?)
            """,
            (
                user_id,
                amount,
                payment.currency,
                payment.invoice_payload,
                charge_id,
                payment.provider_payment_charge_id,
                now_iso(),
                now_iso(),
            ),
        )

        conn.execute(
            """
            UPDATE users
            SET balance = balance + ?, updated_at = ?
            WHERE user_id = ?
            """,
            (amount, now_iso(), user_id),
        )

    await update.message.reply_text(
        f"✅ تم شحن رصيدك تلقائياً بمبلغ <b>{amount:.2f} {payment.currency}</b>.",
        parse_mode="HTML",
        reply_markup=main_menu(user_id),
    )


# =========================
# AUTO ORDER STATUS
# =========================

async def refresh_order(order_id, app):
    with db() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()

    if not order or not order["provider_order_id"]:
        return

    provider = get_provider(order["provider_id"])
    if not provider:
        return

    try:
        result = await provider_client(provider).status(order["provider_order_id"])
    except Exception as exc:
        logger.warning("Status failed for #%s: %s", order_id, exc)
        return

    raw_status = result["status"]
    normalized = raw_status.strip().lower()
    new_status = normalized or order["status"]
    refund = 0.0

    if normalized in ("canceled", "cancelled", "refunded", "failed"):
        refund = max(
            0.0,
            float(order["sell_price"]) - float(order["refunded_amount"]),
        )

    elif normalized == "partial" and result["remains"] is not None:
        remains = max(0, int(result["remains"]))
        proportional = round(
            float(order["sell_price"])
            * min(remains, order["quantity"])
            / max(1, order["quantity"]),
            6,
        )
        refund = max(
            0.0,
            proportional - float(order["refunded_amount"]),
        )

    with db() as conn:
        if refund > 0:
            conn.execute(
                """
                UPDATE users
                SET balance = balance + ?, updated_at = ?
                WHERE user_id = ?
                """,
                (refund, now_iso(), order["user_id"]),
            )

        conn.execute(
            """
            UPDATE orders
            SET provider_status = ?, status = ?, provider_charge = ?,
                refunded_amount = refunded_amount + ?, updated_at = ?
            WHERE id = ?
            """,
            (
                raw_status,
                new_status,
                result["charge"],
                refund,
                now_iso(),
                order_id,
            ),
        )

    if refund > 0:
        try:
            await app.bot.send_message(
                order["user_id"],
                f"💸 رجعنا لك <b>{refund:.4f}$</b> تلقائياً للطلب #{order_id} "
                f"لأن حالة الطلب صارت: <b>{html.escape(raw_status)}</b>",
                parse_mode="HTML",
            )
        except TelegramError:
            pass


async def order_job(context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        orders = conn.execute(
            """
            SELECT id FROM orders
            WHERE LOWER(status) IN ('sending','pending','processing','in progress','partial')
            AND provider_order_id != ''
            ORDER BY id
            LIMIT 100
            """
        ).fetchall()

    for row in orders:
        try:
            await refresh_order(row["id"], context.application)
        except Exception as exc:
            logger.warning("Order job failed #%s: %s", row["id"], exc)


async def provider_job(context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        providers = conn.execute(
            """
            SELECT id FROM providers
            WHERE active = 1 AND auto_sync = 1
            """
        ).fetchall()

    for row in providers:
        provider_id = row["id"]

        try:
            await test_provider(provider_id)
            await sync_provider(provider_id)
        except Exception as exc:
            logger.warning("Provider sync failed #%s: %s", provider_id, exc)

            with db() as conn:
                conn.execute(
                    """
                    UPDATE providers
                    SET last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (str(exc)[:500], now_iso(), provider_id),
                )


async def error_handler(update, context):
    logger.exception("Unhandled error", exc_info=context.error)


# =========================
# RUN
# =========================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN غير موجود في Railway Variables.")

    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("admin", admin_command))

    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(
        MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment)
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )

    app.add_error_handler(error_handler)

    if app.job_queue:
        app.job_queue.run_repeating(
            order_job,
            interval=ORDER_CHECK_INTERVAL,
            first=60,
            name="order-status-sync",
        )
        app.job_queue.run_repeating(
            provider_job,
            interval=SYNC_INTERVAL,
            first=120,
            name="provider-auto-sync",
        )

    print(f"{store_name()} AUTO STORE BOT IS RUNNING")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
