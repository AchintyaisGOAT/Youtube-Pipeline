You are writing YouTube metadata for a history/education video. YouTube category:
{category}

Topic/working title: "{topic}"

Full narration script:

{script}

{sources_block}

Produce:

1. 3-5 candidate titles. Each should be front-loaded with the key term/name, create a
   curiosity gap without being clickbait or misleading, and stay under 70 characters.
2. A description that includes: a 2-3 sentence hook/summary, a chapter list with
   timestamps inferred from the script's structure (use MM:SS starting at 00:00 — best
   estimate, this is not frame-accurate), and ends with the image sources block
   verbatim if one was provided above (attribution + license, required for legal
   compliance — do not paraphrase or drop it).
3. 8-15 search tags relevant to the topic (no hashtags, no channel name).

Do not editorialize or add claims beyond what's in the script. This video uses
synthetic (AI) narration — do not claim otherwise anywhere in the description.

Return ONLY a JSON object of this exact shape, no prose outside the JSON:

{{
  "titles": ["<title 1>", "<title 2>", "..."],
  "description": "<full description text, chapters + sources block included>",
  "tags": ["<tag 1>", "<tag 2>", "..."]
}}
