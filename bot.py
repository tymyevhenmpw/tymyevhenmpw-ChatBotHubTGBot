import os
import logging
import json
import asyncio
from flask import Flask, request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown
from aiohttp import ClientSession

# --- Globals & Config ---
# We define these at the global level so they are accessible throughout the app.
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_PATH = "/telegram/webhook"
NOTIFY_WEBHOOK_PATH = "/notify"
EXPRESS_STAFF_LOGIN_URL = os.getenv("EXPRESS_STAFF_LOGIN_URL", "http://host.docker.internal:3001/api/staff/login")
EXPRESS_USER_LOGIN_URL = os.getenv("EXPRESS_USER_LOGIN_URL", "http://host.docker.internal:3001/api/users/login")

AUTH_CHOICE, GET_STAFF_EMAIL, GET_STAFF_PASSWORD, GET_OWNER_EMAIL, GET_OWNER_PASSWORD = range(5)
AUTHORIZED = {}
AUTHENTICATED_STAFF_DETAILS = {}

# --- Telegram Bot Handlers ---
# (Your handler functions like start, button_callback_handler, get_owner_password, etc., go here)
# (I've omitted them for brevity, but you must copy all your handler functions into this space)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Handles the /start command. Greets the user and presents authentication options.
    Also logs out the user by removing their authentication data.
    """
    user = update.effective_user
    chat_id = update.effective_chat.id

    # Log out the user if they were previously authenticated (owner or staff)
    if user.id in AUTHORIZED: # Check by Telegram user_id for owner
        del AUTHORIZED[user.id]
        logger.info(f"User {user.id} logged out as owner.")
    
    if chat_id in AUTHENTICATED_STAFF_DETAILS: # Check by Telegram chat_id for staff
        del AUTHENTICATED_STAFF_DETAILS[chat_id]
        logger.info(f"Chat ID {chat_id} logged out as staff.")

    keyboard = [
        [InlineKeyboardButton("Authenticate as Owner", callback_data="auth_owner")],
        [InlineKeyboardButton("Authenticate as Staff", callback_data="auth_staff")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Hello {user.full_name}\\! 👋\n\n"
        "Welcome to the notification bot\\. Please choose how you would like to authenticate:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN_V2
    )
    logger.info(f"User {user.id} ({user.full_name}) started the bot and received auth options.")
    return AUTH_CHOICE

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Handles inline keyboard button presses for authentication.
    """
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data == "auth_owner":
        await query.edit_message_text(
            f"Okay, let's authenticate you as the *owner*\\. Please send me your *email address* \\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        logger.info(f"User {user.id} ({query.from_user.full_name}) chose owner authentication.")
        return GET_OWNER_EMAIL
    
    elif data == "auth_staff":
        await query.edit_message_text(
            f"Okay, let's authenticate you as staff\\. Please send me your *email address* \\.",
            parse_mode=ParseMode.MARKDOWN_V2
        ) 
        logger.info(f"User {user.id} ({query.from_user.full_name}) chose staff authentication.")
        return GET_STAFF_EMAIL
    
    else:
        await query.edit_message_text(
            "Invalid authentication option selected\\. Please use /start again\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        ) 
        logger.warning(f"User {user.id} ({query.from_user.full_name}) selected invalid callback_data: {data}")
        return ConversationHandler.END

async def get_owner_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Receives the owner's email and prompts for password.
    """
    email = update.message.text.strip()
    if not email:
        await update.message.reply_text(
            "Email cannot be empty\\. Please enter your email address \\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return GET_OWNER_EMAIL

    context.user_data['owner_email'] = email
    await update.message.reply_text(
        "Thanks\\! Now, please send me your *password* \\.",
        parse_mode=ParseMode.MARKDOWN_V2
    ) 
    logger.info(f"Received owner email '{email}' from {update.effective_user.id}.")
    return GET_OWNER_PASSWORD

async def get_owner_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Receives the owner's password, sends credentials to Express server's user login endpoint, and handles response.
    """
    password = update.message.text.strip()
    if not password:
        await update.message.reply_text(
            f"Password cannot be empty\\. Please enter your *password* \\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return GET_OWNER_PASSWORD

    email = context.user_data.get('owner_email')
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id # This is Telegram's user ID

    if not email:
        await update.message.reply_text(
            "It seems I lost your email\\. Please restart the authentication process with /start\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return ConversationHandler.END

    logger.info(f"Attempting to authenticate owner {email} from {user_id} with Express server at {EXPRESS_USER_LOGIN_URL}.")

    try:
        async with ClientSession() as session:
            async with session.post(EXPRESS_USER_LOGIN_URL, json={'email': email, 'password': password}) as response:
                if response.status == 200:
                    data = await response.json()
                    user_info = data.get('user') # This user_info contains the Express backend user ID
                    token = data.get('token')
                    if user_info and user_info.get('id') and token:
                        # Store all necessary owner info in AUTHORIZED, indexed by Telegram user_id
                        AUTHORIZED[user_id] = {
                            "chat_id": chat_id, # Telegram chat ID
                            "user_id": user_info['id'], # Express backend User _id (MongoDB ObjectId as string)
                            "token": token, # JWT token for API calls to Express backend
                            "email": email # Owner's email
                        }
                        
                        escaped_email = escape_markdown(email, version=2)

                        await update.message.reply_text(
                            f"Authentication successful for owner `{escaped_email}`\\! ✅\n"
                            f"You are now registered to receive important notifications\\.",
                            parse_mode=ParseMode.MARKDOWN_V2
                        )
                        logger.info(f"Owner {email} (Telegram User ID: {user_id}, Express User ID: {user_info['id']}) successfully authenticated.")
                    else:
                        await update.message.reply_text(
                            "Authentication successful, but could not retrieve owner details or token\\. "
                            "Please contact support\\.",
                            parse_mode=ParseMode.MARKDOWN_V2
                        )
                        logger.warning(f"Owner {email} (Telegram User ID: {user_id}) authenticated but no user info/token in response.")
                    return ConversationHandler.END

                else:
                    error_data = await response.json()
                    error_message = error_data.get('message', 'Unknown error during login.')
                    escaped_error_message = escape_markdown(error_message, version=2)
                    await update.message.reply_text(
                        f"Authentication failed: {escaped_error_message}\n"
                        "Please check your credentials and try again, or /cancel\\.",
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
                    logger.warning(f"Owner {email} (Telegram User ID: {user_id}) login failed: {error_message} (Status: {response.status}).")
                    return GET_OWNER_PASSWORD

    except Exception as e:
        logger.exception(f"Error communicating with Express server for owner login from {user_id}: {e}")
        await update.message.reply_text(
            "An error occurred while trying to authenticate with the server\\. "
            "Please try again later or contact support\\. You can /cancel this process\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
    return ConversationHandler.END

async def get_staff_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Receives the staff's email and prompts for password.
    """
    email = update.message.text.strip()
    if not email:
        await update.message.reply_text(
            "Email cannot be empty\\. Please enter your email address \\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return GET_STAFF_EMAIL

    context.user_data['staff_email'] = email
    await update.message.reply_text(
        "Thanks\\! Now, please send me your password \\.",
        parse_mode=ParseMode.MARKDOWN_V2
    ) 
    logger.info(f"Received staff email '{email}' from {update.effective_user.id}.")
    return GET_STAFF_PASSWORD

async def get_staff_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Receives the staff's password, sends credentials to Express server, and handles response.
    """
    password = update.message.text.strip()
    if not password:
        await update.message.reply_text(
            f"Password cannot be empty\\. Please enter your *password* \\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return GET_STAFF_PASSWORD

    email = context.user_data.get('staff_email')
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if not email:
        await update.message.reply_text(
            "It seems I lost your email\\. Please restart the authentication process with /start\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return ConversationHandler.END

    logger.info(f"Attempting to authenticate staff {email} from {user_id} with Express server at {EXPRESS_STAFF_LOGIN_URL}.")

    try:
        async with ClientSession() as session:
            async with session.post(EXPRESS_STAFF_LOGIN_URL, json={'email': email, 'password': password}) as response:
                if response.status == 200:
                    data = await response.json()
                    staff_info = data.get('staff')
                    token = data.get('token')
                    if staff_info and staff_info.get('id') and token:
                        staff_id = staff_info.get('id')
                        website_id = staff_info.get('websiteId')
                        staff_name = staff_info.get('name', 'Staff Member')
                        
                        escaped_staff_name = escape_markdown(staff_name, version=2)
                        escaped_website_id = escape_markdown(str(website_id), version=2) 

                        AUTHENTICATED_STAFF_DETAILS[chat_id] = {
                            "staff_id": staff_id,
                            "email": email,
                            "website_id": website_id,
                            "name": staff_name,
                            "token": token
                        }
                        await update.message.reply_text(
                            f"Authentication successful for {escaped_staff_name}\\! ✅\n"
                            f"You are now linked to website ID: `{escaped_website_id}`\\.",
                            parse_mode=ParseMode.MARKDOWN_V2
                        )
                        logger.info(f"Staff {email} ({user_id}) successfully authenticated and linked to website {website_id}.")
                    else:
                        await update.message.reply_text(
                            "Authentication successful, but could not retrieve staff details or token\\. "
                            "Please contact support\\.",
                            parse_mode=ParseMode.MARKDOWN_V2
                        )
                        logger.warning(f"Staff {email} ({user_id}) authenticated but no staff info/token in response.")
                    return ConversationHandler.END
                else:
                    error_data = await response.json()
                    error_message = error_data.get('message', 'Unknown error during login.')
                    escaped_error_message = escape_markdown(error_message, version=2)
                    await update.message.reply_text(
                        f"Authentication failed: {escaped_error_message}\n"
                        "Please check your credentials and try again, or /cancel\\.",
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
                    logger.warning(f"Staff {email} ({user_id}) login failed: {error_message} (Status: {response.status}).")
                    return GET_STAFF_PASSWORD

    except Exception as e:
        logger.exception(f"Error communicating with Express server for staff login from {user_id}: {e}")
        await update.message.reply_text(
            "An error occurred while trying to authenticate with the server\\. "
            "Please try again later or contact support\\. You can /cancel this process\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Cancels the current conversation.
    """
    user_id = update.effective_user.id
    context.user_data.clear()
    await update.message.reply_text(
        "Authentication process cancelled\\. You can always /start again\\.",
        parse_mode=ParseMode.MARKDOWN_V2
    )
    logger.info(f"User {user_id} cancelled the authentication process.")
    return ConversationHandler.END


# --- Application Factory ---
def create_app():
    """
    Creates and configures the Flask app and the Telegram bot.
    This is the function that Gunicorn will call.
    """
    # Create and configure the Flask app
    app = Flask(__name__)

    # Create the PTB application instance
    ptb_app = Application.builder().token(BOT_TOKEN).build()

    # Define the conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            AUTH_CHOICE: [CallbackQueryHandler(button_callback_handler, pattern="^auth_owner$|^auth_staff$")],
            GET_OWNER_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_owner_email)],
            GET_OWNER_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_owner_password)],
            GET_STAFF_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_staff_email)],
            GET_STAFF_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_staff_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True, allow_reentry=True,
    )
    ptb_app.add_handler(conv_handler)
    
    # Set the webhook in an asyncio task
    async def setup_bot():
        try:
            full_webhook_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
            await ptb_app.bot.set_webhook(url=full_webhook_url, allowed_updates=Update.ALL_TYPES)
            logger.info(f"Telegram webhook set successfully to: {full_webhook_url}")
        except Exception as e:
            logger.error(f"Failed to set Telegram webhook: {e}")

    # The PTB app needs to be initialized before we can schedule tasks
    # The webhook setup runs in the background.
    loop = asyncio.get_event_loop()
    loop.run_until_complete(ptb_app.initialize())
    loop.create_task(setup_bot())

    # --- Flask Routes ---
    @app.route("/")
    def health_check():
        return "OK", 200

    @app.route(NOTIFY_WEBHOOK_PATH, methods=['POST'])
    async def handle_notify_route():
        data = await request.get_json()
        # ... logic to process notification and send messages with ptb_app.bot ...
        return {"status": "ok"}

    @app.route(WEBHOOK_PATH, methods=['POST'])
    async def telegram_webhook_handler_route():
        update_data = await request.get_json()
        update = Update.de_json(update_data, ptb_app.bot)
        await ptb_app.process_update(update)
        return Response(status=200)

    # Return the configured Flask app
    return app