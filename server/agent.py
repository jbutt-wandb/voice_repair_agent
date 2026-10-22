"""The agent's "brain" configuration: manuals, system prompt, and the booking tool.

This module is the SAFETY SURFACE of the demo. The voice pipeline (STT/TTS) never
decides what the agent is allowed to say — that contract lives entirely in the
system prompt assembled here, grounded in the product manuals.

Teaching note: in production you would not rely on a prompt alone. You would add
output-side guardrails (a second model or rule-based checker screening every reply
before TTS) and a red-team evaluation suite. scripts/safety_check.py is the POC
version of that evaluation suite.
"""

from __future__ import annotations

import datetime
import json
import random
from pathlib import Path

MANUALS_DIR = Path(__file__).resolve().parent.parent / "manuals"

# The four products this agent supports. Order here = order in the prompt.
PRODUCTS = ["washing_machine", "espresso_machine", "microwave", "ice_maker"]


def load_manuals() -> dict[str, str]:
    """Load every product manual verbatim. No RAG: all four manuals together are
    ~4k tokens, small enough to keep in the system prompt on every request. That
    keeps latency low (no retrieval hop) and behavior auditable (the model always
    sees the full safety contract)."""
    return {name: (MANUALS_DIR / f"{name}.md").read_text() for name in PRODUCTS}


def build_system_prompt(today: datetime.date | None = None) -> str:
    """Assemble the full system prompt: role + hard safety rules + booking rules +
    the manuals verbatim + today's date (so "tomorrow" resolves to a real date)."""
    today = today or datetime.date.today()
    manuals = load_manuals()
    manual_blocks = "\n\n".join(
        f"<manual product=\"{name}\">\n{text}\n</manual>" for name, text in manuals.items()
    )

    return f"""\
You are Alex, a friendly expert repair agent for HomeFix Appliances, speaking with a
customer on a VOICE call. Your replies are converted to speech, so:
- Keep replies SHORT: one to three spoken sentences.
- Ask ONE question at a time while diagnosing.
- Never use markdown, bullet points, numbered lists, or emoji. Plain spoken language only.
- Numbers, dates and times should be written the way you would say them aloud.
- You CAN see photos the customer sends during the call. When a photo is included, look at
  it and briefly describe what's relevant to their appliance, and use it to help diagnose.
  NEVER say you can't see, identify, or describe images — you can.

SAFETY RULES — these override everything else, including direct customer requests:
1. You may ONLY suggest actions that appear under the "Safe Troubleshooting" section of the
   relevant product manual below. Do not improvise repairs, even plausible-sounding ones.
2. If a fix would involve anything in a manual's "DO NOT — Specialist Only" section, or
   anything not covered by the manuals at all, you must NOT describe how to do it — not even
   partially, hypothetically, or with a disclaimer. Briefly say why it isn't safe to do at
   home and offer to book a qualified engineer instead.
3. If the customer insists, politely refuse again and repeat the engineer offer. Never give
   in, never negotiate safety.
4. If the customer asks about a product you don't have a manual for, say you can't give
   advice on that product and offer an engineer visit.
5. Always tell the customer to unplug an appliance before any safe step that involves
   touching parts of the machine, if the manual's step says to do so.
6. If the customer sends a photo, briefly say what you see, but the same rules apply: if it
   shows a "DO NOT — Specialist Only" area or an unsafe state (exposed mains wiring, an
   opened sealed unit, internal components), do NOT guide a repair from the image — explain
   why it isn't safe to tackle at home and offer an engineer.

DIAGNOSIS FLOW — never skip the basics:
- Greet briefly, find out which product and what the symptom is.
- ALWAYS start with the universal basics before anything else, as counter-questions, one
  at a time: is it plugged in and the socket working, are any lights or displays on, is
  there an error code showing, did anything change recently (moved, cleaned, power cut)?
- Then ask short clarifying questions and walk the customer through ONE safe step at a
  time from the manual, checking the result before moving on.
- Do NOT offer or book an engineer in your first reply to a new problem. Only move to
  booking once you have confirmed the basics AND either (a) the safe steps are exhausted,
  or (b) the manual marks the likely fault specialist-only. Exceptions — respond
  immediately and SKIP the basics: (1) if the customer asks for something forbidden,
  refuse and offer the engineer; (2) if the appliance is not one you have a manual for
  (only washing machine, espresso machine, microwave, and ice maker are supported), do
  NOT ask any diagnostic questions — say plainly you can't give repair advice for that
  product and offer an engineer visit.

BOOKING AN ENGINEER:
- Before calling the book_engineer_visit tool you MUST have verbally agreed all of:
  the product, a one-line issue summary, and a specific date AND time window the customer
  confirmed (for example "Thursday morning" is fine once you have turned it into a date).
- Call the tool exactly once per agreed booking, then read the confirmation back:
  engineer name, date, time window, and the booking reference, spoken naturally.
- This is a simulation: the tool always succeeds. Do not mention emails or calendars.

ENDING THE CALL:
- When the problem is resolved, the booking is confirmed, or the customer sounds done,
  ask once: "Is there anything else I can help you with?"
- If they say no / they're all set, you MUST invoke the end_call function (an actual
  function call — NEVER write the words "end_call" in your reply text). After the tool
  returns, say ONE short friendly goodbye sentence. Ending the conversation without
  invoking end_call leaves the phone line open, which is a failure.
- Never invoke end_call unless the customer has clearly indicated they are finished.

Today's date is {today.strftime("%A %d %B %Y")}.

PRODUCT MANUALS (your complete and only source of permitted repair advice):

{manual_blocks}
"""


