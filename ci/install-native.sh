#!/usr/bin/env bash
set -euo pipefail

# This script must never run on a personal or persistent runner.
[[ "${RUNNER_ENVIRONMENT:-}" == github-hosted ]]
[[ "${RUNNER_OS:-}" == Linux ]]
[[ "${GITHUB_REPOSITORY:-}" == NexaFlowFrance/openfamily-catalog-tests ]]
[[ "$(uname -m)" == x86_64 ]]
[[ "$(docker info --format '{{.Swarm.LocalNodeState}}')" == inactive ]]

available_bytes() { df --output=avail -B1 / | tail -n 1 | tr -d ' '; }
minimum=$((30 * 1024 * 1024 * 1024))
if (( $(available_bytes) < minimum )); then
  # Fixed SDK paths, only after proving this is the fresh standard GitHub VM.
  for target in /usr/local/lib/android /usr/share/dotnet /opt/ghc /opt/hostedtoolcache/CodeQL; do
    if [[ -d "$target" && ! -L "$target" && "$(readlink -f "$target")" == "$target" ]]; then
      sudo rm -rf -- "$target"
    fi
  done
fi
free_bytes=$(available_bytes)
if (( free_bytes < minimum )); then
  echo 'BLOCKED: this standard VM does not provide the required 30 GiB free disk space.'
  exit 2
fi
printf 'PREFLIGHT PASS: free disk bytes=%s; required=%s; fresh standard ubuntu-24.04 VM.\n' "$free_bytes" "$minimum"

for command in docker curl jq python3 openssl nsenter iptables ip6tables; do command -v "$command" >/dev/null; done
[[ "$(awk '/MemTotal/{print $2}' /proc/meminfo)" -ge 2097152 ]]

# Images are downloaded by the VM's Docker daemon; no registry login or token.
docker pull dokploy/dokploy:v0.31.3 >/dev/null
docker pull postgres:16 >/dev/null
docker pull traefik:v3.7.19 >/dev/null
docker pull ghcr.io/nexaflowfrance/openfamily:2.2.0 >/dev/null

vm_address=$(ip -4 route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1);exit}}')
[[ -n "$vm_address" ]]
# Swarm output contains a join token; it is deliberately never logged.
docker swarm init --advertise-addr "$vm_address" >/dev/null
docker network create --driver overlay --attachable dokploy-network >/dev/null
sudo mkdir -p /etc/dokploy
sudo chmod 755 /etc/dokploy
openssl rand -hex 32 | docker secret create dokploy_postgres_password - >/dev/null
openssl rand -hex 32 | docker secret create dokploy_auth_secret - >/dev/null

docker service create --quiet \
  --name dokploy-postgres --constraint 'node.role==manager' \
  --network dokploy-network \
  --env POSTGRES_USER=dokploy --env POSTGRES_DB=dokploy \
  --secret source=dokploy_postgres_password,target=/run/secrets/postgres_password \
  --env POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password \
  --mount type=volume,source=dokploy-postgres,target=/var/lib/postgresql/data \
  postgres:16 >/dev/null
postgres_address=$(docker service inspect dokploy-postgres --format '{{json .Endpoint.VirtualIPs}}' | jq -r '.[0].Addr|split("/")[0]')
[[ -n "$postgres_address" && "$postgres_address" != null ]]

# Normal v0.31.3 CMD: wait for PostgreSQL, migrate, run server. Do not run setup.mjs.
# No --publish: the controller has no host port at any stage.
docker service create --quiet \
  --name dokploy --replicas 1 --constraint 'node.role==manager' \
  --network dokploy-network \
  --host "dokploy-postgres:$postgres_address" \
  --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock \
  --mount type=bind,source=/etc/dokploy,target=/etc/dokploy \
  --mount type=volume,source=dokploy,target=/root/.docker \
  --secret source=dokploy_postgres_password,target=/run/secrets/postgres_password \
  --secret source=dokploy_auth_secret,target=/run/secrets/dokploy_auth_secret \
  --env POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password \
  --env BETTER_AUTH_SECRET_FILE=/run/secrets/dokploy_auth_secret \
  --env RELEASE_TAG=v0.31.3 \
  --update-parallelism 1 --update-order stop-first \
  dokploy/dokploy:v0.31.3 >/dev/null

