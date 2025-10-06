#!/bin/bash
# Store API keys in AWS Secrets Manager

set -e

REGION=${AWS_REGION:-us-east-2}
SECRET_NAME="stock-chatbot/api-keys"

echo "🔐 Storing API keys in AWS Secrets Manager..."

# Create or update secret
aws secretsmanager create-secret \
    --name "$SECRET_NAME" \
    --description "API keys for Stock Trading Chatbot" \
    --secret-string "{\"OPENAI_API_KEY\":\"$OPENAI_API_KEY\",\"MARKETAUX_API_KEY\":\"$MARKETAUX_API_KEY\"}" \
    --region "$REGION" 2>/dev/null || \
aws secretsmanager update-secret \
    --secret-id "$SECRET_NAME" \
    --secret-string "{\"OPENAI_API_KEY\":\"$OPENAI_API_KEY\",\"MARKETAUX_API_KEY\":\"$MARKETAUX_API_KEY\"}" \
    --region "$REGION"

echo "✅ Secrets stored successfully!"
echo "Secret ARN: arn:aws:secretsmanager:$REGION:$(aws sts get-caller-identity --query Account --output text):secret:$SECRET_NAME"
