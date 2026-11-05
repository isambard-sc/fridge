import pulumi
from pulumi import ComponentResource, ResourceOptions
from pulumi_kubernetes.core.v1 import Namespace
from pulumi_kubernetes.helm.v3 import Release, ReleaseArgs
from pulumi_kubernetes.helm.v4 import RepositoryOptsArgs
from pulumi_kubernetes.meta.v1 import ObjectMetaArgs
from pulumi_kubernetes.node.v1 import RuntimeClass
from pulumi_kubernetes.yaml import ConfigFile

from enums import K8sEnvironment, PodSecurityStandard, SoftwareVersion


class GPUOperatorArgs:
    def __init__(
        self,
        config: pulumi.config.Config,
        k8s_environment: K8sEnvironment,
    ) -> None:
        self.config = config
        self.k8s_environment = k8s_environment


class GPUOperator(ComponentResource):
    def __init__(
        self, name: str, args: GPUOperatorArgs, opts: ResourceOptions | None = None
    ) -> None:
        super().__init__("fridge:k8s:GPUOperator", name, {}, opts)
        child_opts = ResourceOptions.merge(opts, ResourceOptions(parent=self))

        self.node_feature_discovery_ns = Namespace(
            "node-feature-discovery-ns",
            metadata=ObjectMetaArgs(
                name="node-feature-discovery",
                # Talos enforces the baseline standard by default, which forbids the worker's
                # hostPath mounts
                labels=(
                    PodSecurityStandard.PRIVILEGED.value
                    if args.k8s_environment is K8sEnvironment.TALOS
                    else None
                ),
            ),
            opts=child_opts,
        )

        self.node_feature_discovery = Release(
            "node-feature-discovery",
            ReleaseArgs(
                name="node-feature-discovery",
                namespace=self.node_feature_discovery_ns.metadata.name,
                chart="node-feature-discovery",
                version=SoftwareVersion.NODE_FEATURE_DISCOVERY.value,
                repository_opts=RepositoryOptsArgs(
                    repo="https://kubernetes-sigs.github.io/node-feature-discovery/charts",
                ),
            ),
            opts=child_opts,
        )

        match args.k8s_environment:
            case K8sEnvironment.DAWN:
                self.intel_node_feature_rules = ConfigFile(
                    "intel-node-feature-rules",
                    file="https://raw.githubusercontent.com/intel/intel-device-plugins-for-kubernetes/main/deployments/nfd/overlays/node-feature-rules/node-feature-rules.yaml",
                    opts=ResourceOptions.merge(
                        child_opts,
                        ResourceOptions(depends_on=self.node_feature_discovery),
                    ),
                )

                self.gpu_operator_ns = Namespace(
                    "gpu-operator-ns",
                    metadata=ObjectMetaArgs(
                        name="intel-device-plugins",
                    ),
                    opts=child_opts,
                )

                # Deploy the Intel GPU Operator using the official Helm chart
                self.gpu_operator = Release(
                    "intel-gpu-operator",
                    ReleaseArgs(
                        name="intel-gpu-operator",
                        namespace=self.gpu_operator_ns.metadata.name,
                        chart="intel-device-plugins-operator",
                        version=SoftwareVersion.INTEL_GPU_OPERATOR.value,
                        repository_opts=RepositoryOptsArgs(
                            repo="https://intel.github.io/helm-charts/",
                        ),
                        values={
                            "tolerations": [
                                {
                                    "key": "gpu.intel.com/i915",
                                    "operator": "Exists",
                                    "effect": "NoSchedule",
                                }
                            ]
                        },
                    ),
                    opts=ResourceOptions.merge(
                        child_opts,
                        ResourceOptions(depends_on=self.node_feature_discovery),
                    ),
                )

                self.gpu_device = ConfigFile(
                    "gpu-device",
                    file="./k8s/gpu_operator/intel-gpu-device.yaml",
                    opts=ResourceOptions.merge(
                        child_opts, ResourceOptions(depends_on=self.gpu_operator)
                    ),
                )

            case K8sEnvironment.ZENITH:
                self.gpu_operator_ns = Namespace(
                    "gpu-operator-ns",
                    metadata=ObjectMetaArgs(
                        name="kube-amd-gpu",
                    ),
                    opts=child_opts,
                )

                # Deploy the AMD GPU Operator using the official Helm chart
                self.gpu_operator = Release(
                    "amd-gpu-operator",
                    ReleaseArgs(
                        name="amd-gpu-operator",
                        namespace=self.gpu_operator_ns.metadata.name,
                        chart="gpu-operator-charts",
                        version=SoftwareVersion.AMD_GPU_OPERATOR.value,
                        repository_opts=RepositoryOptsArgs(
                            repo="https://rocm.github.io/gpu-operator",
                        ),
                        values={
                            # do not install the node-feature-discovery chart, since we already installed it above
                            "node-feature-discovery": {
                                "enabled": False,
                            },
                        },
                    ),
                    opts=ResourceOptions.merge(
                        child_opts,
                        ResourceOptions(depends_on=self.node_feature_discovery),
                    ),
                )

            case K8sEnvironment.TALOS:
                # The driver and container toolkit are Talos system extensions, so only NVIDIA's
                # device plugin is needed here, not the GPU operator. Its containers run under the
                # toolkit's `nvidia` runtime, which the extension registers with containerd
                self.runtime_class = RuntimeClass(
                    "nvidia-runtime-class",
                    metadata=ObjectMetaArgs(name="nvidia"),
                    handler="nvidia",
                    opts=child_opts,
                )

                self.gpu_operator_ns = Namespace(
                    "gpu-operator-ns",
                    metadata=ObjectMetaArgs(
                        name="nvidia-device-plugin",
                        # The plugin mounts the kubelet's device plugin directory from the host
                        labels={} | PodSecurityStandard.PRIVILEGED.value,
                    ),
                    opts=child_opts,
                )

                self.gpu_operator = Release(
                    "nvidia-device-plugin",
                    ReleaseArgs(
                        name="nvidia-device-plugin",
                        namespace=self.gpu_operator_ns.metadata.name,
                        chart="nvidia-device-plugin",
                        version=SoftwareVersion.NVIDIA_DEVICE_PLUGIN.value,
                        repository_opts=RepositoryOptsArgs(
                            repo="https://nvidia.github.io/k8s-device-plugin",
                        ),
                        values={
                            "runtimeClassName": "nvidia",
                            # node-feature-discovery is installed above
                            "nfd": {"enabled": False},
                            "gfd": {"enabled": False},
                            # NFD's default configuration labels PCI devices by class and vendor,
                            # so a GPU (class 0302, vendor 10de) is pci-0302_10de, not the
                            # chart's pci-10de
                            "affinity": {
                                "nodeAffinity": {
                                    "requiredDuringSchedulingIgnoredDuringExecution": {
                                        "nodeSelectorTerms": [
                                            {
                                                "matchExpressions": [
                                                    {
                                                        "key": f"feature.node.kubernetes.io/{label}.present",
                                                        "operator": "In",
                                                        "values": ["true"],
                                                    }
                                                ]
                                            }
                                            for label in ("pci-0302_10de", "pci-10de")
                                        ]
                                    }
                                }
                            },
                        },
                    ),
                    opts=ResourceOptions.merge(
                        child_opts,
                        ResourceOptions(
                            depends_on=[
                                self.node_feature_discovery,
                                self.runtime_class,
                            ]
                        ),
                    ),
                )
