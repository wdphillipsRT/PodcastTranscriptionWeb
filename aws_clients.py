"""Shared AWS endpoint policy; credentials, signing and regions remain SDK-owned."""

import boto3
from botocore.config import Config


def create_aws_client(service, region, *, connect_timeout=10, read_timeout=60):
    """Ignore environment/shared-config endpoints without replacing SDK chains."""
    return boto3.client(service, region_name=region, config=Config(
        ignore_configured_endpoint_urls=True,
        connect_timeout=connect_timeout, read_timeout=read_timeout,
        retries={"total_max_attempts": 1},
        s3={"us_east_1_regional_endpoint": "regional"}))
