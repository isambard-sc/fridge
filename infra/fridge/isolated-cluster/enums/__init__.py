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


class SoftwareVersion(Enum):
    AMD_GPU_OPERATOR = "1.5.1"
    ARGO_WORKFLOWS = "2.0.6"  # Corresponds to Argo Workflows v4.1.3
    CERT_MANAGER = "1.21.2"
    CURL = "8.22.0"
    FRIDGE_API = "0.8.0"
    HAPROXY = "3.4.6"
    INTEL_GPU_OPERATOR = "0.35.0"
    LONGHORN = "1.9.0"
    # Talos 1.14 runs Kubernetes 1.37, newer than Longhorn 1.9 was tested against
    LONGHORN_TALOS = "1.12.1"
    NVIDIA_DEVICE_PLUGIN = "0.20.1"
    NODE_FEATURE_DISCOVERY = "0.18.3"
    SEAWEEDFS = "4.51.0"
    TRUST_MANAGER = "0.25.0"
