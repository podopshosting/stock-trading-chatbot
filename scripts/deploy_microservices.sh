#!/bin/bash
# Deploy lightweight Lambda microservices

set -e

echo "🚀 Deploying Stock Trading Chatbot Microservices..."

REGION=${AWS_REGION:-us-east-2}
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

# Create deployment directory
mkdir -p deployments

# Function to deploy a microservice
deploy_microservice() {
    local SERVICE_NAME=$1
    local SERVICE_DIR=$2
    local DESCRIPTION=$3

    echo -e "${BLUE}Deploying: $SERVICE_NAME${NC}"

    cd "lambda-micro/$SERVICE_DIR"

    # Create deployment package
    mkdir -p ../../deployments
    zip -r ../../deployments/${SERVICE_NAME}.zip handler.py > /dev/null 2>&1

    # Install dependencies if requirements.txt exists and has content
    if [ -f requirements.txt ] && grep -v '^#' requirements.txt | grep -v '^$' > /dev/null 2>&1; then
        echo "  Installing dependencies..."
        mkdir -p package
        pip3 install -r requirements.txt -t package/ --quiet --break-system-packages 2>/dev/null || pip3 install -r requirements.txt -t package/ --quiet
        if [ -d package ] && [ "$(ls -A package)" ]; then
            cd package && zip -r ../../../deployments/${SERVICE_NAME}.zip . > /dev/null 2>&1 && cd ..
        fi
        rm -rf package
    fi

    cd ../..

    # Check if function exists
    if aws lambda get-function --function-name $SERVICE_NAME --region $REGION > /dev/null 2>&1; then
        echo "  Updating existing function..."
        aws lambda update-function-code \
            --function-name $SERVICE_NAME \
            --zip-file fileb://deployments/${SERVICE_NAME}.zip \
            --region $REGION > /dev/null
    else
        echo "  Creating new function..."

        # Get or create IAM role
        ROLE_NAME="stock-chatbot-lambda-role"
        ROLE_ARN=$(aws iam get-role --role-name $ROLE_NAME --query 'Role.Arn' --output text 2>/dev/null || echo "")

        if [ -z "$ROLE_ARN" ]; then
            echo "  Creating IAM role..."
            TRUST_POLICY='{
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": "sts:AssumeRole"
                }]
            }'

            ROLE_ARN=$(aws iam create-role \
                --role-name $ROLE_NAME \
                --assume-role-policy-document "$TRUST_POLICY" \
                --query 'Role.Arn' \
                --output text)

            # Attach policies
            aws iam attach-role-policy \
                --role-name $ROLE_NAME \
                --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

            aws iam attach-role-policy \
                --role-name $ROLE_NAME \
                --policy-arn arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess

            aws iam attach-role-policy \
                --role-name $ROLE_NAME \
                --policy-arn arn:aws:iam::aws:policy/SecretsManagerReadWrite

            echo "  Waiting for role to propagate..."
            sleep 10
        fi

        # Create function
        aws lambda create-function \
            --function-name $SERVICE_NAME \
            --runtime python3.12 \
            --role $ROLE_ARN \
            --handler handler.lambda_handler \
            --zip-file fileb://deployments/${SERVICE_NAME}.zip \
            --timeout 30 \
            --memory-size 256 \
            --environment "Variables={DYNAMODB_TABLE_NAME=stock-chatbot-predictions}" \
            --region $REGION > /dev/null
    fi

    echo -e "${GREEN}✓ $SERVICE_NAME deployed${NC}"
}

# Deploy each microservice
deploy_microservice "stock-chatbot-router" "chatbot-router" "Chatbot router with OpenAI"
deploy_microservice "stock-data-service" "stock-data" "Stock data fetcher"
deploy_microservice "stock-news-service" "news-fetcher" "News fetcher"
deploy_microservice "stock-prediction-service" "prediction-tracker" "Prediction tracker"

echo -e "${GREEN}✅ All microservices deployed!${NC}"
echo ""
echo "Functions deployed:"
echo "  • stock-chatbot-router"
echo "  • stock-data-service"
echo "  • stock-news-service"
echo "  • stock-prediction-service"
echo ""
echo "Next: Configure API Gateway"
echo "  ./scripts/deploy_api_gateway_micro.sh"
