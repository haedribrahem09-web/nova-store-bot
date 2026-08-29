import os
import html
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
STORE_NAME = os.getenv("STORE_NAME", "ZENTRO STORE")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "support")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

def blue(text, callback):
    return InlineKeyboardButton(text=text, callback_data=callback, style="primary")

def red(text, callback):
    return InlineKeyboardButton(text=text, callback_data=callback, style="danger")

def back_home():
    return InlineKeyboardMarkup([[red("⬅️ العودة للرئيسية", "home")]])

def main_menu(user_id):
    keyboard = [
        [blue("🛍️ تصفح المتجر", "store"), red("💳 إيداع الرصيد", "deposit")],
        [blue("👤 حسابي", "account"), red("📦 طلباتي", "orders")],
        [blue("🎁 العروض", "offers"), red("🎧 الدعم الفني", "support")],
    ]
    if user_id == OWNER_ID:
        keyboard.append([blue("⚙️ مركز إدارة المتجر", "admin")])
    return InlineKeyboardMarkup(keyboard)

def admin_menu():
    return InlineKeyboardMarkup([
        [blue("📊 إحصائيات المتجر", "admin_stats"), red("📥 طلبات الإيداع", "admin_deposits")],
        [blue("🛍️ إدارة المنتجات", "admin_products"), red("🗂️ إدارة الأقسام", "admin_categories")],
        [blue("📦 إدارة الطلبات", "admin_orders"), red("👥 إدارة المستخدمين", "admin_users")],
        [blue("🔗 إعدادات API", "admin_api"), red("📢 الإذاعة", "admin_broadcast")],
        [blue("💵 الأسعار والأرباح", "admin_prices"), red("🛠 إعدادات المتجر", "admin_settings")],
        [red("⬅️ العودة للمتجر", "home")],
    ])

async def post_init(application):
    description = (
        f"مرحباً بك في {STORE_NAME} 🛍️\n"
        "متجر للخدمات والمنتجات الرقمية.\n"
        "🎮 ألعاب وشحن\n"
        "📱 تطبيقات واشتراكات\n"
        "💳 بطاقات وحسابات\n"
        "🎧 دعم فني"
    )
    short_description = "متجر خدمات رقمية | ألعاب • تطبيقات • بطاقات • حسابات"
    try:
        await application.bot.set_my_description(description=description)
        await application.bot.set_my_short_description(short_description=short_description)
        await application.bot.set_my_commands([
            BotCommand("start", "🏠 فتح المتجر"),
            BotCommand("id", "🆔 معرفة معرف حسابك"),
            BotCommand("ping", "🏓 فحص البوت"),
            BotCommand("admin", "⚙️ لوحة الإدارة"),
        ])
    except Exception as e:
        logger.warning("Could not set bot profile info: %s", e)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = html.escape(user.first_name or "صديقي")
    store_name = html.escape(STORE_NAME)
    text = (
        f"👋 <b>أهلاً وسهلاً {name}</b>\n\n"
        f"🔵🔴 <b>{store_name}</b>\n\n"
        "🛍️ متجر متكامل للخدمات والمنتجات الرقمية\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎮 شحن وألعاب\n"
        "📱 تطبيقات واشتراكات\n"
        "💳 بطاقات وخدمات رقمية\n"
        "👤 حسابات جاهزة\n"
        "🤖 خدمات الذكاء الاصطناعي\n"
        "🌐 VPN و Proxy\n"
        "🎨 برامج وأدوات التصميم\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "⚡ خدمة سريعة ومنظمة\n"
        "🎧 دعم فني عند الحاجة\n\n"
        "اختر القسم المطلوب من الأزرار بالأسفل 👇"
    )
    await update.message.reply_text(text=text, parse_mode="HTML", reply_markup=main_menu(user.id))

