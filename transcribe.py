import os
import json
import time
from typing import Dict, Any, Optional, List, Tuple
import boto3
from botocore.exceptions import ClientError, NoCredentialsError
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

def _convert_audio_for_transcribe(audio_path: str) -> str:
    """
    Convert audio file to format compatible with Amazon Transcribe.
    Amazon Transcribe supports: mp3, mp4, wav, flac, ogg, amr, webm, m4a
    """
    import subprocess
    import tempfile
    
    # Check if file is already in a supported format
    supported_formats = ['.mp3', '.mp4', '.wav', '.flac', '.ogg', '.amr', '.webm', '.m4a']
    file_ext = os.path.splitext(audio_path)[1].lower()
    
    if file_ext in supported_formats:
        return audio_path
    
    # Convert to mp3 if not in supported format
    converted_path = audio_path.replace(file_ext, '.mp3')
    
    try:
        # Use ffmpeg to convert audio
        subprocess.run([
            'ffmpeg', '-i', audio_path, 
            '-acodec', 'mp3', 
            '-y',            # Overwrite output
            converted_path
        ], check=True, capture_output=True)
        
        logger.info(f"Converted audio from {file_ext} to mp3: {converted_path}")
        return converted_path
        
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.warning(f"Audio conversion failed: {e}")
        # Return original file and let Transcribe handle it
        return audio_path

class TranscriptionResult:
    """Container for transcription results."""
    
    def __init__(self, transcript_path: str, segments: List[Dict], info: Dict):
        self.transcript_path = transcript_path
        self.segments = segments
        self.info = info

def get_transcribe_client():
    """
    Initialize and return Amazon Transcribe client.
    
    Returns:
        boto3 Transcribe client instance
    """
    try:
        # Get AWS region from environment or default to us-west-2
        region = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-west-2"))
        
        client = boto3.client('transcribe', region_name=region)
        logger.info(f"Successfully initialized Amazon Transcribe client in region: {region}")
        return client
        
    except NoCredentialsError:
        raise ValueError("AWS credentials not found. Please configure AWS credentials using environment variables, AWS credentials file, or IAM role.")
    except Exception as e:
        raise ValueError(f"Failed to initialize Amazon Transcribe client: {e}")

