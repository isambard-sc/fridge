from string import Template

import pulumi
from pulumi import ComponentResource, ResourceOptions
from pulumi_kubernetes.apps.v1 import (
    Deployment,
    DeploymentSpecArgs,
    DeploymentStrategyArgs,
)
from pulumi_kubernetes.core.v1 import (
    CapabilitiesArgs,
    ConfigMap,
    ConfigMapVolumeSourceArgs,
    ContainerArgs,
    ContainerPortArgs,
    EnvVarArgs,
    EnvVarSourceArgs,
    Namespace,
    PersistentVolumeClaim,
    PersistentVolumeClaimSpecArgs,
    PersistentVolumeClaimVolumeSourceArgs,
    PodSpecArgs,
    PodTemplateSpecArgs,
    Secret,
    SecretKeySelectorArgs,
    SeccompProfileArgs,
    SecurityContextArgs,
    Service,
    ServicePortArgs,
    ServiceSpecArgs,
    VolumeArgs,
    VolumeMountArgs,
    VolumeResourceRequirementsArgs,
)
from pulumi_kubernetes.meta.v1 import LabelSelectorArgs, ObjectMetaArgs

from enums import K8sEnvironment, PodSecurityStandard, SoftwareVersion


class VpnServerArgs:
    def __init__(self, config: pulumi.config.Config) -> None:
        self.config = config


