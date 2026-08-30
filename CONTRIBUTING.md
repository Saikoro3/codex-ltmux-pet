# Contributing

Bug reports and focused pull requests are welcome. Please describe the
Hyprland, Python, Kitty/tmux, and Codex versions involved.

Before submitting a change, run:

```bash
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
ruff check .
```

Do not commit runtime session records, prompts, terminal captures, user Hook
configuration, generated-image intermediates, or credentials. Changes to the
Lumi spritesheet must follow the repository artwork license and pass the full
Codex v2 pet QA workflow.
