Output demos/capability_fabric.gif

Set Shell "bash"
Set FontFamily "JetBrains Mono"
Set FontSize 14
Set Width 1280
Set Height 720
Set Padding 18
Set Theme "Catppuccin Mocha"
Set TypingSpeed 25ms

# Clear the shell line before revealing the app.  The fixture is committed;
# the recording only replays it through the production DualPaneSurface.
Hide
Type "clear && stty cols 120 rows 40 && exec .venv/bin/python demos/capability_fabric_demo.py --replay demos/fixtures/capability_fabric.jsonl --speed 1"
Enter
Sleep 2s
Show
# Nine chapters with deliberate reading pauses; the resulting recording is
# approximately 90 seconds and ends at the real REPL prompt.
Sleep 5s
