"""
Prediction tracker microservice - DynamoDB only
"""
import json
import os
import boto3
from datetime import datetime
from decimal import Decimal


dynamodb = boto3.resource('dynamodb', region_name='us-east-2')
table_name = os.getenv('DYNAMODB_TABLE_NAME', 'stock-chatbot-predictions')


def lambda_handler(event, context):
    """Handle prediction tracking operations"""
    try:
        if isinstance(event.get('body'), str):
            body = json.loads(event['body'])
        else:
            body = event

        action = body.get('action')

        if action == 'save':
            return save_prediction(body.get('data', {}))
        elif action == 'get':
            return get_predictions(body.get('symbol'), body.get('limit', 20))
        elif action == 'accuracy':
            return get_accuracy(body.get('symbol'))
        else:
            return {
                'statusCode': 400,
                'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
                'body': json.dumps({'error': 'Invalid action'})
            }

    except Exception as e:
        print(f"Error: {e}")
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'error': str(e)})
        }


def save_prediction(data):
    """Save a prediction"""
    table = dynamodb.Table(table_name)

    prediction_id = f"{data['symbol']}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    item = {
        'prediction_id': prediction_id,
        'symbol': data['symbol'],
        'prediction': data['prediction'],
        'confidence': Decimal(str(data.get('confidence', 0))),
        'price': Decimal(str(data.get('price', 0))),
        'timestamp': datetime.now().isoformat(),
        'outcome': 'pending',
        'created_at': int(datetime.now().timestamp())
    }

    table.put_item(Item=item)

    return {
        'statusCode': 200,
        'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
        'body': json.dumps({'prediction_id': prediction_id, 'message': 'Saved'})
    }


def get_predictions(symbol=None, limit=20):
    """Get predictions"""
    table = dynamodb.Table(table_name)

    if symbol:
        response = table.query(
            IndexName='symbol-index',
            KeyConditionExpression='symbol = :symbol',
            ExpressionAttributeValues={':symbol': symbol},
            Limit=limit,
            ScanIndexForward=False
        )
    else:
        response = table.scan(Limit=limit)

    items = response.get('Items', [])

    # Convert Decimal to float
    for item in items:
        for key, value in item.items():
            if isinstance(value, Decimal):
                item[key] = float(value)

    return {
        'statusCode': 200,
        'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
        'body': json.dumps({'predictions': items, 'count': len(items)})
    }


def get_accuracy(symbol=None):
    """Calculate accuracy stats"""
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

    correct = sum(1 for item in items if item.get('outcome') == 'correct')
    incorrect = sum(1 for item in items if item.get('outcome') == 'incorrect')
    pending = sum(1 for item in items if item.get('outcome') == 'pending')

    accuracy = (correct / (correct + incorrect) * 100) if (correct + incorrect) > 0 else 0

    return {
        'statusCode': 200,
        'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
        'body': json.dumps({
            'total': len(items),
            'correct': correct,
            'incorrect': incorrect,
            'pending': pending,
            'accuracy': round(accuracy, 2)
        })
    }
