import pulumi
from pulumi import ComponentResource, Output, ResourceOptions
from pulumi_kubernetes.apiextensions import CustomResource
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
    EmptyDirVolumeSourceArgs,
    EnvFromSourceArgs,
    HTTPGetActionArgs,
    Namespace,
    PodSpecArgs,
    PodTemplateSpecArgs,
    ProbeArgs,
    ProjectedVolumeSourceArgs,
    SeccompProfileArgs,
    Secret,
    SecretEnvSourceArgs,
    SecretVolumeSourceArgs,
    SecurityContextArgs,
    Service,
    ServiceAccount,
    ServiceAccountTokenProjectionArgs,
    ServicePortArgs,
    ServiceSpecArgs,
    VolumeArgs,
    VolumeMountArgs,
    VolumeProjectionArgs,
)
from pulumi_kubernetes.meta.v1 import LabelSelectorArgs, ObjectMetaArgs
from pulumi_kubernetes.rbac.v1 import (
    PolicyRuleArgs,
    Role,
    RoleBinding,
    RoleRefArgs,
    SubjectArgs,
)

from enums import K8sEnvironment, PodSecurityStandard, SoftwareVersion

API_SERVER_IMAGE = (
    f"ghcr.io/alan-turing-institute/fridge:{SoftwareVersion.FRIDGE_API.value}"
)


class ApiServerArgs:
    def __init__(
        self,
        argo_server_ns: str,
        argo_workflows_ns: str,
        cluster_issuer: CustomResource,
        config: pulumi.Config,
        fridge_api_ip: Output[str],
        k8s_environment: K8sEnvironment,
        s3_url: Output[str],
        verify_tls: bool = True,
    ) -> None:
        self.argo_server_ns = argo_server_ns
        self.argo_workflows_ns = argo_workflows_ns
        self.cluster_issuer = cluster_issuer
        self.config = config
        self.fridge_api_ip = fridge_api_ip
        self.k8s_environment = k8s_environment
        self.s3_url = s3_url
        self.verify_tls = verify_tls


