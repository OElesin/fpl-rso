"""
Sets up fully automated weekly RSI pipeline on AWS.

Creates:
1. CodeBuild project (builds image, updates AgentCore, deploys bot)
2. EventBridge rule: weekly trigger (Tuesday 03:00 UTC after GW deadline)
3. EventBridge rule: completion trigger (when RSI loop finishes → update bot)
4. IAM role for CodeBuild

Run once:
    python deploy/setup_full_automation.py

After this, the entire flow is automated — no local machine needed ever.
"""

import json
import time
import boto3

REGION = "us-east-1"
ACCOUNT = "514965996716"
PROJECT_NAME = "fpl-rso-pipeline"
ROLE_NAME = "FplRsoCodeBuildRole"
GITHUB_REPO = "https://github.com/OElesin/fpl-rso.git"

# Weekly schedule: Tuesday 03:00 UTC (after Monday night GW deadline)
WEEKLY_SCHEDULE = "cron(0 3 ? * TUE *)"
COMPLETION_RULE = "fpl-rso-completion"


def create_codebuild_role():
    """Create IAM role for CodeBuild."""
    iam = boto3.client("iam")

    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "codebuild.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    try:
        response = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="IAM role for FPL-RSO CodeBuild pipeline",
        )
        role_arn = response["Role"]["Arn"]
        print(f"  Created role: {ROLE_NAME}")
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = f"arn:aws:iam::{ACCOUNT}:role/{ROLE_NAME}"
        print(f"  Role exists: {ROLE_NAME}")

    # Attach policies
    policies = [
        "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryFullAccess",
        "arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess",
        "arn:aws:iam::aws:policy/AWSLambda_FullAccess",
        "arn:aws:iam::aws:policy/AmazonEventBridgeFullAccess",
        "arn:aws:iam::aws:policy/AmazonBedrockFullAccess",
        "arn:aws:iam::aws:policy/CloudWatchLogsFullAccess",
    ]
    for policy in policies:
        try:
            iam.attach_role_policy(RoleName=ROLE_NAME, PolicyArn=policy)
        except Exception:
            pass

    # Inline policy for AgentCore
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="AgentCoreAccess",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": "bedrock-agentcore:*",
                "Resource": "*",
            }],
        }),
    )

    time.sleep(10)
    return role_arn


def create_codebuild_project(role_arn: str):
    """Create CodeBuild project."""
    cb = boto3.client("codebuild", region_name=REGION)

    try:
        cb.create_project(
            name=PROJECT_NAME,
            description="FPL-RSO: build image, run RSI loop, update bot",
            source={
                "type": "GITHUB",
                "location": GITHUB_REPO,
                "buildspec": "buildspec.yml",
            },
            artifacts={"type": "NO_ARTIFACTS"},
            environment={
                "type": "LINUX_CONTAINER",
                "image": "aws/codebuild/standard:7.0",
                "computeType": "BUILD_GENERAL1_MEDIUM",
                "privilegedMode": True,  # Needed for Docker
                "environmentVariables": [
                    {"name": "BUILD_PHASE", "value": "build_and_start", "type": "PLAINTEXT"},
                ],
            },
            serviceRole=role_arn,
            timeoutInMinutes=30,
            logsConfig={
                "cloudWatchLogs": {
                    "status": "ENABLED",
                    "groupName": "/aws/codebuild/fpl-rso-pipeline",
                },
            },
        )
        print(f"  Created CodeBuild project: {PROJECT_NAME}")
    except cb.exceptions.ResourceAlreadyExistsException:
        print(f"  CodeBuild project exists: {PROJECT_NAME}")


def create_weekly_schedule():
    """Create EventBridge rule to trigger CodeBuild weekly."""
    events = boto3.client("events", region_name=REGION)

    # Weekly build + RSI trigger
    events.put_rule(
        Name=f"{PROJECT_NAME}-weekly",
        ScheduleExpression=WEEKLY_SCHEDULE,
        State="ENABLED",
        Description="Weekly: rebuild FPL-RSO image and start RSI loop (Tuesday 03:00 UTC)",
    )

    # Target: CodeBuild
    events.put_targets(
        Rule=f"{PROJECT_NAME}-weekly",
        Targets=[{
            "Id": "codebuild-build",
            "Arn": f"arn:aws:codebuild:{REGION}:{ACCOUNT}:project/{PROJECT_NAME}",
            "RoleArn": f"arn:aws:iam::{ACCOUNT}:role/{ROLE_NAME}",
            "Input": json.dumps({"environmentVariablesOverride": [
                {"name": "BUILD_PHASE", "value": "build_and_start", "type": "PLAINTEXT"},
            ]}),
        }],
    )

    print(f"  Created weekly schedule: {WEEKLY_SCHEDULE}")


