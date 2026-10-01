You are the picture and sound editor for PantherTellsHistory, a history YouTube channel. The
video is about: "{title}" ({summary})

Each numbered scene below is a few seconds of narration, shown over ONE historical image.
For every scene, give:

1. "sfx" — a sound effect to play as the scene starts, or null. Use effects sparingly
   (roughly one scene in four at most, never two scenes in a row). whoosh, impact and
   riser can mark a big turn or a shocking reveal anywhere; every other effect (bell,
   crowd, thunder, door, paper...) ONLY when that scene's narration actually mentions the
   thing that makes the sound. Only these are available: {sfx_tags}

2. "overlay" — on-screen text, or null. Use it sparingly:
   - {{"kind": "label", "text": "..."}} when the story arrives at a new named place or a
     date: short, e.g. "FALL RIVER, MASSACHUSETTS · 1892". Not for objects or rooms.
   - {{"kind": "number", "text": "..."}} for one striking figure the narration states,
     e.g. "$300,000" or "40 WHACKS".
   - {{"kind": "chapter", "text": "..."}} at the first scene of a new part of the story:
     2–5 words, e.g. "THE MURDERS", "THE TRIAL", "AFTERMATH". At most {max_chapters}.
   Every name, place, date and number in an overlay must appear in that scene's
   narration. Never add a fact the narration doesn't state.

Scenes:
{scenes}

Return ONLY a JSON object of this exact shape, one entry per scene id, no prose outside
the JSON:

{{
  "scenes": [
    {{"id": 1, "sfx": null, "overlay": null}},
    {{"id": 2, "sfx": "whoosh", "overlay": {{"kind": "label", "text": "..."}}}}
  ]
}}
