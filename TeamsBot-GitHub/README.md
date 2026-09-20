# TeamsBot

TeamsBot is a macOS desktop helper that watches Microsoft Teams locally for a
fresh poll card and, only after its local safety checks pass, can target the
current card's **Submit** control.

This repository is the **TeamsBot build**. It is an internal beta,
not a released product.

## What it does

- Uses local Screen Recording, Accessibility, visual matching, and short-lived
  on-device OCR.
- Checks a visible card for a current, card-local timestamp, completion state,
  and Teams' **Last read** boundary before any normal-mode action.
- Keeps timestamp-only local history to avoid repeat work. It does not retain
  screenshots, poll questions, or answer text.

## Requirements

- macOS on Apple silicon
- Python 3.14 with the project dependencies
- PyInstaller, Xcode Command Line Tools, and Screen Recording + Accessibility
  permissions for the installed app

## Build

From the repository directory:

```sh
clang -framework Cocoa -o teamsbot_menu_bar teamsbot_menu_bar.m
swiftc teamsbot_vision_ocr.swift -framework Vision -framework AppKit -o teamsbot_vision_ocr
pyinstaller --noconfirm TeamsBot.spec
```

The finished app is placed at `dist/TeamsBot.app`.

## Repository hygiene

Do not commit `dist/`, app bundles, local Application Support data, build
output, temporary captures, or private Microsoft Entra configuration. See
`project_context.md` and `CHANGELOG.md` for the current project state and
development history.
