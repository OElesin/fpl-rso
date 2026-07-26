# FPL-RSO: Recursive Self-Optimizing Fantasy Premier League Agent

Inspired by [AIDE² (Weco AI)](https://www.weco.ai/blog/first-evidence-of-recursive-self-improvement), this system applies recursive self-improvement to Fantasy Premier League decision-making. Powered by [Strands Agents SDK](https://strandsagents.com) and [Amazon Bedrock](https://aws.amazon.com/bedrock/).

## Architecture

![Architecture](blog/svg/architecture.svg)

### Deployment Mode: Bedrock AgentCore Runtime

The Strands proposer agent can run locally or be deployed to [Amazon Bedrock AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html) for serverless, pay-per-use execution.

![AgentCore Deployment](blog/svg/agentcore-deploy.svg)

## Directory Structure

```
fpl-rso/
├── inner_agent/       # The FPL decision-making agent (strategy code)
│   ├── strategy.py    # Core decision logic (what the outer loop rewrites)
│   └── player.py      # Player evaluation utilities
├── outer_loop/        # Meta-optimizer that improves the inner agent
│   ├── optimizer.py   # Strands Agent-driven rewrite loop
│   └── proposer.py    # Multi-step reasoning agent with 6 tools
├── eval/              # Backtest engine
│   ├── backtest.py    # Simulates a season of FPL decisions
│   ├── scorer.py      # Computes public/private scores
│   └── constraints.py # Budget and rule enforcement
├── data/              # Data fetching and loading
│   ├── fetcher.py     # Downloads FPL historical data
│   └── loader.py      # Normalizes data into dataframes
├── deploy/            # Bedrock AgentCore Runtime deployment
│   ├── app.py         # BedrockAgentCoreApp entrypoint
│   ├── Dockerfile     # Container image for AgentCore
│   └── README.md      # Deployment instructions
├── blog/              # Technical blog post + animated SVGs
├── config/            # Configuration
│   └── settings.yaml  # Seasons, budgets, model, loop params
├── scripts/           # Entry points
│   └── run.py         # Main script to run the RSI loop
├── requirements.txt
└── README.md
```

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Fetch historical data
python scripts/run.py --fetch-data

# Evaluate the baseline agent
python scripts/run.py --eval-only

# Run the RSI loop locally (default: Claude Sonnet 5, 50 iterations)
python scripts/run.py

# Or deploy proposer to AgentCore and run remotely
python scripts/run.py --agentcore-arn arn:aws:bedrock:us-east-1:<account>:agent-runtime/fpl-rso-proposer
```

## How It Works

1. **Inner agent** starts with a hand-coded baseline strategy (form-weighted captain picks, simple transfer logic).
2. **Evaluation harness** backtests the agent across historical gameweeks, producing a score.
3. **Outer loop (Strands Agent)** uses multi-step reasoning with tools to propose modifications to the inner agent's strategy code.
4. If the modified agent scores higher on *private* gameweeks (ones it wasn't optimized on), the change is kept.
5. Repeat. Each iteration produces a potentially better agent.

### Two Execution Modes

| Mode | Command | Best for |
|------|---------|----------|
| **Local** | `python scripts/run.py` | Development, quick testing |
| **AgentCore** | `python scripts/run.py --agentcore-arn <arn>` | Production, unattended runs |

In both modes, the backtest runs locally (fast, needs data). Only the LLM reasoning step differs in where it executes.

## RSI Level Target

Following the AIDE² RSI ladder:
- **Level 0**: The system runs autonomously but improves slower than manual tuning.
- **Level 1**: The system improves itself faster than you could manually. ← *Target*

## Data Source

Uses the [vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League) dataset (free, complete historical data for every season since 2016-17).
