# Deploying FPL-RSO to Bedrock AgentCore Runtime

## Two Deployment Modes

| Mode | Entry Point | What Runs Remotely | Use Case |
|------|-------------|-------------------|----------|
| **Full Loop** | `deploy/full_loop.py` | Everything (data fetch, backtest, propose, select) | Fire-and-forget, no local machine needed |
| **Proposer Only** | `deploy/app.py` | Just the LLM reasoning step | Local backtest, remote LLM |

## Full Loop Mode (Recommended)

Run the entire RSI optimization on AgentCore — zero local execution.

### Deploy

```bash
# Install CLI
npm install -g @aws/agentcore

# Deploy (uses full_loop.py by default)
agentcore deploy
```

### Invoke (fire and forget)

```bash
# From your laptop, phone, anywhere — just triggers the cloud run
python scripts/run_remote.py --arn <your-agentcore-arn> --iterations 50

# With custom settings
python scripts/run_remote.py --arn <arn> --iterations 100 --model claude-sonnet-5 --seasons 2022-23 2023-24

# Auto-install the best strategy when done
python scripts/run_remote.py --arn <arn> --install
```

### Or invoke directly with boto3

```python
import boto3
import json

client = boto3.client('bedrock-agentcore', region_name='us-east-1')

response = client.invoke_agent_runtime(
    agentRuntimeArn='arn:aws:bedrock:us-east-1:<account>:agent-runtime/fpl-rso',
    runtimeSessionId='overnight-run-001',
    payload=json.dumps({
        "iterations": 50,
        "seasons": ["2022-23", "2023-24"],
        "model": "claude-sonnet-5"
    }).encode()
)

result = json.loads(response['payload'].read())
print(f"Best score: {result['best_score']:.2f} (+{result['improvement']:.2f})")
print(result['best_strategy'][:200])  # Preview the discovered code
```

## Proposer-Only Mode

If you want to run the backtest locally but offload just the LLM reasoning:

```bash
# Deploy proposer-only
docker build -f deploy/Dockerfile --build-arg ENTRYPOINT=deploy/app.py -t fpl-rso-proposer .

# Or override CMD
agentcore deploy --cmd "python deploy/app.py"

# Then run locally with remote proposer
python scripts/run.py --agentcore-arn <proposer-arn> --iterations 50
```

## Manual Deployment (boto3)

```bash
# Build and push to ECR
docker build -f deploy/Dockerfile -t fpl-rso .
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <account>.dkr.ecr.us-east-1.amazonaws.com
docker tag fpl-rso:latest <account>.dkr.ecr.us-east-1.amazonaws.com/fpl-rso:latest
docker push <account>.dkr.ecr.us-east-1.amazonaws.com/fpl-rso:latest
```

```python
import boto3

client = boto3.client('bedrock-agentcore-control', region_name='us-east-1')

response = client.create_agent_runtime(
    agentRuntimeName='fpl-rso',
    agentRuntimeArtifact={
        'containerConfiguration': {
            'containerUri': '<account>.dkr.ecr.us-east-1.amazonaws.com/fpl-rso:latest'
        }
    },
    networkConfiguration={"networkMode": "PUBLIC"},
    roleArn='arn:aws:iam::<account>:role/AgentCoreRuntimeRole'
)
print(response['agentRuntimeArn'])
```

## Local Testing

```bash
# Test full loop locally
python deploy/full_loop.py &
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"iterations": 2, "seasons": ["2023-24"], "model": "claude-sonnet-5"}'

# Test proposer only
python deploy/app.py &
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"current_code": "...", "eval_result": {"public_score": 5.6, "private_score": 4.1}}'
```

## Architecture

```
You (laptop/phone)                     Bedrock AgentCore Runtime
┌─────────────────────┐               ┌──────────────────────────────┐
│                      │  one API call │                              │
│ run_remote.py        ├──────────────►│  full_loop.py                │
│                      │               │                              │
│ "Run 50 iterations   │               │  1. Fetch FPL data (GitHub)  │
│  on 2023-24 season   │               │  2. Evaluate baseline        │
│  with Sonnet 5"      │               │  3. For each iteration:      │
│                      │               │     • Strands Agent proposes  │
│                      │  results      │     • Backtest evaluates      │
│                      │◄──────────────┤     • Keep if improved        │
│ Save best strategy   │               │  4. Return best strategy     │
│                      │               │                              │
└─────────────────────┘               └──────────────────────────────┘
                                       Session isolated (microVM)
                                       Pay only for runtime
                                       Scales to zero when idle
```
