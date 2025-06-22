import os
import logging
import json
import asyncio
from flask import Flask, request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown
from aiohttp import ClientSession # Still using aiohttp for outgoing requests

# --- Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Flask App Initialization ---
app = Flask(__name__)

# --- Environment ---
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://xxxx.up.railway.app
LISTEN_PORT = int(os.getenv("PORT", "8080")) # Keep 8080 for Railway compatibility
WEBHOOK_PATH = "/telegram/webhook" # Path for Telegram updates
NOTIFY_WEBHOOK_PATH = "/notify" # Your custom notify endpoint

EXPRESS_STAFF_LOGIN_URL = os.getenv("EXPRESS_STAFF_LOGIN_URL", "http://host.docker.internal:3001/api/staff/login")
EXPRESS_USER_LOGIN_URL = os.getenv("EXPRESS_USER_LOGIN_URL", "http://host.docker.internal:3001/api/users/login")
EXPRESS_USER_PROFILE_URL_BASE = os.getenv("EXPRESS_USER_PROFILE_URL_BASE", "http://host.docker.internal:3001/api/users")

# --- Conversation States ---
AUTH_CHOICE, GET_STAFF_EMAIL, GET_STAFF_PASSWORD, GET_OWNER_EMAIL, GET_OWNER_PASSWORD = range(5)

# --- Data Stores (Global) ---
AUTHORIZED = {}
AUTHENTICATED_STAFF_DETAILS = {}
ptb_app: Application = None # Global variable to hold the PTB application instance

# --- Environment Variable Checks ---
if not BOT_TOKEN:
    logger.error("TELEGRAM_TOKEN environment variable not set. Exiting.")
    exit(1)
if not WEBHOOK_URL:
    logger.error("WEBHOOK_URL environment variable not set. Exiting.")
    exit(1)
if not os.getenv("EXPRESS_STAFF_LOGIN_URL"):
    logger.warning("EXPRESS_STAFF_LOGIN_URL environment variable not set. Using default: %s", EXPRESS_STAFF_LOGIN_URL)
if not os.getenv("EXPRESS_USER_LOGIN_URL"):
    logger.warning("EXPRESS_USER_LOGIN_URL environment variable not set. Using default: %s", EXPRESS_USER_LOGIN_URL)
if not os.getenv("EXPRESS_USER_PROFILE_URL_BASE"):
    logger.warning("EXPRESS_USER_PROFILE_URL_BASE environment variable not set. Using default: %s", EXPRESS_USER_PROFILE_URL_BASE)

