#!/usr/bin/env bash
set -Eeuo pipefail

K8S_MINOR="v1.35"
POD_CIDR="192.168.0.0/16"
CALICO_VERSION="v3.31.4"
AGENT_SANDBOX_VERSION="v0.2.1"
ROUTER_LOCAL_IMAGE="sandbox-router:local"
NETWORK_SANDBOX_LOCAL_IMAGE="python-network-sandbox:local"
WORKSPACE_SANDBOX_LOCAL_IMAGE="debian-workspace-sandbox:local"
WORKDIR="/root/agent-sandbox"
SDK_VENV="/root/agent-sdk-venv"

log() {
  echo
  echo "==> $*"
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo bash $0" >&2
    exit 1
  fi
}

wait_for_node_ready() {
  log "Waiting for node to become Ready"
  local node
  node="$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')"
  for _ in $(seq 1 90); do
    if kubectl get node "$node" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' | grep -q True; then
      echo "Node is Ready"
      return 0
    fi
    sleep 5
  done
  echo "Node did not become Ready in time" >&2
  kubectl get nodes -o wide || true
  kubectl get pods -A || true
  exit 1
}

wait_for_runtimeclass() {
  log "Waiting for RuntimeClass kata-qemu"
  for _ in $(seq 1 90); do
    if kubectl get runtimeclass kata-qemu >/dev/null 2>&1; then
      echo "RuntimeClass kata-qemu is present"
      return 0
    fi
    sleep 5
  done
  echo "kata-qemu RuntimeClass not found in time" >&2
  kubectl get runtimeclass || true
  kubectl get pods -A || true
  exit 1
}

wait_for_deployment_ready() {
  local ns="$1"
  local name="$2"
  log "Waiting for deployment ${name} in namespace ${ns}"
  kubectl rollout status "deployment/${name}" -n "${ns}" --timeout=600s
}

wait_for_pods_ready_selector() {
  local ns="$1"
  local selector="$2"
  log "Waiting for pods in namespace=${ns} selector=${selector}"
  kubectl wait --namespace "$ns" \
    --for=condition=Ready pod \
    --selector="$selector" \
    --timeout=600s
}

require_root

log "Checking OS"
. /etc/os-release
if [ "${ID:-}" != "ubuntu" ]; then
  echo "This script expects Ubuntu. Detected: ${ID:-unknown}" >&2
  exit 1
fi

log "Checking KVM availability for Kata"
if [ ! -e /dev/kvm ]; then
  echo "WARNING: /dev/kvm is missing. Kata typically requires nested virtualization or bare metal."
  echo "Install will continue, but Kata workloads may fail later."
fi

log "Installing base packages"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  apt-transport-https \
  ca-certificates \
  curl \
  gpg \
  jq \
  git \
  docker.io \
  python3-venv \
  python3-pip \
  socat

log "Installing containerd"
DEBIAN_FRONTEND=noninteractive apt-get install -y containerd

log "Configuring kernel modules and sysctls"
modprobe br_netfilter || true
modprobe overlay || true

cat >/etc/modules-load.d/k8s.conf <<'EOF'
overlay
br_netfilter
EOF

cat >/etc/sysctl.d/99-kubernetes-cri.conf <<'EOF'
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward = 1
EOF

sysctl --system

log "Configuring containerd"
mkdir -p /etc/containerd
containerd config default >/etc/containerd/config.toml
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
sed -i '/disabled_plugins/s/"cri",\?//g' /etc/containerd/config.toml
sed -i '/disabled_plugins = \[\]/d' /etc/containerd/config.toml || true

log "Disabling swap"
swapoff -a || true
cp /etc/fstab /etc/fstab.bak.$(date +%s)
sed -ri '/\sswap\s/s/^#?/#/' /etc/fstab

systemctl daemon-reload
systemctl enable --now containerd
systemctl enable --now docker

log "Installing Kubernetes apt repo ${K8S_MINOR}"
mkdir -p -m 755 /etc/apt/keyrings
curl -fsSL "https://pkgs.k8s.io/core:/stable:/${K8S_MINOR}/deb/Release.key" \
  | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg

echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/${K8S_MINOR}/deb/ /" \
  >/etc/apt/sources.list.d/kubernetes.list

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y kubelet kubeadm kubectl
apt-mark hold kubelet kubeadm kubectl
systemctl enable --now kubelet

log "Restarting services before kubeadm"
systemctl restart containerd
systemctl restart kubelet

log "Resetting any prior kubeadm state"
kubeadm reset -f || true
rm -rf /etc/cni/net.d || true
systemctl restart containerd

log "Initializing single-node Kubernetes cluster"
kubeadm init --pod-network-cidr="${POD_CIDR}"

mkdir -p /root/.kube
cp -f /etc/kubernetes/admin.conf /root/.kube/config
chmod 600 /root/.kube/config
export KUBECONFIG=/etc/kubernetes/admin.conf

log "Installing Calico ${CALICO_VERSION}"
kubectl create -f "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/operator-crds.yaml"
kubectl create -f "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/tigera-operator.yaml"
curl -fsSL "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/custom-resources.yaml" \
  | sed "s#192.168.0.0/16#${POD_CIDR}#g" \
  | kubectl create -f -

