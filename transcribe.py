import os
import json
from typing import Dict, Any, Optional, List, Tuple
from faster_whisper import WhisperModel
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

class TranscriptionResult:
    """Container for transcription results."""
    
    def __init__(self, transcript_path: str, segments: List[Dict], info: Dict):
        self.transcript_path = transcript_path
        self.segments = segments
        self.info = info

def get_whisper_model(model_name: str = "medium", device: str = "auto", compute_type: str = "auto") -> WhisperModel:
    """
    Initialize and return a Whisper model.
    
    Args:
        model_name: Model size (tiny, base, small, medium, large-v3)
        device: Device to run on (auto, cpu, cuda)
        compute_type: Compute type (auto, int8, float16, float32)
    """
    logger.info(f"Loading Whisper model: {model_name}")
    
    try:
        model = WhisperModel(
            model_name, 
            device=device, 
            compute_type=compute_type
        )
        logger.info(f"Successfully loaded Whisper model: {model_name}")
        return model
        
    except Exception as e:
        logger.error(f"Failed to load Whisper model {model_name}: {e}")
        raise

def transcribe_audio(
    audio_path: str, 
    model_name: str = "medium",
    language: Optional[str] = None,
    vad_filter: bool = True,
    word_timestamps: bool = False
) -> TranscriptionResult:
    """
    Transcribe audio file using faster-whisper.
    
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
        # Perform transcription
        segments, info = model.transcribe(
            audio_path,
            language=language,
            vad_filter=vad_filter,
            word_timestamps=word_timestamps
        )
        
        # Convert segments to list and collect transcript
        segment_list = []
        transcript_lines = []
        
        for segment in segments:
            segment_dict = {
                "start": round(segment.start, 2),
                "end": round(segment.end, 2),
                "text": segment.text.strip()
            }
            
            # Add word timestamps if requested
            if word_timestamps and hasattr(segment, 'words') and segment.words:
                segment_dict["words"] = [
                    {
                        "start": round(word.start, 2),
                        "end": round(word.end, 2),
                        "word": word.word,
                        "probability": round(word.probability, 3)
                    }
                    for word in segment.words
                ]
            
            segment_list.append(segment_dict)
            
            # Format for text transcript
            timestamp = f"[{segment_dict['start']:.2f}–{segment_dict['end']:.2f}]"
            transcript_lines.append(f"{timestamp} {segment_dict['text']}")
        
        # Write transcript text file
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write("\n".join(transcript_lines))
        
        # Write segments JSON file
        transcription_metadata = {
            "audio_file": str(audio_path),
            "model": model_name,
            "language": info.language,
            "language_probability": round(info.language_probability, 3),
            "duration": round(info.duration, 2),
            "duration_after_vad": round(info.duration_after_vad, 2) if hasattr(info, 'duration_after_vad') else None,
            "all_language_probs": info.all_language_probs if hasattr(info, 'all_language_probs') else None,
            "segments": segment_list
        }
        
        with open(segments_path, "w", encoding="utf-8") as f:
            json.dump(transcription_metadata, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Transcription completed:")
        logger.info(f"  - Language: {info.language} (confidence: {info.language_probability:.3f})")
        logger.info(f"  - Duration: {info.duration:.2f}s")
        logger.info(f"  - Segments: {len(segment_list)}")
        logger.info(f"  - Transcript: {transcript_path}")
        logger.info(f"  - Segments: {segments_path}")
        
        return TranscriptionResult(
            transcript_path=str(transcript_path),
            segments=segment_list,
            info={
                "language": info.language,
                "language_probability": info.language_probability,
                "duration": info.duration,
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