def transcribe_audio(
    audio_path: str, 
    model_name: str = "basic",
    language: Optional[str] = None,
    vad_filter: bool = True,
    word_timestamps: bool = False
) -> TranscriptionResult:
    """
    Transcribe audio file using Amazon Transcribe service.
    
    Args:
        audio_path: Path to audio file
        model_name: Transcribe model to use ("basic" or "enhanced")
        language: Language code (e.g., "en-US", "es-ES", None for auto-detection)
        vad_filter: Not used with Transcribe (included for compatibility)
        word_timestamps: Include word-level timestamps
    
    Returns:
        TranscriptionResult containing paths and metadata
    """
    if not os.path.exists(audio_path):
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    
    logger.info(f"Transcribing audio via Amazon Transcribe: {audio_path}")
    
    # Initialize Transcribe client
    client = get_transcribe_client()
    
    # Convert audio to supported format if needed
    processed_audio_path = _convert_audio_for_transcribe(audio_path)
    
    # Generate output paths
    audio_path_obj = Path(audio_path)
    base_dir = audio_path_obj.parent
    
    # Replace /raw/ with /transcripts/ in the path
    transcript_dir = str(base_dir).replace("/raw/", "/transcripts/")
    transcript_dir = Path(transcript_dir)
    transcript_dir.mkdir(parents=True, exist_ok=True)
    
    transcript_path = transcript_dir / "transcript.txt"
    segments_path = transcript_dir / "segments.json"
    
    try:
        from s3_utils import get_s3_manager
        import uuid
        
        s3_manager = get_s3_manager()
        
        # Generate unique job name
        job_name = f"transcribe-{uuid.uuid4().hex[:8]}-{int(time.time())}"
        
        # Upload audio to S3 for Transcribe processing
        audio_s3_key = f"transcribe-temp/{job_name}.mp3"
        with open(processed_audio_path, 'rb') as f:
            audio_content = f.read()
        
        audio_s3_uri = s3_manager.upload_binary_content(
            audio_content, 
            audio_s3_key, 
            'audio/mpeg'
        )
        
        if not audio_s3_uri:
            raise Exception("Failed to upload audio to S3 for transcription")
        
        # Start transcription job
        job_config = {
            'TranscriptionJobName': job_name,
            'LanguageCode': language or 'en-US',
            'MediaFormat': 'mp3',
            'Media': {
                'MediaFileUri': audio_s3_uri
            },
            'OutputBucketName': s3_manager.bucket_name,
            'OutputKey': f"transcribe-results/{job_name}/"
        }
        
        # Add advanced settings
        settings = {}
        if word_timestamps:
            settings['ShowSpeakerLabels'] = False  # Can be enabled if needed
            settings['MaxSpeakerLabels'] = 2       # Adjust as needed
        
        if model_name == "enhanced":
            job_config['ModelSettings'] = {
                'LanguageModelName': 'enhanced'  # Use enhanced model if available
            }
        
        if settings:
            job_config['Settings'] = settings
        
        # Auto-detect language if not specified
        if not language:
            job_config['IdentifyLanguage'] = True
            job_config.pop('LanguageCode')  # Remove language code for auto-detection
        
        logger.info(f"Starting Amazon Transcribe job: {job_name}")
        response = client.start_transcription_job(**job_config)
        
        # Wait for job completion
        logger.info("Waiting for transcription job to complete...")
        while True:
            status_response = client.get_transcription_job(
                TranscriptionJobName=job_name
            )
            
            status = status_response['TranscriptionJob']['TranscriptionJobStatus']
            logger.info(f"Transcription job status: {status}")
            
            if status == 'COMPLETED':
                break
            elif status == 'FAILED':
                failure_reason = status_response['TranscriptionJob'].get('FailureReason', 'Unknown error')
                raise Exception(f"Transcription job failed: {failure_reason}")
            
            # Wait before checking again
            time.sleep(10)
        
        # Get the transcription results
        job_details = status_response['TranscriptionJob']
        results_uri = job_details['Transcript']['TranscriptFileUri']
        detected_language = job_details.get('LanguageCode', language or 'en-US')
        
        # Download and parse results
        import requests
        results_response = requests.get(results_uri)
        results_response.raise_for_status()
        transcription_data = results_response.json()
        
        # Extract transcript and segments
        transcript_text = transcription_data['results']['transcripts'][0]['transcript']
        items = transcription_data['results']['items']
        
        # Process segments - group by speaker or time intervals
        segment_list = []
        transcript_lines = []
        current_segment = {"start": 0.0, "end": 0.0, "text": ""}
        segment_duration = 30.0  # Group words into 30-second segments
        
        for item in items:
            if item['type'] == 'pronunciation':
                start_time = float(item['start_time'])
                end_time = float(item['end_time'])
                content = item['alternatives'][0]['content']
                
                # Start new segment if we've exceeded duration or this is the first item
                if not current_segment["text"] or start_time - current_segment["start"] > segment_duration:
                    if current_segment["text"]:
                        segment_list.append(current_segment.copy())
                        timestamp = f"[{current_segment['start']:.2f}–{current_segment['end']:.2f}]"
                        transcript_lines.append(f"{timestamp} {current_segment['text'].strip()}")
                    
                    current_segment = {
                        "start": start_time,
                        "end": end_time,
                        "text": content
                    }
                else:
                    current_segment["end"] = end_time
                    current_segment["text"] += " " + content
            elif item['type'] == 'punctuation':
                if current_segment["text"]:
                    current_segment["text"] += item['alternatives'][0]['content']
        
        # Add the last segment
        if current_segment["text"]:
            segment_list.append(current_segment)
            timestamp = f"[{current_segment['start']:.2f}–{current_segment['end']:.2f}]"
            transcript_lines.append(f"{timestamp} {current_segment['text'].strip()}")
        
        # Get duration from the last item
        duration = float(items[-1]['end_time']) if items and 'end_time' in items[-1] else 0.0
        
        # Write transcript text file
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write("\n".join(transcript_lines))
        
        # Write segments JSON file
        transcription_metadata = {
            "audio_file": str(audio_path),
            "model": f"amazon-transcribe-{model_name}",
            "language": detected_language,
            "language_probability": 0.99,  # Transcribe is generally very confident
            "duration": duration,
            "segments": segment_list
        }
        
        with open(segments_path, "w", encoding="utf-8") as f:
            json.dump(transcription_metadata, f, indent=2, ensure_ascii=False)
        
        # Clean up temporary files
        try:
            # Delete transcription job
            client.delete_transcription_job(TranscriptionJobName=job_name)
            # Clean up S3 files
            s3_manager.delete_file(audio_s3_key)
        except Exception as e:
            logger.warning(f"Failed to clean up transcription job resources: {e}")
        
        # Clean up converted audio file if we created one
        if processed_audio_path != audio_path and os.path.exists(processed_audio_path):
            os.unlink(processed_audio_path)
        
        logger.info(f"Amazon Transcribe transcription completed:")
        logger.info(f"  - Language: {detected_language}")
        logger.info(f"  - Duration: {duration:.2f}s")
        logger.info(f"  - Segments: {len(segment_list)}")
        logger.info(f"  - Transcript: {transcript_path}")
        logger.info(f"  - Segments: {segments_path}")
        
        return TranscriptionResult(
            transcript_path=str(transcript_path),
            segments=segment_list,
            info={
                "language": detected_language,
                "language_probability": 0.99,
                "duration": duration,
                "model": f"amazon-transcribe-{model_name}"
            }
        )
        
    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        raise

