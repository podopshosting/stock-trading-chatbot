#!/usr/bin/env python3
"""
Script to create DynamoDB table for prediction tracking
"""
import boto3
import os
from botocore.exceptions import ClientError


def create_predictions_table(table_name='stock-chatbot-predictions', region='us-east-2'):
    """Create DynamoDB table for storing predictions"""

    dynamodb = boto3.client('dynamodb', region_name=region)

    try:
        # Check if table exists
        try:
            dynamodb.describe_table(TableName=table_name)
            print(f"Table {table_name} already exists")
            return
        except ClientError as e:
            if e.response['Error']['Code'] != 'ResourceNotFoundException':
                raise

        # Create table
        print(f"Creating table {table_name}...")

        table = dynamodb.create_table(
            TableName=table_name,
            KeySchema=[
                {
                    'AttributeName': 'prediction_id',
                    'KeyType': 'HASH'  # Partition key
                }
            ],
            AttributeDefinitions=[
                {
                    'AttributeName': 'prediction_id',
                    'AttributeType': 'S'
                },
                {
                    'AttributeName': 'symbol',
                    'AttributeType': 'S'
                },
                {
                    'AttributeName': 'created_at',
                    'AttributeType': 'N'
                }
            ],
            GlobalSecondaryIndexes=[
                {
                    'IndexName': 'symbol-index',
                    'KeySchema': [
                        {
                            'AttributeName': 'symbol',
                            'KeyType': 'HASH'
                        },
                        {
                            'AttributeName': 'created_at',
                            'KeyType': 'RANGE'
                        }
                    ],
                    'Projection': {
                        'ProjectionType': 'ALL'
                    },
                    'ProvisionedThroughput': {
                        'ReadCapacityUnits': 5,
                        'WriteCapacityUnits': 5
                    }
                }
            ],
            ProvisionedThroughput={
                'ReadCapacityUnits': 5,
                'WriteCapacityUnits': 5
            },
            Tags=[
                {
                    'Key': 'Project',
                    'Value': 'StockTradingChatbot'
                },
                {
                    'Key': 'Environment',
                    'Value': 'Production'
                }
            ]
        )

        # Wait for table to be created
        print("Waiting for table to be created...")
        waiter = dynamodb.get_waiter('table_exists')
        waiter.wait(TableName=table_name)

        print(f"✓ Table {table_name} created successfully!")
        print(f"  Region: {region}")
        print(f"  Partition Key: prediction_id")
        print(f"  GSI: symbol-index")

    except ClientError as e:
        print(f"Error creating table: {e.response['Error']['Message']}")
        raise
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        raise


def delete_predictions_table(table_name='stock-chatbot-predictions', region='us-east-2'):
    """Delete DynamoDB table (use with caution!)"""

    dynamodb = boto3.client('dynamodb', region_name=region)

    try:
        print(f"Deleting table {table_name}...")
        dynamodb.delete_table(TableName=table_name)

        # Wait for table to be deleted
        print("Waiting for table to be deleted...")
        waiter = dynamodb.get_waiter('table_not_exists')
        waiter.wait(TableName=table_name)

        print(f"✓ Table {table_name} deleted successfully!")

    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            print(f"Table {table_name} does not exist")
        else:
            print(f"Error deleting table: {e.response['Error']['Message']}")
            raise


if __name__ == "__main__":
    import sys

    # Get region from environment or use default
    region = os.getenv('AWS_REGION', 'us-east-2')
    table_name = os.getenv('DYNAMODB_TABLE_NAME', 'stock-chatbot-predictions')

    if len(sys.argv) > 1 and sys.argv[1] == 'delete':
        confirm = input(f"Are you sure you want to delete table {table_name}? (yes/no): ")
        if confirm.lower() == 'yes':
            delete_predictions_table(table_name, region)
        else:
            print("Deletion cancelled")
    else:
        create_predictions_table(table_name, region)
