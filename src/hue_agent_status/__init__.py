"""Philips Hue and WiZ lights as status indicators for AI coding agents.

Selected lights breathe while Claude Code or OpenAI Codex is working, turn red
for an explicit master-session input wait, blink green when a child finishes,
and hold green before restoration when every watched session has completed.
"""

__version__ = "0.1.0"

#: Hard cap on hook payload size read from stdin / accepted by the daemon.
MAX_HOOK_PAYLOAD_BYTES = 64 * 1024
