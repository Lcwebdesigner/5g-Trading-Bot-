# 5G Trading Bot — put it online (about 30–45 minutes)

What you end up with: `https://bot.yourdomain.com` that works on phones and desktops. Each friend signs in with
their own Deriv account. Everything below was written without access to live Deriv or a real server, so do step 7
(the test) before inviting anyone.

## 0. What you need
- A small Linux server (VPS), Ubuntu 24.04, 1 GB RAM is enough. Roughly $5–8 a month.
- A domain name (about $10–15 a year). Deriv only accepts an **https** return address, so you need one.
- Your Deriv OAuth app (step 3).

## 1. Domain → server
Buy the server, note its IP address. At your domain registrar add a DNS **A record**:
`bot` → the server's IP (so the address is `bot.yourdomain.com`). Wait a few minutes.

## 2. Prepare the server (log in with SSH as root)
```
apt update && apt install -y python3-venv python3-pip ufw
ufw allow 22 && ufw allow 80 && ufw allow 443 && ufw --force enable
adduser --system --group --home /opt/5g bot5g
```
Install Caddy (the https front door) by following https://caddyserver.com/docs/install for Ubuntu.

## 3. Register the Deriv OAuth app
At https://developers.deriv.com register an **OAuth2** application. Set its redirect (callback) address to exactly
`https://bot.yourdomain.com/callback` — same spelling, https, no trailing slash. Copy its **App ID**.
(The Personal-Access-Token app you used for the desktop bot is a different kind of app; make a new one.)

## 4. Copy the project and install
Upload this whole folder to `/opt/5g` (for example with `scp -r` or WinSCP), then:
```
cd /opt/5g
python3 -m venv .venv
.venv/bin/python -m pip install -r webapp/requirements-web.txt
mkdir -p data && chown -R bot5g:bot5g /opt/5g/data
cp webapp/deploy/env.example /etc/5g-bot.env && chmod 600 /etc/5g-bot.env
nano /etc/5g-bot.env          # put in your App ID and your https address
```

## 5. Start it
```
cp webapp/deploy/5g-bot.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now 5g-bot
cp webapp/deploy/Caddyfile /etc/caddy/Caddyfile      # edit the address first
systemctl restart caddy
journalctl -u 5g-bot -f        # live log; Ctrl+C to leave
```
Run **exactly one** copy of the bot. Sign-ins and running bots live in memory; several copies would break the limits.

## 6. Check
Open `https://bot.yourdomain.com/healthz`. You should see `{"ok":true,...}`.

## 7. Test before inviting friends (demo only!)
1. Sign in with your own Deriv account. Accept the risk notice. It opens on **DEMO**.
2. Confirm the balance appears and the price is moving ("Live").
3. Press play. Wait for a trade. Check it appears in your Deriv account history and in "Recent auto trades".
4. Only if all that works: tell friends.

## Things to know
- A Deriv sign-in lasts about **1 hour** and Deriv documents no refresh. The bot stops then; people sign in again.
- Safety rules live on the server: 10 trades a day max, stop after 3 losses in a row, fixed stake, real-mode typed
  confirmation, real stake cap 5.00 and daily loss stop 10.00 (change in `/etc/5g-bot.env`).
  Set `ALLOW_REAL=0` to switch real-money trading off for everybody at once.
- Day resets at midnight in `BOT_TZ` (Nairobi by default).
- Data (limits, trade log, ticks) is in `/opt/5g/data`. `ticks_R_100.csv` there can be fed to `backtest.py`.
- Update: upload new files, then `systemctl restart 5g-bot`. Everyone is signed out.
- This is a tool for people you know. Offering automated trading to others can have legal/licensing duties
  where you or they live, and Deriv has rules for apps. Check before sharing widely.
