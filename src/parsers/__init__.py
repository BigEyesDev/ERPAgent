"""Attachment/email parsers. Every parser here is LLM-free and either
returns a fully populated `ParsedEmail`/`ParsedDocument` or raises
`ParserError` - never a silently empty result.
"""


class ParserError(Exception):
    """Raised when input cannot be parsed into a well-formed structure."""
