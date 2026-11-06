from enum import Enum, unique


@unique
class K8sEnvironment(Enum):
    AKS = "AKS"
    DAWN = "Dawn"
    K3S = "K3s"
    # k3s access cluster; Talos isolated cluster reached through a TCP gateway (docs/deploy/talos.md)
    TALOS = "Talos"
    ZENITH = "Zenith"


@unique
class PodSecurityStandard(Enum):
    RESTRICTED = {"pod-security.kubernetes.io/enforce": "restricted"}
    PRIVILEGED = {"pod-security.kubernetes.io/enforce": "privileged"}


@unique
class TlsEnvironment(Enum):
    STAGING = "staging"
    PRODUCTION = "production"
    DEVELOPMENT = "development"


tls_issuer_names = {
    TlsEnvironment.STAGING: "letsencrypt-staging",
    TlsEnvironment.PRODUCTION: "letsencrypt-prod",
    TlsEnvironment.DEVELOPMENT: "dev-issuer",
}

# Class for software versions - e.g. for helm charts and container images
# where possible, these should be set to specific versions rather than "latest" to ensure reproducibility,
# and protect against breaking changes.
# But in some cases (e.g. curl-jq) no numbered version tags are available.
@unique
class SoftwareVersion(Enum):
    CERT_MANAGER = "1.21.2"
    CURL_JQ = "latest"
    HAPROXY = "3.4.6"
    HARBOR = "1.19.2"
    # Talos target: the tenant VM is arm64, and no released Harbor (<= 2.15.4) has arm64 images.
    # 2.16.0-rc1 is the first multi-arch build; replace with 2.16.0 when it is released
    HARBOR_TALOS = "1.19.4"
    HARBOR_TALOS_IMAGE_TAG = "v2.16.0-rc1"
    INGRESS_NGINX = "4.13.2"
    LONGHORN = "1.9.0"
    NETBIRD = "0.80.0"
