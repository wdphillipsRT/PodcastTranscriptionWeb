import os
import asyncio
import httpx
from openai import AsyncOpenAI
from typing import Optional, Dict, Any
import logging
from tenacity import retry, stop_after_attempt, wait_exponential
from prompts import SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)

class SummarizationError(Exception):
    """Custom exception for summarization errors."""
    pass

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def summarize_with_openrouter(
    text: str, 
    model: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None
) -> str:
    """
    Summarize transcript using OpenRouter API.
    
    Args:
        text: The transcript text to summarize
        model: Model to use (defaults to env var OPENROUTER_MODEL)
        options: Summarization options
    
    Returns:
        Generated summary text
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise SummarizationError("OPENROUTER_API_KEY environment variable not set")
    
    base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    model = model or os.getenv("OPENROUTER_MODEL")
    
    if not model:
        raise SummarizationError("No OpenRouter model specified. Set OPENROUTER_MODEL env var or pass model parameter")
    
    # Truncate text if too long (keep last part which usually has conclusions)
    max_chars = 280000  # Conservative limit for most models
    if len(text) > max_chars:
        logger.warning(f"Transcript too long ({len(text)} chars), truncating to {max_chars} chars")
        # Take from the middle and end to preserve context
        middle_start = len(text) // 4
        text = text[middle_start:middle_start + max_chars]
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://podcast-transcription.local",
        "X-Title": "Podcast Transcription Pipeline"
    }
    
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(text, options)}
        ],
        "temperature": 0.2,
        "max_tokens": 4000
    }
    
    logger.info(f"Sending summarization request to OpenRouter (model: {model})")
    
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload
            )
            
            if response.status_code == 429:
                logger.warning("Rate limited by OpenRouter, will retry")
                raise httpx.HTTPStatusError("Rate limited", request=response.request, response=response)
            
            response.raise_for_status()
            
            result = response.json()
            
            if "choices" not in result or not result["choices"]:
                raise SummarizationError(f"Invalid response from OpenRouter: {result}")
            
            summary = result["choices"][0]["message"]["content"]
            
            if not summary or summary.strip() == "":
                raise SummarizationError("Empty summary returned from OpenRouter")
            
            logger.info("Successfully generated summary with OpenRouter")
            return summary.strip()
            
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error from OpenRouter: {e.response.status_code} - {e.response.text}")
        raise SummarizationError(f"OpenRouter API error: {e.response.status_code}")
    except Exception as e:
        logger.error(f"OpenRouter summarization failed: {e}")
        raise SummarizationError(f"OpenRouter error: {str(e)}")

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def summarize_with_openai(
    text: str, 
    model: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None
) -> str:
    """
    Summarize transcript using OpenAI API.
    
    Args:
        text: The transcript text to summarize
        model: Model to use (defaults to env var OPENAI_MODEL)
        options: Summarization options
    
    Returns:
        Generated summary text
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SummarizationError("OPENAI_API_KEY environment variable not set")
    
    model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    
    # Truncate text if too long
    max_chars = 300000  # Conservative limit for GPT-4
    if len(text) > max_chars:
        logger.warning(f"Transcript too long ({len(text)} chars), truncating to {max_chars} chars")
        # Take from the middle and end to preserve context
        middle_start = len(text) // 4
        text = text[middle_start:middle_start + max_chars]
    
    logger.info(f"Sending summarization request to OpenAI (model: {model})")
    
    try:
        client = AsyncOpenAI(api_key=api_key)
        
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(text, options)}
            ],
            temperature=0.2,
            max_tokens=4000
        )
        
        if not response.choices or not response.choices[0].message.content:
            raise SummarizationError("Empty response from OpenAI")
        
        summary = response.choices[0].message.content.strip()
        
        logger.info("Successfully generated summary with OpenAI")
        return summary
        
    except Exception as e:
        logger.error(f"OpenAI summarization failed: {e}")
        raise SummarizationError(f"OpenAI error: {str(e)}")

async def summarize_transcript(
    text: str,
    provider: str = "openai",
    model: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None
) -> str:
    """
    Summarize transcript using the specified provider.
    
    Args:
        text: The transcript text to summarize
        provider: API provider ("openai" or "openrouter")
        model: Model to use (provider-specific)
        options: Summarization options
    
    Returns:
        Generated summary text
    """
    if not text or text.strip() == "":
        raise SummarizationError("Empty transcript provided")
    
    logger.info(f"Starting transcript summarization with {provider}")
    
    if provider.lower() == "openrouter":
        return await summarize_with_openrouter(text, model, options)
    elif provider.lower() == "openai":
        return await summarize_with_openai(text, model, options)
    else:
        raise SummarizationError(f"Unknown provider: {provider}. Use 'openai' or 'openrouter'")

def save_summary(summary: str, output_path: str) -> str:
    """
    Save summary to markdown file.
    
    Args:
        summary: The generated summary text
        output_path: Path where to save the summary
    
    Returns:
        Path to saved file
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(summary)
        
        logger.info(f"Summary saved to: {output_path}")
        return output_path
        
    except Exception as e:
        logger.error(f"Failed to save summary to {output_path}: {e}")
        raise

def load_summary(summary_path: str) -> str:
    """Load summary from file."""
    try:
        with open(summary_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.error(f"Failed to load summary from {summary_path}: {e}")
        raise

async def generate_and_save_summary(
    transcript_path: str,
    summary_path: str,
    provider: str = "openai",
    model: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None
) -> str:
    """
    Complete workflow: load transcript, generate summary, save to file.
    
    Args:
        transcript_path: Path to transcript file
        summary_path: Path where to save summary
        provider: API provider to use
        model: Model to use
        options: Summarization options
    
    Returns:
        Path to saved summary file
    """
    # Load transcript
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            transcript_text = f.read()
    except Exception as e:
        raise SummarizationError(f"Failed to load transcript from {transcript_path}: {e}")
    
    # Generate summary
    summary = await summarize_transcript(transcript_text, provider, model, options)
    
    # Save summary
    return save_summary(summary, summary_path)

# Convenience function for sync usage
def summarize_transcript_sync(
    text: str,
    provider: str = "openai",
    model: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None
) -> str:
    """Synchronous wrapper for summarize_transcript."""
    return asyncio.run(summarize_transcript(text, provider, model, options))