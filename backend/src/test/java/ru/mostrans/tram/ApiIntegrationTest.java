package ru.mostrans.tram;

import static org.assertj.core.api.Assertions.assertThat;

import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webtestclient.autoconfigure.AutoConfigureWebTestClient;
import org.springframework.http.MediaType;
import org.springframework.test.web.reactive.server.WebTestClient;

/** Интеграционные тесты API на реальных данных service_data (запуск из каталога backend/). */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@AutoConfigureWebTestClient
class ApiIntegrationTest {

    /** Сумма почасового прогноза 1.11–31.12.2025 = сабмит с WAPE-score 0.91250. */
    static final double SUBMISSION_TOTAL = 12_818_544.0;

    @Autowired
    WebTestClient web;

    @org.springframework.boot.test.web.server.LocalServerPort
    int port;

    @SuppressWarnings("unchecked")
    Map<String, Object> get(String uri) {
        return web.mutate().codecs(c -> c.defaultCodecs().maxInMemorySize(16 * 1024 * 1024)).build()
                .get().uri(uri).exchange().expectStatus().isOk()
                .expectBody(Map.class).returnResult().getResponseBody();
    }

    @SuppressWarnings("unchecked")
    static Map<String, Object> totals(Map<String, Object> r) {
        return (Map<String, Object>) r.get("totals");
    }

    @Test
    void metaHasAllRoutesAndHorizons() {
        Map<String, Object> m = get("/api/v1/meta");
        assertThat((List<?>) m.get("routes")).hasSize(10);
        assertThat((List<?>) m.get("horizons")).hasSize(4);
    }

    @Test
    void dayHorizonTotalEqualsSubmission() {
        Map<String, Object> r = get("/api/v1/forecast?granularity=total&split=none");
        assertThat(((Number) totals(r).get("pred")).doubleValue()).isEqualTo(SUBMISSION_TOTAL);
    }

    @Test
    void granularitiesAreConsistent() {
        double hourly = ((Number) totals(get("/api/v1/forecast?routes=17&granularity=hour&from=2025-12-01&to=2025-12-07"))
                .get("pred")).doubleValue();
        double daily = ((Number) totals(get("/api/v1/forecast?routes=17&granularity=day&from=2025-12-01&to=2025-12-07"))
                .get("pred")).doubleValue();
        assertThat(hourly).isEqualTo(daily).isPositive();
    }

    @Test
    void allStopsOfRouteSumToRouteTotal() {
        double route = ((Number) totals(get("/api/v1/forecast?routes=1&granularity=total")).get("pred")).doubleValue();
        double seg0 = ((Number) totals(get("/api/v1/forecast?segment=1:0:1-999&granularity=total")).get("pred"))
                .doubleValue();
        double seg1 = ((Number) totals(get("/api/v1/forecast?segment=1:1:1-999&granularity=total")).get("pred"))
                .doubleValue();
        assertThat(seg0 + seg1).isCloseTo(route, org.assertj.core.data.Offset.offset(1.0));
    }

    @Test
    void scenarioChangesForecastAndKeepsBase() {
        Map<String, Object> r = get("/api/v1/forecast?routes=7&granularity=total&from=2025-12-20&to=2025-12-20"
                + "&event=7:2025-12-20:2025-12-20:0.5");
        Map<String, Object> t = totals(r);
        assertThat(((Number) t.get("pred")).doubleValue())
                .isCloseTo(((Number) t.get("base")).doubleValue() * 0.5, org.assertj.core.data.Offset.offset(0.2));
    }

    @Test
    void weatherFactorMatchesPythonFormula() {
        // exp(β·(ln(1+мм) − норма месяца)) — сверено с ml/scenario.py: маршрут 17, декабрь, 5.12 = 15 мм, ×0.95
        Map<String, Object> r = get("/api/v1/forecast?granularity=total&routes=7,17&from=2025-12-01&to=2025-12-31"
                + "&precip=2025-12-05:15&monthMult=12:1.03&routeMult=17:0.95"
                + "&event=7:2025-12-20:2025-12-21:0.5:10-18");
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> byRoute = (List<Map<String, Object>>) r.get("byRoute");
        assertThat(((Number) byRoute.get(0).get("pred")).doubleValue()).isCloseTo(713080.5,
                org.assertj.core.data.Offset.offset(1.0));
        assertThat(((Number) byRoute.get(1).get("pred")).doubleValue()).isCloseTo(1506328.5,
                org.assertj.core.data.Offset.offset(1.0));
    }

