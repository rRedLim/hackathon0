package ru.mostrans.tram.api;

import java.nio.charset.StandardCharsets;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.springframework.core.io.buffer.DataBuffer;
import org.springframework.core.io.buffer.DataBufferFactory;
import org.springframework.core.io.buffer.DefaultDataBufferFactory;
import org.springframework.http.ContentDisposition;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.util.MultiValueMap;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.Parameter;
import io.swagger.v3.oas.annotations.enums.ParameterIn;
import io.swagger.v3.oas.annotations.media.Content;
import io.swagger.v3.oas.annotations.media.ExampleObject;
import io.swagger.v3.oas.annotations.media.Schema;
import io.swagger.v3.oas.annotations.responses.ApiResponse;
import io.swagger.v3.oas.annotations.tags.Tag;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Schedulers;
import ru.mostrans.tram.data.DataStore;
import ru.mostrans.tram.data.Stop;
import ru.mostrans.tram.forecast.ExportService;
import ru.mostrans.tram.forecast.ForecastEngine;
import ru.mostrans.tram.forecast.ForecastQuery;
import ru.mostrans.tram.forecast.Granularity;
import ru.mostrans.tram.forecast.Horizon;
import ru.mostrans.tram.forecast.MapService;
import ru.mostrans.tram.forecast.Scenario;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.JsonNode;

@RestController
@RequestMapping("/api/v1")
@Tag(name = "Прогноз", description = "Прогноз посадок, карта, выгрузка, справочники")
public class ForecastController {

    private static final MediaType XLSX =
            MediaType.parseMediaType("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");
    private static final MediaType CSV = MediaType.parseMediaType("text/csv;charset=UTF-8");

    private final DataStore data;
    private final ForecastEngine engine;
    private final MapService map;
    private final ExportService export;
    private final ObjectMapper json;
    private final ResponseCache mapCache = new ResponseCache(512);

    public ForecastController(DataStore data, ForecastEngine engine, MapService map, ExportService export,
                              ObjectMapper json) {
        this.data = data;
        this.engine = engine;
        this.map = map;
        this.export = export;
        this.json = json;
    }

    @GetMapping("/meta")
    @Operation(summary = "Справочная информация: модель, маршруты, горизонты, события, внешние источники",
            responses = @ApiResponse(responseCode = "200", content = @Content(mediaType = "application/json",
                    examples = @ExampleObject(value = "{\"model\":{\"name\":\"Структурная модель\",\"version\":\"r5_v8_W06000\","
                            + "\"leaderboardWapeScore\":0.9125},\"routes\":[{\"route\":7,\"name\":\"...\",\"color\":\"#3cb44b\","
                            + "\"stops\":87,\"geometry\":\"gtfs\"}],\"horizons\":[{\"id\":\"day\",\"from\":\"2025-11-01\","
                            + "\"to\":\"2025-12-31\",\"defaultGranularity\":\"hour\"}],\"events\":[],\"regimes\":[],"
                            + "\"sources\":[{\"name\":\"...\",\"effect\":\"...\",\"url\":\"...\"}],"
                            + "\"weather\":{\"beta\":-0.012}}"))))
    public Map<String, Object> meta() {
        JsonNode m = data.meta();
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("model", m.get("model"));
        out.put("routes", m.get("routes"));
        List<Map<String, Object>> horizons = new ArrayList<>();
        for (Horizon h : Horizon.values()) {
            Map<String, Object> hm = new LinkedHashMap<>();
            hm.put("id", h.id);
            hm.put("label", h.label);
            hm.put("from", h.from.toString());
            hm.put("to", h.to.toString());
            hm.put("defaultGranularity", h.defaultGranularity.id());
            hm.put("granularities", h.granularities.stream().map(Granularity::id).toList());
            horizons.add(hm);
        }
        out.put("horizons", horizons);
        out.put("events", m.get("events"));
        out.put("regimes", m.get("regimes"));
        out.put("sources", m.get("sources"));
        Map<String, Double> norm = new LinkedHashMap<>();
        Map<String, Double> normLog = new LinkedHashMap<>();
        for (int mo = 1; mo <= 12; mo++) {
            norm.put(String.valueOf(mo), Math.round(data.monthNormMm(mo) * 100.0) / 100.0);
            normLog.put(String.valueOf(mo), Math.round(data.monthNormLog(mo) * 10000.0) / 10000.0);
        }
        // k = exp(beta · (ln(1 + мм) − monthNormLog1p[месяц])), см. Scenario.dayFactor
        out.put("weather", Map.of("beta", Scenario.DEFAULT_BETA, "monthNormMm", norm, "monthNormLog1p", normLog));
        return out;
    }

