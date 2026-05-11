#!/usr/bin/env python3
"""Harness layer: Agent Loop

    +-----------+     +-----------+     +-----------+
    |  User     | --> |  Agent    | --> |  Model    |
    |  Input    |     |  Loop     |     |  (LLM)    |
    +-----------+     +-----------+     +-----------+
                            |
                     +-----------+
                     |  stop_    |
                     |  reason?  |
                     +-----------+
                      |         |
                  end_turn   continue
                      |         |
                   response    loop

Key insight: The agent loop is just while True. The model drives
the loop via stop_reason — not hardcoded control flow.

Evolution path: As the agent grows, the simple while True loop
naturally evolves into a formal state machine:

    RESTORE -> BUILD -> RUN -> SAVE -> RESPOND -> DONE

Each state is a focused handler; transitions are driven by events.
Later sessions introduce this pattern incrementally.
"""

import os
import sys

from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()

WORKDIR = os.path.dirname(os.path.abspath(__file__))
MODEL = os.getenv("MODEL_ID", "claude-sonnet-4-6")
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


SYSTEM_PROMPT = """You are an AI agent running in an interactive loop.
Respond to the user's questions naturally. You have no tools yet."""


def agent_loop(messages):
    """The universal agent loop: call LLM, check stop_reason, repeat."""
    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM_PROMPT,
            messages=messages,
            max_tokens=4096,
        )

        # The model decides when to stop
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            break
        # In later sessions, tool_use will also stop here

    return response.content


def repl():
    messages = []
    print("=== OpenClaw s01: Agent Loop ===")
    print("Type 'q' to quit\n")

    while True:
        try:
            user_input = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        if user_input.lower() in ("q", "quit", "exit"):
            break

        messages.append({"role": "user", "content": user_input})
        content = agent_loop(messages)
        for block in content:
            if block.type == "text":
                print(block.text)


if __name__ == "__main__":
    repl()