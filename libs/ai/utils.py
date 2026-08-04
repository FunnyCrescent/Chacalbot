"""Utility helpers for ai_client."""
import re

def strip_stray_tags(text: str) -> str:
    """Defensive cleanup for model output that echoes the [PLAYER]/[ROLL]/[DM]-style
    input markup into its own narrative (observed with weaker models: they've seen the
    tag vocabulary in the prompt and sometimes imitate it in the response). The prompt
    now explicitly forbids this, but this strip is cheap insurance regardless of cause.
    Only strips short ALL-CAPS/underscore bracket tags (e.g. [DM], [ROLL], [PLAYER],
    [SYSTEM]) — never touches normal bracketed prose like "[смотрит с презрением]"."""
    if not text:
        return text
    return re.sub(r'\[(?:PLAYER|ROLL|DM|GM|SYSTEM|ASSISTANT|USER|NARRATOR)\]', '', text, flags=re.IGNORECASE).strip()