    @GetMapping("/stops")
    @Operation(summary = "Все остановки с координатами и априорной долей посадок маршрута")
    public List<Stop> stops() {
        return data.stops();
    }

    public record Direction(int direction, List<Stop> stops) {
    }

    public record RouteStops(int route, List<Direction> directions) {
    }

    @GetMapping("/routes/{route}/stops")
    @Operation(summary = "Остановки маршрута по направлениям в порядке следования")
    public RouteStops routeStops(@PathVariable("route") String routeStr) {
        int route;
        try {
            route = Integer.parseInt(routeStr);
        } catch (NumberFormatException e) {
            throw ApiException.badRequest("route", "Номер маршрута «" + routeStr + "» — не число");
        }
        if (!data.hasRoute(route)) {
            throw ApiException.notFound("route", "Маршрут " + route + " не найден. Доступные маршруты: "
                    + java.util.Arrays.toString(DataStore.ROUTES));
        }
        Map<Integer, List<Stop>> byDir = new LinkedHashMap<>();
        for (Stop s : data.stopsOf(route)) {
            byDir.computeIfAbsent(s.direction(), x -> new ArrayList<>()).add(s);
        }
        return new RouteStops(route, byDir.entrySet().stream().map(e -> new Direction(e.getKey(), e.getValue()))
                .toList());
    }

    @GetMapping("/forecast")
    @Operation(summary = "Прогноз с агрегацией по маршруту / остановкам / участку, интервалу и гранулярности",
            description = "Корректирующие коэффициенты (precip, beta, monthMult, routeMult, dowMult, event) необязательны. "
                    + "Подробно — docs/api.md",
            parameters = {
            @Parameter(name = "horizon", in = ParameterIn.QUERY, description = "Горизонт", example = "day", schema = @Schema(type = "string", allowableValues = {"day", "month", "year", "history"})),
            @Parameter(name = "routes", in = ParameterIn.QUERY, description = "Маршруты через запятую (по умолчанию все 10)", example = "7"),
            @Parameter(name = "stops", in = ParameterIn.QUERY, description = "Идентификаторы остановок через запятую (вместо routes)"),
            @Parameter(name = "segment", in = ParameterIn.QUERY, description = "Участок маршрут:направление:seqFrom-seqTo, напр. 1:0:3-10"),
            @Parameter(name = "from", in = ParameterIn.QUERY, description = "Начало интервала, YYYY-MM-DD (по умолчанию начало горизонта)", example = "2025-11-10"),
            @Parameter(name = "to", in = ParameterIn.QUERY, description = "Конец интервала, YYYY-MM-DD (по умолчанию конец горизонта)", example = "2025-11-10"),
            @Parameter(name = "hourFrom", in = ParameterIn.QUERY, description = "Первый час 0–23 (включительно)"),
            @Parameter(name = "hourTo", in = ParameterIn.QUERY, description = "Последний час 0–23 (включительно)"),
            @Parameter(name = "granularity", in = ParameterIn.QUERY, description = "Гранулярность (по умолчанию: day → hour, month → day, year → month)", schema = @Schema(type = "string", allowableValues = {"hour", "day", "month", "total"})),
            @Parameter(name = "split", in = ParameterIn.QUERY, description = "route — ряд на маршрут, none — один суммарный ряд", schema = @Schema(type = "string", allowableValues = {"route", "none"})),
            @Parameter(name = "precip", in = ParameterIn.QUERY, description = "Осадки по дням, мм: дата:мм через запятую, напр. 2025-12-05:15"),
            @Parameter(name = "beta", in = ParameterIn.QUERY, description = "Чувствительность к осадкам β (по умолчанию −0.012), −1…1"),
            @Parameter(name = "monthMult", in = ParameterIn.QUERY, description = "Множитель месяца: месяц:множитель, напр. 12:1.03"),
            @Parameter(name = "routeMult", in = ParameterIn.QUERY, description = "Множитель маршрута: маршрут:множитель, напр. 17:0.95"),
            @Parameter(name = "dowMult", in = ParameterIn.QUERY, description = "Множитель дня недели (1 = Пн … 7 = Вс): день:множитель, напр. 6:0.9"),
            @Parameter(name = "event", in = ParameterIn.QUERY, description = "Событие маршруты:начало:конец:множитель[:часы], маршруты через | или *, напр. 7|50:2025-12-20:2025-12-21:0.5:10-17")})
    public ForecastEngine.Result forecast(
            @Parameter(hidden = true) @RequestParam MultiValueMap<String, String> params) {
        return engine.forecast(ForecastQuery.parse(params, data));
    }

