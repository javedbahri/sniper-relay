# Sniper Relay

FastAPI + Docker worker that relays TradingView alerts to Interactive Brokers (TWS/Gateway).

## Quick start
```bash
docker compose up -d --build


### 4) Add `.env.example` (document keys; keep real `.env` ignored)
```bash
cat > .env.example <<'EOF'
PATH_TOKEN=__set_in_local_env__
SHARED_SECRET=__set_in_local_env__
NONCE_TTL_SECONDS=60
MAX_SKEW_SECONDS=15
IB_TWS_HOST=host.docker.internal
IB_TWS_PORT=7497
IB_CLIENT_ID=1001