    @Test
    void yearAndHistoryHorizonsWork() {
        assertThat(((Number) totals(get("/api/v1/forecast?horizon=year&granularity=total&split=none")).get("pred"))
                .doubleValue()).isGreaterThan(70_000_000);
        assertThat(((Number) totals(get("/api/v1/forecast?horizon=history&granularity=total&split=none")).get("pred"))
                .doubleValue()).isEqualTo(59_667_191.0);
    }

    @Test
    void badParametersGiveProblemJson() {
        web.get().uri("/api/v1/forecast?from=2025-10-01").exchange()
                .expectStatus().isBadRequest()
                .expectHeader().contentTypeCompatibleWith(MediaType.APPLICATION_PROBLEM_JSON)
                .expectBody().jsonPath("$.parameter").isEqualTo("from")
                .jsonPath("$.detail").value(d -> assertThat(d.toString()).contains("вне горизонта"));
        web.get().uri("/api/v1/forecast?routes=99").exchange().expectStatus().isBadRequest();
        web.get().uri("/api/v1/routes/99/stops").exchange().expectStatus().isNotFound();
        web.get().uri("/api/v1/map").exchange().expectStatus().isBadRequest();
        web.get().uri("/api/v1/export?horizon=year&granularity=hour&level=stop").exchange()
                .expectStatus().isEqualTo(413);
    }

    @Test
    void mapReturns24HoursPerStop() {
        Map<String, Object> m = get("/api/v1/map?date=2025-11-10&routes=1");
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> stops = (List<Map<String, Object>>) m.get("stops");
        assertThat(stops).isNotEmpty();
        assertThat((List<?>) stops.get(0).get("hourly")).hasSize(24);
        assertThat(m.get("mode")).isEqualTo("forecast");
    }

    @Test
    void csvExportHasHeaderAndRows() {
        byte[] body = web.get().uri("/api/v1/export?format=csv&routes=1&from=2025-11-10&to=2025-11-10&level=stop")
                .exchange().expectStatus().isOk()
                .expectBody().returnResult().getResponseBody();
        String csv = new String(body, StandardCharsets.UTF_8);
        assertThat(csv).startsWith("﻿horizon;route;direction;seq;stop_id;stop_name;period;base;pred;lo;hi");
        assertThat(csv.lines().count()).isEqualTo(1 + 44 * 24);
    }

    @Test
    void ingestCsvAndMonitoring() {
        String csv = "tran_no;tran_date_time;validation_result;ngpt_route\n"
                + "1;2025-11-02 08:10:00;1;7 трамвай\n"
                + "2;2025-11-02 08:11:00;1;7 трамвай\n"
                + "3;2025-11-02 08:12:00;90;7 трамвай\n"
                + "4;not-a-date;1;7 трамвай\n";
        web.post().uri("/api/v1/ingest/validations").contentType(MediaType.parseMediaType("text/csv"))
                .bodyValue(csv).exchange().expectStatus().isOk()
                .expectBody().jsonPath("$.received").isEqualTo(4)
                .jsonPath("$.accepted").isEqualTo(3)
                .jsonPath("$.boardings").isEqualTo(2);
        web.get().uri("/api/v1/monitoring?date=2025-11-02").exchange().expectStatus().isOk()
                .expectBody().jsonPath("$.lastHour").isEqualTo(8)
                .jsonPath("$.actualTotal").isEqualTo(2);
    }

    @Test
    void segmentStopExportSumsToSegmentTotal() {
        // stop_id 2594 есть в обоих направлениях маршрута 1: в выгрузку участка попадает только направление 0
        double seg = ((Number) totals(get("/api/v1/forecast?segment=1:0:1-3&granularity=total")).get("pred"))
                .doubleValue();
        byte[] body = web.get().uri("/api/v1/export?format=csv&segment=1:0:1-3&granularity=total&level=stop")
                .exchange().expectStatus().isOk().expectBody().returnResult().getResponseBody();
        List<String> lines = new String(body, StandardCharsets.UTF_8).lines().skip(1).toList();
        assertThat(lines).hasSize(3);
        double sum = lines.stream().mapToDouble(l -> Double.parseDouble(l.split(";")[8].replace(',', '.'))).sum();
        assertThat(sum).isCloseTo(seg, org.assertj.core.data.Offset.offset(0.1));
    }

