# Game Presence

A [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin that shows the game you are playing on Discord while Steam is in Game Mode.

Friends see a normal Discord **Playing** status, with the official game name and artwork when Discord knows the title.

## Install

1. Install [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader).
2. Copy the `GamePresence` folder into `~/homebrew/plugins/`.
3. Restart Decky, or reboot into Game Mode.
4. Open the Quick Access menu → **Game Presence** → leave **Show activity** on.

You need a logged-in Discord account on the same machine. The plugin starts Discord in the background only while a game is running, so it does not take a Game Mode tab.

## Usage

Launch a game from Steam. After a few seconds, Discord should show that title.

- **Show activity** — turn reporting on or off
- **Now playing** — the title currently sent to Discord
- **Reconnect** — appears if Discord did not accept the status
- **Clear** — remove the status

## How it works

Discord will only show **Playing [game]** if a Discord client is running. Game Mode normally tears that client down, so the old workaround was to keep Discord open as a Steam tab.

This plugin does three things instead:

1. **Detect the current game.** It watches Steam’s running-app events. If Steam does not list the title in the overlay, it also reads the Steam App ID from the running process (the same `AppId=` / `SteamAppId=` values Steam already uses to launch the game) and looks up the name from your installed `appmanifest_*.acf` files or Discord’s public game list.
2. **Match Discord’s official game.** It uses [Discord’s detectable applications list](https://discord.com/api/v10/applications/detectable) and the Steam App ID so the status uses Discord’s real application (name + cover), not a generic placeholder.
3. **Keep a background Discord client only while you play.** The client runs on a dummy display, at low priority, and is stopped when the game exits. The plugin holds the Rich Presence connection open for the whole session so Discord does not fall back to the raw `.exe` name.

This is Discord Rich Presence only. It does not Go Live, share video, or change what Steam friends see.

## Requirements

- Linux / Steam Deck / CachyOS Deckify (or similar) with Decky Loader
- Discord installed and logged in (`discord` from your distro, not only Flatpak)
- `Xvfb` (`xorg-server-xvfb` on Arch)

## Development

```
GamePresence/
  plugin.json
  package.json
  main.py
  dist/index.js
  LICENSE
  README.md
```

After editing, copy into `~/homebrew/plugins/GamePresence/` and restart the `plugin_loader` service.

## License

MIT
