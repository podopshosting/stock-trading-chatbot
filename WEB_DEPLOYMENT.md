# 🌐 Web Frontend Deployment Guide

## Overview

The Stock Trading Chatbot web frontend is a **static HTML/JavaScript application** that connects directly to the AWS API Gateway endpoint. This makes it easy to deploy on various platforms.

---

## 📦 What's Deployed

**File:** `/web/static/index.html`

**Features:**
- ✅ AI chatbot interface powered by GPT-4o-mini
- ✅ Direct connection to API Gateway
- ✅ Responsive design (mobile-friendly)
- ✅ Real-time chat with typing indicators
- ✅ Supports all 10 major stocks (AAPL, TSLA, MSFT, etc.)
- ✅ No backend server required (pure static hosting)

**API Endpoint:** `https://lmi4hshs7h.execute-api.us-east-2.amazonaws.com/prod/chatbot`

---

## 🚀 Deployment Options

### Option 1: AWS S3 + CloudFront (Recommended)

**Cost:** ~$0.50/month for low traffic

#### Step 1: Create S3 Bucket
```bash
# Set variables
BUCKET_NAME="stock-chatbot-web"
REGION="us-east-2"

# Create bucket
aws s3 mb s3://$BUCKET_NAME --region $REGION

# Enable static website hosting
aws s3 website s3://$BUCKET_NAME \
  --index-document index.html \
  --error-document index.html
```

#### Step 2: Upload Files
```bash
cd /Users/Brian\ 1/Documents/GitHub/stock-trading-chatbot/web/static

# Upload index.html
aws s3 cp index.html s3://$BUCKET_NAME/index.html \
  --content-type "text/html" \
  --acl public-read

# Verify upload
aws s3 ls s3://$BUCKET_NAME
```

#### Step 3: Configure Bucket Policy
```bash
# Create bucket policy
cat > /tmp/bucket-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PublicReadGetObject",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::stock-chatbot-web/*"
    }
  ]
}
EOF

# Apply policy
aws s3api put-bucket-policy \
  --bucket $BUCKET_NAME \
  --policy file:///tmp/bucket-policy.json
```

#### Step 4: Access Your Site
```bash
# Get website URL
echo "http://$BUCKET_NAME.s3-website.$REGION.amazonaws.com"
```

#### Step 5 (Optional): Add CloudFront CDN
```bash
# Create CloudFront distribution
aws cloudfront create-distribution \
  --origin-domain-name $BUCKET_NAME.s3.$REGION.amazonaws.com \
  --default-root-object index.html

# This provides HTTPS and global CDN
```

---

### Option 2: AWS Amplify

**Cost:** Free tier includes hosting for static sites

#### Steps:
```bash
# Install Amplify CLI
npm install -g @aws-amplify/cli

# Initialize Amplify
cd /Users/Brian\ 1/Documents/GitHub/stock-trading-chatbot
amplify init

# Add hosting
amplify add hosting

# Choose: Hosting with Amplify Console (Managed hosting)

# Publish
amplify publish
```

**Benefits:**
- ✅ Automatic HTTPS
- ✅ Global CDN
- ✅ CI/CD integration
- ✅ Custom domain support
- ✅ Free tier available

---

### Option 3: Netlify (Easiest)

**Cost:** Free

#### Steps:

1. **Via Netlify CLI:**
```bash
# Install Netlify CLI
npm install -g netlify-cli

# Deploy
cd /Users/Brian\ 1/Documents/GitHub/stock-trading-chatbot/web/static
netlify deploy --dir=. --prod
```

2. **Via Drag & Drop:**
   - Go to https://app.netlify.com/drop
   - Drag `web/static` folder
   - Done!

**Benefits:**
- ✅ Instant deployment
- ✅ Free HTTPS
- ✅ Free custom domain
- ✅ No AWS account needed

---

### Option 4: GitHub Pages

**Cost:** Free

#### Steps:

1. Create `docs` folder in root:
```bash
cd /Users/Brian\ 1/Documents/GitHub/stock-trading-chatbot
mkdir -p docs
cp web/static/index.html docs/
```

2. Commit and push:
```bash
git add docs/
git commit -m "Add GitHub Pages deployment"
git push
```

3. Enable GitHub Pages:
   - Go to GitHub repo → Settings → Pages
   - Source: `main` branch, `/docs` folder
   - Save

4. Access at: `https://podopshosting.github.io/stock-trading-chatbot/`

---

### Option 5: Vercel

**Cost:** Free

#### Steps:
```bash
# Install Vercel CLI
npm install -g vercel

# Deploy
cd /Users/Brian\ 1/Documents/GitHub/stock-trading-chatbot/web/static
vercel --prod
```

---

## 🧪 Local Testing

Before deploying, test locally:

### Method 1: Python HTTP Server
```bash
cd /Users/Brian\ 1/Documents/GitHub/stock-trading-chatbot/web/static
python3 -m http.server 8000
```
Visit: http://localhost:8000

### Method 2: Node.js HTTP Server
```bash
npx http-server -p 8000
```

### Method 3: Live Server (VS Code Extension)
- Install "Live Server" extension
- Right-click `index.html` → "Open with Live Server"

---

## 🔧 Configuration

### Update API Endpoint (if needed)

If you redeploy the Lambda functions or create a new API Gateway, update the endpoint:

**File:** `web/static/index.html`
```javascript
// Line 185 - Update this constant
const API_ENDPOINT = 'https://YOUR-NEW-API-GATEWAY-URL/prod/chatbot';
```

