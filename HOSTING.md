# RA7 LVL - Hosting

## Start command

```bash
python Main.py
```

The app binds to `0.0.0.0` and reads the hosting provider's `PORT` environment variable. If `PORT` is not set, it uses `20335`.

Your hosting provider should expose the generated public URL, for example:

`https://your-app.example.com`

## Health check

`/health` returns:

```json
{"status":"ok"}
```

## Access

- Normal users: open the public URL and enter an access code.
- Owner: open the same URL, choose **Owner**, and sign in.
- Demo permanent code: `84FF-DEMO-PERMANENT`

## Important

Do not expose the default owner password on a public deployment. Set these environment variables before deployment:

- `DASHBOARD_OWNER_USER`
- `DASHBOARD_OWNER_PASSWORD`

The normal user code form and owner login use regular HTML POST forms so they continue to work on hosting proxies where browser `fetch()` POST requests can fail.
