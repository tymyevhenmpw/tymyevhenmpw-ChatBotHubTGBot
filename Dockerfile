# Use a stable and slim Python base image
FROM python:3.11-slim

# Set an environment variable for the port, with 8080 as a default
ENV PORT 8080

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the bot's source code into the container
COPY bot.py .

#
# --- THIS IS THE CRITICAL CHANGE ---
#
# Use Gunicorn to run the application.
# This CMD instruction uses the shell form to properly substitute the $PORT variable
# provided by the Railway environment. 'exec' ensures Gunicorn runs as the main process.
#
CMD exec gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 0 bot:app