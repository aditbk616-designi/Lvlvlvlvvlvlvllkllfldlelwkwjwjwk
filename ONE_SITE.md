# RA7 LVL — One Site Deployment

This package is a single web application. User access, owner login, license management, and the dashboard all use the same site URL.

The server binds to `0.0.0.0` and reads the hosting provider's `PORT` environment variable. If `PORT` is not set, it uses `20335`.

## Hosting

Use the start command:

```bash
python Main.py
```

If the host uses a Procfile, it is already included:

```text
web: python Main.py
```

The hosting provider's generated URL is the only URL users need. There are no separate frontend sites.

## One URL, two access modes

Open the generated site URL. The landing page contains both:

- User access: enter an activation code.
- Owner: sign in and manage licenses/accounts.

After authentication, the same URL shows the dashboard.

## Default demo

User demo code:

`84FF-DEMO-PERMANENT`

Default owner credentials on first run:

- username: `owner`
- password: `ChangeMe_84FF!`

Change the owner password/environment variables before public deployment.
