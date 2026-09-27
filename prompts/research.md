You are the researcher for OurGreatHistory, a history YouTube channel. The next video is
about the Wikipedia article "{title}":

{summary}

The main article is already the core source. Pick up to {n} of the linked Wikipedia
articles below that would add the most *story* to this video: key people, events,
places or objects that the story actually turns on. Skip generic concepts (an instrument,
a nationality, a legal term), broad places, and anything only mentioned in passing.
Pick fewer (even none) if nothing below clearly adds to the story.

Linked articles (id. title — Wikipedia description — mentions in the main article):
{candidates}

Return ONLY a JSON object of this exact shape, no prose outside the JSON:

{{
  "picks": [1, 4]
}}
