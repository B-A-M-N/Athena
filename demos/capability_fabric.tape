Output demos/capability_fabric.gif

Set Shell "bash"
Set FontFamily "JetBrains Mono"
Set FontSize 14
Set Width 1280
Set Height 720
Set Padding 18
Set Theme "Catppuccin Mocha"
Set TypingSpeed 25ms

Hide
Type ".venv/bin/python demos/capability_fabric_demo.py --emit-fixture demos/fixtures/capability_fabric.jsonl"
Enter
Sleep 2s
Show

# Replay the canonical fixture at 8x speed: 9 scenes x 0.2s ~= 1.8s of motion,
# then a 2s tail.  Total well under the 60s render timeout.  The Output above
# is authoritative; scripts/render-demo overrides it with -o <temp> for
# race-free single-writer rendering.
Type ".venv/bin/python demos/capability_fabric_demo.py --replay demos/fixtures/capability_fabric.jsonl --speed 8"
Enter
Sleep 2s
