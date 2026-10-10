from __future__ import annotations
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters
from config.settings import Settings
from core.agent import Agent

log = logging.getLogger(__name__)

def build_app(agent: Agent, settings: Settings):
    app = Application.builder().token(settings.telegram_bot_token).build()
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("Hi, I'm Crayon. Ask me something or use /help.")
    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("I can chat, save notes, and set reminders. Google access is read-only and actions require approval.")
    async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        agent.memory.delete_user(str(update.effective_user.id)); await update.message.reply_text("Your local Crayon data was deleted.")
    async def message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        r = agent.handle_message(str(update.effective_user.id), update.message.text)
        buttons = None
        if r.pending_actions: buttons = InlineKeyboardMarkup([[InlineKeyboardButton("Approve", callback_data="approve:0"), InlineKeyboardButton("Cancel", callback_data="cancel:0")]])
        await update.message.reply_text(r.text + (f"\n\n(model: {r.provider})" if r.provider else ""), reply_markup=buttons)
    async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query; await q.answer(); await q.edit_message_text("Action cancelled." if q.data.startswith("cancel") else "Approval received; this prototype logs the approval but does not execute external writes.")
    app.add_handler(CommandHandler("start", start)); app.add_handler(CommandHandler("help", help_cmd)); app.add_handler(CommandHandler("delete_my_data", delete_cmd)); app.add_handler(CallbackQueryHandler(callback)); app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message))
    return app

def run(agent, settings):
    if missing := settings.validate_for_bot(): raise RuntimeError("Missing configuration: " + ", ".join(missing))
    build_app(agent, settings).run_polling(allowed_updates=Update.ALL_TYPES)
