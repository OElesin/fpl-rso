#!/usr/bin/env python3
"""
Set up the EventBridge + DynamoDB infrastructure for scheduled RSI iterations.

Creates:
1. DynamoDB table for run state
2. Lambda function (orchestrator)
3. EventBridge rule to trigger every 5 minutes
4. IAM role for Lambda

Usage:
    python deploy/setup_scheduler.py --iterations 50 --seasons 2023-24

This creates a self-running loop that:
- Triggers every 5 min via EventBridge
- Runs 1 iteration per trigger on AgentCore
- Stores state in DynamoDB between iterations
- Auto-stops when max iterations reached
"""

import argparse
import json
import sys
import time
import zipfile
import io
from pathlib import Path

import boto3

REGION = "us-east-1"
TABLE_NAME = "fpl-rso-state"
LAMBDA_NAME = "fpl-rso-orchestrator"
RULE_NAME = "fpl-rso-iteration"
ROLE_NAME = "FplRsoOrchestratorRole"
AGENTCORE_ARN = "arn:aws:bedrock-agentcore:us-east-1:514965996716:runtime/fplrso_loop-YvEJ6s29Vd"


def create_dynamodb_table():
    """Create the state table."""
    dynamodb = boto3.client("dynamodb", region_name=REGION)

    try:
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "run_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "run_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        print(f"  Created DynamoDB table: {TABLE_NAME}")
        waiter = dynamodb.get_waiter("table_exists")
        waiter.wait(TableName=TABLE_NAME)
    except dynamodb.exceptions.ResourceInUseException:
        print(f"  DynamoDB table already exists: {TABLE_NAME}")


def create_lambda_role():
    """Create IAM role for the orchestrator Lambda."""
    iam = boto3.client("iam")

    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    try:
        response = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="IAM role for FPL-RSO orchestrator Lambda",
        )
        role_arn = response["Role"]["Arn"]
        print(f"  Created IAM role: {ROLE_NAME}")
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = f"arn:aws:iam::{boto3.client('sts').get_caller_identity()['Account']}:role/{ROLE_NAME}"
        print(f"  IAM role already exists: {ROLE_NAME}")

    # Attach policies
    policies = [
        "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        "arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess",
        "arn:aws:iam::aws:policy/AmazonEventBridgeFullAccess",
        "arn:aws:iam::aws:policy/AmazonBedrockFullAccess",
    ]
    for policy in policies:
        try:
            iam.attach_role_policy(RoleName=ROLE_NAME, PolicyArn=policy)
        except Exception:
            pass

    time.sleep(10)  # Wait for IAM propagation
    return role_arn


def create_lambda(role_arn: str, config: dict):
    """Create/update the orchestrator Lambda function."""
    lambda_client = boto3.client("lambda", region_name=REGION)

    # Package the orchestrator code
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        orchestrator_path = Path(__file__).parent / "orchestrator.py"
        zf.write(orchestrator_path, "orchestrator.py")

    zip_buffer.seek(0)

    env_vars = {
        "STATE_TABLE": TABLE_NAME,
        "AGENTCORE_ARN": AGENTCORE_ARN,
        "AWS_REGION_NAME": REGION,
        "EVENTBRIDGE_RULE": RULE_NAME,
        "RUN_ID": config["run_id"],
        "MAX_ITERATIONS": str(config["max_iterations"]),
        "MODEL": config["model"],
        "SEASONS": json.dumps(config["seasons"]),
    }

    try:
        lambda_client.create_function(
            FunctionName=LAMBDA_NAME,
            Runtime="python3.12",
            Role=role_arn,
            Handler="orchestrator.handler",
            Code={"ZipFile": zip_buffer.read()},
            Timeout=900,  # 15 min max
            MemorySize=256,
            Environment={"Variables": env_vars},
        )
        print(f"  Created Lambda: {LAMBDA_NAME}")
    except lambda_client.exceptions.ResourceConflictException:
        # Update existing
        zip_buffer.seek(0)
        lambda_client.update_function_code(
            FunctionName=LAMBDA_NAME, ZipFile=zip_buffer.read()
        )
        lambda_client.update_function_configuration(
            FunctionName=LAMBDA_NAME,
            Timeout=900,
            Environment={"Variables": env_vars},
        )
        print(f"  Updated Lambda: {LAMBDA_NAME}")


