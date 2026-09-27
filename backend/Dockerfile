# syntax=docker/dockerfile:1
# Контекст сборки — корень репозитория (см. docker-compose.yml).

# 1. Пайплайн данных: артефакты ML-модели + справочники + внешние данные → service_data/
FROM python:3.13-slim AS data
WORKDIR /work
COPY pipeline/requirements.txt pipeline/requirements.txt
RUN pip install --no-cache-dir -r pipeline/requirements.txt
COPY Tramvay/ml/outputs Tramvay/ml/outputs
COPY Tramvay/ml/artifacts/raw_route_hour.parquet Tramvay/ml/artifacts/raw_route_hour.parquet
COPY Tramvay/ml/external Tramvay/ml/external
COPY Tramvay/ml/regimes.json Tramvay/ml/regimes.json
COPY data/reference data/reference
COPY pipeline pipeline
RUN python pipeline/build_service_data.py --ml Tramvay --ref data/reference --out /service_data

# 2. Сборка backend (Java 21, Spring Boot 4.1 WebFlux)
FROM maven:3.9-eclipse-temurin-21 AS build
WORKDIR /src
COPY backend/pom.xml .
RUN mvn -B -q dependency:go-offline
COPY backend/src src
COPY --from=data /service_data /service_data
RUN mvn -B -q package -DskipTests

# 3. Runtime
FROM eclipse-temurin:21-jre-alpine
RUN addgroup -S app && adduser -S app -G app
WORKDIR /app
COPY --from=build /src/target/tram-forecast.jar app.jar
COPY --from=data /service_data /app/service_data
USER app
ENV APP_DATA_DIR=/app/service_data \
    JAVA_TOOL_OPTIONS="-XX:MaxRAMPercentage=60 -XX:+UseG1GC -XX:+ExitOnOutOfMemoryError"
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=5 \
  CMD wget -qO- http://127.0.0.1:8080/actuator/health/readiness | grep -q UP || exit 1
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