    @GetMapping(value = "/map", produces = MediaType.APPLICATION_JSON_VALUE)
    @Operation(summary = "Данные карты на день: 24 часа по маршрутам и остановкам",
            parameters = {
            @Parameter(name = "date", in = ParameterIn.QUERY, description = "Дата 2025-01-01 … 2026-12-31 (до 31.10.2025 — факт, ноябрь–декабрь — прогноз, 2026 — сценарий)", example = "2025-11-10", required = true),
            @Parameter(name = "routes", in = ParameterIn.QUERY, description = "Маршруты через запятую (по умолчанию все)"),
            @Parameter(name = "precip", in = ParameterIn.QUERY, description = "Осадки по дням, мм: дата:мм через запятую, напр. 2025-12-05:15"),
            @Parameter(name = "beta", in = ParameterIn.QUERY, description = "Чувствительность к осадкам β (по умолчанию −0.012), −1…1"),
            @Parameter(name = "monthMult", in = ParameterIn.QUERY, description = "Множитель месяца: месяц:множитель, напр. 12:1.03"),
            @Parameter(name = "routeMult", in = ParameterIn.QUERY, description = "Множитель маршрута: маршрут:множитель, напр. 17:0.95"),
            @Parameter(name = "dowMult", in = ParameterIn.QUERY, description = "Множитель дня недели (1 = Пн … 7 = Вс): день:множитель, напр. 6:0.9"),
            @Parameter(name = "event", in = ParameterIn.QUERY, description = "Событие маршруты:начало:конец:множитель[:часы], маршруты через | или *, напр. 7|50:2025-12-20:2025-12-21:0.5:10-17")},
            responses = @ApiResponse(responseCode = "200", content = @Content(mediaType = "application/json",
                    schema = @Schema(implementation = MapService.MapDay.class))))
    public ResponseEntity<byte[]> map(@Parameter(hidden = true) @RequestParam MultiValueMap<String, String> params,
                                      @RequestHeader(value = HttpHeaders.ACCEPT_ENCODING, required = false)
                                      String acceptEncoding) {
        ResponseCache.Entry e = mapCache.get(ResponseCache.key(params), () -> json.writeValueAsBytes(map.day(params)));
        ResponseEntity.BodyBuilder ok = ResponseEntity.ok().contentType(MediaType.APPLICATION_JSON)
                .header(HttpHeaders.VARY, HttpHeaders.ACCEPT_ENCODING);
        if (acceptEncoding != null && acceptEncoding.contains("gzip")) {
            return ok.header(HttpHeaders.CONTENT_ENCODING, "gzip").body(e.gzip());
        }
        return ok.body(e.raw());
    }