def load_transcript(transcript_path: str) -> str:
    """Load transcript text from file."""
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.error(f"Failed to load transcript from {transcript_path}: {e}")
        raise

def load_segments(segments_path: str) -> Dict[str, Any]:
    """Load transcription segments and metadata from JSON file."""
    try:
        with open(segments_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load segments from {segments_path}: {e}")
        raise

def transcribe_audio_from_s3(audio_s3_key: str, podcast_id: int, episode_id: int, 
                           model_name: str = "basic") -> Dict[str, Any]:
    """
    Transcribe audio file from S3 using Amazon Transcribe and upload results back to S3.
    
    Args:
        audio_s3_key: S3 key of the audio file
        podcast_id: Database ID of the podcast
        episode_id: Database ID of the episode
        model_name: Transcribe model to use ("basic" or "enhanced")
    
    Returns:
        Dictionary with transcription results and S3 URLs
    """
    from s3_utils import get_s3_manager
    import tempfile
    import json
    import uuid
    
    s3_manager = get_s3_manager()
    client = get_transcribe_client()
    
    try:
        # Generate unique job name
        job_name = f"transcribe-s3-{uuid.uuid4().hex[:8]}-{int(time.time())}"
        
        # Get S3 URI for the audio file
        audio_s3_uri = f"s3://{s3_manager.bucket_name}/{audio_s3_key}"
        
        # Determine audio format from file extension
        file_ext = os.path.splitext(audio_s3_key)[1].lower()
        format_map = {
            '.mp3': 'mp3', '.mp4': 'mp4', '.wav': 'wav', '.flac': 'flac',
            '.ogg': 'ogg', '.amr': 'amr', '.webm': 'webm', '.m4a': 'm4a'
        }
        media_format = format_map.get(file_ext, 'mp3')  # Default to mp3
        
        # Start transcription job directly from S3
        job_config = {
            'TranscriptionJobName': job_name,
            'LanguageCode': 'en-US',  # Default to English, can be made configurable
            'MediaFormat': media_format,
            'Media': {
                'MediaFileUri': audio_s3_uri
            },
            'OutputBucketName': s3_manager.bucket_name,
            'OutputKey': f"transcribe-results/{job_name}/"
        }
        
        # Add model settings for enhanced model
        if model_name == "enhanced":
            job_config['ModelSettings'] = {
                'LanguageModelName': 'enhanced'
            }
        
        # Enable language auto-detection
        job_config['IdentifyLanguage'] = True
        job_config.pop('LanguageCode')  # Remove for auto-detection
        
        logger.info(f"Starting Amazon Transcribe job for S3 file: {audio_s3_key}")
        response = client.start_transcription_job(**job_config)
        
        # Wait for job completion
        logger.info("Waiting for transcription job to complete...")
        while True:
            status_response = client.get_transcription_job(
                TranscriptionJobName=job_name
            )
            
            status = status_response['TranscriptionJob']['TranscriptionJobStatus']
            logger.info(f"Transcription job status: {status}")
            
            if status == 'COMPLETED':
                break
            elif status == 'FAILED':
                failure_reason = status_response['TranscriptionJob'].get('FailureReason', 'Unknown error')
                raise Exception(f"Transcription job failed: {failure_reason}")
            
            # Wait before checking again
            time.sleep(10)
        
        # Get the transcription results
        job_details = status_response['TranscriptionJob']
        results_uri = job_details['Transcript']['TranscriptFileUri']
        detected_language = job_details.get('LanguageCode', 'en-US')
        
        # Download and parse results
        import requests
        results_response = requests.get(results_uri)
        results_response.raise_for_status()
        transcription_data = results_response.json()
        
        # Extract transcript and segments
        transcript_text = transcription_data['results']['transcripts'][0]['transcript']
        items = transcription_data['results']['items']
        
        # Process segments - group by time intervals
        segment_list = []
        transcript_lines = []
        current_segment = {"start": 0.0, "end": 0.0, "text": ""}
        segment_duration = 30.0  # Group words into 30-second segments
        
        for item in items:
            if item['type'] == 'pronunciation':
                start_time = float(item['start_time'])
                end_time = float(item['end_time'])
                content = item['alternatives'][0]['content']
                
                # Start new segment if we've exceeded duration or this is the first item
                if not current_segment["text"] or start_time - current_segment["start"] > segment_duration:
                    if current_segment["text"]:
                        segment_list.append(current_segment.copy())
                        timestamp = f"[{current_segment['start']:.2f}–{current_segment['end']:.2f}]"
                        transcript_lines.append(f"{timestamp} {current_segment['text'].strip()}")
                    
                    current_segment = {
                        "start": start_time,
                        "end": end_time,
                        "text": content
                    }
                else:
                    current_segment["end"] = end_time
                    current_segment["text"] += " " + content
            elif item['type'] == 'punctuation':
                if current_segment["text"]:
                    current_segment["text"] += item['alternatives'][0]['content']
        
        # Add the last segment
        if current_segment["text"]:
            segment_list.append(current_segment)
            timestamp = f"[{current_segment['start']:.2f}–{current_segment['end']:.2f}]"
            transcript_lines.append(f"{timestamp} {current_segment['text'].strip()}")
        
        # Get duration from the last item
        duration = float(items[-1]['end_time']) if items and 'end_time' in items[-1] else 0.0
        
        # Upload transcript text to S3
        transcript_s3_key = s3_manager.generate_s3_key(
            podcast_id, episode_id, 'transcripts', 'transcript.txt'
        )
        
        transcript_content = "\n".join(transcript_lines)
        transcript_s3_url = s3_manager.upload_text_content(
            transcript_content, 
            transcript_s3_key, 
            'text/plain'
        )
        
        if not transcript_s3_url:
            raise Exception("Failed to upload transcript to S3")
        
        # Upload segments JSON to S3
        segments_s3_key = s3_manager.generate_s3_key(
            podcast_id, episode_id, 'transcripts', 'segments.json'
        )
        
        segments_data = {
            "model": f"amazon-transcribe-{model_name}",
            "language": detected_language,
            "language_probability": 0.99,
            "duration": duration,
            "segments": segment_list
        }
        
        segments_s3_url = s3_manager.upload_text_content(
            json.dumps(segments_data, indent=2, ensure_ascii=False),
            segments_s3_key,
            'application/json'
        )
        
        # Clean up transcription job
        try:
            client.delete_transcription_job(TranscriptionJobName=job_name)
        except Exception as e:
            logger.warning(f"Failed to clean up transcription job: {e}")
        
        logger.info(f"Amazon Transcribe S3 transcription completed:")
        logger.info(f"  - Language: {detected_language}")
        logger.info(f"  - Duration: {duration:.2f}s")
        logger.info(f"  - Segments: {len(segment_list)}")
        logger.info(f"  - Transcript: {transcript_s3_url}")
        logger.info(f"  - Segments: {segments_s3_url}")
        
        return {
            "transcript_s3_url": transcript_s3_url,
            "segments_s3_url": segments_s3_url,
            "language": detected_language,
            "language_probability": 0.99,
            "duration": duration,
            "segments": segment_list
        }
        
    except Exception as e:
        logger.error(f"Error in S3 transcription with Amazon Transcribe: {e}")
        raise

def get_transcript_summary_stats(transcript_path: str) -> Dict[str, Any]:
    """Get basic statistics about a transcript."""
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        lines = content.strip().split("\n")
        word_count = len(content.split())
        char_count = len(content)
        
        return {
            "line_count": len(lines),
            "word_count": word_count,
            "character_count": char_count,
            "estimated_reading_time_minutes": round(word_count / 200, 1)  # Assume 200 WPM
        }
        
    except Exception as e:
        logger.error(f"Failed to get transcript stats: {e}")
        return {}