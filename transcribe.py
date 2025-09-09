import os
import json
from typing import Dict, Any, Optional, List, Tuple
from openai import OpenAI
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

def _compress_audio_for_api(audio_path: str) -> str:
    """
    Compress audio file to fit within OpenAI API's 25MB limit.
    Uses ffmpeg if available, otherwise reduces quality.
    """
    import subprocess
    import tempfile
    
    # Create compressed version with lower bitrate
    compressed_path = audio_path.replace('.mp3', '_compressed.mp3')
    
    try:
        # Use ffmpeg to compress audio if available
        subprocess.run([
            'ffmpeg', '-i', audio_path, 
            '-acodec', 'mp3', 
            '-ab', '64k',  # Lower bitrate for smaller size
            '-ar', '16000',  # Lower sample rate
            '-ac', '1',      # Mono channel
            '-y',            # Overwrite output
            compressed_path
        ], check=True, capture_output=True)
        
        logger.info(f"Compressed audio using ffmpeg from {os.path.getsize(audio_path) / (1024*1024):.1f}MB to {os.path.getsize(compressed_path) / (1024*1024):.1f}MB")
        return compressed_path
        
    except (subprocess.CalledProcessError, FileNotFoundError):
        # If ffmpeg not available, try pydub as fallback
        try:
            from pydub import AudioSegment
            from pydub.utils import which
            
            # Check if ffmpeg/ffprobe are available for pydub
            if not which("ffmpeg") or not which("ffprobe"):
                logger.warning("ffmpeg/ffprobe not available for pydub")
                return audio_path
                
            logger.info("Using pydub for audio compression")
            
            # Load audio with pydub
            audio = AudioSegment.from_mp3(audio_path)
            
            # Compress: lower bitrate, sample rate, and make mono
            compressed_audio = audio.set_frame_rate(16000).set_channels(1)
            
            # Export with lower bitrate
            compressed_audio.export(compressed_path, format="mp3", bitrate="64k")
            
            logger.info(f"Compressed audio using pydub from {os.path.getsize(audio_path) / (1024*1024):.1f}MB to {os.path.getsize(compressed_path) / (1024*1024):.1f}MB")
            return compressed_path
            
        except (ImportError, Exception) as e:
            logger.warning(f"Pydub compression failed: {e}")
            # Last resort: return original and let API reject it with a better error message
            return audio_path

class TranscriptionResult:
    """Container for transcription results."""
    
    def __init__(self, transcript_path: str, segments: List[Dict], info: Dict):
        self.transcript_path = transcript_path
        self.segments = segments
        self.info = info

