# Instagram announcement mirror — one-time setup

The bot can mirror messages from the Discord announcements channel to the club
Instagram as **stories**: it renders the text as a branded story card
(coloured background + text, like Instagram's own "create mode"), attaches any
images from the announcement as extra frames, and posts a **preview in Discord
first** — nothing reaches Instagram until someone with the exec role clicks
**Post to Instagram**.

This document is the one-time setup. It needs no code changes, costs nothing,
and takes about 20 minutes. You need: access to the club Instagram account,
the Discord developer portal (bot application), and the server `.env`.

## 1. Make the club Instagram a professional account

The publishing API only works for **professional** accounts (Business or
Creator). This is free and reversible:

Instagram app → profile → menu → **Settings and privacy** → **Account type and
tools** → **Switch to professional account** → choose **Business** (category
doesn't matter). No Facebook Page is required.

## 2. Create a Meta app

1. Go to https://developers.facebook.com/ and log in **with the club's
   account** (or an account you're happy to own the app; the club account is
   better for handover).
2. **My Apps → Create App** → use case **"Instagram"** → type **Business**.
   Name it something like `RBGA Announcement Mirror`.
3. In the app dashboard, under **Instagram → API setup with Instagram login**:
   - **Add account**: log in as the club Instagram account and authorise the
     app. Because the account has a role on the app, **no App Review is
     needed** — permissions (`instagram_business_basic`,
     `instagram_business_content_publish`) work immediately in this setup.
4. The same page shows the account's **Instagram user ID** (a long number) —
   that is `IG_USER_ID`.
5. **Generate token** next to the account → copy the **long-lived access
   token** — that is `IG_ACCESS_TOKEN`. It's shown once; if you lose it, just
   generate another.

> The token expires after 60 days, but the bot refreshes it weekly and stores
> the current one in the database, so **one paste lasts forever** as long as
> the bot keeps running. If the token ever dies anyway (e.g. the bot was off
> for 2+ months, or the account password changed), generate a fresh token and
> paste it into `IG_ACCESS_TOKEN` again — the bot notices the changed value
> and starts using it.

## 3. Enable the message-content intent in Discord

The bot needs to *read* the announcement text, which is a privileged intent:

https://discord.com/developers/applications → the bot application → **Bot** →
**Privileged Gateway Intents** → enable **Message Content Intent** → Save.

Do this **before** setting the env vars below: once configured, the bot
requests this intent at login and Discord refuses the connection if the
portal toggle is off.

## 4. Configure and deploy

Get the announcements channel id in Discord (Settings → Advanced → Developer
Mode on, then right-click the channel → **Copy Channel ID**), then fill in
`.env` on the server:

```dotenv
IG_ANNOUNCE_CHANNEL_ID=<channel id>
IG_USER_ID=<from step 2>
IG_ACCESS_TOKEN=<from step 2>
PUBLIC_API_BASE_URL=https://rmitbga.duckdns.org
```

Then `docker compose --profile caddy up -d` (or wait for the next deploy —
the vars are read at bot startup). The bot log should show:

```
[instagram] announcement mirror active on channel <id>.
```

## 5. Day to day

- Post an announcement in the channel as normal. The bot replies with a
  preview of the story card.
- Anyone with the exec role clicks **Post to Instagram** (or **Skip**). The
  reply updates to show the result; if posting failed, the buttons come back
  and the error says why.
- The preview buttons keep working across bot restarts — clicking Post always
  publishes the *current* content of the original announcement message, so you
  can edit a typo in Discord and then post.

### Limits & troubleshooting

- Instagram allows **25 API posts per 24 h** (stories + posts combined) —
  effectively unlimited for club use.
- Story frames are images only; the card is 1080×1920. Text is auto-shrunk to
  stay readable and very long announcements are truncated with
  "…full announcement on our Discord".
- `TOKEN REFRESH FAILED` in the bot logs → generate a new token (step 2.5) and
  paste it into `IG_ACCESS_TOKEN`.
- "This interaction failed" right after a deploy is the usual ~1 min bot
  restart window; retry.
- The story-frame images are served briefly from `PUBLIC_API_BASE_URL`
  (`/ig-media/...`) so Instagram can fetch them; they auto-delete within an
  hour. If publishing fails with a fetch error, check that URL is reachable
  from the internet.
