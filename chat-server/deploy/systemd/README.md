# Non-container systemd deployment

These units assume the application checkout is `/opt/agentWorkCluster`, with
`chat-server`, `agent-service`, and `chat-client` as sibling projects. The Python
virtual environment is `/opt/agentWorkCluster/.venv`, and runtime data lives under
`/var/lib/chat-server`. Change all paths consistently if a different location is
required.

Install PostgreSQL 16 and Redis 7 locally or use private managed instances.
Neither service may be reachable from the public Internet. Create a dedicated
`chat-server` system user with no login shell, give it ownership of
`/var/lib/chat-server`, and make the environment file readable only by root and
that user.

```bash
sudo useradd --system --home-dir /var/lib/chat-server --shell /usr/sbin/nologin chat-server
sudo install -d -o chat-server -g chat-server /var/lib/chat-server/data /var/lib/chat-server/client
sudo install -d -m 750 -o root -g chat-server /etc/chat-server
sudo install -m 600 -o root -g chat-server /opt/agentWorkCluster/chat-server/.agent.env.example /etc/chat-server/agent.env
sudoedit /etc/chat-server/agent.env

cd /opt/agentWorkCluster
python3 -m venv .venv
.venv/bin/pip install -r agent-service/requirements.txt
cd chat-server
cargo build --release
install -m 755 target/release/chat-server ./chat-server

cd /opt/agentWorkCluster/chat-client
npm ci
npm run build
sudo rsync -a --delete dist/ /var/lib/chat-server/client/

cd /opt/agentWorkCluster/chat-server
sudo install -m 644 deploy/systemd/*.service /etc/systemd/system/
sudo caddy validate --config deploy/systemd/Caddyfile --adapter caddyfile
sudo install -m 644 deploy/systemd/Caddyfile /etc/caddy/Caddyfile
sudo systemctl daemon-reload
sudo systemctl enable --now chat-server agent-api agent-worker caddy
```

`agent-api` runs Alembic before every start; the separate `agent-migrate` unit is
available for an explicit migration deployment step. Check the private listeners
before exposing Caddy:

```bash
curl --fail http://127.0.0.1:9012/healthz
curl --fail http://127.0.0.1:9011/healthz
sudo ss -ltnp | rg ':9010|:9011|:9012|:5432|:6379'
sudo journalctl -u chat-server -u agent-api -u agent-worker -u caddy -f
```

Use `systemctl restart chat-server agent-api agent-worker caddy` after updating
the server. Do not use `start.sh` or `stop.sh` on a host where these systemd
units are enabled.
