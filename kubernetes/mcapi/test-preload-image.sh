#!/usr/bin/env bash
set -euo pipefail

root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
temporary=$(mktemp -d)
trap 'rm -rf "${temporary}"' EXIT

mock_bin=${temporary}/bin
evidence_dir=${temporary}/evidence
mkdir -p "${mock_bin}"

cat >"${mock_bin}/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "${1-} ${2-}" in
  "image inspect")
    if [[ " $* " == *" --format "* ]]; then
      printf '%s\n' 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    else
      printf '%s\n' '[{"Id":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]'
    fi
    ;;
  "tag local/test:one")
    ;;
  "save local/test:one")
    printf '%s' image-stream
    ;;
  *)
    printf 'unexpected docker invocation: %q\n' "$*" >&2
    exit 1
    ;;
esac
EOF

cat >"${mock_bin}/ssh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${SSH_LOG:?}"
if [[ " $* " == *" crictl inspecti "* ]]; then
  [[ ${1-} == -n ]] || {
    echo 'verification ssh must detach from caller stdin' >&2
    exit 1
  }
  printf '%s\n' '{"status":{"id":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}'
else
  test "$(cat)" = image-stream
fi
EOF

chmod +x "${mock_bin}/docker" "${mock_bin}/ssh"
export SSH_LOG=${temporary}/ssh.log

PATH="${mock_bin}:${PATH}" PRELOAD_COMPRESSION=none \
  "${root_dir}/kubernetes/mcapi/preload-image.sh" \
  local/test:one local/test:one "${evidence_dir}" guest@example \
  -i /tmp/test-key -o IdentitiesOnly=yes

grep -q '^-n .*crictl inspecti' "${SSH_LOG}"
test "$(. "${evidence_dir}/image.env"; printf '%s' "${TARGET_IMAGE_ID}")" = \
  sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa

echo 'PASS: preload verification does not consume caller stdin'
