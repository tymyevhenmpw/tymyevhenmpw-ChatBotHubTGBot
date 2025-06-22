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
# --- THIS IS THE FINAL CMD INSTRUCTION ---
#
# Use Gunicorn with the Uvicorn worker to run the app from the create_app factory.
# The quotes around "bot:create_app()" are important.
#
CMD exec gunicorn --bind 0.0.0.0:$PORT --workers 1 -k uvicorn.workers.UvicornWorker "bot:create_app()"