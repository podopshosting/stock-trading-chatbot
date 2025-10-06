#!/bin/bash
# Deploy Lambda functions to AWS

set -e

echo "🚀 Deploying Stock Trading Chatbot Lambda Functions..."

# Configuration
REGION=${AWS_REGION:-us-east-2}
LAYER_NAME="stock-chatbot-dependencies"
RUNTIME="python3.9"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Create deployment packages directory
mkdir -p lambda-packages

echo -e "${BLUE}Step 1: Creating Lambda Layer with dependencies...${NC}"

# Create layer directory
mkdir -p lambda-layer/python

# Install dependencies to layer (use minimal requirements for Lambda)
pip3 install -r requirements-lambda.txt -t lambda-layer/python/ --upgrade --break-system-packages --no-cache-dir

# Create layer zip
cd lambda-layer
zip -r ../lambda-packages/dependencies-layer.zip python/ > /dev/null
cd ..

# Upload layer to AWS
echo "Uploading dependencies layer..."
LAYER_VERSION=$(aws lambda publish-layer-version \
    --layer-name $LAYER_NAME \
    --description "Dependencies for Stock Trading Chatbot" \
    --zip-file fileb://lambda-packages/dependencies-layer.zip \
    --compatible-runtimes $RUNTIME \
    --region $REGION \
    --query 'Version' \
    --output text)

LAYER_ARN="arn:aws:lambda:${REGION}:$(aws sts get-caller-identity --query Account --output text):layer:${LAYER_NAME}:${LAYER_VERSION}"

echo -e "${GREEN}✓ Layer created: $LAYER_ARN${NC}"

# Function to deploy a Lambda function
deploy_function() {
    local FUNCTION_NAME=$1
    local HANDLER_PATH=$2
    local HANDLER=$3
    local DESCRIPTION=$4

    echo -e "${BLUE}Step: Deploying $FUNCTION_NAME...${NC}"

    # Create deployment package
    cd $HANDLER_PATH
    zip -r ../../lambda-packages/${FUNCTION_NAME}.zip . -x "*.pyc" -x "__pycache__/*" > /dev/null
    cd - > /dev/null

    # Add shared modules to package
    cd shared
    zip -r ../lambda-packages/${FUNCTION_NAME}.zip . -x "*.pyc" -x "__pycache__/*" > /dev/null
    cd ..

    # Check if function exists
    if aws lambda get-function --function-name $FUNCTION_NAME --region $REGION > /dev/null 2>&1; then
        echo "Updating existing function..."
        aws lambda update-function-code \
            --function-name $FUNCTION_NAME \
            --zip-file fileb://lambda-packages/${FUNCTION_NAME}.zip \
            --region $REGION > /dev/null

        aws lambda update-function-configuration \
            --function-name $FUNCTION_NAME \
            --layers $LAYER_ARN \
            --region $REGION > /dev/null
    else
        echo "Creating new function..."

        # Get or create execution role
        ROLE_NAME="stock-chatbot-lambda-role"
        ROLE_ARN=$(aws iam get-role --role-name $ROLE_NAME --query 'Role.Arn' --output text 2>/dev/null || true)

        if [ -z "$ROLE_ARN" ]; then
            echo "Creating IAM role..."
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

            echo "Waiting for role to propagate..."
            sleep 10
        fi

        aws lambda create-function \
            --function-name $FUNCTION_NAME \
            --runtime $RUNTIME \
            --role $ROLE_ARN \
            --handler $HANDLER \
            --zip-file fileb://lambda-packages/${FUNCTION_NAME}.zip \
            --timeout 30 \
            --memory-size 512 \
            --environment "Variables={MARKETAUX_API_KEY=${MARKETAUX_API_KEY},DYNAMODB_TABLE_NAME=stock-chatbot-predictions,AWS_REGION=${REGION}}" \
            --layers $LAYER_ARN \
            --region $REGION > /dev/null
    fi

    echo -e "${GREEN}✓ $FUNCTION_NAME deployed${NC}"
}

# Deploy all Lambda functions
deploy_function "stock-chatbot-query" "lambda/chatbot" "handler.lambda_handler" "Chatbot query handler"
deploy_function "stock-chatbot-analysis" "lambda/stock_analysis" "handler.lambda_handler" "Stock analysis handler"
deploy_function "stock-chatbot-news" "lambda/news_analysis" "handler.lambda_handler" "News analysis handler"
deploy_function "stock-chatbot-tracking" "lambda/tracking" "handler.lambda_handler" "Prediction tracking handler"

# Clean up
echo -e "${BLUE}Cleaning up...${NC}"
rm -rf lambda-layer
# rm -rf lambda-packages  # Comment out to keep packages for debugging

echo -e "${GREEN}✅ All Lambda functions deployed successfully!${NC}"
echo ""
echo "Next steps:"
echo "1. Set up API Gateway using: ./scripts/deploy_api_gateway.sh"
echo "2. Configure environment variables in Lambda console"
echo "3. Test the functions"
