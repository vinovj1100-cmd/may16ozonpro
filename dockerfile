# Use the same Python version from your logs
FROM python:3.14-slim

# Install system dependencies (zbar)
RUN apt-get update && apt-get install -y \
    libzbar0 \
    && rm -rf /var/lib/apt/lists/*

# Set up your project directory
WORKDIR /app
COPY . /app

# Install Python packages
RUN pip install --no-cache-dir -r requirements.txt

# Start your application (replace with your actual start command)
CMD ["gunicorn", "may16ozonpro.wsgi:application", "--bind", "0.0.0.0:10000"]
