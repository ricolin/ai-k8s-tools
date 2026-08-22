#!/usr/bin/env bash
set -euo pipefail

if [[ $# -eq 0 ]]; then
  echo "usage: $0 MANIFEST [MANIFEST ...]" >&2
  exit 2
fi

status=0
for manifest in "$@"; do
  if [[ ! -s ${manifest} ]]; then
    echo "manifest is missing or empty: ${manifest}" >&2
    status=1
    continue
  fi

  while IFS= read -r finding; do
    echo "${manifest}:${finding}: container image is empty" >&2
    status=1
  done < <(
    grep -nE \
      "^[[:space:]]*(-[[:space:]]*)?image:[[:space:]]*(\"\"|'')[[:space:]]*(#.*)?$|^[[:space:]]*(-[[:space:]]*)?image:[[:space:]]*(#.*)?$" \
      "${manifest}" || true
  )
done

exit "${status}"
