"""Legacy multi-frame mascot art (owl / cat / robot).

Extracted verbatim from the original ``dual_pane.Mascot`` so the classic
animated characters remain a drop-in asset set.  Each entry maps a legacy
state name (idle/thinking/executing/waiting/done/failed) to two animation
frames; ``Mascot.render`` alternates them.
"""

from __future__ import annotations

__all__ = ["FRAMES", "CAT_FRAMES", "ROBOT_FRAMES"]

FRAMES = {
    "idle": (
        r"""
       ,___,
       (O,O)
       /)_)
      """,
        r"""
       ,___,
       (o,o)
       /)_)
      """,
    ),
    "thinking": (
        r"""
      ╔══════╗
      ║ ◉ ▄ ◉║   ∿∿∿
      ║  ‗‗  ║  ∿∿∿  ideas
      ╚══════╝   ⚡
      """,
        r"""
      ╔══════╗
      ║ ◎ ≈ ◎║ ~ ∿∿
      ║  ‗‗  ║  ∿∿∿  forming…
      ╚══════╝   ⚡
      """
    ),
    "executing": (
        r"""
        ▄▄▄▄▄
       █ ◉_◉ █    ▤▤▓▒
       █  ▽  █ ▧▤▓▒▒ running
        ▀▀▀▀▀
      """,
        r"""
        ▄▄▄▄▄
       █ ◉^◉ █    ▒▒▓▤
       █  ≡  █ ▒▒▓▤▧ hacking
        ▀▀▀▀▀
      """,
    ),
    "waiting": (
        r"""
        ▄▄▄▄▄
       █ ⊙︵⊙ █
       █  ▽  █   ⏸ awaiting permission
        ‛‛‛‛‛
      """,
        r"""
        ▄▄▄▄▄
       █ ⊙‿⊙ █
       █  ⌣  █   ⏸ may i?
        ‛‛‛‛‛
      """,
    ),
    "done": (
        r"""
        ▄▄▄▄▄
       █ ★‿★ █
       █  ◡  █   ✓ complete
        ▀▀▀▀▀
      """,
        r"""
        ▄▄▄▄▄
       █ ^‿^ █
       █  ◡  █   ✓ done!
        ▀▀▀▀▀
      """,
    ),
    "failed": (
        r"""
        ▄▄▄▄▄
       █ ✕︵✕ █
       █  ─  █   ✗ oops
        ▀▀▀▀▀
      """,
        r"""
        ▄▄▄▄▄
       █ ✕﹏✕ █
       █  ˘  █   ✗ failed
        ▀▀▀▀▀
      """,
    ),
}

CAT_FRAMES = {
    "idle": (
        r"""
     /\_/\
    ( -.- )   zZ
     > ^ <
      """,
        r"""
     /\_/\
    ( •‿• )
     > ~ <   *blink*
      """,
    ),
    "thinking": (
        r"""
     /\_/\   ?????
    ( ⊙﹏⊙ )  ┌─┐
     |    |  └─┤ think…
      """,
        r"""
     /\_/\   ????
    ( ⊙▽⊙ )  ┌─┐
     |    |  └─┤ hmm…
      """,
    ),
    "executing": (
        r"""
     /\_/\   ▨▤▹
    ( =⍤= )  ▹▨▤  typing
     /|   |\
      """,
        r"""
     /\_/\   ◃▤▨
    ( =◔= )  ▨◃▤  pouncing on bugs
     /|   |\
      """,
    ),
    "waiting": (
        r"""
     /\_/\
    ( ˇ︵ˇ )
     > ? <   ⏸ let me in…
      """,
        r"""
     /\_/\
    ( •︵• )
     > ? <   ⏸ pretty please?
      """,
    ),
    "done": (
        r"""
     /\_/\
    ( ★‿★ )  ✓ caught it
     \_~_/
      """,
        r"""
     /\_/\   ✓ purr…
    ( ‿‿‿ )
     \_~_/
      """,
    ),
    "failed": (
        r"""
     /\_/\   ✗ hiss
    ( ✕﹏✕ )
     > ~ <
      """,
        r"""
     /\_/\   ✗ mrow.
    ( ✕︵✕ )
     > ~ <   ears flat
      """,
    ),
}

ROBOT_FRAMES = {
    "idle": (
        r"""
      ┌───┐
      │ ‿ │  [standby]
     ╭┴─┴╮
      """,
        r"""
      ┌───┐
      │ ° │  [standby]
     ╭┴─┴╮  ·
      """,
    ),
    "thinking": (
        r"""
      ┌───┐
      │ ▓▓│  [CPU 97%]
     ╭┴─┴╮ ⟨⟨⟨
      """,
        r"""
      ┌───┐
      │ ▒▓│  [CPU 84%]
     ╭┴─┴╮ ⟩⟩⟩ computing
      """,
    ),
    "executing": (
        r"""
      ┌───┐  ▸▸
      │ ◉ │  EXEC
     ╭┴─┴╮  ▸▸▸
      """,
        r"""
      ┌───┐  ▸▸
      │ ◎ │  RUN
     ╰┬─┬╯  ▸ ▸
      """,
    ),
    "waiting": (
        r"""
      ┌───┐
      │ ○?│  [HALT]
     ╭┴─┴╮  awaiting input
      """,
        r"""
      ┌───┐
      │ ◇?│  [HALT]
     ╭┴─┴╯  …authorization?
      """,
    ),
    "done": (
        r"""
      ┌───┐
      │ ^^│  ✓ EXIT 0
     ╭┴─┴╮  task complete
      """,
        r"""
      ┌───┐  ♪
      │ ‿ │  ✓ SUCCESS
     ╰┬─┬╯
      """,
    ),
    "failed": (
        r"""
      ┌───┐
      │ ✕✕│  ✗ SEGFAULT
     ╭┴─┴╮  stack trace…
      """,
        r"""
      ┌───┐
      │ ❧❧│  ✗ ERROR
     ╰┬─┬╯  dumping core…
      """,
    ),
}
