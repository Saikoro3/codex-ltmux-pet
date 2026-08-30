# Security Policy

## Supported versions

Security fixes are provided for the latest release.

## Reporting

Please report a suspected vulnerability privately through GitHub's security
advisory feature rather than opening a public issue. Do not include real Codex
prompts, terminal captures, tokens, socket paths, or session-state files.

Lumi runs as the current user. Its Hook receiver accepts lifecycle JSON on
standard input and writes only compact state records under the user's XDG state
directory. It does not open a network listener.