---

## 📊 Features Roadmap

Current static site is MVP. For full features, deploy Flask app:

**Current (Static):**
- ✅ AI chatbot
- ❌ Stock watchlist
- ❌ Charts
- ❌ News feed
- ❌ Predictions

**Full Flask App:**
- ✅ AI chatbot
- ✅ Stock watchlist (real-time prices)
- ✅ Interactive charts (Chart.js)
- ✅ News sentiment analysis
- ✅ Prediction tracking
- ✅ Accuracy metrics

### Deploy Full Flask App (Advanced)

**Option A: AWS Elastic Beanstalk**
```bash
cd /Users/Brian\ 1/Documents/GitHub/stock-trading-chatbot/web

# Install EB CLI
pip install awsebcli

# Initialize
eb init -p python-3.12 stock-chatbot-web --region us-east-2

# Create environment
eb create stock-chatbot-prod

# Deploy
eb deploy
```

**Option B: Heroku**
```bash
cd /Users/Brian\ 1/Documents/GitHub/stock-trading-chatbot/web

# Create Procfile
echo "web: gunicorn app:app" > Procfile

# Create runtime.txt
echo "python-3.12" > runtime.txt

# Deploy
heroku create stock-chatbot
git push heroku main
```

**Option C: AWS Lambda + API Gateway (Serverless Flask)**
Use Zappa or AWS Chalice to deploy Flask as serverless.

---

## 🔒 Security Considerations

### CORS
The API Gateway is configured with CORS enabled:
```
Access-Control-Allow-Origin: *
```

For production, restrict to your domain:
```bash
aws apigateway put-method-response \
  --rest-api-id lmi4hshs7h \
  --resource-id xxx \
  --http-method OPTIONS \
  --status-code 200 \
  --response-parameters method.response.header.Access-Control-Allow-Origin=false

# Update Lambda to return specific domain
Access-Control-Allow-Origin: https://yourdomain.com
```

### API Key Protection
Add API key authentication to prevent abuse:
```bash
aws apigateway create-api-key --name "web-frontend-key"
aws apigateway create-usage-plan --name "web-usage-plan"
```

---

## 📈 Monitoring

### CloudWatch Metrics
Monitor API Gateway usage:
```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/ApiGateway \
  --metric-name Count \
  --dimensions Name=ApiName,Value=lmi4hshs7h \
  --start-time 2025-10-06T00:00:00Z \
  --end-time 2025-10-07T00:00:00Z \
  --period 3600 \
  --statistics Sum \
  --region us-east-2
```

### Cost Tracking
- S3 hosting: ~$0.023/GB storage + $0.09/GB transfer
- CloudFront: ~$0.085/GB transfer
- API Gateway: $3.50 per million requests
- Lambda: Covered by free tier for low traffic

---

## 🎯 Quick Start Commands

### Deploy to S3 (Fastest)
```bash
# One-liner deployment
cd /Users/Brian\ 1/Documents/GitHub/stock-trading-chatbot && \
aws s3 cp web/static/index.html s3://stock-chatbot-web/index.html \
  --content-type "text/html" --acl public-read && \
echo "Deployed to: http://stock-chatbot-web.s3-website.us-east-2.amazonaws.com"
```

### Deploy to Netlify (Easiest)
```bash
cd /Users/Brian\ 1/Documents/GitHub/stock-trading-chatbot/web/static
netlify deploy --dir=. --prod
```

---

## 🆘 Troubleshooting

### CORS Errors
**Error:** `No 'Access-Control-Allow-Origin' header`

**Fix:** Verify API Gateway CORS:
```bash
curl -X OPTIONS https://lmi4hshs7h.execute-api.us-east-2.amazonaws.com/prod/chatbot \
  -H "Origin: http://localhost:8000" \
  -H "Access-Control-Request-Method: POST" \
  -i
```

### API Connection Failed
**Error:** `Failed to fetch`

**Fix:** Check API Gateway is deployed:
```bash
curl -X POST https://lmi4hshs7h.execute-api.us-east-2.amazonaws.com/prod/chatbot \
  -H "Content-Type: application/json" \
  -d '{"query":"Test"}'
```

### S3 403 Forbidden
**Error:** `AccessDenied`

**Fix:** Update bucket policy to allow public read:
```bash
aws s3api put-bucket-policy \
  --bucket stock-chatbot-web \
  --policy file://bucket-policy.json
```

---

## ✅ Deployment Checklist

- [ ] Test locally at http://localhost:8000
- [ ] Verify API endpoint is correct in index.html
- [ ] Choose deployment platform (S3, Netlify, Vercel, etc.)
- [ ] Deploy static site
- [ ] Test chatbot functionality
- [ ] Verify CORS headers
- [ ] Set up custom domain (optional)
- [ ] Configure HTTPS (S3 requires CloudFront)
- [ ] Set up monitoring/analytics
- [ ] Document deployment URL

---

## 🎉 Success!

Once deployed, users can:
1. Visit your site
2. Type stock questions (e.g., "Should I buy AAPL?")
3. Get AI-powered recommendations instantly
4. No login required
5. Works on mobile and desktop

**Estimated Time to Deploy:** 5-10 minutes
**Monthly Cost:** $0 (Netlify/Vercel) to $0.50 (AWS S3)

---

## 📞 Support

**Issues:**
- CloudWatch logs: `/aws/lambda/stock-chatbot-router`
- API Gateway stage: `prod`
- Region: `us-east-2`

**Repository:** https://github.com/podopshosting/stock-trading-chatbot
