FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project code
COPY . .

# AgentCore expects the app to serve on port 8080
EXPOSE 8080

# Default: full loop (runs all iterations inside the container)
CMD ["python", "deploy/full_loop.py"]
