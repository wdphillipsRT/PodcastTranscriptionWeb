"""
S3 utilities for podcast transcription service.
Handles file uploads, downloads, and management for audio files, transcripts, and summaries.
"""

import os
import boto3
import logging
from pathlib import Path
from typing import Optional, Dict, Any
from botocore.exceptions import ClientError, NoCredentialsError

logger = logging.getLogger(__name__)

class S3Manager:
    """Manages S3 operations for podcast transcription files."""
    
    def __init__(self):
        """Initialize S3 client with configuration from environment."""
        self.bucket_name = os.environ.get('AWS_S3_BUCKET_NAME')
        self.region = os.environ.get('AWS_REGION', 'us-east-1')
        
        if not self.bucket_name:
            raise ValueError("AWS_S3_BUCKET_NAME environment variable is required")
        
        try:
            self.s3_client = boto3.client(
                's3',
                aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
                aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY'),
                region_name=self.region
            )
            logger.info(f"Initialized S3 client for bucket: {self.bucket_name}")
        except Exception as e:
            logger.error(f"Failed to initialize S3 client: {e}")
            raise
    
    def generate_s3_key(self, podcast_id: int, episode_id: int, file_type: str, filename: str) -> str:
        """Generate S3 key for organized file storage.
        
        Args:
            podcast_id: Database ID of the podcast
            episode_id: Database ID of the episode
            file_type: Type of file ('audio', 'transcripts', 'summaries')
            filename: Original filename
        
        Returns:
            S3 key in format: {file_type}/podcast_{id}/episode_{id}/{filename}
        """
        return f"{file_type}/podcast_{podcast_id}/episode_{episode_id}/{filename}"
    
    def upload_file(self, local_file_path: str, s3_key: str, 
                   content_type: Optional[str] = None) -> Optional[str]:
        """Upload a local file to S3.
        
        Args:
            local_file_path: Path to local file
            s3_key: S3 key (path) for the file
            content_type: MIME type of the file (auto-detected if None)
        
        Returns:
            S3 URL of uploaded file, or None if upload failed
        """
        try:
            extra_args = {}
            if content_type:
                extra_args['ContentType'] = content_type
            
            self.s3_client.upload_file(
                local_file_path, 
                self.bucket_name, 
                s3_key,
                ExtraArgs=extra_args
            )
            
            s3_url = f"https://{self.bucket_name}.s3.{self.region}.amazonaws.com/{s3_key}"
            logger.info(f"Uploaded {local_file_path} to {s3_url}")
            return s3_url
            
        except FileNotFoundError:
            logger.error(f"Local file not found: {local_file_path}")
            return None
        except NoCredentialsError:
            logger.error("AWS credentials not found")
            return None
        except ClientError as e:
            logger.error(f"Failed to upload to S3: {e}")
            return None
    
    def download_file(self, s3_key: str, local_file_path: str) -> bool:
        """Download a file from S3 to local storage.
        
        Args:
            s3_key: S3 key of the file to download
            local_file_path: Local path where file should be saved
        
        Returns:
            True if download successful, False otherwise
        """
        try:
            # Create directory if it doesn't exist
            Path(local_file_path).parent.mkdir(parents=True, exist_ok=True)
            
            self.s3_client.download_file(
                self.bucket_name, 
                s3_key, 
                local_file_path
            )
            
            logger.info(f"Downloaded {s3_key} to {local_file_path}")
            return True
            
        except ClientError as e:
            if e.response['Error']['Code'] == '404':
                logger.error(f"File not found in S3: {s3_key}")
            else:
                logger.error(f"Failed to download from S3: {e}")
            return False
    
    def upload_text_content(self, content: str, s3_key: str, 
                           content_type: str = 'text/plain') -> Optional[str]:
        """Upload text content directly to S3.
        
        Args:
            content: Text content to upload
            s3_key: S3 key for the file
            content_type: MIME type of the content
        
        Returns:
            S3 URL of uploaded content, or None if upload failed
        """
        try:
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=s3_key,
                Body=content.encode('utf-8'),
                ContentType=content_type
            )
            
            s3_url = f"https://{self.bucket_name}.s3.{self.region}.amazonaws.com/{s3_key}"
            logger.info(f"Uploaded text content to {s3_url}")
            return s3_url
            
        except ClientError as e:
            logger.error(f"Failed to upload text to S3: {e}")
            return None
    
    def download_text_content(self, s3_key: str) -> Optional[str]:
        """Download text content from S3.
        
        Args:
            s3_key: S3 key of the text file
        
        Returns:
            Text content as string, or None if download failed
        """
        try:
            response = self.s3_client.get_object(
                Bucket=self.bucket_name,
                Key=s3_key
            )
            
            content = response['Body'].read().decode('utf-8')
            logger.info(f"Downloaded text content from {s3_key}")
            return content
            
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchKey':
                logger.error(f"File not found in S3: {s3_key}")
            else:
                logger.error(f"Failed to download text from S3: {e}")
            return None
    
    def delete_file(self, s3_key: str) -> bool:
        """Delete a file from S3.
        
        Args:
            s3_key: S3 key of the file to delete
        
        Returns:
            True if deletion successful, False otherwise
        """
        try:
            self.s3_client.delete_object(
                Bucket=self.bucket_name,
                Key=s3_key
            )
            
            logger.info(f"Deleted {s3_key} from S3")
            return True
            
        except ClientError as e:
            logger.error(f"Failed to delete from S3: {e}")
            return False
    
    def file_exists(self, s3_key: str) -> bool:
        """Check if a file exists in S3.
        
        Args:
            s3_key: S3 key to check
        
        Returns:
            True if file exists, False otherwise
        """
        try:
            self.s3_client.head_object(
                Bucket=self.bucket_name,
                Key=s3_key
            )
            return True
        except ClientError as e:
            if e.response['Error']['Code'] == '404':
                return False
            else:
                logger.error(f"Error checking file existence: {e}")
                return False
    
    def get_file_info(self, s3_key: str) -> Optional[Dict[str, Any]]:
        """Get metadata about a file in S3.
        
        Args:
            s3_key: S3 key of the file
        
        Returns:
            Dictionary with file metadata, or None if file doesn't exist
        """
        try:
            response = self.s3_client.head_object(
                Bucket=self.bucket_name,
                Key=s3_key
            )
            
            return {
                'size': response.get('ContentLength', 0),
                'last_modified': response.get('LastModified'),
                'content_type': response.get('ContentType'),
                'etag': response.get('ETag', '').strip('"')
            }
            
        except ClientError as e:
            if e.response['Error']['Code'] == '404':
                logger.debug(f"File not found in S3: {s3_key}")
            else:
                logger.error(f"Error getting file info: {e}")
            return None


# Global S3 manager instance
s3_manager = None

def get_s3_manager() -> S3Manager:
    """Get global S3 manager instance (singleton pattern)."""
    global s3_manager
    if s3_manager is None:
        s3_manager = S3Manager()
    return s3_manager


def extract_s3_key_from_url(s3_url: str) -> Optional[str]:
    """Extract S3 key from S3 URL.
    
    Args:
        s3_url: S3 URL like https://bucket.s3.region.amazonaws.com/path/to/file
    
    Returns:
        S3 key (path/to/file) or None if invalid URL
    """
    try:
        if not s3_url or not s3_url.startswith('https://'):
            return None
        
        # Split URL and extract key part
        parts = s3_url.split('.amazonaws.com/')
        if len(parts) != 2:
            return None
        
        return parts[1]
    except Exception:
        return None