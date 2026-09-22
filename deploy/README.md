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

## Recommended path: ECS/Fargate inside the VPC

Run the Dockerfile as an ECS service on a task with an ENI in a subnet that has
proper network connectivity to Redshift (peering/Transit Gateway/PrivateLink -
no VPN client to babysit). There's no application-level auth in front of this
service (see ARCHITECTURE.md's "Why no application-level auth") - whatever fronts
it (an internal ALB/NLB, or nothing at all if only specific hosts need to reach it
directly), lock its security group down to only the specific hosts/services that
should be able to call `/query`, and never expose it to the open internet.
