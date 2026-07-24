# VHS demo tape — regenerates docs/demo.gif for the README.
#
# Run from the repo root (needs Docker). The container needs the checker's
# dependencies too, since main.py imports Playwright at startup:
#   docker run --rm -v "${PWD}:/vhs" --entrypoint bash ghcr.io/charmbracelet/vhs \
#     -c "apt-get update -qq && apt-get install -y -qq python3 python3-pip python-is-python3 >/dev/null \
#         && pip install --break-system-packages -q -r requirements.txt \
#         && vhs docs/demo.tape"

Output docs/demo.gif

Set Shell bash
Set FontSize 16
Set Width 950
Set Height 260
Set Padding 12
Set TypingSpeed 40ms
Set Theme "Dracula"

Sleep 500ms
Type "python scripts/main.py https://zsoltfrks.xyz --skip-domains linkedin.com"
Sleep 500ms
Enter
Wait+Screen@60s /Crawl completed/
Sleep 5s
