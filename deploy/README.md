# Deploying FPL-RSO to Bedrock AgentCore Runtime

## Option A: AgentCore CLI (Recommended)

```bash
# Install CLI
npm install -g @aws/agentcore

# Deploy
agentcore deploy

# Test
agentcore invoke --payload '{"current_code": "...", "iteration": 1}'
```

## Option B: Manual Deployment

### 1. Build and push Docker image

```bash
# From project root
docker build -f deploy/Dockerfile -t fpl-rso-proposer .

# Tag and push to ECR
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <account>.dkr.ecr.us-east-1.amazonaws.com
docker tag fpl-rso-proposer:latest <account>.dkr.ecr.us-east-1.amazonaws.com/fpl-rso-proposer:latest
docker push <account>.dkr.ecr.us-east-1.amazonaws.com/fpl-rso-proposer:latest
```

### 2. Create AgentCore Runtime

```python
import boto3

client = boto3.client('bedrock-agentcore-control', region_name='us-east-1')

response = client.create_agent_runtime(
    agentRuntimeName='fpl-rso-proposer',
    agentRuntimeArtifact={
        'containerConfiguration': {
            'containerUri': '<account>.dkr.ecr.us-east-1.amazonaws.com/fpl-rso-proposer:latest'
        }
    },
    networkConfiguration={"networkMode": "PUBLIC"},
    roleArn='arn:aws:iam::<account>:role/AgentCoreRuntimeRole'
)

print(response['agentRuntimeArn'])
```

### 3. Invoke remotely

```python
import boto3
import json

client = boto3.client('bedrock-agentcore')

response = client.invoke_agent_runtime(
    agentRuntimeArn='arn:aws:bedrock:us-east-1:<account>:agent-runtime/fpl-rso-proposer',
    runtimeSessionId='session-001',
    payload=json.dumps({
        "current_code": open("inner_agent/strategy.py").read(),
        "eval_result": {"public_score": 45.2, "private_score": 43.1, "total_score": 44.3},
        "failed_attempts": [],
        "iteration": 1,
        "max_iterations": 50
    }).encode()
)

result = json.loads(response['payload'].read())
print(result['candidate_code'][:200])
```

## Local Testing

```bash
# Run locally
python deploy/app.py

# Test with curl
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{
    "current_code": "def make_gameweek_decision(...): pass",
    "eval_result": {"public_score": 45.0, "private_score": 42.0, "total_score": 43.5},
    "failed_attempts": [],
    "iteration": 1,
    "max_iterations": 50
  }'
```

## Architecture

```
Local Machine                          AgentCore Runtime
┌─────────────────────┐               ┌─────────────────────────┐
│ scripts/run.py       │               │ deploy/app.py            │
│                      │  invoke       │                          │
│ • Load data          ├──────────────►│ Strands Agent:           │
│ • Run backtest       │               │  • read_strategy         │
│ • Selection logic    │◄──────────────┤  • read_eval_results     │
│ • State management   │  candidate    │  • validate_code         │
│                      │               │  • write_candidate       │
└─────────────────────┘               └─────────────────────────┘
```

The backtest stays local (fast, needs data). The LLM reasoning runs on AgentCore (serverless, scales, pay-per-use).