class ApiServer(ComponentResource):
    def __init__(
        self,
        name: str,
        args: ApiServerArgs,
        opts: ResourceOptions | None = None,
    ) -> None:
        super().__init__("fridge:ApiServer", name, {}, opts)
        child_opts = ResourceOptions.merge(opts, ResourceOptions(parent=self))

        api_server_ns = Namespace(
            "api-server-ns",
            metadata=ObjectMetaArgs(
                name="fridge-api",
                labels={"tls-trust-bundle": "enabled"}
                | PodSecurityStandard.RESTRICTED.value,
            ),
            opts=child_opts,
        )

        # Define argo workflows service accounts and roles
        # See https://argo-workflows.readthedocs.io/en/latest/security/
        argo_workflows_api_role = Role(
            "argo-workflows-api-role",
            metadata=ObjectMetaArgs(
                name="argo-workflows-api-role",
                namespace=args.argo_workflows_ns,
            ),
            rules=[
                PolicyRuleArgs(
                    api_groups=["argoproj.io"],
                    resources=[
                        "workflowtemplates",
                    ],
                    verbs=[
                        "get",
                        "list",
                        "watch",
                        "update",
                    ],
                ),
                PolicyRuleArgs(
                    api_groups=["argoproj.io"],
                    resources=[
                        "workflows",
                    ],
                    verbs=[
                        "create",
                        "get",
                        "list",
                        "watch",
                        "update",
                    ],
                ),
                PolicyRuleArgs(
                    api_groups=[""],
                    resources=[
                        "pods",
                        "pods/log",
                    ],
                    verbs=[
                        "get",
                        "list",
                        "watch",
                    ],
                ),
            ],
            opts=child_opts,
        )

        fridge_api_sa = ServiceAccount(
            "fridge-api-sa",
            metadata=ObjectMetaArgs(
                name="fridge-api-sa",
                namespace=api_server_ns.metadata.name,
            ),
            opts=child_opts,
        )

        fridge_api_config = Secret(
            "fridge-api-config",
            metadata=ObjectMetaArgs(
                name="fridge-api-config",
                namespace=api_server_ns.metadata.name,
            ),
            string_data={
                "ARGO_SERVER_NS": args.argo_server_ns,
                "FRIDGE_API_ADMIN": args.config.require_secret("fridge_api_admin"),
                "FRIDGE_API_PASSWORD": args.config.require_secret(
                    "fridge_api_password"
                ),
                "S3_ACCESS_KEY": args.config.require_secret("s3_access_key"),
                "S3_SECRET_KEY": args.config.require_secret("s3_secret_key"),
                "S3_SECURE": str(True),
                "S3_URL": args.s3_url,
                "VERIFY_TLS": str(args.verify_tls),
            },
            opts=child_opts,
        )

        argo_workflows_api_rolebinding = RoleBinding(
            "argo-workflows-api-role-binding",
            metadata=ObjectMetaArgs(
                name="argo-workflows-api-role-binding",
                namespace=args.argo_workflows_ns,
            ),
            role_ref=RoleRefArgs(
                api_group="rbac.authorization.k8s.io",
                kind="Role",
                name=argo_workflows_api_role.metadata.name,
            ),
            subjects=[
                SubjectArgs(
                    kind="ServiceAccount",
                    name=fridge_api_sa.metadata.name,
                    namespace=api_server_ns.metadata.name,
                )
            ],
            opts=ResourceOptions.merge(
                child_opts,
                ResourceOptions(depends_on=[argo_workflows_api_role, fridge_api_sa]),
            ),
        )

        with open("./k8s/haproxy/haproxy.cfg", "r") as f:
            haproxy_cfg_file = f.read()

        haproxy_config = ConfigMap(
            "haproxy-config",
            metadata=ObjectMetaArgs(
                namespace=api_server_ns.metadata.name,
                name="fridge-api-haproxy-config",
            ),
            data={
                "haproxy.cfg": haproxy_cfg_file,
            },
            opts=child_opts,
        )

        fridge_api_ip_raw = args.fridge_api_ip.apply(
            lambda ip: ip.split("/")[0] if "/" in ip else ip
        )

        fridge_api_tls_cert = CustomResource(
            "fridge-api-certificate",
            api_version="cert-manager.io/v1",
            kind="Certificate",
            metadata=ObjectMetaArgs(
                name="fridge-api-tls",
                namespace=api_server_ns.metadata.name,
            ),
            spec={
                "secretName": "fridge-api-tls",
                "issuerRef": {
                    "name": args.cluster_issuer.metadata["name"],
                    "kind": "ClusterIssuer",
                },
                # On Talos the access cluster reaches the API by the gateway's Service name
                (
                    "dnsNames"
                    if args.k8s_environment is K8sEnvironment.TALOS
                    else "ipAddresses"
                ): [
                    fridge_api_ip_raw,
                ],
            },
            opts=ResourceOptions.merge(
                child_opts,
                ResourceOptions(depends_on=[api_server_ns]),
            ),
        )

        fridge_api_server = Deployment(
            "fridge-api-server",
            metadata=ObjectMetaArgs(
                name="fridge-api-server",
                namespace=api_server_ns.metadata.name,
            ),
            spec=DeploymentSpecArgs(
                replicas=1,
                selector=LabelSelectorArgs(match_labels={"app": "fridge-api-server"}),
                template=PodTemplateSpecArgs(
                    metadata=ObjectMetaArgs(labels={"app": "fridge-api-server"}),
                    spec=PodSpecArgs(
                        init_containers=[
                            ContainerArgs(
                                name="haproxy-cert-init",
                                image="alpine:3.20",
                                command=[
                                    "/bin/sh",
                                    "-c",
                                    "cat /tls/tls.crt /tls/tls.key > /haproxy/certs/fridge_api.pem",
                                ],
                                security_context=SecurityContextArgs(
                                    allow_privilege_escalation=False,
                                    capabilities=CapabilitiesArgs(
                                        drop=["ALL"],
                                    ),
                                    run_as_user=1001,
                                    run_as_group=3000,
                                    run_as_non_root=True,
                                    seccomp_profile=SeccompProfileArgs(
                                        type="RuntimeDefault"
                                    ),
                                ),
                                volume_mounts=[
                                    VolumeMountArgs(
                                        name="fridge-api-tls",
                                        mount_path="/tls",
                                        read_only=True,
                                    ),
                                    VolumeMountArgs(
                                        name="haproxy-certs",
                                        mount_path="/haproxy/certs",
                                    ),
                                ],
                            ),
                        ],
                        containers=[
                            ContainerArgs(
                                name="fridge-api-server",
                                env_from=[
                                    EnvFromSourceArgs(
                                        secret_ref=SecretEnvSourceArgs(
                                            name=fridge_api_config.metadata.name
                                        )
                                    )
                                ],
                                image=API_SERVER_IMAGE,
                                image_pull_policy="Always",
                                # for liveness and readiness probes, use an initial delay to allow the other services to start up
                                # use a higher failure threshold for readiness probe to allow for temporary unavailability of Argo or MinIO
                                liveness_probe=ProbeArgs(
                                    http_get=HTTPGetActionArgs(
                                        path="/healthz", port=8000
                                    ),
                                    initial_delay_seconds=5,
                                    period_seconds=20,
                                    failure_threshold=3,
                                ),
                                ports=[ContainerPortArgs(container_port=8000)],
                                readiness_probe=ProbeArgs(
                                    http_get=HTTPGetActionArgs(
                                        path="/readyz", port=8443, scheme="HTTPS"
                                    ),
                                    initial_delay_seconds=10,
                                    period_seconds=30,
                                    timeout_seconds=5,
                                    failure_threshold=5,
                                ),
                                security_context=SecurityContextArgs(
                                    allow_privilege_escalation=False,
                                    capabilities=CapabilitiesArgs(
                                        drop=["ALL"],
                                    ),
                                    run_as_user=1001,
                                    run_as_group=3000,
                                    run_as_non_root=True,
                                    seccomp_profile=SeccompProfileArgs(
                                        type="RuntimeDefault"
                                    ),
                                ),
                                startup_probe=ProbeArgs(
                                    http_get=HTTPGetActionArgs(
                                        path="/healthz", port=8000
                                    ),
                                    initial_delay_seconds=5,
                                    period_seconds=10,
                                    failure_threshold=30,
                                ),
                                volume_mounts=[
                                    VolumeMountArgs(
                                        name="token-vol",
                                        mount_path="/service-account",
                                        read_only=True,
                                    ),
                                    VolumeMountArgs(
                                        name="tls-trust-bundle",
                                        mount_path="/etc/ssl/certs",
                                        read_only=True,
                                    ),
                                ],
                            ),
                            ContainerArgs(
                                name="haproxy",
                                image=f"haproxy:{SoftwareVersion.HAPROXY.value}",
                                ports=[
                                    ContainerPortArgs(
                                        container_port=8443, protocol="TCP"
                                    ),
                                ],
                                security_context=SecurityContextArgs(
                                    allow_privilege_escalation=False,
                                    capabilities=CapabilitiesArgs(
                                        drop=["ALL"],
                                    ),
                                    run_as_user=1001,
                                    run_as_group=3000,
                                    run_as_non_root=True,
                                    seccomp_profile=SeccompProfileArgs(
                                        type="RuntimeDefault"
                                    ),
                                ),
                                volume_mounts=[
                                    VolumeMountArgs(
                                        name="haproxy-config",
                                        mount_path="/usr/local/etc/haproxy/haproxy.cfg",
                                        sub_path="haproxy.cfg",
                                    ),
                                    VolumeMountArgs(
                                        name="haproxy-certs",
                                        mount_path="/usr/local/etc/haproxy/certs",
                                        read_only=True,
                                    ),
                                ],
                            ),
                        ],
                        service_account_name=fridge_api_sa.metadata.name,
                        volumes=[
                            VolumeArgs(
                                name="token-vol",
                                projected=ProjectedVolumeSourceArgs(
                                    sources=[
                                        VolumeProjectionArgs(
                                            service_account_token=ServiceAccountTokenProjectionArgs(
                                                expiration_seconds=3600,
                                                path="token",
                                            )
                                        )
                                    ]
                                ),
                            ),
                            VolumeArgs(
                                name="tls-trust-bundle",
                                secret=SecretVolumeSourceArgs(
                                    secret_name="trusted-certificates",
                                ),
                            ),
                            VolumeArgs(
                                name="haproxy-config",
                                config_map=ConfigMapVolumeSourceArgs(
                                    name=haproxy_config.metadata.name
                                ),
                            ),
                            VolumeArgs(
                                name="haproxy-certs",
                                empty_dir=EmptyDirVolumeSourceArgs(),
                            ),
                            VolumeArgs(
                                name="fridge-api-tls",
                                secret=SecretVolumeSourceArgs(
                                    secret_name=fridge_api_tls_cert.spec["secretName"],
                                ),
                            ),
                        ],
                    ),
                ),
            ),
            opts=child_opts,
        )

        # Azure specific annotations for internal load balancer
        api_service_annotations = (
            {
                "service.beta.kubernetes.io/azure-load-balancer-internal": "true",
                "service.beta.kubernetes.io/azure-load-balancer-ipv4": args.config.require(
                    "fridge_api_ip"
                ),
            }
            if args.k8s_environment == K8sEnvironment.AKS
            else {}
        )

        match args.k8s_environment:
            case K8sEnvironment.AKS:
                api_svc_specs = ServiceSpecArgs(
                    type="LoadBalancer",
                    selector=fridge_api_server.spec.template.metadata.labels,
                    ports=[
                        ServicePortArgs(
                            protocol="TCP",
                            port=443,
                            target_port=8443,
                        )
                    ],
                )
            case K8sEnvironment.DAWN | K8sEnvironment.ZENITH:
                api_svc_specs = ServiceSpecArgs(
                    type="NodePort",
                    selector=fridge_api_server.spec.template.metadata.labels,
                    ports=[
                        ServicePortArgs(
                            protocol="TCP",
                            node_port=30180,
                            port=80,
                            target_port=8443,
                        )
                    ],
                )
            case K8sEnvironment.TALOS:
                # A ClusterIP, with a relay on the node's port 30180 (below)
                api_svc_specs = ServiceSpecArgs(
                    type="ClusterIP",
                    selector=fridge_api_server.spec.template.metadata.labels,
                    ports=[
                        ServicePortArgs(
                            protocol="TCP",
                            port=80,
                            target_port=8443,
                        )
                    ],
                )

        self.api_service = Service(
            "fridge-api-service",
            metadata=ObjectMetaArgs(
                name="fridge-api",
                namespace=api_server_ns.metadata.name,
                labels={"app": "fridge-api-server"},
                annotations=api_service_annotations,
            ),
            spec=api_svc_specs,
            opts=child_opts,
        )

        if args.k8s_environment is K8sEnvironment.TALOS:
            self._talos_api_relay(self.api_service, child_opts)

        self.register_outputs(
            {
                "api_server_ns": api_server_ns,
                "argo_workflows_api_role": argo_workflows_api_role,
                "argo_workflows_api_rolebinding": argo_workflows_api_rolebinding,
                "fridge_api_config": fridge_api_config,
                "fridge_api_sa": fridge_api_sa,
                "fridge_api_server": fridge_api_server,
            }
        )

    def _talos_api_relay(self, api_service: Service, opts: ResourceOptions) -> None:
        """
        Relay the node's port 30180 to the FRIDGE API Service.

        The access cluster reaches a Talos node only through its SideroLink tunnel, over IPv6,
        and Talos's firewall admits 30180 only from the tunnel's subnet. In this IPv4 cluster
        neither Cilium nor kube-proxy serves a NodePort on IPv6, so a host-network TCP relay
        listens there instead. TLS passes through to the API pod.
        """
        relay_ns = Namespace(
            "fridge-api-relay-ns",
            metadata=ObjectMetaArgs(
                name="fridge-api-relay",
                # hostNetwork is only allowed by the privileged standard; the container itself
                # is unprivileged
                labels={} | PodSecurityStandard.PRIVILEGED.value,
            ),
            opts=opts,
        )

        relay_config = ConfigMap(
            "fridge-api-relay-config",
            metadata=ObjectMetaArgs(
                name="fridge-api-relay",
                namespace=relay_ns.metadata.name,
            ),
            data={
                "haproxy.cfg": api_service.spec.cluster_ip.apply(
                    lambda cluster_ip: "\n".join(
                        [
                            "global",
                            "  log stdout format raw local0",
                            "defaults",
                            "  mode tcp",
                            "  log global",
                            "  option tcplog",
                            "  timeout connect 5s",
                            "  timeout client 300s",
                            "  timeout server 300s",
                            "frontend fridge_api_in",
                            "  bind :::30180 v4v6",
                            "  default_backend fridge_api",
                            "backend fridge_api",
                            f"  server fridge_api {cluster_ip}:80 check inter 10s",
                            "",
                        ]
                    )
                )
            },
            opts=opts,
        )

        Deployment(
            "fridge-api-relay",
            metadata=ObjectMetaArgs(
                name="fridge-api-relay",
                namespace=relay_ns.metadata.name,
            ),
            spec=DeploymentSpecArgs(
                replicas=1,
                # The new pod can't bind the node's port while the old one holds it
                strategy=DeploymentStrategyArgs(type="Recreate"),
                selector=LabelSelectorArgs(match_labels={"app": "fridge-api-relay"}),
                template=PodTemplateSpecArgs(
                    metadata=ObjectMetaArgs(labels={"app": "fridge-api-relay"}),
                    spec=PodSpecArgs(
                        host_network=True,
                        automount_service_account_token=False,
                        containers=[
                            ContainerArgs(
                                name="haproxy",
                                image=f"haproxy:{SoftwareVersion.HAPROXY.value}",
                                ports=[
                                    ContainerPortArgs(
                                        container_port=30180, protocol="TCP"
                                    )
                                ],
                                security_context=SecurityContextArgs(
                                    allow_privilege_escalation=False,
                                    capabilities=CapabilitiesArgs(drop=["ALL"]),
                                    read_only_root_filesystem=True,
                                    run_as_non_root=True,
                                    run_as_user=99,  # haproxy in the official image
                                    run_as_group=99,
                                    seccomp_profile=SeccompProfileArgs(
                                        type="RuntimeDefault"
                                    ),
                                ),
                                volume_mounts=[
                                    VolumeMountArgs(
                                        name="config",
                                        mount_path="/usr/local/etc/haproxy",
                                        read_only=True,
                                    )
                                ],
                            )
                        ],
                        volumes=[
                            VolumeArgs(
                                name="config",
                                config_map=ConfigMapVolumeSourceArgs(
                                    name=relay_config.metadata.name
                                ),
                            )
                        ],
                    ),
                ),
            ),
            opts=ResourceOptions.merge(
                opts, ResourceOptions(depends_on=[relay_config, api_service])
            ),
        )
