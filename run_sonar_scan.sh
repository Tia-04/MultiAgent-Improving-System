#!/bin/bash

set -euo pipefail

REPO_PATH="${1:-/app/cloned_repo/commons-csv}"
PROJECT_KEY="${2:?project key is required}"
SONAR_TOKEN="${3:?sonar token is required}"
SONAR_HOST_URL="${4:-http://sonarqube:9000}"
SONAR_SOURCES="${5:-src/main/java}"
SONAR_BINARIES="${6:-target/classes}"
SONAR_TESTS="${7:-src/test/java}"
SONAR_TEST_BINARIES="${8:-target/test-classes}"
SONAR_INCLUSIONS="${9:-src/main/java/**/*.java}"
SONAR_EXCLUSIONS="${10:-}"

cd "${REPO_PATH}"

SCANNER_ARGS=(
  "-Dsonar.projectKey=${PROJECT_KEY}"
  "-Dsonar.projectName=${PROJECT_KEY}"
  "-Dsonar.projectBaseDir=${REPO_PATH}"
  "-Dsonar.host.url=${SONAR_HOST_URL}"
  "-Dsonar.login=${SONAR_TOKEN}"
  "-Dsonar.sourceEncoding=UTF-8"
  "-Dsonar.scm.disabled=true"
  "-Dsonar.sources=${SONAR_SOURCES}"
  "-Dsonar.tests=${SONAR_TESTS}"
  "-Dsonar.java.binaries=${SONAR_BINARIES}"
  "-Dsonar.java.test.binaries=${SONAR_TEST_BINARIES}"
  "-Dsonar.inclusions=${SONAR_INCLUSIONS}"
)

if [ -n "${SONAR_EXCLUSIONS}" ]; then
  SCANNER_ARGS+=("-Dsonar.exclusions=${SONAR_EXCLUSIONS}")
fi

exec sonar-scanner "${SCANNER_ARGS[@]}"