# --- Handlers for Telegram Bot (Identical to original) ---
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
        logger.info(f"User {user_id} ({query.from_user.full_name}) chose owner authentication.")
        return GET_OWNER_EMAIL
    
    elif data == "auth_staff":
        await query.edit_message_text(
            f"Okay, let's authenticate you as staff\\. Please send me your *email address* \\.",
            parse_mode=ParseMode.MARKDOWN_V2
        ) 
        logger.info(f"User {user_id} ({query.from_user.full_name}) chose staff authentication.")
        return GET_STAFF_EMAIL
    
    else:
        await query.edit_message_text(
            "Invalid authentication option selected\\. Please use /start again\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        ) 
        logger.warning(f"User {user_id} ({query.from_user.full_name}) selected invalid callback_data: {data}")
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

# --- Flask Web Server Routes ---

@app.route("/")
def health_check():
    """
    A simple health check endpoint that hosting platforms can use.
    Responds with a 200 OK status to indicate the app is live.
    """
    logger.info("Health check endpoint was hit.")
    return "OK", 200
    
@app.route(NOTIFY_WEBHOOK_PATH, methods=['POST'])
def handle_notify_route():
    """
    Flask route to handle incoming POST requests to the /notify endpoint.
    Sends messages to authorized owners and staff based on the request payload.
    """
    try:
        data = request.get_json()
        logger.info(f"Received notification request: {data}")
    except Exception as e:
        logger.error(f"Invalid JSON in notify request: {e}")
        return {"status": "error", "message": "Invalid JSON"}, 400

    message_text = data.get("message")
    websiteId = data.get("websiteId", "N/A")
    notify_owner = data.get("notifyOwner", False)
    notify_all_staff = data.get("notifyAllStaff", False)
    owner_id_from_payload = data.get("ownerId")

    if not message_text:
        logger.warning("Notification request missing 'message' field.")
        return {"status": "error", "message": "Missing 'message' in payload"}, 400

    asyncio.run(process_notification(
        message_text,
        websiteId,
        notify_owner,
        notify_all_staff,
        owner_id_from_payload
    ))

    return {"status": "ok", "message": "Notification processed"}

async def process_notification(message_text, websiteId, notify_owner, notify_all_staff, owner_id_from_payload):
    """
    Asynchronous helper to send notifications.
    """
    global ptb_app
    escaped_message_text = escape_markdown(message_text, version=2)
    escaped_websiteId = escape_markdown(str(websiteId), version=2)
    full_message = f"💬 New message on website ID: `{escaped_websiteId}`\n`{escaped_message_text}`"

    if notify_owner and owner_id_from_payload:
        owner_chat_id_to_notify = None
        for telegram_user_id, owner_info in AUTHORIZED.items():
            if owner_info.get("user_id") == owner_id_from_payload:
                owner_chat_id_to_notify = owner_info.get("chat_id")
                break
        
        if owner_chat_id_to_notify:
            try:
                await ptb_app.bot.send_message(owner_chat_id_to_notify, full_message, parse_mode=ParseMode.MARKDOWN_V2)
                logger.info(f"Message sent to owner (Express User ID: {owner_id_from_payload})")
            except Exception as e:
                logger.error(f"Error sending message to owner {owner_id_from_payload}: {e}")
        else:
            logger.warning(f"Notify owner requested, but no matching owner found for Express User ID: {owner_id_from_payload}")

    if notify_all_staff:
        if not AUTHENTICATED_STAFF_DETAILS:
            logger.warning("Notify all staff requested, but no staff are authenticated.")
        
        for chat_id, staff_info in AUTHENTICATED_STAFF_DETAILS.items():
            if str(staff_info.get("website_id")) == str(websiteId):
                try:
                    await ptb_app.bot.send_message(chat_id, full_message, parse_mode=ParseMode.MARKDOWN_V2)
                    logger.info(f"Message sent to staff '{staff_info.get('email')}' for website {websiteId}.")
                except Exception as e:
                    logger.error(f"Error sending message to staff {staff_info.get('email')}: {e}")

@app.route(WEBHOOK_PATH, methods=['POST'])
def telegram_webhook_handler_route():
    """
    Flask route that handles incoming Telegram updates.
    This function processes the update and passes it to the PTB application.
    """
    global ptb_app
    try:
        update_data = request.get_json(force=True)
        logger.debug(f"Received Telegram webhook update: {update_data}")
        update = Update.de_json(update_data, ptb_app.bot)

        # Process the update asynchronously
        asyncio.run(ptb_app.process_update(update))

        return Response(status=200)
    except json.JSONDecodeError:
        logger.error("Failed to decode JSON from Telegram webhook request.")
        return Response("Invalid JSON", status=400)
    except Exception as e:
        logger.exception(f"Error processing Telegram webhook: {e}")
        return Response("Internal Server Error", status=500)

# --- Main application setup and execution ---
if __name__ == "__main__":
    # This block runs only when the script is executed directly
    
    # Initialize the PTB Application
    logger.info("Initializing Telegram Application...")
    ptb_app = Application.builder().token(BOT_TOKEN).read_timeout(7).write_timeout(7).build()
    
    # Define the conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            AUTH_CHOICE: [
                CallbackQueryHandler(button_callback_handler, pattern="^auth_owner$|^auth_staff$")
            ],
            GET_OWNER_EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_owner_email)
            ],
            GET_OWNER_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_owner_password)
            ],
            GET_STAFF_EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_staff_email)
            ],
            GET_STAFF_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_staff_password)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        allow_reentry=True,
    )
    
    # Add handlers to the application
    ptb_app.add_handler(conv_handler)
    
    # Set the Telegram webhook
    # This is run in a temporary event loop before the Flask app starts.
    try:
        full_webhook_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
        logger.info(f"Setting Telegram webhook to: {full_webhook_url}")
        asyncio.run(ptb_app.bot.set_webhook(url=full_webhook_url, allowed_updates=Update.ALL_TYPES))
        logger.info("Telegram webhook set successfully.")
    except Exception as e:
        logger.error(f"Failed to set Telegram webhook: {e}. Ensure WEBHOOK_URL is reachable from the internet.")
        exit(1) # Exit if webhook setup fails, as the bot will not function.

    # Start the Flask web server
    logger.info(f"Starting Flask server on 0.0.0.0:{LISTEN_PORT}")
    logger.info(f"Custom HTTP endpoint will be at: {NOTIFY_WEBHOOK_PATH}")
    logger.info(f"Telegram webhook endpoint is at: {WEBHOOK_PATH}")
    
    # Use a production-ready WSGI server like gunicorn or waitress in production
    # For example: gunicorn --bind 0.0.0.0:8080 flask_bot:app
    app.run(host="0.0.0.0", port=LISTEN_PORT)