def create_eventbridge_rule(run_id: str):
    """Create EventBridge rule to trigger every 5 minutes."""
    events = boto3.client("events", region_name=REGION)
    lambda_client = boto3.client("lambda", region_name=REGION)

    # Create rule (every 5 minutes)
    events.put_rule(
        Name=RULE_NAME,
        ScheduleExpression="rate(5 minutes)",
        State="ENABLED",
        Description="Triggers FPL-RSO iteration every 5 minutes",
    )
    print(f"  Created EventBridge rule: {RULE_NAME} (every 5 min)")

    # Get Lambda ARN
    func = lambda_client.get_function(FunctionName=LAMBDA_NAME)
    lambda_arn = func["Configuration"]["FunctionArn"]

    # Add target
    events.put_targets(
        Rule=RULE_NAME,
        Targets=[
            {
                "Id": "fpl-rso-orchestrator",
                "Arn": lambda_arn,
                "Input": json.dumps({"run_id": run_id}),
            }
        ],
    )

    # Add permission for EventBridge to invoke Lambda
    try:
        lambda_client.add_permission(
            FunctionName=LAMBDA_NAME,
            StatementId="eventbridge-invoke",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=f"arn:aws:events:{REGION}:{boto3.client('sts').get_caller_identity()['Account']}:rule/{RULE_NAME}",
        )
    except lambda_client.exceptions.ResourceConflictException:
        pass  # Permission already exists

    print(f"  EventBridge → Lambda trigger configured")


def main():
    parser = argparse.ArgumentParser(description="Set up scheduled RSI loop")
    parser.add_argument("--iterations", type=int, default=50, help="Max iterations")
    parser.add_argument("--model", type=str, default="claude-sonnet-5", help="Bedrock model")
    parser.add_argument("--seasons", nargs="+", default=["2023-24"], help="Seasons")
    parser.add_argument("--run-id", type=str, default="run-001", help="Run identifier")
    args = parser.parse_args()

    config = {
        "run_id": args.run_id,
        "max_iterations": args.iterations,
        "model": args.model,
        "seasons": args.seasons,
    }

    print(f"\nSetting up FPL-RSO Scheduled Loop")
    print(f"  Run ID: {config['run_id']}")
    print(f"  Iterations: {config['max_iterations']}")
    print(f"  Model: {config['model']}")
    print(f"  Seasons: {config['seasons']}")
    print(f"  AgentCore: {AGENTCORE_ARN}")
    print()

    print("1. DynamoDB table...")
    create_dynamodb_table()

    print("2. IAM role...")
    role_arn = create_lambda_role()

    print("3. Lambda function...")
    create_lambda(role_arn, config)

    print("4. EventBridge rule...")
    create_eventbridge_rule(config["run_id"])

    print(f"\n{'='*60}")
    print(f"  SCHEDULED LOOP ACTIVE")
    print(f"{'='*60}")
    print(f"  Every 5 minutes, EventBridge triggers the Lambda.")
    print(f"  Lambda invokes AgentCore for 1 iteration.")
    print(f"  State is persisted in DynamoDB between iterations.")
    print(f"  Loop auto-stops after {config['max_iterations']} iterations.")
    print(f"")
    print(f"  Monitor: aws dynamodb get-item --table-name {TABLE_NAME} --key '{{\"run_id\": {{\"S\": \"{config['run_id']}\"}}}}'")
    print(f"  Stop:    aws events disable-rule --name {RULE_NAME}")
    print(f"  Logs:    aws logs tail /aws/lambda/{LAMBDA_NAME} --follow")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
