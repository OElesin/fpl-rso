FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir aws-opentelemetry-distro>=0.10.1

# Copy project code
COPY . .

# AgentCore expects the app to serve on port 8080
EXPOSE 8080

# Run with OpenTelemetry auto-instrumentation for AgentCore observability
CMD ["python", "deploy/full_loop.py"]