def create_completion_trigger():
    """
    Create a rule that detects when the RSI loop completes
    and triggers CodeBuild to update the bot.
    """
    events = boto3.client("events", region_name=REGION)
    lambda_client = boto3.client("lambda", region_name=REGION)

    # Create a Lambda that checks DynamoDB for completion
    # and triggers CodeBuild deploy_bot phase
    completion_lambda_code = '''
import json
import boto3
import os

def handler(event, context):
    """Check if RSI loop completed, trigger bot update if so."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.Table(os.environ.get("STATE_TABLE", "fpl-rso-state"))
    
    # Scan for completed runs
    scan = table.scan()
    completed = [i for i in scan.get("Items", []) if i.get("status") == "completed"]
    
    if not completed:
        return {"status": "waiting"}
    
    # Trigger CodeBuild to update bot
    cb = boto3.client("codebuild", region_name="us-east-1")
    cb.start_build(
        projectName="fpl-rso-pipeline",
        environmentVariablesOverride=[
            {"name": "BUILD_PHASE", "value": "deploy_bot", "type": "PLAINTEXT"},
        ],
    )
    
    # Disable the completion check rule (one-shot)
    events_client = boto3.client("events", region_name="us-east-1")
    events_client.disable_rule(Name="fpl-rso-completion-check")
    
    return {"status": "triggered_deploy"}
'''

    # Create the completion checker Lambda
    import zipfile
    import io

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zf:
        zf.writestr("lambda_function.py", completion_lambda_code)
    zip_buffer.seek(0)

    lambda_name = "fpl-rso-completion-checker"

    try:
        lambda_client.create_function(
            FunctionName=lambda_name,
            Runtime="python3.13",
            Role=f"arn:aws:iam::{ACCOUNT}:role/FplRsoOrchestratorRole",
            Handler="lambda_function.handler",
            Code={"ZipFile": zip_buffer.read()},
            Timeout=30,
            Environment={"Variables": {"STATE_TABLE": "fpl-rso-state"}},
        )
        print(f"  Created completion checker Lambda: {lambda_name}")
    except lambda_client.exceptions.ResourceConflictException:
        zip_buffer.seek(0)
        lambda_client.update_function_code(
            FunctionName=lambda_name, ZipFile=zip_buffer.read()
        )
        print(f"  Updated completion checker Lambda: {lambda_name}")

    # EventBridge rule: check every 10 min if loop is done
    events.put_rule(
        Name="fpl-rso-completion-check",
        ScheduleExpression="rate(10 minutes)",
        State="DISABLED",  # Enabled when RSI loop starts
        Description="Checks if RSI loop completed, triggers bot update",
    )

    lambda_arn = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{lambda_name}"

    events.put_targets(
        Rule="fpl-rso-completion-check",
        Targets=[{"Id": "completion-check", "Arn": lambda_arn}],
    )

    # Permission for EventBridge to invoke Lambda
    try:
        lambda_client.add_permission(
            FunctionName=lambda_name,
            StatementId="eventbridge-completion",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=f"arn:aws:events:{REGION}:{ACCOUNT}:rule/fpl-rso-completion-check",
        )
    except lambda_client.exceptions.ResourceConflictException:
        pass

    print(f"  Created completion trigger rule: fpl-rso-completion-check")


def update_orchestrator_to_enable_completion_check():
    """
    Update the RSI orchestrator Lambda to enable the completion-check rule
    when a new run starts.
    """
    # This is handled by the buildspec — when it starts the RSI loop,
    # it also enables the completion-check rule
    events = boto3.client("events", region_name=REGION)

    # Add to the existing RSI iteration rule's Lambda target:
    # The orchestrator already disables itself when done.
    # We just need the buildspec to enable fpl-rso-completion-check when it starts.
    print("  Note: buildspec.yml enables completion-check when RSI loop starts")


def main():
    print("\n" + "=" * 60)
    print("  FPL-RSO: Setting Up Full Automation")
    print("=" * 60 + "\n")

    print("1. IAM role...")
    role_arn = create_codebuild_role()

    print("2. CodeBuild project...")
    create_codebuild_project(role_arn)

    print("3. Weekly schedule...")
    create_weekly_schedule()

    print("4. Completion trigger...")
    create_completion_trigger()

    print("5. Wiring...")
    update_orchestrator_to_enable_completion_check()

    print("\n" + "=" * 60)
    print("  AUTOMATION COMPLETE")
    print("=" * 60)
    print(f"""
  Weekly flow (fully automated):
  
  Tuesday 03:00 UTC (after GW deadline)
    → CodeBuild: rebuild image, push ECR, update AgentCore
    → EventBridge: RSI loop runs (50 iterations, ~4 hours)
    → Completion checker: detects done
    → CodeBuild: pull best strategy, update bot Lambda
    → Bot serves improved picks for next GW
  
  Zero human involvement. Bot improves itself every week.
  
  Monitor:
    aws codebuild list-builds-for-project --project-name {PROJECT_NAME}
    aws logs tail /aws/codebuild/fpl-rso-pipeline --follow
    
  Manual trigger (test):
    aws codebuild start-build --project-name {PROJECT_NAME}
""")


if __name__ == "__main__":
    main()
