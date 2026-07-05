"""Credential redaction — strips API keys, passwords, tokens from text before LLM sees it."""

import re

# Patterns to redact (replace with [REDACTED])
REDACT_PATTERNS = [
    # API keys (generic 20+ char alphanumeric strings after key/token/secret words)
    (r'(?i)(api[_-]?key|token|secret|password|passwd|pwd|auth)\s*[=:]\s*["\']?([A-Za-z0-9_\-\.]{16,})["\']?', r'\1=[REDACTED]'),
    # PGPASSWORD='...'
    (r"PGPASSWORD=['\"]?[^'\"\\s]+['\"]?", "PGPASSWORD=[REDACTED]"),
    # Bearer tokens
    (r'Bearer\s+[A-Za-z0-9_\-\.]+', 'Bearer [REDACTED]'),
    # AWS-style keys
    (r'(?:AKIA|ASIA)[A-Z0-9]{16}', '[REDACTED-AWS-KEY]'),
    # Private key blocks
    (r'-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |DSA )?PRIVATE KEY-----', '[REDACTED-PRIVATE-KEY]'),
    # Common credential file contents
    (r'"private_key":\s*"[^"]*"', '"private_key": "[REDACTED]"'),
    (r'"client_secret":\s*"[^"]*"', '"client_secret": "[REDACTED]"'),
    # Specific known credentials from this VM
    (r"AH\)AE\$EOd\}7sdGY>k_A1Pyd\+B\*::_=w=", "[REDACTED-DB-PASSWORD]"),
    (r"dXm6yXE2cxFNm2t9cY7yXQtk", "[REDACTED-BACKTEST-PASSWORD]"),
    (r"CWop58HvIGiLWPCNARJhtFtBJFb81UqC", "[REDACTED-POLYGON-KEY]"),
    (r"Easyas123!@#", "[REDACTED-VM-PASSWORD]"),
    (r"ClaudeTest2024!@#", "[REDACTED-TEST-PASSWORD]"),
]

_compiled = [(re.compile(p), r) for p, r in REDACT_PATTERNS]


def redact(text):
    """Strip credentials from text. Returns redacted text."""
    for pattern, replacement in _compiled:
        text = pattern.sub(replacement, text)
    return text


def redact_file(path):
    """Read a file and return redacted contents."""
    with open(path) as f:
        return redact(f.read())


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(redact_file(sys.argv[1]))
    else:
        print(redact(sys.stdin.read()))
