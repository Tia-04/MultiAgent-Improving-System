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
SONAR_EXCLUSIONS="${10:-**/*.js,**/*.ts,**/*.py,**/*.xml,**/*.html,**/*.css,**/*.json,**/*.yaml,**/*.yml}"

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
  "-Dsonar.java.binaries=${SONAR_BINARIES}"
  "-Dsonar.inclusions=${SONAR_INCLUSIONS}"
)

if [ -n "${SONAR_EXCLUSIONS}" ]; then
  SCANNER_ARGS+=("-Dsonar.exclusions=${SONAR_EXCLUSIONS}")
fi
#NOTE: 
#RULE S1113 is removing @Deprecated annotations, 
#which is unnecessary as it would break retrocompatibility and is not relevant to the task of improving code quality. We will ignore this rule for all Java files.
#SCANNER_ARGS+=(
#  "-Dsonar.issue.ignore.multicriteria=e1"
#  "-Dsonar.issue.ignore.multicriteria.e1.ruleKey=java:S1133"
#  "-Dsonar.issue.ignore.multicriteria.e1.resourceKey=**/*.java"
#)

exec sonar-scanner "${SCANNER_ARGS[@]}"