    @Test
    void bandsOnlyWhereMeasured() {
        web.get().uri("/api/v1/forecast?routes=1&from=2025-11-10&to=2025-11-10&hourFrom=8&hourTo=8&granularity=total")
                .exchange().expectBody().jsonPath("$.series[0].points[0].lo").isEmpty();
        web.get().uri("/api/v1/forecast?stops=2594&granularity=month").exchange()
                .expectBody().jsonPath("$.series[0].points[0].lo").isEmpty();
        web.get().uri("/api/v1/forecast?routes=1&granularity=month&from=2025-11-01&to=2025-12-15").exchange()
                .expectBody().jsonPath("$.series[0].points[0].lo").isNotEmpty()
                .jsonPath("$.series[0].points[1].lo").isEmpty();
    }

    @Test
    void repeatedRoutesAreJoinedAndFractionalRejected() {
        web.get().uri("/api/v1/forecast?routes=1&routes=7&granularity=total").exchange()
                .expectBody().jsonPath("$.query.routes.length()").isEqualTo(2);
        web.get().uri("/api/v1/forecast?event=7.9:2025-12-01:2025-12-02:0.5").exchange().expectStatus().isBadRequest();
    }

    @Test
    void quotedCsvAndOutOfRangeDates() {
        String csv = "\"tran_date_time\";\"validation_result\";\"ngpt_route\"\n"
                + "\"2025-11-03 08:10:00\";\"1\";\"7 трамвай\"\n"
                + "\"2031-01-01 08:10:00\";\"1\";\"7 трамвай\"\n";
        web.post().uri("/api/v1/ingest/validations").contentType(MediaType.parseMediaType("text/csv"))
                .bodyValue(csv).exchange().expectStatus().isOk()
                .expectBody().jsonPath("$.accepted").isEqualTo(1).jsonPath("$.rejected").isEqualTo(1);
    }

    @Test
    void summaryComparesSameDayTypeAndGivesRecommendations() {
        web.get().uri("/api/v1/summary?date=2025-11-10").exchange().expectStatus().isOk()
                .expectBody()
                .jsonPath("$.network.weekAgoDate").isEqualTo("2025-10-27")   // 3.11 — праздник, пропущен
                .jsonPath("$.network.peakHour").isEqualTo(8)
                .jsonPath("$.fleet.available").isEqualTo(true)
                .jsonPath("$.recommendations.length()").value(n -> assertThat((Integer) n).isGreaterThan(2))
                .jsonPath("$.hotStops.length()").isEqualTo(10);
        web.get().uri("/api/v1/summary/export?date=2025-11-10").exchange().expectStatus().isOk()
                .expectHeader().contentType("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");
        web.get().uri("/api/v1/summary").exchange().expectStatus().isBadRequest();
    }

    @Test
    void detectorFindsInjectedAnomalyOnly() {
        web.delete().uri("/api/v1/ingest").exchange().expectStatus().isOk();
        web.post().uri("/api/v1/ingest/simulate?date=2025-11-11&toHour=14&anomalyRoute=7&anomalyFromHour=9"
                        + "&anomalyToHour=12&anomalyMult=0.4").exchange().expectStatus().isOk()
                .expectBody().jsonPath("$.simulated").isEqualTo(true);
        web.get().uri("/api/v1/monitoring?date=2025-11-11").exchange().expectStatus().isOk()
                .expectBody()
                .jsonPath("$.alerts.length()").isEqualTo(1)
                .jsonPath("$.alerts[0].route").isEqualTo(7)
                .jsonPath("$.alerts[0].fromHour").isEqualTo(9)
                .jsonPath("$.alerts[0].toHour").isEqualTo(12)
                .jsonPath("$.alerts[0].kind").isEqualTo("drop");
        web.delete().uri("/api/v1/ingest").exchange().expectStatus().isOk();
    }

    @Test
    @SuppressWarnings("unchecked")
    void reserveDoesNotOverlapReinforcementAndKeepsMinimum() {
        Map<String, Object> f = get("/api/v1/fleet");
        List<Map<String, Object>> reserve = (List<Map<String, Object>>) f.get("reserve");
        List<Map<String, Object>> cand = (List<Map<String, Object>>) f.get("candidates");
        assertThat(reserve).isNotEmpty();
        java.util.Set<String> hot = new java.util.HashSet<>();
        cand.forEach(c -> hot.add(c.get("route") + "|" + c.get("date") + "|" + c.get("hour")));
        for (Map<String, Object> r : reserve) {
            assertThat(hot).doesNotContain(r.get("route") + "|" + r.get("date") + "|" + r.get("hour"));
            int hour = ((Number) r.get("hour")).intValue();
            assertThat(hour).isBetween(7, 22);
            double nPlan = ((Number) r.get("nPlan")).doubleValue();
            int res = ((Number) r.get("reserve")).intValue();
            assertThat(res).isBetween(1, (int) Math.floor(0.3 * nPlan));
            assertThat(((Number) r.get("need")).intValue()).isGreaterThanOrEqualTo(2);
        }
        web.get().uri("/api/v1/summary?date=2025-12-01").exchange().expectStatus().isOk()
                .expectBody().jsonPath("$.fleet.reserveVehicleHours").value(v -> assertThat((Integer) v).isPositive());
    }

