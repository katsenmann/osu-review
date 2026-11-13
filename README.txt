osu!review – replay analysis right after every score
====================================================

SETUP
1. Unzip the folder somewhere it can stay (e.g. Documents\osu-review).
2. Double-click "Install.bat" -> desktop shortcut (+ optional autostart).
   Only Python 3 is needed - if it's missing, Install.bat downloads a small
   private copy into this folder.
3. Start osu!review with the shortcut or "Start.bat".

UPDATE / UNINSTALL
- New version: double-click "Uninstall.bat" in the old folder and pick 1
  (program only - your scores and settings are kept), then unzip the new
  version and run its "Install.bat". Simply unzipping over the old folder
  works too.
- Remove completely: "Uninstall.bat" and pick 2 (also deletes your scores and
  settings in %APPDATA%\osu-review). Rendered videos are never deleted.

HOW IT WORKS
- osu!review runs in the background and watches
    stable:  osu!\Data\r and osu!\Replays (F2 export)
    lazer:   the lazer data folder (every saved score and exports)
- When a new score appears, the analysis opens automatically:
    accuracy, combo, UR, 300/100/50/miss, a timeline of every hit,
    the reason for every miss and slider break, a replay viewer with your
    skin, song and hitsounds, frame-by-frame mode and aim / tablet stats.
- You can also drop .osr files into the window.

THE TABS
- Home:    your form, today's session, what to work on, top plays.
- Scores:  the miss analyzer (new scores land here automatically).
           Delete a score with the X in the list, the Delete button or the
           Del key - undo within 6 seconds (Ctrl+Z).
- Studio:  render replays to video with danser-go and our own HUD.
           It has its own render folder: drop the replays you want to
           render into the window while you are in Studio.
- Tablet:  your OpenTabletDriver area live, plus an overall rating of the
           area from all your replays and a suggested size.
- Ctrl+K:  search scores, maps, players, settings and themes.
- The paint palette (bottom left) customizes the look: colours, map
  background, blur, font, density, slim sidebar. Themes can be shared as a code.

PP
The client always uses the most exact source it has:
  1. osu! API (optional, needs a free OAuth app - see Settings > pp & data)
  2. tosu, if it was running while you played
  3. osu!lazer's own pp calculator (the same build tosu uses) - downloaded
     automatically in the background the first time (~60 MB)
  4. rosu-pp (built in; older pp formulas, only used as a fallback)

SKINS
- Pick any skin from your osu!\Skins folder above the replay. Hit circles,
  sliders, spinners (old and new style incl. RPM, metre, "spin"/"clear"),
  hit bursts incl. geki/katu, particles, hit lighting, combo bursts, cursor,
  trail and smoke are drawn from the skin, with the skin.ini settings.
- lazer skins: export them as .osk in lazer and unzip into osu!\Skins.

DATA
- Settings and data: %APPDATA%\osu-review     Local port: 127.0.0.1:47290
- Updates written into this folder are picked up automatically.

Made by Katsenmann x Claude
