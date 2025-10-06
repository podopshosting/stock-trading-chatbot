#!/bin/bash
# Deploy web frontend to AWS S3

set -e

# Configuration
BUCKET_NAME="stock-chatbot-web"
REGION="us-east-2"
WEB_DIR="/Users/Brian 1/Documents/GitHub/stock-trading-chatbot/web/static"

echo "🌐 Deploying Stock Trading Chatbot Web Frontend"
echo "==============================================="
echo ""

# Check if bucket exists
if aws s3 ls "s3://$BUCKET_NAME" 2>&1 | grep -q 'NoSuchBucket'; then
    echo "📦 Creating S3 bucket: $BUCKET_NAME"
    aws s3 mb "s3://$BUCKET_NAME" --region $REGION

    echo "⚙️  Configuring static website hosting..."
    aws s3 website "s3://$BUCKET_NAME" \
        --index-document index.html \
        --error-document index.html

    echo "🔓 Setting bucket policy for public access..."
    cat > /tmp/bucket-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PublicReadGetObject",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::$BUCKET_NAME/*"
    }
  ]
}
EOF

    aws s3api put-bucket-policy \
        --bucket $BUCKET_NAME \
        --policy file:///tmp/bucket-policy.json

    echo "✅ Bucket created and configured"
else
    echo "✓ Bucket already exists: $BUCKET_NAME"
fi

echo ""
echo "📤 Uploading files..."

# Upload index.html
aws s3 cp "$WEB_DIR/index.html" "s3://$BUCKET_NAME/index.html" \
    --content-type "text/html" \
    --acl public-read \
    --region $REGION

echo "✅ Files uploaded successfully"
echo ""
echo "🎉 Deployment complete!"
echo ""
echo "📍 Website URL:"
echo "   http://$BUCKET_NAME.s3-website.$REGION.amazonaws.com"
echo ""
echo "💡 Next Steps:"
echo "   1. Visit the URL above to test your chatbot"
echo "   2. (Optional) Set up CloudFront for HTTPS and CDN"
echo "   3. (Optional) Add custom domain name"
echo ""
echo "📊 To add CloudFront distribution:"
echo "   aws cloudfront create-distribution \\"
echo "     --origin-domain-name $BUCKET_NAME.s3.$REGION.amazonaws.com \\"
echo "     --default-root-object index.html"
echo ""