    @GetMapping("/export")
    @Operation(summary = "Выгрузка прогноза в CSV или XLSX (те же параметры, что у /forecast)",
            parameters = {
            @Parameter(name = "format", in = ParameterIn.QUERY, description = "Формат файла", example = "csv", schema = @Schema(type = "string", allowableValues = {"csv", "xlsx"})),
            @Parameter(name = "level", in = ParameterIn.QUERY, description = "route — строка на маршрут, stop — строка на остановку", schema = @Schema(type = "string", allowableValues = {"route", "stop"})),
            @Parameter(name = "horizon", in = ParameterIn.QUERY, description = "Горизонт", example = "day", schema = @Schema(type = "string", allowableValues = {"day", "month", "year", "history"})),
            @Parameter(name = "routes", in = ParameterIn.QUERY, description = "Маршруты через запятую (по умолчанию все 10)", example = "7"),
            @Parameter(name = "stops", in = ParameterIn.QUERY, description = "Идентификаторы остановок через запятую (вместо routes)"),
            @Parameter(name = "segment", in = ParameterIn.QUERY, description = "Участок маршрут:направление:seqFrom-seqTo, напр. 1:0:3-10"),
            @Parameter(name = "from", in = ParameterIn.QUERY, description = "Начало интервала, YYYY-MM-DD (по умолчанию начало горизонта)", example = "2025-11-10"),
            @Parameter(name = "to", in = ParameterIn.QUERY, description = "Конец интервала, YYYY-MM-DD (по умолчанию конец горизонта)", example = "2025-11-10"),
            @Parameter(name = "hourFrom", in = ParameterIn.QUERY, description = "Первый час 0–23 (включительно)"),
            @Parameter(name = "hourTo", in = ParameterIn.QUERY, description = "Последний час 0–23 (включительно)"),
            @Parameter(name = "granularity", in = ParameterIn.QUERY, description = "Гранулярность (по умолчанию: day → hour, month → day, year → month)", schema = @Schema(type = "string", allowableValues = {"hour", "day", "month", "total"})),
            @Parameter(name = "split", in = ParameterIn.QUERY, description = "route — ряд на маршрут, none — один суммарный ряд", schema = @Schema(type = "string", allowableValues = {"route", "none"})),
            @Parameter(name = "precip", in = ParameterIn.QUERY, description = "Осадки по дням, мм: дата:мм через запятую, напр. 2025-12-05:15"),
            @Parameter(name = "beta", in = ParameterIn.QUERY, description = "Чувствительность к осадкам β (по умолчанию −0.012), −1…1"),
            @Parameter(name = "monthMult", in = ParameterIn.QUERY, description = "Множитель месяца: месяц:множитель, напр. 12:1.03"),
            @Parameter(name = "routeMult", in = ParameterIn.QUERY, description = "Множитель маршрута: маршрут:множитель, напр. 17:0.95"),
            @Parameter(name = "dowMult", in = ParameterIn.QUERY, description = "Множитель дня недели (1 = Пн … 7 = Вс): день:множитель, напр. 6:0.9"),
            @Parameter(name = "event", in = ParameterIn.QUERY, description = "Событие маршруты:начало:конец:множитель[:часы], маршруты через | или *, напр. 7|50:2025-12-20:2025-12-21:0.5:10-17")})
    public Mono<ResponseEntity<Flux<DataBuffer>>> export(
            @Parameter(hidden = true) @RequestParam MultiValueMap<String, String> params) {
        String format = params.getFirst("format") == null ? "csv" : params.getFirst("format").trim().toLowerCase();
        if (!format.equals("csv") && !format.equals("xlsx")) {
            throw ApiException.badRequest("format", "format: ожидается csv или xlsx, получено «" + format + "»");
        }
        ForecastQuery q = ForecastQuery.parse(params, data);
        ExportService.Export e = export.prepare(q, params.getFirst("level"));
        String name = "forecast_" + q.horizon().id + "_" + q.from() + "_" + q.to() + "." + format;
        HttpHeaders h = new HttpHeaders();
        h.setContentDisposition(ContentDisposition.attachment().filename(name, StandardCharsets.UTF_8).build());
        h.add("X-Rows", String.valueOf(e.rows()));
        DataBufferFactory f = DefaultDataBufferFactory.sharedInstance;
        if (format.equals("csv")) {
            h.setContentType(CSV);
            return Mono.just(ResponseEntity.ok().headers(h).body(csv(e, f)));
        }
        h.setContentType(XLSX);
        return Mono.fromCallable(() -> {
                    java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream(1 << 16);
                    export.writeXlsx(e, q, bos);
                    return bos.toByteArray();
                })
                .subscribeOn(Schedulers.boundedElastic())
                .map(bytes -> ResponseEntity.ok().headers(h).contentLength(bytes.length)
                        .body(Flux.just(f.wrap(bytes))));
    }