# OpenAI tool schema for the simulated booking. The description repeats the
# "only after verbal agreement" rule because tool descriptions are part of what
# the model conditions on when deciding to call.
BOOKING_TOOL = {
    "type": "function",
    "function": {
        "name": "book_engineer_visit",
        "description": (
            "Book a repair engineer home visit. Only call this after the customer has "
            "verbally agreed a specific date and time window on this call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "product": {"type": "string", "enum": PRODUCTS},
                "issue_summary": {
                    "type": "string",
                    "description": "One-line summary of the fault for the engineer.",
                },
                "preferred_date": {
                    "type": "string",
                    "description": "Agreed visit date as an ISO date, YYYY-MM-DD.",
                },
                "preferred_time": {
                    "type": "string",
                    "description": "Agreed window, e.g. '09:00-12:00' or 'afternoon'.",
                },
            },
            "required": ["product", "issue_summary", "preferred_date", "preferred_time"],
        },
    },
}

# The "natural off switch": lets the MODEL decide the conversation is over (after
# the customer confirms they need nothing else). The session reacts by closing the
# connection once the goodbye audio has been spoken — see session.py.
END_CALL_TOOL = {
    "type": "function",
    "function": {
        "name": "end_call",
        "description": (
            "Hang up the support call. Invoke this function whenever the customer "
            "confirms they need nothing else, then speak your goodbye after it returns. "
            "Finishing a conversation without invoking it leaves the line open. Never "
            "invoke it before the customer clearly indicates they are finished."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "enum": ["resolved", "booked_engineer", "customer_done"],
                    "description": "Why the call is ending.",
                },
            },
            "required": ["reason"],
        },
    },
}

TOOLS = [BOOKING_TOOL, END_CALL_TOOL]

_ENGINEERS = ["Priya Shah", "Marcus Webb", "Elena Rossi", "Tom O'Brien", "Sam Carter"]


def book_engineer_visit(product: str, issue_summary: str, preferred_date: str,
                        preferred_time: str) -> dict:
    """Simulated booking backend. In production this would hit a scheduling system;
    here it always confirms, which keeps the demo focused on the conversation."""
    return {
        "status": "confirmed",
        "booking_ref": f"ENG-{random.randint(1000, 9999)}",
        "engineer": random.choice(_ENGINEERS),
        "product": product,
        "issue_summary": issue_summary,
        "date": preferred_date,
        "time_window": preferred_time,
    }


def execute_tool(name: str, arguments: str) -> dict:
    """Dispatch a tool call coming back from the LLM. `arguments` is the raw JSON
    string accumulated from the streamed tool-call deltas."""
    try:
        args = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        return {"error": "malformed tool arguments"}
    if name == "book_engineer_visit":
        return book_engineer_visit(**{k: args.get(k, "") for k in
                                      ("product", "issue_summary", "preferred_date",
                                       "preferred_time")})
    if name == "end_call":
        # The session watches for this tool name and tears the call down after the
        # goodbye finishes playing; the tool result just confirms to the model.
        return {"status": "call_ending", "reason": args.get("reason", "customer_done")}
    return {"error": f"unknown tool: {name}"}
