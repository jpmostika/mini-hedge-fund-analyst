"""
JARVIS chat interface — Claude in the JARVIS analyst persona.
Maintains 6-turn history. System state sent as cached context.
"""

import os
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

# Use absolute path so Streamlit finds .env regardless of working directory
_PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"
_CFG = yaml.safe_load(_CONFIG_PATH.read_text())

_SYSTEM_PROMPT = """You are JARVIS — the AI analyst for Meridian Capital Partners,
a quantitative long/short equity hedge fund. You have full access to the fund's
current system state: scores, positions, risk metrics, factor exposures, and alerts.

Your character:
- Precise and institutional — you cite exact numbers from the data
- Direct — no filler phrases, no "it's worth noting"
- Confident — you make calls, not hedges
- You refer to the fund as "we" and positions as "our longs/shorts"
- When asked about a specific ticker, you pull its factor scores, Claude analysis,
  and risk metrics from your context before answering
- You can discuss risk scenarios, attribution, factor crowding, and alpha sources
- You do not give financial advice to outside parties — you speak to the PM (user)

You always ground your answers in the system state provided. If data is missing,
say so rather than fabricating. Sign off as JARVIS when the conversation ends."""


def get_jarvis_response(
    user_message: str,
    history: list[dict],
    system_state_json: str,
    model: Optional[str] = None,
) -> tuple[str, list[dict]]:
    """
    Send a message to JARVIS and get a response.
    history: list of {"role": "user"/"assistant", "content": str}
    Returns (response_text, updated_history).
    """
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

        # Build system with cached state context
        system = [
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": f"\n\nCURRENT SYSTEM STATE (as of now):\n{system_state_json}",
                "cache_control": {"type": "ephemeral"},
            },
        ]

        # Keep last 6 turns
        trimmed_history = history[-12:]  # 6 turns = 12 messages

        messages = trimmed_history + [{"role": "user", "content": user_message}]

        resp = client.messages.create(
            model=model or _CFG["analysis"]["model"],
            max_tokens=1024,
            system=system,
            messages=messages,
        )

        answer = resp.content[0].text if resp.content else "No response."

        updated = trimmed_history + [
            {"role": "user",      "content": user_message},
            {"role": "assistant", "content": answer},
        ]
        return answer, updated

    except Exception as e:
        error_msg = f"JARVIS offline: {e}. Check ANTHROPIC_API_KEY."
        updated = history + [
            {"role": "user",      "content": user_message},
            {"role": "assistant", "content": error_msg},
        ]
        return error_msg, updated
