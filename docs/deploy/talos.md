(deploy-talos)=
# Deploying to Talos

The `Talos` target runs the isolated cluster on [Talos Linux](https://www.talos.dev/) and the access cluster on k3s.
It was built for a single-node Talos cluster on a virtual machine that the operator of the hardware cannot reach.
The node joins its tenant over [SideroLink](https://docs.siderolabs.com/talos/latest/networking/siderolink), a WireGuard tunnel to an agent the tenant runs in their own cluster, and that cluster is the access cluster.

The tunnel changes how the two clusters talk to each other:

- Talos's firewall admits the Talos API (50000), the Kubernetes API (6443) and the FRIDGE API (30180) **only from the tunnel's subnet**.
- The tunnel's end exists only inside the agent's pod in the access cluster.
  A gateway container in that pod forwards between the tunnel and the access cluster.

```
users ──kubectl port-forward──> HAProxy (vpn-server)  :8000 ─┐  :6443 ─┐
                                                             v         v
                talos-gateway Service ──> gateway container ──tunnel──> Talos node :30180 (relay) / :6443
Talos containerd ──tunnel──> gateway container :443 ──> ingress-nginx ──> Harbor
```

So every flow between the clusters that FRIDGE allows goes through the tunnel:
HAProxy to the isolated cluster's API server, HAProxy to the FRIDGE API, and the isolated node's container runtime to Harbor.
There is no NetBird; set `netbird.enabled: false` and reach HAProxy with `kubectl port-forward`.

## What is different from Dawn

| | Dawn | Talos |
|---|---|---|
| Access to the isolated cluster | NetBird + HAProxy | HAProxy only, through the SideroLink gateway |
| FRIDGE API | NodePort 30180 | ClusterIP, plus a host-network HAProxy relay on the node's port 30180. The tunnel is IPv6 and the cluster IPv4-only, so neither Cilium nor kube-proxy would serve the NodePort there |
| Registry mirrors, Harbor CA, Harbor host name | Privileged DaemonSets write the node's files | Machine configuration, applied by the tenant with `talosctl`. Talos has no writable host filesystem for pods |
| Encrypted storage | Longhorn | Longhorn (1.12.1; Talos 1.14 runs Kubernetes 1.37), on the node's KMS-encrypted EPHEMERAL volume |
| GPUs | Intel GPU operator | NVIDIA device plugin. The driver and container toolkit are Talos system extensions |
| Harbor proxy projects | docker.io, quay.io, ghcr.io, registry.k8s.io | Also `proxy-nvcr.io`, for the device plugin and CUDA images |

## Requirements

### Access cluster (k3s)

- k3s with Cilium as the CNI, kube-proxy replacement and Hubble, as in `infra/k3s/install.sh`.
  Traefik off. ServiceLB on, because ingress-nginx and the SideroLink agent are LoadBalancer Services.
- The tenant's SideroLink pod in namespace `talos-tenant` (label `app.kubernetes.io/name: talos-tenant`), with:
  - a gateway container that listens on 6443 and 30180 and forwards them to the node's tunnel address;
  - a listener on 443 at the tunnel's end that forwards to `ingress-nginx-controller.ingress-nginx.svc:443`. TCP passthrough keeps the TLS SNI, so Harbor's own certificate is served. It must be 443, not a high port: Harbor's token realm is its `externalURL`, `https://<harbor_fqdn>/`, so the node fetches its pull tokens there;
  - a ClusterIP Service `talos-gateway` for 6443 and 30180.
- Its NetworkPolicy must admit 6443 and 30180 from the `vpn-server` namespace, and should allow the pod no egress.
  The access stack adds the gateway's one way out itself, as a CiliumNetworkPolicy (`talos-gateway-to-harbor`): DNS for ingress-nginx's Service name only, and 443 to the ingress-nginx controller only with Harbor's TLS SNI (`serverNames`, which needs Cilium's L7 proxy, on by default).
  A policy applies to the whole pod, and that pod also holds the tenant's KMS key and admin credentials, so keep it this narrow.

### Isolated cluster (Talos)

The machine configuration must have:

- no CNI and no kube-proxy (`KubeFlannelCNIConfig` deleted, `KubeProxyConfig` `enabled: false`), then Cilium installed with Talos's values (KubePrism on `localhost:7445`, `cgroup.autoMount.enabled=false`, the reduced capability lists, kube-proxy replacement). Leave `bpf.masquerade` off: with Talos's `forwardKubeDNSToHost`, CoreDNS breaks with it on;
- the `siderolabs/iscsi-tools` and `siderolabs/util-linux-tools` system extensions, and a kubelet bind mount of `/var/lib/longhorn` (`rshared`), for Longhorn;
- a firewall rule admitting 30180 from the tunnel's subnet;
- once the access stack is up, the Harbor settings that the DaemonSets write on Dawn:
  - a `StaticHostConfig` mapping Harbor's host name to the tunnel's end;
  - a `RegistryMirrorConfig` for each of docker.io, quay.io, ghcr.io, registry.k8s.io and nvcr.io, pointing at `https://<harbor_fqdn>/v2/<project>` with `overridePath: true` (and `skipFallback: true`, so the node pulls only through Harbor);
  - a `RegistryTLSConfig` for `<harbor_fqdn>` with the access stack's `harbor_ca_bundle` output.

  The project names are the ones in `HARBOR_MIRRORS` in `isolated-cluster/components/container_runtime.py`, plus `proxy-nvcr.io`.

## Deploying

Use a local Pulumi backend, so that the isolated stack's `StackReference` (`organization/fridge-access/<stack>`) resolves:

```console
pulumi login file://~/.pulumi-fridge
```

Example stack files, with placeholders for the secrets, are `Pulumi.talos-demo.yaml.example` in each project.
Copy each to `Pulumi.talos-demo.yaml`, then set the secrets with `pulumi config set --secret` (never commit them).

1. **Access cluster.** In `infra/fridge/access-cluster`, with `kubernetes:context` set to the k3s cluster:

   ```console
   pulumi up -s talos-demo
   pulumi stack output -s talos-demo harbor_fqdn
   pulumi stack output -s talos-demo --show-secrets harbor_ca_bundle
   ```

2. **Talos Harbor settings.** Apply the machine configuration above with `talosctl patch mc`, using those two outputs.
   From then on the node pulls every image through Harbor.

3. **A kubeconfig through HAProxy.** Forward HAProxy's 6443 and write a kubeconfig whose server is `https://127.0.0.1:<port>`.
   Set `tls-server-name` to a name in the API server's certificate, for example `talos-gateway.talos-tenant.svc.cluster.local` added as an extra certificate SAN.
   This kubeconfig's context is the isolated stack's `kubernetes:context`.

4. **Isolated cluster.** In `infra/fridge/isolated-cluster`, with the port-forward running:

   ```console
   pulumi up -s talos-demo
   ```

5. **Test.** Forward HAProxy's 8000 and use the FRIDGE API at `https://127.0.0.1:8000/docs`.
   With `run_tests: true`, the `nvidia-gpu-example` workflow template runs `nvidia-smi` on four GPUs and CUDA's `vectoradd`.

## Known limitations

- Single node only: the API endpoint is the one node's tunnel address.
- Harbor on arm64: no released Harbor (up to 2.15.4) has arm64 images, so on Talos the access stack installs chart 1.19.4 with every image at `v2.16.0-rc1`, the first multi-arch build (`SoftwareVersion.HARBOR_TALOS*`). Move to 2.16.0 when it is released.
- The gateway's egress is limited by TLS SNI, not by the HTTP `Host` header. It holds because Harbor's is the only Ingress on the access cluster: a request with Harbor's SNI and another `Host` gets ingress-nginx's default 404 backend. Do not add other Ingresses to that ingress-nginx without revisiting `talos-gateway-to-harbor` (a separate IngressClass for Harbor, or L7 rules on `Host`).
- Anything that can pull through Harbor's proxy projects can make Harbor ask the upstream registries for image names it chooses: a narrow covert channel, which the node has by design. The tunnel pod has no Harbor credentials, so it can only pull.
- Cilium 1.20 is tested by Cilium up to Kubernetes 1.36; Talos 1.14 runs 1.37.
- Longhorn on Talos 1.14 runs with workload isolation (the kubelet and pods in their own PID and mount namespace), which Longhorn does not document. If volumes fail to attach, set `SecurityProfileConfig` `workloadIsolation: false` and report it.
