#!/bin/bash
# Set up API Gateway for Lambda functions

set -e

echo "🚀 Setting up API Gateway for Stock Trading Chatbot..."

# Configuration
REGION=${AWS_REGION:-us-east-2}
API_NAME="stock-chatbot-api"
STAGE_NAME="prod"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

# Get AWS Account ID
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo -e "${BLUE}Creating REST API...${NC}"

# Check if API already exists
API_ID=$(aws apigateway get-rest-apis --region $REGION --query "items[?name=='$API_NAME'].id" --output text)

if [ -z "$API_ID" ]; then
    API_ID=$(aws apigateway create-rest-api \
        --name $API_NAME \
        --description "API for Stock Trading Chatbot" \
        --region $REGION \
        --query 'id' \
        --output text)
    echo -e "${GREEN}✓ Created API: $API_ID${NC}"
else
    echo -e "${GREEN}✓ Using existing API: $API_ID${NC}"
fi

# Get root resource ID
ROOT_ID=$(aws apigateway get-resources \
    --rest-api-id $API_ID \
    --region $REGION \
    --query 'items[?path==`/`].id' \
    --output text)

# Function to create API resource and method
create_endpoint() {
    local RESOURCE_PATH=$1
    local HTTP_METHOD=$2
    local LAMBDA_FUNCTION=$3
    local DESCRIPTION=$4

    echo -e "${BLUE}Creating endpoint: $HTTP_METHOD /$RESOURCE_PATH${NC}"

    # Create resource if it doesn't exist
    RESOURCE_ID=$(aws apigateway get-resources \
        --rest-api-id $API_ID \
        --region $REGION \
        --query "items[?path=='/$RESOURCE_PATH'].id" \
        --output text)

    if [ -z "$RESOURCE_ID" ]; then
        RESOURCE_ID=$(aws apigateway create-resource \
            --rest-api-id $API_ID \
            --parent-id $ROOT_ID \
            --path-part $RESOURCE_PATH \
            --region $REGION \
            --query 'id' \
            --output text)
    fi

    # Create method
    aws apigateway put-method \
        --rest-api-id $API_ID \
        --resource-id $RESOURCE_ID \
        --http-method $HTTP_METHOD \
        --authorization-type NONE \
        --region $REGION > /dev/null 2>&1 || true

    # Set up Lambda integration
    LAMBDA_URI="arn:aws:apigateway:${REGION}:lambda:path/2015-03-31/functions/arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${LAMBDA_FUNCTION}/invocations"

    aws apigateway put-integration \
        --rest-api-id $API_ID \
        --resource-id $RESOURCE_ID \
        --http-method $HTTP_METHOD \
        --type AWS_PROXY \
        --integration-http-method POST \
        --uri "$LAMBDA_URI" \
        --region $REGION > /dev/null 2>&1 || true

    # Grant API Gateway permission to invoke Lambda
    aws lambda add-permission \
        --function-name $LAMBDA_FUNCTION \
        --statement-id "apigateway-${RESOURCE_PATH}-${HTTP_METHOD}" \
        --action lambda:InvokeFunction \
        --principal apigateway.amazonaws.com \
        --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*/${HTTP_METHOD}/${RESOURCE_PATH}" \
        --region $REGION > /dev/null 2>&1 || true

    echo -e "${GREEN}✓ Created $HTTP_METHOD /$RESOURCE_PATH${NC}"
}

# Create endpoints
create_endpoint "chatbot" "POST" "stock-chatbot-query" "Chatbot query endpoint"
create_endpoint "analysis" "POST" "stock-chatbot-analysis" "Stock analysis endpoint"
create_endpoint "news" "POST" "stock-chatbot-news" "News analysis endpoint"
create_endpoint "tracking" "POST" "stock-chatbot-tracking" "Prediction tracking endpoint"

# Enable CORS
echo -e "${BLUE}Enabling CORS...${NC}"

for RESOURCE_PATH in "chatbot" "analysis" "news" "tracking"; do
    RESOURCE_ID=$(aws apigateway get-resources \
        --rest-api-id $API_ID \
        --region $REGION \
        --query "items[?path=='/$RESOURCE_PATH'].id" \
        --output text)

    # Add OPTIONS method for CORS
    aws apigateway put-method \
        --rest-api-id $API_ID \
        --resource-id $RESOURCE_ID \
        --http-method OPTIONS \
        --authorization-type NONE \
        --region $REGION > /dev/null 2>&1 || true

    aws apigateway put-integration \
        --rest-api-id $API_ID \
        --resource-id $RESOURCE_ID \
        --http-method OPTIONS \
        --type MOCK \
        --request-templates '{"application/json":"{\"statusCode\": 200}"}' \
        --region $REGION > /dev/null 2>&1 || true

    aws apigateway put-method-response \
        --rest-api-id $API_ID \
        --resource-id $RESOURCE_ID \
        --http-method OPTIONS \
        --status-code 200 \
        --response-parameters '{"method.response.header.Access-Control-Allow-Headers":false,"method.response.header.Access-Control-Allow-Methods":false,"method.response.header.Access-Control-Allow-Origin":false}' \
        --region $REGION > /dev/null 2>&1 || true

    aws apigateway put-integration-response \
        --rest-api-id $API_ID \
        --resource-id $RESOURCE_ID \
        --http-method OPTIONS \
        --status-code 200 \
        --response-parameters '{"method.response.header.Access-Control-Allow-Headers":"'\''Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token'\''","method.response.header.Access-Control-Allow-Methods":"'\''POST,OPTIONS'\''","method.response.header.Access-Control-Allow-Origin":"'\''*'\''"}' \
        --region $REGION > /dev/null 2>&1 || true
done

# Deploy API
echo -e "${BLUE}Deploying API to $STAGE_NAME stage...${NC}"

aws apigateway create-deployment \
    --rest-api-id $API_ID \
    --stage-name $STAGE_NAME \
    --stage-description "Production stage" \
    --description "Deployment $(date)" \
    --region $REGION > /dev/null

API_URL="https://${API_ID}.execute-api.${REGION}.amazonaws.com/${STAGE_NAME}"

echo -e "${GREEN}✅ API Gateway deployed successfully!${NC}"
echo ""
echo "API Endpoint: $API_URL"
echo ""
echo "Available endpoints:"
echo "  POST $API_URL/chatbot"
echo "  POST $API_URL/analysis"
echo "  POST $API_URL/news"
echo "  POST $API_URL/tracking"
echo ""
echo "Update your web app configuration with this API URL."