ready=false
for _ in $(seq 1 120); do
  controller=$(docker ps -q --filter label=com.docker.swarm.service.name=dokploy | head -n 1)
  if [[ -n "$controller" ]] && docker exec "$controller" curl -fsS --max-time 3 \
    http://localhost:3000/api/trpc/settings.health >/dev/null 2>&1; then ready=true; break; fi
  sleep 2
done
if [[ "$ready" != true ]]; then
  echo 'FAIL: pinned native Dokploy service did not become healthy.'
  exit 1
fi
[[ "$(docker service inspect dokploy --format '{{json .Endpoint.Spec.Ports}}')" == null ]]

# Dokploy's first signup tries public-IP lookup. Block that controller's external
# egress, including Docker's embedded DNS, before any signup. PostgreSQL is in
# /etc/hosts and the local overlay; host Docker can still pull public images.
controller_pid=$(docker inspect "$controller" --format '{{.State.Pid}}')
overlay_subnet=$(docker network inspect dokploy-network --format '{{json .IPAM.Config}}' | jq -r '.[0].Subnet')
[[ "$controller_pid" -gt 1 && -n "$overlay_subnet" && "$overlay_subnet" != null ]]
sudo nsenter -t "$controller_pid" -n iptables -N OPENFAMILY_CI_EGRESS
sudo nsenter -t "$controller_pid" -n iptables -I OUTPUT 1 -j OPENFAMILY_CI_EGRESS
sudo nsenter -t "$controller_pid" -n iptables -A OPENFAMILY_CI_EGRESS -d 127.0.0.11/32 -j REJECT
sudo nsenter -t "$controller_pid" -n iptables -A OPENFAMILY_CI_EGRESS -d 127.0.0.0/8 -j RETURN
sudo nsenter -t "$controller_pid" -n iptables -A OPENFAMILY_CI_EGRESS -d "$overlay_subnet" -j RETURN
sudo nsenter -t "$controller_pid" -n iptables -A OPENFAMILY_CI_EGRESS -j REJECT
sudo nsenter -t "$controller_pid" -n ip6tables -N OPENFAMILY_CI_EGRESS6
sudo nsenter -t "$controller_pid" -n ip6tables -I OUTPUT 1 -j OPENFAMILY_CI_EGRESS6
sudo nsenter -t "$controller_pid" -n ip6tables -A OPENFAMILY_CI_EGRESS6 -d ::1/128 -j RETURN
sudo nsenter -t "$controller_pid" -n ip6tables -A OPENFAMILY_CI_EGRESS6 -j REJECT
# No outbound probe is sent: the rules are checked locally in this namespace.
sudo nsenter -t "$controller_pid" -n iptables -C OUTPUT -j OPENFAMILY_CI_EGRESS
sudo nsenter -t "$controller_pid" -n iptables -C OPENFAMILY_CI_EGRESS -j REJECT
sudo nsenter -t "$controller_pid" -n ip6tables -C OUTPUT -j OPENFAMILY_CI_EGRESS6
sudo nsenter -t "$controller_pid" -n ip6tables -C OPENFAMILY_CI_EGRESS6 -j REJECT
printf '%s' "$controller" > .controller-id
echo 'PRIVACY PASS: controller unpublished; IPv4/IPv6 external controller egress blocked before signup.'

[[ -s /etc/dokploy/traefik/traefik.yml ]]
docker run -d --name dokploy-traefik --restart always \
  --network dokploy-network \
  -v /etc/dokploy/traefik/traefik.yml:/etc/traefik/traefik.yml \
  -v /etc/dokploy/traefik/dynamic:/etc/dokploy/traefik/dynamic \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -p 127.0.0.1:18080:80/tcp traefik:v3.7.19 >/dev/null
echo 'NATIVE SETUP PASS: Dokploy v0.31.3 and loopback-only Traefik v3.7.19.'
