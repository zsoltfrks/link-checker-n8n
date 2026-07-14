# VHS demo tape — regenerates scripts/demo.gif for the README.
#
# Run from the repo root (needs Docker):
#   docker run --rm -v "${PWD}:/vhs" --entrypoint bash ghcr.io/charmbracelet/vhs \
#     -c "apt-get update -qq && apt-get install -y -qq python3 python-is-python3 >/dev/null && vhs docs/demo.tape"

Output scripts/demo.gif

Set Shell bash
Set FontSize 16
Set Width 950
Set Height 220
Set Padding 12
Set TypingSpeed 40ms
Set Theme "Dracula"

Sleep 500ms
Type "python scripts/link_health_checker.py https://zsoltfrks.xyz --skip-domains linkedin.com"
Sleep 500ms
Enter
Wait+Screen@60s /Link check for/
Sleep 5s
