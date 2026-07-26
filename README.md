# FPL-RSO: Recursive Self-Optimizing Fantasy Premier League Agent

Inspired by [AIDE² (Weco AI)](https://www.weco.ai/blog/first-evidence-of-recursive-self-improvement), this system applies recursive self-improvement to Fantasy Premier League decision-making.

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  OUTER LOOP                       │
│  (rewrites inner agent strategy code via LLM)    │
│                                                   │
│   ┌───────────────────────────────────────────┐  │
│   │              INNER AGENT                   │  │
│   │  (makes FPL decisions each gameweek)       │  │
│   │                                            │  │
│   │  • Transfer selection                      │  │
│   │  • Captain pick                            │  │
│   │  • Lineup / bench order                    │  │
│   │  • Chip timing (WC, BB, TC, FH)           │  │
│   └───────────────────────────────────────────┘  │
│                                                   │
│   ┌───────────────────────────────────────────┐  │
│   │           EVALUATION HARNESS               │  │
│   │  (backtests inner agent over seasons)      │  │
│   │                                            │  │
│   │  • Public score (visible to inner agent)   │  │
│   │  • Private score (selection signal)        │  │
│   │  • Cost budget constraint                  │  │
│   └───────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
```

## Directory Structure

```
fpl-rso/
├── inner_agent/       # The FPL decision-making agent (strategy code)
│   ├── strategy.py    # Core decision logic (what the outer loop rewrites)
│   └── player.py      # Player evaluation utilities
├── outer_loop/        # Meta-optimizer that improves the inner agent
│   ├── optimizer.py   # LLM-driven rewrite loop
│   └── proposer.py    # Generates candidate rewrites
├── data/              # Data fetching and loading
│   ├── fetcher.py     # Downloads FPL historical data
│   └── loader.py      # Normalizes data into dataframes
├── eval/              # Backtest engine
│   ├── backtest.py    # Simulates a season of FPL decisions
│   ├── scorer.py      # Computes public/private scores
│   └── constraints.py # Budget and rule enforcement
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
python -m data.fetcher

# Run the RSI loop
python scripts/run.py
```

## How It Works

1. **Inner agent** starts with a hand-coded baseline strategy (form-weighted captain picks, simple transfer logic).
2. **Evaluation harness** backtests the agent across historical gameweeks, producing a score.
3. **Outer loop** uses an LLM to propose modifications to the inner agent's strategy code.
4. If the modified agent scores higher on *private* gameweeks (ones it wasn't optimized on), the change is kept.
5. Repeat. Each iteration produces a potentially better agent.

## RSI Level Target

Following the AIDE² RSI ladder:
- **Level 0**: The system runs autonomously but improves slower than manual tuning.
- **Level 1**: The system improves itself faster than you could manually. ← *Target*

## Data Source

Uses the [vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League) dataset (free, complete historical data for every season since 2016-17).
