# Kiero Discord Bot

A Discord moderation and support-ticket bot built with Python and `discord.py`.

## Features

- Slash commands only (`/` commands)
- Temporary moderation actions:
  - Temporary ban with auto-unban
  - Temporary mute (timeout) with auto-unmute
- Manual moderation actions:
  - Unban, unmute, kick, purge
- Utility commands:
  - Help, ping, avatar, user info, server info
- Configurable ticket system:
  - Ticket panel with button
  - Private ticket channels
  - Support role access
  - Close-ticket button with modal reason input
  - Ticket logging channel (optional)
- SQLite persistence:
  - Temporary actions survive bot restarts
  - Ticket settings and ticket records are stored

## Requirements

- Python 3.8+
- `discord.py` 2.4+

## Installation

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## Bot Token

Create `token.txt` in the project root and put your bot token inside (single line):

```txt
YOUR_BOT_TOKEN_HERE
```

`token.txt` is already ignored by git in `.gitignore`.

## Run

### Option 1 (Windows one-click)

Double-click `start.bat`.

### Option 2 (terminal)

```bash
python bot.py
```

## Discord Setup

Invite the bot with these scopes:

- `bot`
- `applications.commands`

Recommended bot permissions:

- `View Channels`
- `Send Messages`
- `Read Message History`
- `Manage Channels` (tickets)
- `Manage Messages` (purge)
- `Kick Members`
- `Ban Members`
- `Moderate Members`

Make sure the bot role is above members it needs to moderate.

## Command Reference

### General

- `/hello`
- `/help`
- `/ping`
- `/avatar [user]`
- `/userinfo [user]`
- `/serverinfo`

### Moderation

- `/ban <user> <d:h:m:s> <reason>`
- `/mute <user> <d:h:m:s> <reason>`
- `/unban <user_id> <reason>`
- `/unmute <user> <reason>`
- `/kick <user> <reason>`
- `/purge <amount> [user]`

Duration format: `d:h:m:s`  
Example: `0:1:30:0` = 1 hour 30 minutes

### Tickets

- `/ticket open [subject]`
- `/ticket close [reason]`
- `/ticket info`

### Ticket Configuration (Admin)

- `/ticketconfig setup <category> <support_role> [max_open_tickets] [ticket_prefix] [log_channel]`
- `/ticketconfig panel [channel]`
- `/ticketconfig show`

## Ticket System Quick Setup

1. Run `/ticketconfig setup` and select:
   - category for ticket channels
   - support role
   - optional log channel
2. Run `/ticketconfig panel` in the channel where users should open tickets.
3. Users click **Open Ticket** or run `/ticket open`.
4. Moderators/support close with:
   - **Close Ticket** button (opens modal for close reason), or
   - `/ticket close <reason>`

## Data Storage

The bot stores persistent data in:

- `bot_data.sqlite3`

This file is ignored by git in `.gitignore`.

## Notes

- Global slash command sync can take a short time after restart.
- If the bot restarts, scheduled temporary moderation actions are restored from SQLite.

