"""
AWS Lambda handler for prediction tracking
"""
import json
import os
import boto3
from datetime import datetime
from decimal import Decimal
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize DynamoDB
dynamodb = boto3.resource('dynamodb')
table_name = os.getenv('DYNAMODB_TABLE_NAME', 'stock-chatbot-predictions')


def lambda_handler(event, context):
    """
    Handle prediction tracking operations

    Event format for saving prediction:
    {
        "action": "save_prediction",
        "data": {
            "symbol": "AAPL",
            "prediction": "BUY",
            "confidence": 0.85,
            "price_at_prediction": 175.50,
            "rationale": "Technical indicators..."
        }
    }

    Event format for getting predictions:
    {
        "action": "get_predictions",
        "symbol": "AAPL",  # optional
        "limit": 10        # optional
    }

    Event format for updating prediction outcome:
    {
        "action": "update_outcome",
        "prediction_id": "uuid",
        "outcome": "correct"  # correct, incorrect, pending
    }
    """
    try:
        # Parse input
        if isinstance(event.get('body'), str):
            body = json.loads(event['body'])
        else:
            body = event

        action = body.get('action')

        if action == 'save_prediction':
            return save_prediction(body.get('data', {}))
        elif action == 'get_predictions':
            return get_predictions(
                symbol=body.get('symbol'),
                limit=body.get('limit', 10)
            )
        elif action == 'update_outcome':
            return update_prediction_outcome(
                prediction_id=body.get('prediction_id'),
                outcome=body.get('outcome')
            )
        elif action == 'get_accuracy':
            return get_accuracy_stats(symbol=body.get('symbol'))
        else:
            return {
                'statusCode': 400,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps({
                    'error': 'Invalid action. Use: save_prediction, get_predictions, update_outcome, or get_accuracy'
                })
            }

    except Exception as e:
        logger.error(f"Error in tracking handler: {str(e)}", exc_info=True)
        return {
            'statusCode': 500,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({
                'error': 'Internal server error',
                'message': str(e)
            })
        }


def save_prediction(data):
    """Save a new prediction to DynamoDB"""
    try:
        table = dynamodb.Table(table_name)

        # Generate prediction ID
        prediction_id = f"{data['symbol']}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

        item = {
            'prediction_id': prediction_id,
            'symbol': data['symbol'],
            'prediction': data['prediction'],
            'confidence': Decimal(str(data['confidence'])),
            'price_at_prediction': Decimal(str(data.get('price_at_prediction', 0))),
            'rationale': data.get('rationale', ''),
            'timestamp': datetime.now().isoformat(),
            'outcome': 'pending',
            'created_at': int(datetime.now().timestamp())
        }

        table.put_item(Item=item)

        logger.info(f"Saved prediction: {prediction_id}")

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({
                'message': 'Prediction saved successfully',
                'prediction_id': prediction_id
            })
        }

    except Exception as e:
        logger.error(f"Error saving prediction: {str(e)}")
        raise


def get_predictions(symbol=None, limit=10):
    """Retrieve predictions from DynamoDB"""
    try:
        table = dynamodb.Table(table_name)

        if symbol:
            # Query by symbol
            response = table.query(
                IndexName='symbol-index',  # Requires GSI
                KeyConditionExpression='symbol = :symbol',
                ExpressionAttributeValues={':symbol': symbol},
                Limit=limit,
                ScanIndexForward=False  # Most recent first
            )
        else:
            # Scan all (expensive, but OK for MVP)
            response = table.scan(Limit=limit)

        items = response.get('Items', [])

        # Convert Decimal to float for JSON serialization
        items = json.loads(json.dumps(items, default=decimal_default))

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({
                'predictions': items,
                'count': len(items)
            })
        }

    except Exception as e:
        logger.error(f"Error getting predictions: {str(e)}")
        raise


def update_prediction_outcome(prediction_id, outcome):
    """Update the outcome of a prediction"""
    try:
        table = dynamodb.Table(table_name)

        table.update_item(
            Key={'prediction_id': prediction_id},
            UpdateExpression='SET outcome = :outcome, updated_at = :updated_at',
            ExpressionAttributeValues={
                ':outcome': outcome,
                ':updated_at': int(datetime.now().timestamp())
            }
        )

        logger.info(f"Updated prediction {prediction_id} outcome to {outcome}")

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({
                'message': 'Prediction outcome updated successfully',
                'prediction_id': prediction_id,
                'outcome': outcome
            })
        }

    except Exception as e:
        logger.error(f"Error updating prediction outcome: {str(e)}")
        raise


def get_accuracy_stats(symbol=None):
    """Calculate accuracy statistics"""
    try:
        table = dynamodb.Table(table_name)

        if symbol:
            response = table.query(
                IndexName='symbol-index',
                KeyConditionExpression='symbol = :symbol',
                ExpressionAttributeValues={':symbol': symbol}
            )
        else:
            response = table.scan()

        items = response.get('Items', [])

        # Calculate stats
        total = len(items)
        correct = sum(1 for item in items if item.get('outcome') == 'correct')
        incorrect = sum(1 for item in items if item.get('outcome') == 'incorrect')
        pending = sum(1 for item in items if item.get('outcome') == 'pending')

        accuracy = (correct / (correct + incorrect) * 100) if (correct + incorrect) > 0 else 0

        stats = {
            'total_predictions': total,
            'correct': correct,
            'incorrect': incorrect,
            'pending': pending,
            'accuracy_percentage': round(accuracy, 2)
        }

        if symbol:
            stats['symbol'] = symbol

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps(stats)
        }

    except Exception as e:
        logger.error(f"Error calculating accuracy: {str(e)}")
        raise


def decimal_default(obj):
    """Helper to convert Decimal to float"""
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError


# For local testing
if __name__ == "__main__":
    # Test save prediction
    test_event = {
        "action": "save_prediction",
        "data": {
            "symbol": "AAPL",
            "prediction": "BUY",
            "confidence": 0.85,
            "price_at_prediction": 175.50,
            "rationale": "Strong technical indicators"
        }
    }

    result = lambda_handler(test_event, None)
    print(json.dumps(json.loads(result['body']), indent=2))
