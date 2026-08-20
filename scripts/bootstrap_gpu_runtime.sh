#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

container_runtime=${CONTAINER_RUNTIME:-docker}
case "$container_runtime" in
  docker|containerd) ;;
  *)
    echo "unsupported CONTAINER_RUNTIME: $container_runtime" >&2
    exit 1
    ;;
esac
if [[ "$container_runtime" == containerd ]]; then
  command -v containerd >/dev/null 2>&1 || {
    echo "containerd is not installed" >&2
    exit 1
  }
fi
install -d -m 0755 /var/lib/ai-build-tools
target_kernel=$(basename "$(readlink -f /boot/vmlinuz)")
target_kernel=${target_kernel#vmlinuz-}
[[ -n "$target_kernel" ]] || {
  echo "cannot derive target kernel" >&2
  exit 1
}

apt_proxy=()
curl_proxy=()
apt_proxy_conf=
cleanup() {
  [[ -z "$apt_proxy_conf" ]] || rm -f "$apt_proxy_conf"
}
trap cleanup EXIT
if [[ -n "${EGRESS_PROXY:-}" ]]; then
  PROXY_URL="${EGRESS_PROXY}"
  [[ "$PROXY_URL" =~ ^socks5h://127\.0\.0\.1:[0-9]{2,5}$ ]] || {
    echo "unsupported EGRESS_PROXY: $PROXY_URL" >&2
    exit 1
  }
  apt_proxy=(
    -o "Acquire::http::Proxy=$PROXY_URL"
    -o "Acquire::https::Proxy=$PROXY_URL"
  )
  curl_proxy=(--proxy "$PROXY_URL")
  apt_proxy_conf=/etc/apt/apt.conf.d/99-temporary-egress-proxy
  cat >"$apt_proxy_conf" <<EOF
Acquire::http::Proxy "$PROXY_URL";
Acquire::https::Proxy "$PROXY_URL";
EOF
fi

apt-get "${apt_proxy[@]}" update
base_packages=(
  ca-certificates
  curl
  gnupg2
  "linux-headers-$target_kernel"
  "linux-modules-extra-$target_kernel"
  ubuntu-drivers-common
)
if [[ "$container_runtime" == docker ]]; then
  base_packages+=(docker.io)
fi
apt-get "${apt_proxy[@]}" install -y --no-install-recommends \
  "${base_packages[@]}"

# Use Ubuntu's hardware-aware, Secure-Boot-aware server driver selection only
# for the first installation. Re-running the selector on this host changed its
# recommendation from 595 to 580, so preserve an already-installed server-open
# branch rather than oscillating between valid branches.
driver_pkg=$(dpkg-query -W -f='${binary:Package}\n' \
  'nvidia-headless-no-dkms-*-server-open' 2>/dev/null | head -1 || true)
if [[ -z "$driver_pkg" ]]; then
  ubuntu-drivers install --gpgpu
  driver_pkg=$(dpkg-query -W -f='${binary:Package}\n' \
    'nvidia-headless-no-dkms-*-server-open' 2>/dev/null | head -1 || true)
  driver_selection=ubuntu-drivers-install-gpgpu
else
  printf 'Preserving installed NVIDIA driver package: %s\n' "$driver_pkg"
  driver_selection=preserve-installed-server-open
fi

# ubuntu-drivers --gpgpu selects the headless compute stack but does not
# necessarily install nvidia-smi. A kernel upgrade can also omit the "extra"
# module set that carries mlx5_ib, breaking this host's IPoIB on the next boot.
[[ "$driver_pkg" =~ ^nvidia-headless-no-dkms-([0-9]+)-server-open$ ]] || {
  echo "cannot derive NVIDIA server branch from: $driver_pkg" >&2
  exit 1
}
driver_branch=${BASH_REMATCH[1]}
apt-get "${apt_proxy[@]}" install -y --no-install-recommends \
  "nvidia-utils-$driver_branch-server" \
  "nvidia-fabricmanager-$driver_branch" \
  "linux-modules-extra-$target_kernel"
modinfo -k "$target_kernel" nvidia >/dev/null 2>&1 || {
  echo "NVIDIA kernel module is unavailable for $target_kernel" >&2
  exit 1
}
systemctl enable nvidia-fabricmanager
if nvidia-smi >/dev/null 2>&1; then
  systemctl start nvidia-fabricmanager
else
  printf '%s\n' \
    'NVIDIA modules are not active yet; Fabric Manager will start after reboot.'
fi

curl "${curl_proxy[@]}" -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | gpg --dearmor --yes \
      -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl "${curl_proxy[@]}" -fsSL \
  https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list

apt-get "${apt_proxy[@]}" update
toolkit_version=1.19.1-1
apt-get "${apt_proxy[@]}" install -y \
  "nvidia-container-toolkit=$toolkit_version" \
  "nvidia-container-toolkit-base=$toolkit_version" \
  "libnvidia-container-tools=$toolkit_version" \
  "libnvidia-container1=$toolkit_version"

if [[ "$container_runtime" == docker ]]; then
  nvidia-ctk runtime configure --runtime=docker
  systemctl enable docker
  usermod -aG docker ubuntu
else
  nvidia-ctk runtime configure --runtime=containerd --set-as-default
fi

kubelet_memory_manager_checkpoint_present=false
if [[ -f /var/lib/kubelet/memory_manager_state ]]; then
  kubelet_memory_manager_checkpoint_present=true
  cat >&2 <<'EOF'
WARNING: kubelet uses a Memory Manager checkpoint. Loading the GPU driver can
change the NUMA memory map. Drain the node, stop kubelet, preserve and remove
/var/lib/kubelet/memory_manager_state, then reboot. Do not delete this state
from a node that has not been drained.
EOF
fi

cat > /var/lib/ai-build-tools/runtime-install.json <<EOF
{"container_runtime":"$container_runtime","toolkit_version":"$toolkit_version","driver_selection":"$driver_selection","driver_branch":"$driver_branch","target_kernel":"$target_kernel","temporary_egress_proxy":$([[ -n "${EGRESS_PROXY:-}" ]] && echo true || echo false),"kubelet_memory_manager_checkpoint_present":$kubelet_memory_manager_checkpoint_present,"reboot_required":true}
EOF
