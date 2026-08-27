Output demos/capability_fabric.gif

Set Shell "bash"
Set FontFamily "JetBrains Mono"
Set FontSize 12
Set Width 1280
Set Height 720
Set Padding 18
Set Theme "Catppuccin Mocha"
Set TypingSpeed 25ms
# The interface is intentionally restrained; 10 fps keeps the long GIF
# reviewable without spending minutes encoding redundant terminal frames.
Set Framerate 10

# Clear the shell line before revealing the app.  The fixture is committed;
# the recording only replays it through the production DualPaneSurface.
Hide
Type "export TERM=xterm-256color ATHENA_DISPLAY=ansi && clear && unset COLUMNS LINES && stty cols 100 rows 45 && exec .venv/bin/python demos/capability_fabric_demo.py --replay demos/fixtures/capability_fabric.jsonl --speed 1"
Enter
Sleep 2s
Show
# Nine chapters with deliberate reading pauses; the resulting recording is
# approximately 90 seconds and ends at the real Athena prompt. The extra
# margin lets the final event/result repaint settle before VHS closes.
Sleep 95s
