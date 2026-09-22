# Deploying the gateway

This service holds a real Redshift connection pool, so it needs to run as a
long-lived process, not Lambda.

## Quick path: same EC2 that already has network access (e.g. via the Client VPN)

1. `pip install -r requirements.txt` into a venv at `.venv`.
2. Set the env vars from `app/config.py` (prefixed `GATEWAY_`) in `/opt/redshift-gateway/.env`.
3. Install `redshift-gateway.service` to `/etc/systemd/system/` and
   `systemctl enable --now redshift-gateway`.
4. If relying on a Client VPN tunnel for connectivity, also set up the VPN client as
   its own systemd unit with `Restart=always`, and add it to `After=`/`Wants=` in
   this unit so the gateway doesn't start before the tunnel is up. This is a stopgap -
   see ARCHITECTURE.md's networking note about getting the server its own dedicated
   path instead of depending on a shared/personal VPN profile.

## Running as a Docker container on this same EC2

`docker-compose.yml` (repo root) wraps the run configuration below so this is just:

```bash
cd ~/mosaic_common_service
docker compose up -d --build
```

That's equivalent to the manual `docker build` + `docker run` this replaces, and
does exactly two things worth understanding, not just running blindly:

- **`network_mode: host`** - the VPN tunnel runs on the *host*, outside the
  container. Docker's default networking gives a container its own isolated
  network namespace, which would NOT see the host's `tun0` route to Redshift.
  `network_mode: host` makes the container share the host's network stack
  directly, the same way running uvicorn on the host does. This also means there's
  no port mapping - the app binds directly to the port set in the Dockerfile's
  `CMD` (currently 8339) on the host itself.
- **The credentials file is bind-mounted, not baked into the image.** It stays on
  the host at `/etc/mosaic_common_service/redshift-credentials.json` (outside the
  repo and outside the image), mounted read-only at the same path
  `GATEWAY_REDSHIFT_CREDENTIALS_FILE` expects. A secret baked into a Docker image
  would be stuck in every layer and any registry push, permanently - `docker
  compose up` being one command doesn't change that risk, so this still isn't
  something to fold into the Dockerfile/image itself.

`.env` (repo root, gitignored) should hold the same `GATEWAY_*` vars as the
non-Docker path. Make sure the VPN tunnel is up on the host *before* running
`docker compose up` - `restart: unless-stopped` handles the container crashing or
Redshift being briefly unreachable, but won't bring the tunnel back up itself.

Check it: `docker compose logs -f`, then `curl localhost:8339/health` from the host.

## Recommended path: ECS/Fargate inside the VPC

Run the Dockerfile as an ECS service on a task with an ENI in a subnet that has
proper network connectivity to Redshift (peering/Transit Gateway/PrivateLink -
no VPN client to babysit). There's no application-level auth in front of this
service (see ARCHITECTURE.md's "Why no application-level auth") - whatever fronts
it (an internal ALB/NLB, or nothing at all if only specific hosts need to reach it
directly), lock its security group down to only the specific hosts/services that
should be able to call `/query`, and never expose it to the open internet.
