# VHS demo tape — regenerates docs/demo.gif for the README.
#
# Run from the repo root (needs Docker):
#   docker run --rm -v "${PWD}:/vhs" --entrypoint bash ghcr.io/charmbracelet/vhs \
#     -c "apt-get update -qq && apt-get install -y -qq python3 python3-pip python-is-python3 >/dev/null \
#         && pip install --break-system-packages -q -r requirements.txt \
#         && vhs docs/demo.tape"
#
# The demo runs against tests/fixtures (served locally) rather than a real
# site: it is offline, deterministic, and it actually has broken links to
# show. It also avoids --render, so regenerating never needs the 150 MB
# browser download.

Output docs/demo.gif

Set Shell bash
Set FontSize 14
Set Width 1000
Set Height 780
Set Padding 12
Set TypingSpeed 40ms
Set Theme "Dracula"

Hide
Type "python3 -m http.server 8000 --directory tests/fixtures >/dev/null 2>&1 &"
Enter
Type "clear"
Enter
Show

Sleep 500ms
Type "python scripts/main.py http://localhost:8000 --crawl"
Sleep 500ms
Enter
Wait+Screen@60s /broken/
Sleep 5s
