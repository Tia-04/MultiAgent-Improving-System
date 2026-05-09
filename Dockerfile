FROM maven:3.9.9-eclipse-temurin-17

WORKDIR /app

# Runtime helpers used by the repo-based experiment scripts.
RUN apt-get update && apt-get install -y --no-install-recommends git unzip wget && \
    rm -rf /var/lib/apt/lists/*

# JUnit 5 standalone runner
RUN wget -q -O /app/junit.jar \
    https://repo1.maven.org/maven2/org/junit/platform/junit-platform-console-standalone/1.10.0/junit-platform-console-standalone-1.10.0.jar

# sonar-scanner CLI
RUN wget -q -O /tmp/sonar-scanner.zip \
    https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-6.2.1.4610-linux-x64.zip \
    && unzip -q /tmp/sonar-scanner.zip -d /opt \
    && mv /opt/sonar-scanner-6.2.1.4610-linux-x64 /opt/sonar-scanner \
    && rm /tmp/sonar-scanner.zip

ENV PATH="/opt/sonar-scanner/bin:${PATH}"

COPY run_tests.sh /app/run_tests.sh
COPY run_repo_tests.sh /app/run_repo_tests.sh
COPY run_sonar_scan.sh /app/run_sonar_scan.sh
RUN chmod +x /app/run_tests.sh /app/run_repo_tests.sh /app/run_sonar_scan.sh

CMD ["sleep", "infinity"]
