# OpenFamily catalogue integration tests

This public repository contains only purpose-written integration-test code and public OpenFamily template data. The workflow uses a fresh **standard GitHub-hosted `ubuntu-24.04` VM in a public repository**. GitHub documents this compute as free; it uses no larger runner, paid service, cache/artifact upload or personal server.

The manually triggered `dokploy-native-api.yml` workflow installs pinned Dokploy **v0.31.3** and Traefik **v3.7.19**, imports the real OpenFamily Compose/TOML template through Dokploy's API and checks routing, application health, a synthetic account, native stop/start and persistence across native container replacement.

The Dokploy controller publishes no host port. Traefik listens only on `127.0.0.1:18080`. The controller's external egress is blocked before its first signup to suppress its public-IP lookup. All credentials are generated inside the disposable VM, masked and discarded with it. No GitHub/Docker account secret is referenced or passed to the test; source is fetched anonymously with `permissions: {}`.

The test requires at least **30 GiB actually free disk space** before installation. If needed, it removes fixed unused SDK directories only after checking that it is running in this repository on a fresh GitHub-hosted Linux VM. It stops without provisioning if the requirement cannot be met.

Results are explicit native API results. They do **not** claim visual dashboard testing, public DNS, external access, HTTPS issuance, ARM validation or catalogue acceptance. A failed native operation remains a failure; it is not hidden behind a direct Docker substitute.

## Official sources

- [GitHub runner specifications](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [Dokploy manual installation](https://docs.dokploy.com/docs/core/manual-installation)
- [Dokploy v0.31.3 source](https://github.com/Dokploy/dokploy/tree/v0.31.3)
- [Dokploy Compose API](https://docs.dokploy.com/docs/api/compose)
- [Dokploy template contribution guide](https://github.com/Dokploy/templates/blob/canary/CONTRIBUTING.md)
- [OpenFamily 2.2.0 all-in-one smoke test](https://github.com/NexaFlowFrance/OpenFamily/blob/v2.2.0/scripts/smoke-all-in-one.sh)

OpenFamily is available at [openfamily.fr](https://openfamily.fr/) and [NexaFlowFrance/OpenFamily](https://github.com/NexaFlowFrance/OpenFamily).