    @Test
    void liveMapReflectsIngestedStream() {
        web.delete().uri("/api/v1/ingest").exchange().expectStatus().isOk();
        web.get().uri("/api/v1/map/live?date=2025-11-12").exchange().expectStatus().isOk()
                .expectBody().jsonPath("$.available").isEqualTo(false);
        web.post().uri("/api/v1/ingest/simulate?date=2025-11-12&toHour=10").exchange().expectStatus().isOk();
        web.get().uri("/api/v1/map/live?date=2025-11-12").exchange().expectStatus().isOk()
                .expectBody().jsonPath("$.available").isEqualTo(true)
                .jsonPath("$.lastHour").isEqualTo(10)
                .jsonPath("$.routes.length()").isEqualTo(10);
        web.delete().uri("/api/v1/ingest").exchange().expectStatus().isOk();
    }

    // ------------------------------------------------------------------ исправления по аудиту

    @Test
    void repeatedSimulationReplacesDateInsteadOfSumming() {
        String ws = "test-repeat";
        for (int run = 0; run < 2; run++) {
            web.post().uri("/api/v1/ingest/simulate?date=2025-11-13&toHour=14&anomalyRoute=7&anomalyFromHour=9"
                    + "&anomalyToHour=12&anomalyMult=0.4").header("X-Workspace", ws).exchange().expectStatus().isOk();
        }
        web.get().uri("/api/v1/monitoring?date=2025-11-13").header("X-Workspace", ws).exchange().expectStatus().isOk()
                .expectBody()
                .jsonPath("$.alerts.length()").isEqualTo(1)
                .jsonPath("$.alerts[0].route").isEqualTo(7)
                .jsonPath("$.wapeScore").value(v -> assertThat(((Number) v).doubleValue()).isGreaterThan(0.85));
        web.delete().uri("/api/v1/ingest").header("X-Workspace", ws).exchange().expectStatus().isOk();
    }

    @Test
    void workspacesAreIsolated() {
        web.post().uri("/api/v1/ingest/simulate?date=2025-11-14&toHour=10").header("X-Workspace", "ws-a")
                .exchange().expectStatus().isOk();
        web.get().uri("/api/v1/monitoring?date=2025-11-14").header("X-Workspace", "ws-b").exchange()
                .expectBody().jsonPath("$.actualTotal").isEqualTo(0);
        web.delete().uri("/api/v1/ingest").header("X-Workspace", "ws-b").exchange().expectStatus().isOk();
        web.get().uri("/api/v1/map/live?date=2025-11-14").header("X-Workspace", "ws-a").exchange()
                .expectBody().jsonPath("$.available").isEqualTo(true);
        web.get().uri("/api/v1/monitoring?date=2025-11-14").header("X-Workspace", "bad id!").exchange()
                .expectStatus().isBadRequest();
        web.delete().uri("/api/v1/ingest").header("X-Workspace", "ws-a").exchange().expectStatus().isOk();
    }

    @Test
    void textPlainIngestIsRejected() {
        web.post().uri("/api/v1/ingest/validations").contentType(MediaType.TEXT_PLAIN)
                .bodyValue("tran_date_time;validation_result;ngpt_route\n2025-11-10 08:00:00;1;7 трамвай\n")
                .exchange().expectStatus().isEqualTo(415);
    }

