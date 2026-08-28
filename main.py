import os
import html
import logging

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)


# =====================================
# إعدادات البوت
# =====================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
STORE_NAME = os.getenv("STORE_NAME", "NOVA STORE")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "support")


# =====================================
# Logging
# =====================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =====================================
# الأزرار الملونة
# =====================================

def blue(text, callback):
    return InlineKeyboardButton(
        text=text,
        callback_data=callback,
        style="primary",
    )


def red(text, callback):
    return InlineKeyboardButton(
        text=text,
        callback_data=callback,
        style="danger",
    )


def back_home():
    return InlineKeyboardMarkup([
        [red("⬅️ الرجوع للرئيسية", "home")]
    ])


# =====================================
# الواجهة الرئيسية
# =====================================

def main_menu(user_id):
    keyboard = [
        [
            blue("🛍️ سوق الخدمات", "store"),
            red("💳 شحن المحفظة", "deposit"),
        ],
        [
            blue("👤 ملفي", "account"),
            red("📦 طلباتي", "orders"),
        ],
        [
            blue("🎁 العروض", "offers"),
            red("🎧 المساعدة", "support"),
        ],
    ]

    if user_id == OWNER_ID:
        keyboard.append([
            blue("⚙️ مركز إدارة المتجر", "admin")
        ])

    return InlineKeyboardMarkup(keyboard)


# =====================================
# لوحة الإدارة
# =====================================

def admin_menu():
    return InlineKeyboardMarkup([
        [
            blue("📊 حالة المتجر", "admin_stats"),
            red("📥 الإيداعات", "admin_deposits"),
        ],
        [
            blue("🛍️ إدارة الخدمات", "admin_products"),
            red("🗂️ تنظيم الأقسام", "admin_categories"),
        ],
        [
            blue("📦 متابعة الطلبات", "admin_orders"),
            red("👥 إدارة العملاء", "admin_users"),
        ],
        [
            blue("🔗 مزود الخدمات", "admin_api"),
            red("📢 مركز الإعلانات", "admin_broadcast"),
        ],
        [
            blue("💵 التسعير والأرباح", "admin_prices"),
            red("🛠 إعدادات المتجر", "admin_settings"),
        ],
        [
            red("⬅️ العودة للمتجر", "home")
        ],
    ])


# =====================================
# /start
# =====================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = html.escape(user.first_name or "صديقي")

    text = (
        f"👋 أهلاً <b>{name}</b>\n\n"
        f"🔵🔴 <b>{html.escape(STORE_NAME)}</b>\n\n"
        "مرحباً بك في متجر الخدمات الرقمية.\n\n"
        "🛍️ اختر القسم المطلوب من الأسفل 👇"
    )

    await update.message.reply_text(
        text=text,
        parse_mode="HTML",
        reply_markup=main_menu(user.id),
    )


# =====================================
# /id
# =====================================

async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    await update.message.reply_text(
        "🆔 معرف حسابك:\n\n"
        f"<code>{user_id}</code>",
        parse_mode="HTML",
    )


# =====================================
# /ping
# =====================================

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏓 Pong!\n✅ البوت يعمل بشكل طبيعي."
    )


# =====================================
# /admin
# =====================================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text(
            "❌ هذا القسم خاص بالإدارة."
        )
        return

    await update.message.reply_text(
        "⚙️ <b>مركز إدارة المتجر</b>\n\n"
        "اختر القسم الذي تريد إدارته 👇",
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )


