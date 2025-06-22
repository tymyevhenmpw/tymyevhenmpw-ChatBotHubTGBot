import os
import logging
from flask import Flask

# --- Basic Logging ---
# This helps see the startup messages clearly.
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Flask App Initialization ---
app = Flask(__name__)

# --- Health Check Route ---
@app.route("/")
def health_check():
    """
    A simple health check endpoint.
    If you visit your app's public URL, you should see the text "OK".
    """
    logger.info("Health check endpoint was successfully hit!")
    return "OK", 200

# --- Main Execution Block ---
if __name__ == "__main__":
    # Get the port from the environment variable 'PORT' provided by Railway.
    # Default to 8080 if it's not set (for local testing).
    port = int(os.environ.get("PORT", 8080))
    
    logger.info(f"Starting minimal Flask server on 0.0.0.0:{port}")
    
    # Run the app. 
    # The host '0.0.0.0' is crucial to make it accessible within the container.
    # In a real production scenario, you would use a WSGI server like Gunicorn.
    # For example: gunicorn --bind 0.0.0.0:8080 app:app
    app.run(host="0.0.0.0", port=port)