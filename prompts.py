"""
Prompts for podcast summarization using LLMs.
"""

SYSTEM_PROMPT = """You are an expert podcast summarizer and content editor. You specialize in extracting key insights from spoken-word content while removing irrelevant or promotional material."""

def build_user_prompt(transcript_text: str, options: dict = None) -> str:
    """
    Build the user prompt for podcast summarization.
    
    Args:
        transcript_text: The podcast transcript to summarize
        options: Configuration options for summarization
    
    Returns:
        Formatted prompt string
    """
    # Default options
    default_options = {
        "flag_ads_before_deletion": True,
        "summary_length": "detailed",
        "quotes_target": 7,
        "takeaways_target": 6,
        "include_speaker_profiles": True,
        "preserve_sequence": True,
        "tone": "professional, clear, scan-friendly"
    }
    
    # Merge with provided options
    if options:
        default_options.update(options)
    
    opts = default_options
    
    prompt = f"""ROLE
You are an expert podcast summarizer and content editor. You specialize in extracting key insights from spoken-word content while removing irrelevant or promotional material.

INPUT
I will provide a podcast transcript as text. Read the content in full and follow the instructions below.

OPTIONS
- flag_ads_before_deletion: {opts['flag_ads_before_deletion']}
- summary_length: "{opts['summary_length']}"
- quotes_target: {opts['quotes_target']}
- takeaways_target: {opts['takeaways_target']}
- include_speaker_profiles: {opts['include_speaker_profiles']}
- preserve_sequence: {opts['preserve_sequence']}
- tone: "{opts['tone']}"

DEFINITIONS & GUARDRAILS
- "Ad/Promo segment": sponsor reads, discount codes, URL/QR callouts, affiliate mentions, unrelated brand spots, platform membership plugs, cross-promo for other shows, house ads, readouts of terms & conditions.
- Remove all ad/promo content from the final sections. If flag_ads_before_deletion=true, list and label them first, then proceed with removal.
- If timecodes exist, use them. If not, give approximate line ranges for flagged ads.
- Quotes must be exact (verbatim) and attributed when the speaker can be identified (e.g., "Whitney Wolfe Herd" vs. "Host/Guest" if unclear). Do not fabricate or paraphrase quotes.
- Maintain conversational flow after ad removal. Do not leave orphaned intros/outros that only reference removed ads.
- If the transcript includes pre-roll/post-roll ads, treat them as ads unless they contain essential editorial content.

TASKS
1) Ad Removal (with optional flagging)
   - If flag_ads_before_deletion=true:
     • Produce a short list titled "Flagged Ads/Promos (to be removed)" with timecodes or approximate ranges, brand/sponsor name if discernible, and one-sentence rationale.
   - Then remove those segments and proceed using the cleaned transcript for all subsequent steps.

2) Detailed Summary
   - Provide a well-structured, chronological summary capturing the episode's core ideas, arguments, narratives, and turning points.
   - Use short paragraphs with clear topic signals. Avoid filler.

3) Key Quotes ({opts['quotes_target']})
   - Extract the most insightful or memorable direct quotes.
   - Requirements: exact wording, quotation marks, speaker attribution (if known).
   - Prefer quotes that illuminate the thesis, turning points, or novel insights.

4) Key Takeaways ({opts['takeaways_target']})
   - Bullet points of the episode's most important lessons, actionable insights, or thought-provoking ideas.
   - Keep each bullet crisp and self-contained.

5) {'Speaker Profiles (if include_speaker_profiles=true)' if opts['include_speaker_profiles'] else 'Skip Speaker Profiles'}
   - For each identifiable participant (Host, Guest(s)):
     • Name & Role
     • Perspective/stance (one sentence)
     • Core themes/areas of expertise (1–3 bullets)
     • Representative quote (short, verbatim, with quotation marks)

6) Quality & Integrity Checks
   - Confirm that no sponsor or promotional language appears in Summary, Key Quotes, Key Takeaways, or Speaker Profiles.
   - Confirm all quotes are present verbatim in the (cleaned) transcript.
   - If essential context seems missing (e.g., episode title/date), note "Not available in transcript."

OUTPUT FORMAT (use these exact headings)
{'- Flagged Ads/Promos (to be removed)' if opts['flag_ads_before_deletion'] else ''}
- Summary
- Key Quotes
- Key Takeaways
{'- Speaker Profiles' if opts['include_speaker_profiles'] else ''}

STYLE
- Professional, concise, easy to scan.
- Use plain punctuation (commas, periods, semicolons). Avoid em dashes.
- No marketing language. No filler.

TRANSCRIPT
{transcript_text}"""

    return prompt

def build_simple_summary_prompt(transcript_text: str) -> str:
    """
    Build a simplified prompt for basic summarization.
    
    Args:
        transcript_text: The podcast transcript to summarize
    
    Returns:
        Simplified prompt string
    """
    return f"""Please provide a clear, concise summary of this podcast transcript. 

Include:
1. Main topics discussed
2. Key points and insights
3. Important quotes (exact wording with attribution)
4. Main takeaways

Remove any advertisements, sponsor mentions, or promotional content.

Transcript:
{transcript_text}"""

def build_quotes_extraction_prompt(transcript_text: str, target_count: int = 7) -> str:
    """
    Build a prompt specifically for extracting quotes.
    
    Args:
        transcript_text: The podcast transcript
        target_count: Target number of quotes to extract
    
    Returns:
        Quote extraction prompt
    """
    return f"""Extract the {target_count} most insightful and memorable quotes from this podcast transcript.

Requirements:
- Use exact wording (verbatim)
- Include quotation marks
- Attribute to speaker when identifiable
- Focus on quotes that reveal key insights, turning points, or novel ideas
- Exclude any promotional or advertisement content

Format each quote as:
"Quote text here" - Speaker Name (or Host/Guest if name unknown)

Transcript:
{transcript_text}"""

def build_takeaways_prompt(transcript_text: str, target_count: int = 6) -> str:
    """
    Build a prompt specifically for extracting key takeaways.
    
    Args:
        transcript_text: The podcast transcript
        target_count: Target number of takeaways
    
    Returns:
        Takeaways extraction prompt
    """
    return f"""Extract the {target_count} most important takeaways from this podcast transcript.

Focus on:
- Actionable insights
- Important lessons
- Thought-provoking ideas
- Practical advice
- Key conclusions

Format as bullet points. Keep each takeaway crisp and self-contained.
Exclude any promotional or advertisement content.

Transcript:
{transcript_text}"""