# =====================================
# ضغط الأزرار
# =====================================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id

    # الرئيسية
    if data == "home":
        await query.edit_message_text(
            f"🔵🔴 <b>{html.escape(STORE_NAME)}</b>\n\n"
            "اختر الخدمة المطلوبة 👇",
            parse_mode="HTML",
            reply_markup=main_menu(user_id),
        )
        return

    # المتجر
    if data == "store":
        keyboard = InlineKeyboardMarkup([
            [
                blue("🎮 Gaming Zone", "cat_games"),
                red("📱 Mobile Hub", "cat_apps"),
            ],
            [
                blue("💳 Cards Center", "cat_cards"),
                red("👤 Accounts Market", "cat_accounts"),
            ],
            [
                blue("🤖 AI Services", "cat_ai"),
                red("📺 Digital Pass", "cat_subs"),
            ],
            [
                blue("🌐 Web & VPN", "cat_web"),
                red("🎨 Design Tools", "cat_design"),
            ],
            [
                red("⬅️ الرئيسية", "home")
            ],
        ])

        await query.edit_message_text(
            "🛍️ <b>سوق الخدمات</b>\n\n"
            "اختر نوع الخدمة 👇",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    # الحساب
    if data == "account":
        await query.edit_message_text(
            "👤 <b>ملف العميل</b>\n\n"
            f"🆔 المعرف: <code>{user_id}</code>\n"
            "💰 الرصيد: <b>0.00$</b>\n"
            "📦 الطلبات: <b>0</b>\n\n"
            "سيتم ربط البيانات بقاعدة البيانات بالمرحلة التالية.",
            parse_mode="HTML",
            reply_markup=back_home(),
        )
        return

    # الإيداع
    if data == "deposit":
        keyboard = InlineKeyboardMarkup([
            [
                blue("💵 USD", "deposit_usd"),
                red("💳 Sham Cash", "deposit_sham"),
            ],
            [
                blue("📱 Syriatel Cash", "deposit_syriatel")
            ],
            [
                red("⬅️ الرئيسية", "home")
            ],
        ])

        await query.edit_message_text(
            "💳 <b>شحن المحفظة</b>\n\n"
            "اختر طريقة الدفع 👇",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    # الطلبات
    if data == "orders":
        await query.edit_message_text(
            "📦 <b>طلباتي</b>\n\n"
            "ما عندك طلبات حالياً.",
            parse_mode="HTML",
            reply_markup=back_home(),
        )
        return

    # العروض
    if data == "offers":
        await query.edit_message_text(
            "🎁 <b>العروض</b>\n\n"
            "ما في عروض مضافة حالياً.",
            parse_mode="HTML",
            reply_markup=back_home(),
        )
        return

    # الدعم
    if data == "support":
        username = SUPPORT_USERNAME.strip().replace("@", "")

        keyboard = [
            [red("⬅️ الرئيسية", "home")]
        ]

        if username and username != "support":
            keyboard.insert(
                0,
                [
                    InlineKeyboardButton(
                        text="🎧 مراسلة الدعم",
                        url=f"https://t.me/{username}",
                        style="primary",
                    )
                ],
            )

        await query.edit_message_text(
            "🎧 <b>مركز المساعدة</b>\n\n"
            "لأي مشكلة أو استفسار تواصل مع فريق الدعم.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # لوحة الإدارة
    if data == "admin":
        if user_id != OWNER_ID:
            await query.answer(
                "❌ غير مصرح لك.",
                show_alert=True,
            )
            return

        await query.edit_message_text(
            "⚙️ <b>مركز إدارة المتجر</b>\n\n"
            "اختر القسم المطلوب 👇",
            parse_mode="HTML",
            reply_markup=admin_menu(),
        )
        return

    # أقسام الإدارة المؤقتة
    if data.startswith("admin_"):
        if user_id != OWNER_ID:
            await query.answer(
                "❌ غير مصرح لك.",
                show_alert=True,
            )
            return

        await query.answer(
            "🚧 رح نبرمج هذا القسم بالخطوة القادمة.",
            show_alert=True,
        )
        return

    # أقسام المتجر المؤقتة
    if data.startswith("cat_"):
        await query.answer(
            "🚧 رح نضيف المنتجات لهذا القسم بالمرحلة القادمة.",
            show_alert=True,
        )
        return

    # طرق الإيداع المؤقتة
    if data.startswith("deposit_"):
        await query.answer(
            "🚧 رح نركب نظام الإيداع بالمرحلة القادمة.",
            show_alert=True,
        )
        return


# =====================================
# تشغيل البوت
# =====================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN غير موجود. أضفه في Variables على Railway."
        )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CallbackQueryHandler(buttons))

    print("NOVA STORE BOT IS RUNNING...")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