    @Test
    @SuppressWarnings("unchecked")
    void fleetSummaryFollowsSelectedPeriod() {
        Map<String, Object> day = get("/api/v1/fleet?from=2025-11-10&to=2025-11-10");
        Map<String, Object> period = (Map<String, Object>) day.get("period");
        assertThat(period.get("days")).isEqualTo(1);
        assertThat(period.get("full")).isEqualTo(false);
        List<Map<String, Object>> summary = (List<Map<String, Object>>) day.get("summary");
        long extra = summary.stream().mapToLong(m -> ((Number) m.get("extraVehicleHours")).longValue()).sum();
        long reserve = summary.stream().mapToLong(m -> ((Number) m.get("reserveVehicleHours")).longValue()).sum();
        long candExtra = ((List<Map<String, Object>>) day.get("candidates")).stream()
                .mapToLong(m -> ((Number) m.get("extra")).longValue()).sum();
        long rowsReserve = ((List<Map<String, Object>>) day.get("reserve")).stream()
                .mapToLong(m -> ((Number) m.get("reserve")).longValue()).sum();
        assertThat(extra).isEqualTo(candExtra).isPositive();
        assertThat(reserve).isEqualTo(rowsReserve);
        assertThat(summary.get(0).get("plannedVehicleHours")).isNull();

        Map<String, Object> all = get("/api/v1/fleet");
        assertThat(((Map<String, Object>) all.get("period")).get("full")).isEqualTo(true);
        for (Map<String, Object> m : (List<Map<String, Object>>) all.get("summary")) {
            long need = ((Number) m.get("extraVehicleHours")).longValue();
            long feasible = ((Number) m.get("feasibleVehicleHours")).longValue();
            long network = ((Number) m.get("networkVehicleHours")).longValue();
            assertThat(network).isLessThanOrEqualTo(feasible);
            assertThat(feasible).isLessThanOrEqualTo(need);
            assertThat(m.get("plannedVehicleHours")).isNotNull();
        }
    }

    @Test
    void stopDirectionQualifierSelectsOneDirection() {
        double both = ((Number) totals(get("/api/v1/forecast?stops=2594&granularity=total")).get("pred")).doubleValue();
        double d0 = ((Number) totals(get("/api/v1/forecast?stops=2594@0&granularity=total")).get("pred")).doubleValue();
        double d1 = ((Number) totals(get("/api/v1/forecast?stops=2594@1&granularity=total")).get("pred")).doubleValue();
        assertThat(d0).isPositive().isLessThan(both);
        assertThat(d0 + d1).isCloseTo(both, org.assertj.core.data.Offset.offset(1.0));
        web.get().uri("/api/v1/forecast?stops=2594@7").exchange().expectStatus().isBadRequest();
    }

    @Test
    void csvUsesDecimalCommaWithoutExponent() {
        byte[] body = web.get().uri("/api/v1/export?format=csv&horizon=month&granularity=month&split=none")
                .exchange().expectStatus().isOk().expectBody().returnResult().getResponseBody();
        List<String> lines = new String(body, StandardCharsets.UTF_8).lines().skip(1).toList();
        assertThat(lines).isNotEmpty();
        for (String l : lines) {
            String[] f = l.split(";", -1);
            for (int c = 7; c <= 10; c++) {
                assertThat(f[c]).doesNotContain("E").doesNotContain(".");
            }
        }
        assertThat(String.join("\n", lines)).contains(",");
    }

    @Test
    void parallelXlsxExportsAreLimitedAndStreamed() {
        String uri = "/api/v1/export?format=xlsx&level=stop&granularity=hour&from=2025-11-01&to=2025-11-30";
        org.springframework.web.reactive.function.client.WebClient client =
                org.springframework.web.reactive.function.client.WebClient.create("http://localhost:" + port);
        // 5 одновременных тяжёлых XLSX: 2 отдаются потоком, остальные сразу получают 503 problem+json
        List<long[]> res = reactor.core.publisher.Flux.range(0, 5)
                .flatMap(i -> client.get().uri(uri).exchangeToMono(r -> r.bodyToFlux(
                                org.springframework.core.io.buffer.DataBuffer.class)
                        .reduce(new long[] {r.statusCode().value(), 0, -1, -1}, (acc, b) -> {
                            if (acc[2] < 0 && b.readableByteCount() >= 2) {
                                acc[2] = b.getByte(b.readPosition());
                                acc[3] = b.getByte(b.readPosition() + 1);
                            }
                            acc[1] += b.readableByteCount();
                            org.springframework.core.io.buffer.DataBufferUtils.release(b);
                            return acc;
                        })), 5)
                .collectList().block(java.time.Duration.ofMinutes(3));
        assertThat(res).extracting(x -> x[0]).contains(200L).contains(503L).allMatch(c -> c == 200L || c == 503L);
        for (long[] x : res) {
            if (x[0] == 200) {
                assertThat(x[1]).isGreaterThan(1_000_000);   // полный файл, не обрезан
                assertThat(x[2]).isEqualTo((long) 'P');      // zip-контейнер XLSX
                assertThat(x[3]).isEqualTo((long) 'K');
            }
        }
    }

    @Test
    void openApiServerIsRelative() {
        web.get().uri("/v3/api-docs").exchange().expectStatus().isOk()
                .expectBody().jsonPath("$.servers[0].url").isEqualTo("/");
    }
}
