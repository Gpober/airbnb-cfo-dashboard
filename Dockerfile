# Always-on Kalshi hourly BTC bot worker.
# Deploys to Railway / Render / Fly.io (any Docker host). Runs the --forever
# manage loop, which follows the hourly ticker rollover and reconnects the
# WebSocket as needed. Safety defaults (DEMO + DRY_RUN) still apply unless the
# host's env vars override them.
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY kalshi_btc_hourly_bot.py kalshi_selftest.py ./

# Build-time sanity check: offline self-tests must pass in the image.
RUN python kalshi_btc_hourly_bot.py --selftest

CMD ["python", "kalshi_btc_hourly_bot.py", "--forever"]
