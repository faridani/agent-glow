"""hue-agent-status: Philips Hue lights as a status indicator for AI coding agents.

Selected Hue lights "breathe" while Claude Code or OpenAI Codex is working,
turn red when an agent is waiting for your input, and are restored to their
previous state when every watched session has ended.
"""

__version__ = "0.1.0"

#: Hard cap on hook payload size read from stdin / accepted by the daemon.
MAX_HOOK_PAYLOAD_BYTES = 64 * 1024
