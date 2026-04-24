#!/usr/bin/env python3
"""
Vision parser: sends schedule screenshot to OpenAI GPT-4o Vision,
extracts shifts for מאור פורמן.
Upscales small images before sending to improve OCR accuracy.
"""

import os
import io
import json
import base64
import re
import logging
from PIL import Image
from openai import OpenAI

logger = logging.getLogger(__name__)
client = OpenAI(api_key=os.environ["OPEN_API_KEY"])
_last_debug = ""


def get_last_debug() -> str:
    return _last_debug

SHIFT_TIMES = {
    "בוקר": ("07:00", "15:00"),
    "צהריים": ("15:00", "23:00"),
    "לילה": ("23:00", "07:00"),
    "כפולה בוקר": ("07:00", "19:00"),
    "כפולה לילה": ("15:00", "03:00"),
}

MIN_WIDTH = 2000  # upscale if image is narrower than this

VISION_PROMPT = """You are analyzing a Hebrew work schedule table (סידור עבודה).
The table is read RIGHT TO LEFT. Column headers are days/dates. Row headers on the RIGHT side are employee names.

TASK: Find every cell where the name "מאור פורמן" (Maor Furman) appears.

The table structure:
- Rightmost column = employee names / role names
- Top row = day/date headers (Sunday=ראשון, Monday=שני, etc.)
- Each day is split into 3 sub-columns: בוקר (morning), צהריים (afternoon), לילה (night)
- A cell may contain a name, "12/12", or be empty

For EACH day where "מאור פורמן" appears, extract:

1. date: the date shown in that day's column header (DD/MM or DD/MM/YYYY)
2. column: which sub-column — "בוקר", "צהריים", or "לילה"
3. shift_type: apply 12/12 logic:
   - if name is in "בוקר" AND the "צהריים" cell of the same day contains "12/12" → "כפולה בוקר"
   - if name is in "צהריים" AND the "לילה" cell of the same day contains "12/12" → "כפולה לילה"
   - otherwise → same as column value
4. location: look at the BACKGROUND COLOR of the row where מאור פורמן appears:
   - GREEN background → "הרצליה"
   - any other color → "רמ\"ש"
5. role: read the text in the ROW HEADER (rightmost cell of that row), e.g. "מאבטח 1 אקדח", "צלף", "שיפוצים"

Return ONLY valid JSON, no extra text:
{
  "debug": "brief description of what you see in the table",
  "shifts": [
    {
      "date": "DD/MM/YYYY",
      "column": "בוקר|צהריים|לילה",
      "shift_type": "בוקר|צהריים|לילה|כפולה בוקר|כפולה לילה",
      "location": "הרצליה|רמ\"ש",
      "role": "role text from row header"
    }
  ]
}

If מאור פורמן does not appear anywhere → {"debug": "name not found", "shifts": []}
"""


def _upscale_image(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size
    if w < MIN_WIDTH:
        scale = MIN_WIDTH / w
        new_w = int(w * scale)
        new_h = int(h * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        logger.info(f"Upscaled image from {w}x{h} to {new_w}x{new_h}")
    else:
        logger.info(f"Image size OK: {w}x{h}, no upscale needed")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def parse_schedule_image(image_bytes: bytes) -> list[dict]:
    processed = _upscale_image(image_bytes)
    b64 = base64.b64encode(processed).decode("utf-8")

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{b64}",
                            "detail": "high",
                        },
                    },
                ],
            }
        ],
        max_tokens=1500,
        temperature=0.1,
    )

    raw = response.choices[0].message.content.strip()
    logger.info(f"GPT-4o raw response: {raw[:500]}")
    raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
    data = json.loads(raw)

    global _last_debug
    _last_debug = data.get("debug", "אין מידע")
    logger.info(f"GPT-4o debug: {_last_debug}")
    shifts = data.get("shifts", [])

    for s in shifts:
        shift_type = s.get("shift_type", "")
        start, end = SHIFT_TIMES.get(shift_type, ("00:00", "00:00"))
        s["start_time"] = start
        s["end_time"] = end

    return shifts
