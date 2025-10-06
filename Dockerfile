# Dockerfile for Stock Trading Chatbot Web App

FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY shared/ ./shared/
COPY web/ ./web/

# Set environment variables
ENV FLASK_APP=web/app.py
ENV PYTHONUNBUFFERED=1

# Expose port
EXPOSE 5000

# Change to web directory
WORKDIR /app/web

# Run the application
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]