class VpnServer(ComponentResource):
    def __init__(
        self, name: str, args: VpnServerArgs, opts: ResourceOptions | None = None
    ) -> None:
        super().__init__("fridge:fridge-access-cluster:VpnServer", name, {}, opts)
        child_opts = ResourceOptions.merge(opts, ResourceOptions(parent=self))

        k8s_environment = K8sEnvironment(args.config.require("k8s_env"))
        # Without NetBird, only HAProxy runs, and users reach it with kubectl port-forward
        netbird_enabled = args.config.require_object("netbird").get("enabled", True)

        self.vpn_ns = Namespace(
            "vpn-ns",
            metadata=ObjectMetaArgs(
                name="vpn-server",
                labels={}
                | (
                    PodSecurityStandard.PRIVILEGED.value
                    if netbird_enabled
                    else PodSecurityStandard.RESTRICTED.value
                ),
            ),
            opts=child_opts,
        )

        fridge_api_ip_raw = args.config.require("fridge_api_ip_address").strip()
        fridge_api_ip = fridge_api_ip_raw.split("/", 1)[0]
        match k8s_environment:
            case K8sEnvironment.AKS:
                fridge_api_endpoint = f"{fridge_api_ip}:443"
                isolated_k8s_api_endpoint = (
                    f"{args.config.require('isolated_cluster_api_endpoint')}:443"
                )
            case K8sEnvironment.DAWN | K8sEnvironment.ZENITH:
                fridge_api_endpoint = f"{fridge_api_ip}:30180"
                isolated_k8s_api_endpoint_raw = args.config.require(
                    "isolated_cluster_api_endpoint"
                ).strip()
                isolated_k8s_api_endpoint = (
                    f"{isolated_k8s_api_endpoint_raw.split('/', 1)[0]}:6443"
                )
            case K8sEnvironment.TALOS:
                # Both are the SideroLink gateway Service in the k3s cluster, which forwards
                # through the tunnel to the Talos node. Absolute names (trailing dot), so the
                # resolver doesn't try the search domains, which the DNS policy refuses
                gateway = args.config.require("isolated_cluster_api_endpoint").rstrip(
                    "."
                )
                fridge_api_endpoint = f"{fridge_api_ip.rstrip('.')}.:30180"
                isolated_k8s_api_endpoint = f"{gateway}.:6443"

        with open("./k8s/haproxy/haproxy.cfg", "r") as f:
            haproxy_cfg_file = Template(f.read()).substitute(
                fridge_api_endpoint=fridge_api_endpoint,
                isolated_k8s_api_endpoint=isolated_k8s_api_endpoint,
            )

        self.haproxy_config = ConfigMap(
            "haproxy-config",
            metadata=ObjectMetaArgs(
                namespace=self.vpn_ns.metadata.name,
                name="vpn-proxy-config",
            ),
            data={"haproxy.cfg": haproxy_cfg_file},
            opts=child_opts,
        )

        # Without NetBird the namespace is restricted, and HAProxy needs no privileges
        haproxy_security_context = (
            None
            if netbird_enabled
            else SecurityContextArgs(
                allow_privilege_escalation=False,
                capabilities=CapabilitiesArgs(drop=["ALL"]),
                run_as_non_root=True,
                run_as_user=99,  # haproxy in the official image
                run_as_group=99,
                seccomp_profile=SeccompProfileArgs(type="RuntimeDefault"),
            )
        )
        haproxy_container = ContainerArgs(
            name="haproxy",
            image=f"haproxy:{SoftwareVersion.HAPROXY.value}",
            ports=[
                ContainerPortArgs(container_port=8000, protocol="TCP"),
                ContainerPortArgs(container_port=6443, protocol="TCP"),
            ],
            volume_mounts=[
                VolumeMountArgs(
                    name="haproxy-config",
                    mount_path="/usr/local/etc/haproxy/haproxy.cfg",
                    sub_path="haproxy.cfg",
                ),
            ],
            security_context=haproxy_security_context,
        )
        containers = [haproxy_container]
        volumes = [
            VolumeArgs(
                name="haproxy-config",
                config_map=ConfigMapVolumeSourceArgs(
                    name=self.haproxy_config.metadata.name
                ),
            ),
        ]
        depends_on = [self.haproxy_config, self.vpn_ns]

        if netbird_enabled:
            netbird_config = args.config.require_secret_object("netbird")

            setup_key_secret = Secret(
                "netbird-setup-key",
                metadata=ObjectMetaArgs(
                    namespace=self.vpn_ns.metadata.name,
                    name="netbird-setup-key",
                ),
                string_data=netbird_config.apply(
                    lambda config: {"setup_key": config["setup_key"]}
                ),
                opts=child_opts,
            )

            # Use a PersistentVolumeClaim to store Netbird data, so that it persists across pod restarts
            self.netbird_data_volume = PersistentVolumeClaim(
                "netbird-data",
                metadata=ObjectMetaArgs(
                    namespace=self.vpn_ns.metadata.name,
                    name="netbird-data",
                ),
                spec=PersistentVolumeClaimSpecArgs(
                    access_modes=["ReadWriteOnce"],
                    resources=VolumeResourceRequirementsArgs(
                        requests={"storage": "100Mi"}
                    ),
                ),
                opts=child_opts,
            )

            containers.insert(
                0,
                ContainerArgs(
                    name="netbird-proxy",
                    image=f"netbirdio/netbird:{SoftwareVersion.NETBIRD.value}",
                    env=[
                        EnvVarArgs(
                            name="NB_SETUP_KEY",
                            value_from=EnvVarSourceArgs(
                                secret_key_ref=SecretKeySelectorArgs(
                                    name="netbird-setup-key",
                                    key="setup_key",
                                )
                            ),
                        ),
                        EnvVarArgs(
                            name="NB_MANAGEMENT_URL",
                            value=netbird_config.apply(
                                lambda config: config["management_url"]
                            ),
                        ),
                        EnvVarArgs(
                            name="NB_HOSTNAME",
                            value=netbird_config.apply(
                                lambda config: config["hostname"]
                            ),
                        ),
                    ],
                    volume_mounts=[
                        VolumeMountArgs(
                            name="netbird-data",
                            mount_path="/var/lib/netbird",
                        ),
                    ],
                    security_context=SecurityContextArgs(
                        capabilities=CapabilitiesArgs(
                            add=[
                                "NET_ADMIN",
                                "SYS_RESOURCE",
                                "SYS_ADMIN",
                            ]
                        ),
                    ),
                ),
            )
            volumes.insert(
                0,
                VolumeArgs(
                    name="netbird-data",
                    persistent_volume_claim=PersistentVolumeClaimVolumeSourceArgs(
                        claim_name=self.netbird_data_volume.metadata.name
                    ),
                ),
            )
            depends_on += [self.netbird_data_volume, setup_key_secret]

        self.vpn_deployment = Deployment(
            "netbird-proxy",
            metadata=ObjectMetaArgs(
                namespace=self.vpn_ns.metadata.name,
            ),
            spec=DeploymentSpecArgs(
                selector=LabelSelectorArgs(match_labels={"app": "netbird-proxy"}),
                strategy=DeploymentStrategyArgs(type="Recreate"),
                replicas=1,
                template=PodTemplateSpecArgs(
                    metadata=ObjectMetaArgs(
                        labels={"app": "netbird-proxy"},
                    ),
                    spec=PodSpecArgs(containers=containers, volumes=volumes),
                ),
            ),
            opts=ResourceOptions.merge(
                child_opts,
                ResourceOptions(depends_on=depends_on),
            ),
        )

        if not netbird_enabled:
            # The way in without NetBird: kubectl port-forward svc/vpn-proxy 8000 6443
            self.vpn_service = Service(
                "vpn-proxy",
                metadata=ObjectMetaArgs(
                    namespace=self.vpn_ns.metadata.name,
                    name="vpn-proxy",
                ),
                spec=ServiceSpecArgs(
                    type="ClusterIP",
                    selector={"app": "netbird-proxy"},
                    ports=[
                        ServicePortArgs(name="fridge-api", port=8000, target_port=8000),
                        ServicePortArgs(name="k8s-api", port=6443, target_port=6443),
                    ],
                ),
                opts=ResourceOptions.merge(
                    child_opts, ResourceOptions(depends_on=[self.vpn_deployment])
                ),
            )
