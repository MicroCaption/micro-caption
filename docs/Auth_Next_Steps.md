# Auth Next Steps — Google OAuth2 Setup

MicroCaption uses Google OAuth2 (Authorization Code flow) for authentication.
Auth is currently **disabled** (`auth.enabled: false` in `config/settings.yaml`),
so the app runs without login in local/mock mode. Follow these steps to enable it.

---

## Step 1 — Create a Google Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and sign in.
2. Click the project selector at the top → **New Project**.
3. Name it something like `microcaption-prod` and click **Create**.

---

## Step 2 — Enable the Google Identity API

1. In your new project, go to **APIs & Services → Library**.
2. Search for **"Google Identity"** — select **Google Identity Toolkit API** and click **Enable**.
   (Alternatively: the OAuth2 flow works without explicitly enabling this, but enabling it
   gives you access to usage metrics in the console.)

---

## Step 3 — Configure the OAuth consent screen

1. Go to **APIs & Services → OAuth consent screen**.
2. Choose **External** (unless all your users are in a Google Workspace org, in which case
   choose **Internal** — Internal skips the verification step entirely and is simpler).
3. Fill in:
   - **App name:** MicroCaption
   - **User support email:** your email
   - **Developer contact email:** your email
4. On the **Scopes** screen, add: `openid`, `email`. No other scopes are needed.
5. On the **Test users** screen (External only): add every email address that needs access
   while the app is in *Testing* mode. Users not on this list will be blocked by Google.
6. Click **Save and Continue** through to the end.

> **Note:** While the consent screen is in *Testing* status, tokens expire after 7 days
> and only test users can log in. To remove this restriction, submit the app for Google
> verification — for an internal tool, staying in Testing mode is fine.

---

## Step 4 — Create an OAuth 2.0 client ID

1. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
2. Application type: **Web application**.
3. Name: `MicroCaption Web`.
4. Under **Authorised redirect URIs**, add:
   - `http://localhost:8765/auth/callback` (for local testing)
   - `https://microcap-proto.tail737e71.ts.net/auth/callback` (Tailscale Funnel)
   - Any other public URL you intend to use
5. Click **Create**. You will be shown a **Client ID** and **Client Secret** — copy both now
   (you can retrieve them later from the Credentials page).

---

## Step 5 — Generate a session secret

The session cookie is signed with HMAC-SHA256. Generate a strong secret:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Keep this value private. Rotating it will invalidate all existing sessions.

---

## Step 6 — Configure MicroCaption

Edit `config/settings.yaml` (or `config/settings.prod.yaml` for production):

```yaml
auth:
  enabled: true
  google_client_id: "YOUR_CLIENT_ID.apps.googleusercontent.com"
  google_client_secret: "YOUR_CLIENT_SECRET"
  redirect_uri: "https://microcap-proto.tail737e71.ts.net/auth/callback"
  allowed_emails:
    - peter@yourdomain.com      # add every email that should have access
  session_max_age: 86400        # 24 h — adjust as needed
  session_secret: "YOUR_64_HEX_CHAR_SECRET"
  cookie_name: "mc_session"
  cookie_secure: true           # must be true when serving over HTTPS
```

**For production, use environment variables instead of hardcoding secrets:**

```bash
export MC_AUTH_CLIENT_ID="YOUR_CLIENT_ID.apps.googleusercontent.com"
export MC_AUTH_CLIENT_SECRET="YOUR_CLIENT_SECRET"
export MC_AUTH_SESSION_SECRET="YOUR_64_HEX_CHAR_SECRET"
```

These map directly to the `auth.*` config keys via `_apply_env_overrides()` in `main.py`.

---

## Step 7 — Install PyJWT

If not already installed:

```bash
pip install "PyJWT[crypto]>=2.8,<3"
```

The `[crypto]` extra pulls in the `cryptography` package needed for RS256 signature
verification against Google's public keys.

---

## Step 8 — Restart and test

```bash
python3 main.py   # (without --mock-asr to use real ASR; --mock-asr is fine for auth testing)
```

Then open `https://microcap-proto.tail737e71.ts.net` and click **Log In**.

**Expected flow:**
1. Browser redirects to `accounts.google.com` — Google's own login/consent UI.
2. After login, Google redirects to `/auth/callback?code=...&state=...`.
3. MicroCaption exchanges the code for an ID token, verifies it, checks the
   `allowed_emails` list, sets a signed `mc_session` cookie, and redirects to `/dashboard`.
4. All subsequent requests use the cookie — no round-trip to Google needed.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `redirect_uri_mismatch` error from Google | The URI in settings doesn't exactly match one added in the Cloud Console (including `http` vs `https` and trailing slash) |
| `Access denied` on callback | Email is not in `allowed_emails`, or `allowed_emails` is empty and `is_allowed()` returns True for all — double-check the list |
| `Authentication failed` on callback | `google_client_secret` is wrong, or the OAuth consent screen is not yet saved |
| Cookie not persisting | `cookie_secure: true` but serving over plain HTTP — set `cookie_secure: false` for local HTTP testing |
| Google shows "App not verified" warning | Consent screen is External + Testing; add the user to the test users list, or switch to Internal if all users are in the same Workspace org |
| 7-day token expiry | Expected in Testing mode — log in again, or keep consent screen in Testing and just re-login weekly |

---

## Security notes

- The `session_secret` is the only secret that protects session cookies. Treat it like a
  database password — never commit it to git, use env vars in production.
- `cookie_secure: true` ensures cookies are only sent over HTTPS. Always enable this when
  behind the Tailscale Funnel or any other TLS terminator.
- `SameSite=Lax` on the session cookie provides CSRF protection for form-based POSTs. The
  OAuth `state` parameter provides CSRF protection for the login flow itself.
- An empty `allowed_emails` list means **any** Google account can log in. Add at least one
  email before enabling auth on a public-facing deployment.