    /** CSV потоком: пачки по 2000 строк, UTF-8 с BOM для Excel. */
    private static Flux<DataBuffer> csv(ExportService.Export e, DataBufferFactory f) {
        return Flux.defer(() -> {
            Iterator<ExportService.Row> it = e.iterable().iterator();
            Flux<DataBuffer> head = Flux.just(f.wrap(("﻿" + ExportService.csvHeader()).getBytes(StandardCharsets.UTF_8)));
            Flux<DataBuffer> body = Flux.<DataBuffer>generate(sink -> {
                if (!it.hasNext()) {
                    sink.complete();
                    return;
                }
                StringBuilder sb = new StringBuilder(2000 * 80);
                for (int n = 0; n < 2000 && it.hasNext(); n++) {
                    sb.append(ExportService.csvLine(it.next()));
                }
                sink.next(f.wrap(sb.toString().getBytes(StandardCharsets.UTF_8)));
            });
            return head.concatWith(body);
        }).subscribeOn(Schedulers.boundedElastic());
    }

    @GetMapping("/fleet")
    @Operation(summary = "Усиление выпуска (посадки на вагон выше нормы) и резерв (где выпуск можно сократить)",
            responses = @ApiResponse(responseCode = "200", content = @Content(mediaType = "application/json",
                    examples = @ExampleObject(value = "{\"summary\":[{\"route\":1,\"normBpv\":119.3,\"candHours\":315,"
                            + "\"extraVehicleHours\":528,\"reserveVehicleHours\":444}],\"candidates\":[{\"route\":1,"
                            + "\"date\":\"2025-11-01\",\"hour\":13,\"pred\":1344,\"nPlan\":11.0,\"norm\":119.3,\"extra\":1}],"
                            + "\"reserve\":[{\"route\":50,\"date\":\"2025-12-01\",\"hour\":20,\"pred\":947,\"nPlan\":17.0,"
                            + "\"need\":11,\"reserve\":5}],\"method\":\"...\",\"reserveMethod\":\"...\"}"))))
    public Map<String, Object> fleet(@RequestParam(required = false) String routes,
                                     @RequestParam(required = false) String from,
                                     @RequestParam(required = false) String to) {
        java.util.Set<Long> rs = new java.util.HashSet<>();
        if (routes != null && !routes.isBlank()) {
            for (String r : routes.split(",")) {
                try {
                    int route = Integer.parseInt(r.trim());
                    if (!data.hasRoute(route)) {
                        throw ApiException.badRequest("routes", "Маршрут " + route + " не найден");
                    }
                    rs.add((long) route);
                } catch (NumberFormatException e) {
                    throw ApiException.badRequest("routes", "Номер маршрута «" + r.trim() + "» — не число");
                }
            }
        }
        LocalDate f = from == null || from.isBlank() ? null : parseDate(from, "from");
        LocalDate t = to == null || to.isBlank() ? null : parseDate(to, "to");
        java.util.function.Predicate<Map<String, Object>> keep = m -> {
            if (!rs.isEmpty() && !rs.contains(((Number) m.get("route")).longValue())) {
                return false;
            }
            LocalDate d = LocalDate.parse((String) m.get("date"));
            return (f == null || !d.isBefore(f)) && (t == null || !d.isAfter(t));
        };
        List<Map<String, Object>> cand = data.fleetCandidates().stream().filter(keep).toList();
        List<Map<String, Object>> reserve = data.fleetReserve().stream().filter(keep).toList();
        List<Map<String, Object>> summary = data.fleetSummary().stream()
                .filter(m -> rs.isEmpty() || rs.contains(((Number) m.get("route")).longValue()))
                .toList();
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("summary", summary);
        out.put("candidates", cand);
        out.put("reserve", reserve);
        out.put("method", "Вагон-час — вагон с хотя бы одной валидацией в этом часу; норма — 90-й перцентиль посадок "
                + "на вагон-час в обычном режиме январь–октябрь; выпуск — медиана вагонов в этот час за 4 недели того же "
                + "типа дня; добавка = ceil(прогноз / норма) − выпуск.");
        out.put("reserveMethod", "Резерв (экономия) — часы 7:00–22:59, где прогноз посадок на вагон ниже 50 % нормы "
                + "маршрута. Оставляем need = max(ceil(прогноз / (0,7 · норма)), 2) вагонов — нагрузка не выше 70 % нормы; "
                + "снимаем не больше 30 % планового выпуска часа, чтобы интервал движения не вырос критично. Не "
                + "оцениваются дни, когда маршрут не в обычном режиме, и вечер 31.12 (бесплатный проезд: валидаций "
                + "меньше, чем пассажиров).");
        return out;
    }

