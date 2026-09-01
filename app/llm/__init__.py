"""Model providers and the tool plumbing that feeds them.

Split from app/agent.py, which was built around claude-agent-sdk spawning a
Claude Code CLI subprocess. The local model has no such harness, so the pieces
the SDK used to supply live here instead.
"""
