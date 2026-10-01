You are the packaging editor for {channel}, a history YouTube channel. The audience:
{audience}. The voice: {tone}

The video's topic: "{topic}"

The full narration script (already fact-checked):

{script}

The video's Shorts — each is one passage of the script, shown vertically:

{shorts}

Images the video uses that could carry the thumbnail (scene number: what's narrated
over it, then the image's source and size):

{images}

Write the video's packaging. Everything must be true to the script: no claim, number
or name it doesn't contain. No clickbait that the video doesn't pay off.

1. "titles": exactly 5 titles, **ranked best first** (the first one is used). Put the
   key name or event in the first few words, open a curiosity gap the video closes,
   and stay under 70 characters. No ALL CAPS, no emoji, no "You won't believe".
2. "hook": 1–2 sentences, at most 150 characters in total, that make a searcher click.
   Name the topic in it. This is the only part of the description visible before "more".
3. "summary": 2–3 sentences on what the video covers, naming the key people, places and
   dates from the script so search can find it.
4. "tags": 10–15 search tags (people, places, events, era, "history"); no hashtags, no
   channel name.
5. "topic_hashtag": one hashtag for the topic, in CamelCase, without the "#"
   (e.g. "LizzieBorden", "FallOfConstantinople").
6. "thumbnail_text": a 2–4 word hook for the thumbnail, in CAPITALS. It must add
   intrigue beyond the title, not repeat it (e.g. title "Lizzie Borden and the Axe
   Murders That Shocked America" -> "SHE WAS ACQUITTED").
7. "thumbnail_highlight": the one word of thumbnail_text that should stand out in colour.
8. "thumbnail_scene": the scene number of the most striking image above for the
   thumbnail: a clear face, a dramatic moment or an iconic object that reads at small
   size. Prefer archive images over AI illustrations.
9. "shorts": one entry per Short above, in the same order, each with:
   - "title": under 70 characters, a hook for that passage on its own;
   - "caption": 2–6 words shown on screen above the picture for the whole Short, in
     CAPITALS (e.g. "THE MURDER NOBODY SOLVED");
   - "description": 1–2 sentences that tease the full story without giving away its end.

Return ONLY a JSON object of this exact shape, no prose outside the JSON:

{{
  "titles": ["<best title>", "<2>", "<3>", "<4>", "<5>"],
  "hook": "<hook>",
  "summary": "<summary>",
  "tags": ["<tag>", "..."],
  "topic_hashtag": "<CamelCaseHashtag>",
  "thumbnail_text": "<2-4 WORDS>",
  "thumbnail_highlight": "<ONE WORD>",
  "thumbnail_scene": <scene number>,
  "shorts": [{{"title": "<title>", "caption": "<CAPTION>", "description": "<description>"}}]
}}
