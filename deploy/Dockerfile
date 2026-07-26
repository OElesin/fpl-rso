FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir bedrock-agentcore

# Copy project code
COPY . .

# AgentCore expects the app to serve on port 8080
EXPOSE 8080

# Entry point
CMD ["python", "deploy/app.py"]