def get_openai_client():
    """
    Initialize and return OpenAI client for Whisper API calls.
    
    Returns:
        OpenAI client instance
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY environment variable not set")
    
    client = OpenAI(api_key=api_key)
    logger.info("Successfully initialized OpenAI client for Whisper API")
    return client

def transcribe_audio(
    audio_path: str, 
    model_name: str = "tiny",
    language: Optional[str] = None,
    vad_filter: bool = True,
    word_timestamps: bool = False
) -> TranscriptionResult:
    """
    Transcribe audio file using OpenAI's Whisper API (cloud-based).
    
    Args:
        audio_path: Path to audio file
        model_name: Whisper model to use (whisper-1)
        language: Force specific language (None for auto-detection)
        vad_filter: Not used with API (included for compatibility)
        word_timestamps: Include word-level timestamps
    
    Returns:
        TranscriptionResult containing paths and metadata
    """
    if not os.path.exists(audio_path):
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    
    logger.info(f"Transcribing audio via OpenAI Whisper API: {audio_path}")
    
    # Initialize OpenAI client
    client = get_openai_client()
    
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
        # Check file size - OpenAI API has a 25MB limit
        file_size = os.path.getsize(audio_path)
        max_size = 25 * 1024 * 1024  # 25MB in bytes
        
        if file_size > max_size:
            logger.warning(f"Audio file is {file_size / (1024*1024):.1f}MB, exceeding OpenAI's 25MB limit")
            # Try to compress the audio to reduce file size
            compressed_path = _compress_audio_for_api(audio_path)
            compressed_size = os.path.getsize(compressed_path) if os.path.exists(compressed_path) else file_size
            
            if compressed_path != audio_path and compressed_size <= max_size:
                logger.info(f"Successfully compressed audio to {compressed_size / (1024*1024):.1f}MB")
                upload_path = compressed_path
            elif compressed_path != audio_path:
                logger.warning(f"Compressed audio is still {compressed_size / (1024*1024):.1f}MB, exceeding 25MB limit")
                upload_path = compressed_path  # Try anyway, might work
            else:
                logger.error(f"Cannot compress audio file. Size {file_size / (1024*1024):.1f}MB exceeds OpenAI API limit of 25MB")
                raise ValueError(f"Audio file too large ({file_size / (1024*1024):.1f}MB) for OpenAI Whisper API. Maximum size is 25MB. Please use a smaller audio file or consider splitting it into segments.")
        else:
            upload_path = audio_path
        
        # Read audio file for API upload
        with open(upload_path, "rb") as audio_file:
            # Call OpenAI Whisper API
            transcript_response = client.audio.transcriptions.create(
                model="whisper-1",  # OpenAI's Whisper model
                file=audio_file,
                language=language,  # Optional language hint
                response_format="verbose_json" if word_timestamps else "json",
                timestamp_granularities=["segment"] if word_timestamps else None
            )
        
        # Clean up compressed file if we created one
        if file_size > max_size and upload_path != audio_path and os.path.exists(upload_path):
            os.unlink(upload_path)
        
        # Extract transcript text and segments
        transcript_text = transcript_response.text
        segments = getattr(transcript_response, 'segments', [])
        detected_language = getattr(transcript_response, 'language', language or 'en')
        duration = getattr(transcript_response, 'duration', 0.0)
        
        # Process segments for our format
        segment_list = []
        transcript_lines = []
        
        if segments:
            for segment in segments:
                segment_dict = {
                    "start": round(segment.get('start', 0), 2),
                    "end": round(segment.get('end', 0), 2),
                    "text": segment.get('text', '').strip()
                }
                
                segment_list.append(segment_dict)
                
                # Format for text transcript
                timestamp = f"[{segment_dict['start']:.2f}–{segment_dict['end']:.2f}]"
                transcript_lines.append(f"{timestamp} {segment_dict['text']}")
        else:
            # If no segments available, use the full text
            segment_dict = {
                "start": 0.0,
                "end": duration,
                "text": transcript_text.strip()
            }
            segment_list.append(segment_dict)
            transcript_lines.append(f"[0.00–{duration:.2f}] {transcript_text}")
        
        # Write transcript text file
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write("\n".join(transcript_lines))
        
        # Write segments JSON file
        transcription_metadata = {
            "audio_file": str(audio_path),
            "model": "whisper-1",
            "language": detected_language,
            "language_probability": 0.95,  # OpenAI API doesn't provide this
            "duration": duration,
            "segments": segment_list
        }
        
        with open(segments_path, "w", encoding="utf-8") as f:
            json.dump(transcription_metadata, f, indent=2, ensure_ascii=False)
        
        logger.info(f"OpenAI Whisper API transcription completed:")
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
                "language_probability": 0.95,
                "duration": duration,
                "model": "whisper-1"
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
                           model_name: str = "whisper-1") -> Dict[str, Any]:
    """
    Transcribe audio file from S3 using OpenAI Whisper API and upload results back to S3.
    
    Args:
        audio_s3_key: S3 key of the audio file
        podcast_id: Database ID of the podcast
        episode_id: Database ID of the episode
        model_name: Whisper model to use (whisper-1 for OpenAI API)
    
    Returns:
        Dictionary with transcription results and S3 URLs
    """
    from s3_utils import get_s3_manager
    import tempfile
    import json
    
    s3_manager = get_s3_manager()
    
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_audio_path = os.path.join(temp_dir, "audio.mp3")
        
        try:
            # Download audio from S3 to temporary location
            logger.info(f"Downloading audio from S3: {audio_s3_key}")
            if not s3_manager.download_file(audio_s3_key, temp_audio_path):
                raise Exception("Failed to download audio from S3")
            
            # Transcribe the audio
            logger.info(f"Transcribing audio with model: {model_name}")
            result = transcribe_audio(temp_audio_path, model_name)
            
            if not result:
                raise Exception("Transcription failed")
            
            # Upload transcript text to S3
            transcript_s3_key = s3_manager.generate_s3_key(
                podcast_id, episode_id, 'transcripts', 'transcript.txt'
            )
            
            # Read the transcript content
            with open(result.transcript_path, 'r', encoding='utf-8') as f:
                transcript_content = f.read()
            
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
                "model": model_name,
                "language": result.info.get("language"),
                "language_probability": result.info.get("language_probability"),
                "duration": result.info.get("duration"),
                "segments": result.segments
            }
            
            segments_s3_url = s3_manager.upload_text_content(
                json.dumps(segments_data, indent=2, ensure_ascii=False),
                segments_s3_key,
                'application/json'
            )
            
            logger.info(f"Transcription completed and uploaded to S3:")
            logger.info(f"  - Transcript: {transcript_s3_url}")
            logger.info(f"  - Segments: {segments_s3_url}")
            
            return {
                "transcript_s3_url": transcript_s3_url,
                "segments_s3_url": segments_s3_url,
                "language": result.info.get("language"),
                "language_probability": result.info.get("language_probability"),
                "duration": result.info.get("duration"),
                "segments": result.segments
            }
            
        except Exception as e:
            logger.error(f"Error in S3 transcription: {e}")
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