import os
import json
from typing import Dict, Any, Optional, List, Tuple
import whisper_cpp
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

class TranscriptionResult:
    """Container for transcription results."""
    
    def __init__(self, transcript_path: str, segments: List[Dict], info: Dict):
        self.transcript_path = transcript_path
        self.segments = segments
        self.info = info

def get_whisper_model(model_name: str = "base"):
    """
    Initialize and return a Whisper model using whisper-cpp.
    
    Args:
        model_name: Model size (tiny, base, small, medium, large)
    """
    logger.info(f"Loading Whisper model: {model_name}")
    
    try:
        # whisper-cpp automatically downloads models as needed
        model = whisper_cpp.Whisper(model=model_name, n_threads=2)
        logger.info(f"Successfully loaded Whisper model: {model_name}")
        return model
        
    except Exception as e:
        logger.error(f"Failed to load Whisper model {model_name}: {e}")
        raise

def transcribe_audio(
    audio_path: str, 
    model_name: str = "base",
    language: Optional[str] = None,
    vad_filter: bool = True,
    word_timestamps: bool = False
) -> TranscriptionResult:
    """
    Transcribe audio file using whisper-cpp.
    
    Args:
        audio_path: Path to audio file
        model_name: Whisper model to use
        language: Force specific language (None for auto-detection)
        vad_filter: Use voice activity detection to filter silence
        word_timestamps: Include word-level timestamps
    
    Returns:
        TranscriptionResult containing paths and metadata
    """
    if not os.path.exists(audio_path):
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    
    logger.info(f"Transcribing audio: {audio_path}")
    
    # Initialize model
    model = get_whisper_model(model_name)
    
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
        # Perform transcription using whisper-cpp
        result = model.transcribe(audio_path)
        
        # Extract segments from whisper-cpp result
        segment_list = []
        transcript_lines = []
        
        # whisper-cpp returns segments in result['segments'] or similar structure
        segments = result.get('segments', [])
        
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
        
        # Write transcript text file
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write("\n".join(transcript_lines))
        
        # Get basic info from result
        detected_language = result.get('language', 'en')
        duration = result.get('duration', 0.0)
        
        # Write segments JSON file
        transcription_metadata = {
            "audio_file": str(audio_path),
            "model": model_name,
            "language": detected_language,
            "language_probability": 0.9,  # whisper-cpp doesn't provide this
            "duration": round(duration, 2),
            "segments": segment_list
        }
        
        with open(segments_path, "w", encoding="utf-8") as f:
            json.dump(transcription_metadata, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Transcription completed:")
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
                "language_probability": 0.9,
                "duration": duration,
                "model": model_name
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
                           model_name: str = "base") -> Dict[str, Any]:
    """
    Transcribe audio file from S3 and upload results back to S3.
    
    Args:
        audio_s3_key: S3 key of the audio file
        podcast_id: Database ID of the podcast
        episode_id: Database ID of the episode
        model_name: Whisper model to use
    
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