log "Allowing workloads on the single control-plane node"
kubectl taint nodes --all node-role.kubernetes.io/control-plane- || true

wait_for_node_ready

log "Installing Helm"
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-4 | bash

log "Installing Kata"
KATA_VERSION="$(curl -sSL https://api.github.com/repos/kata-containers/kata-containers/releases/latest | jq -r .tag_name)"
CHART="oci://ghcr.io/kata-containers/kata-deploy-charts/kata-deploy"
helm install kata-deploy "${CHART}" --version "${KATA_VERSION}"

wait_for_runtimeclass

log "Installing Agent Sandbox ${AGENT_SANDBOX_VERSION}"
kubectl apply -f "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VERSION}/manifest.yaml"
kubectl apply -f "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VERSION}/extensions.yaml"

wait_for_deployment_ready "agent-sandbox-system" "agent-sandbox-controller"

log "Cloning agent-sandbox repo"
rm -rf "${WORKDIR}"
git clone https://github.com/kubernetes-sigs/agent-sandbox.git "${WORKDIR}"

log "Building sandbox-router locally"
cd "${WORKDIR}/clients/python/agentic-sandbox-client/sandbox-router"
docker build -t "${ROUTER_LOCAL_IMAGE}" .
docker save -o /tmp/sandbox-router-local.tar "${ROUTER_LOCAL_IMAGE}"
log "Importing sandbox-router image into containerd"
for i in $(seq 1 5); do # ugly fix
  if ctr -n k8s.io images import --local /tmp/sandbox-router-local.tar; then
    echo "Import succeeded"
    break
  fi
  echo "Import attempt $i failed, retrying..."
  sleep 3
done

ctr -n k8s.io images ls | grep sandbox-router || true

log "Deploying sandbox-router from local image"
kubectl delete -f sandbox_router.yaml --ignore-not-found || true
sed \
  -e "s|IMAGE_PLACEHOLDER|${ROUTER_LOCAL_IMAGE}|g" \
  -e '/image: sandbox-router:local/a\        imagePullPolicy: Never' \
  sandbox_router.yaml | kubectl apply -f -

wait_for_pods_ready_selector "default" "app=sandbox-router"

log "Applying upstream python-sandbox-template"
cd "${WORKDIR}"
TEMPLATE_FILE="$(find . -name 'python-sandbox-template.yaml' | head -n1)"
if [ -z "${TEMPLATE_FILE}" ]; then
  echo "Could not find python-sandbox-template.yaml in cloned repo" >&2
  exit 1
fi
kubectl apply -f "${TEMPLATE_FILE}"

log "Patching template to use kata-qemu"
kubectl patch sandboxtemplate python-sandbox-template -n default --type merge -p '
spec:
  podTemplate:
    spec:
      runtimeClassName: kata-qemu
'

log "Installing Agent Sandbox Python SDK"
python3 -m venv "${SDK_VENV}"
. "${SDK_VENV}/bin/activate"
pip install --upgrade pip
pip install k8s-agent-sandbox

log "Writing hello-world SDK test"
cat >/root/sdk_hello.py <<'PY'
from k8s_agent_sandbox import SandboxClient

print("creating client")
with SandboxClient(
    template_name="python-sandbox-template",
    namespace="default",
) as sandbox:
    print("sandbox ready")
    result = sandbox.run("echo hello world from sdk")
    print("stdout:")
    print(result.stdout)
print("done")
PY

log "Running hello-world"
python -u /root/sdk_hello.py

log "Building network sandbox locally"
cd "$(pdw)"
cd "../sandbox/network"
docker build -t "${NETWORK_SANDBOX_LOCAL_IMAGE}" .
docker save -o /tmp/network-sandbox-local.tar "${NETWORK_SANDBOX_LOCAL_IMAGE}"
log "Importing sandbox-router image into containerd"
for i in $(seq 1 5); do # ugly fix
  if ctr -n k8s.io images import --local /tmp/network-sandbox-local.tar ; then
    echo "Import succeeded"
    break
  fi
  echo "Import attempt $i failed, retrying..."
  sleep 3
done

log "Building workspace sandbox locally"
cd "$(pdw)"
cd "../sandbox/workspace"
docker build -t "${WORKSPACE_SANDBOX_LOCAL_IMAGE}" .
docker save -o /tmp/workspace-sandbox-local.tar "${WORKSPACE_SANDBOX_LOCAL_IMAGE}"
log "Importing sandbox-router image into containerd"
for i in $(seq 1 5); do # ugly fix
  if ctr -n k8s.io images import --local /tmp/workspace-sandbox-local.tar ; then
    echo "Import succeeded"
    break
  fi
  echo "Import attempt $i failed, retrying..."
  sleep 3
done

cat <<'EOF'

Install complete.

Checks:
  export KUBECONFIG=/etc/kubernetes/admin.conf
  kubectl get nodes -o wide
  kubectl get runtimeclass
  kubectl get pods -A
  kubectl get sandbox -A
  kubectl get sandboxclaim -A

Re-run hello world:
  . /root/agent-sdk-venv/bin/activate
  python -u /root/sdk_hello.py
EOF