    @GetMapping("/quality")
    @Operation(summary = "Качество модели: ошибка по горизонту, уровню агрегации, маршрутам и фолдам бэктеста",
            responses = @ApiResponse(responseCode = "200", content = @Content(mediaType = "application/json",
                    examples = @ExampleObject(value = "{\"leaderboardWapeScore\":0.9125,\"byHorizon\":[{\"horizon\":\"1–7 дней\","
                            + "\"hourlyScore\":0.8596,\"dailyScore\":0.8864}],\"byLevel\":[{\"level\":\"месяц маршрута\","
                            + "\"score\":0.9209}],\"byRoute\":[{\"route\":17,\"hourlyScore\":0.9055}],"
                            + "\"folds\":[{\"fold\":\"A: ≤31.01 → фев–мар\",\"score\":0.913}]}"))))
    public Map<String, Object> quality() {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("leaderboardWapeScore", 0.9125);
        out.put("byHorizon", data.errorByHorizon());
        out.put("byLevel", data.errorByLevel());
        out.put("byRoute", data.errorByRoute());
        out.put("folds", List.of(
                Map.of("fold", "A: ≤31.01 → фев–мар", "score", 0.913),
                Map.of("fold", "B: ≤31.03 → апр–май (майские праздники, закрытия 17-го)", "score", 0.853),
                Map.of("fold", "C: ≤31.08 → сен–окт (смена сезона и режимов)", "score", 0.834),
                Map.of("fold", "D: ≤30.09 → октябрь", "score", 0.914),
                Map.of("fold", "E: ≤12.10 → 13–31.10", "score", 0.919)));
        return out;
    }

    public record CalendarDay(LocalDate date, int code, int dow, String label, Double precipMm, Double tempC) {
    }

    @GetMapping("/calendar")
    @Operation(summary = "Типы дней (производственный календарь) и погода по датам")
    public List<CalendarDay> calendar(@RequestParam(required = false) String from,
                                      @RequestParam(required = false) String to) {
        LocalDate f = from == null || from.isBlank() ? DataStore.HIST_START : parseDate(from, "from");
        LocalDate t = to == null || to.isBlank() ? DataStore.YEAR_END : parseDate(to, "to");
        if (t.isBefore(f)) {
            throw ApiException.badRequest("to", "Дата to раньше from");
        }
        if (DataStore.days(f, t) > 800) {
            throw ApiException.badRequest("to", "Интервал календаря не больше 800 дней");
        }
        List<CalendarDay> out = new ArrayList<>();
        for (LocalDate d = f; !d.isAfter(t); d = d.plusDays(1)) {
            int code = data.dayCode(d);
            double[] w = data.weather(d);
            out.add(new CalendarDay(d, code, d.getDayOfWeek().getValue(), DataStore.dayLabel(code),
                    w == null || Double.isNaN(w[0]) ? null : w[0], w == null || Double.isNaN(w[1]) ? null : w[1]));
        }
        return out;
    }

    static LocalDate parseDate(String s, String param) {
        try {
            return LocalDate.parse(s.trim());
        } catch (java.time.format.DateTimeParseException e) {
            throw ApiException.badRequest(param, "Параметр " + param + ": «" + s + "» — не дата формата YYYY-MM-DD");
        }
    }
}
