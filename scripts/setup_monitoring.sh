#!/bin/bash
# Setup CloudWatch monitoring dashboard

set -e

REGION=${AWS_REGION:-us-east-2}
DASHBOARD_NAME="stock-chatbot-monitoring"

echo "🔍 Setting up CloudWatch monitoring dashboard..."

# Create dashboard
aws cloudwatch put-dashboard \
    --dashboard-name $DASHBOARD_NAME \
    --dashboard-body file://monitoring/cloudwatch-dashboard.json \
    --region $REGION

echo "✅ Dashboard created: $DASHBOARD_NAME"
echo ""
echo "View dashboard at:"
echo "https://$REGION.console.aws.amazon.com/cloudwatch/home?region=$REGION#dashboards:name=$DASHBOARD_NAME"
echo ""
echo "Monitor your Lambda functions:"
echo "  • Invocation counts"
echo "  • Duration metrics"
echo "  • Error rates"
echo "  • API Gateway latency"
echo "  • Recent error logs"