async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(
        "🆔 <b>معرف حسابك:</b>\n\n"
        f"<code>{user_id}</code>",
        parse_mode="HTML",
    )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Pong!\n✅ البوت يعمل بشكل طبيعي.")

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ هذا القسم خاص بالإدارة.")
        return
    await update.message.reply_text(
        "⚙️ <b>مركز إدارة المتجر</b>\n\nاختر القسم الذي تريد إدارته 👇",
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    store_name = html.escape(STORE_NAME)

    if data == "home":
        await query.edit_message_text(
            f"🔵🔴 <b>{store_name}</b>\n\n🏠 الصفحة الرئيسية\n\nاختر الخدمة المطلوبة 👇",
            parse_mode="HTML",
            reply_markup=main_menu(user_id),
        )
        return

    if data == "store":
        keyboard = InlineKeyboardMarkup([
            [blue("🎮 الألعاب", "cat_games"), red("📱 التطبيقات", "cat_apps")],
            [blue("💳 البطاقات", "cat_cards"), red("👤 الحسابات الجاهزة", "cat_accounts")],
            [blue("🤖 الذكاء الاصطناعي", "cat_ai"), red("📺 الاشتراكات الرقمية", "cat_subs")],
            [blue("🌐 VPN والبروكسي", "cat_web"), red("🎨 برامج التصميم", "cat_design")],
            [blue("💰 العملات الرقمية", "cat_crypto"), red("✅ خدمات التوثيق", "cat_verify")],
            [red("⬅️ العودة للرئيسية", "home")],
        ])
        await query.edit_message_text(
            "🛍️ <b>أقسام المتجر</b>\n\nاختر نوع الخدمة التي تريدها 👇",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    if data == "account":
        await query.edit_message_text(
            "👤 <b>حسابي</b>\n\n"
            f"🆔 المعرف: <code>{user_id}</code>\n"
            "💰 الرصيد: <b>0.00$</b>\n"
            "📦 عدد الطلبات: <b>0</b>\n\n"
            "سيتم ربط هذه البيانات بقاعدة البيانات في المرحلة التالية.",
            parse_mode="HTML",
            reply_markup=back_home(),
        )
        return

    if data == "deposit":
        keyboard = InlineKeyboardMarkup([
            [blue("💵 USD", "deposit_usd"), red("💳 شام كاش", "deposit_sham")],
            [blue("📱 سيريتل كاش", "deposit_syriatel")],
            [red("⬅️ العودة للرئيسية", "home")],
        ])
        await query.edit_message_text(
            "💳 <b>إيداع الرصيد</b>\n\nاختر طريقة الدفع المناسبة 👇",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    if data == "orders":
        await query.edit_message_text(
            "📦 <b>طلباتي</b>\n\nلا يوجد لديك طلبات حالياً.",
            parse_mode="HTML",
            reply_markup=back_home(),
        )
        return

    if data == "offers":
        await query.edit_message_text(
            "🎁 <b>العروض</b>\n\nلا توجد عروض مضافة حالياً.",
            parse_mode="HTML",
            reply_markup=back_home(),
        )
        return

    if data == "support":
        username = SUPPORT_USERNAME.strip().replace("@", "")
        keyboard = [[red("⬅️ العودة للرئيسية", "home")]]
        if username and username != "support":
            keyboard.insert(
                0,
                [InlineKeyboardButton(
                    text="🎧 مراسلة الدعم الفني",
                    url=f"https://t.me/{username}",
                    style="primary",
                )],
            )
        await query.edit_message_text(
            "🎧 <b>الدعم الفني</b>\n\n"
            "إذا واجهتك أي مشكلة أو عندك استفسار، تواصل معنا من الزر الموجود بالأسفل.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "admin":
        if user_id != OWNER_ID:
            await query.answer("❌ غير مصرح لك.", show_alert=True)
            return
        await query.edit_message_text(
            "⚙️ <b>مركز إدارة المتجر</b>\n\nاختر القسم المطلوب 👇",
            parse_mode="HTML",
            reply_markup=admin_menu(),
        )
        return

    if data.startswith("admin_"):
        if user_id != OWNER_ID:
            await query.answer("❌ غير مصرح لك.", show_alert=True)
            return
        await query.answer("🚧 سيتم برمجة هذا القسم في الخطوة القادمة.", show_alert=True)
        return

    if data.startswith("cat_"):
        await query.answer("🚧 سيتم إضافة المنتجات لهذا القسم قريباً.", show_alert=True)
        return

    if data.startswith("deposit_"):
        await query.answer("🚧 سيتم تركيب نظام الإيداع في المرحلة القادمة.", show_alert=True)
        return

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN غير موجود. أضفه في Variables على Railway.")

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
    app.add_handler(CallbackQueryHandler(buttons))

    print(f"{STORE_NAME} BOT IS RUNNING...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
