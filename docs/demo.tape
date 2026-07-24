# VHS demo tape — regenerates docs/demo.gif for the README.
#
# Run from the repo root (needs Docker). The container needs the checker's
# dependencies and, because the demo uses --render, the headless browser:
#   docker run --rm -v "${PWD}:/vhs" --entrypoint bash ghcr.io/charmbracelet/vhs \
#     -c "apt-get update -qq && apt-get install -y -qq python3 python3-pip python-is-python3 >/dev/null \
#         && pip install --break-system-packages -q -r requirements.txt \
#         && playwright install --with-deps chromium >/dev/null \
#         && vhs docs/demo.tape"

Output docs/demo.gif

Set Shell bash
Set FontSize 14
Set Width 1000
Set Height 700
Set Padding 12
Set TypingSpeed 40ms
Set Theme { "name": "GitHubDark", "background": "#0d1117", "foreground": "#c9d1d9", "black": "#484f58", "red": "#ff7b72", "green": "#3fb950", "yellow": "#d29922", "blue": "#58a6ff", "magenta": "#bc8cff", "cyan": "#39c5cf", "white": "#b1bac4", "brightBlack": "#6e7681", "brightRed": "#ffa198", "brightGreen": "#56d364", "brightYellow": "#e3b341", "brightBlue": "#79c0ff", "brightMagenta": "#d2a8ff", "brightCyan": "#56d4dd", "brightWhite": "#f0f6fc", "cursor": "#c9d1d9", "selection": "#264f78" }

Sleep 500ms
Type "python scripts/main.py https://zsoltfrks.xyz --crawl --render --skip-domains linkedin.com"
Sleep 500ms
Enter
Wait+Screen@120s /broken/
Sleep 5s
