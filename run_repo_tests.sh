#!/bin/bash

set -euo pipefail

REPO_PATH="${1:-/app/cloned_repo/commons-csv}"
shift || true

cd "${REPO_PATH}"

exec mvn -B -ntp test "$@"
