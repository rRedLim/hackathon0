package ru.mostrans.tram.api;

import java.time.LocalDate;
import java.util.List;
import java.util.Map;

import org.springframework.core.ResolvableType;
import org.springframework.core.codec.StringDecoder;
import org.springframework.core.io.buffer.DataBuffer;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.media.Content;
import io.swagger.v3.oas.annotations.media.ExampleObject;
import io.swagger.v3.oas.annotations.media.Schema;
import io.swagger.v3.oas.annotations.responses.ApiResponse;
import io.swagger.v3.oas.annotations.tags.Tag;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Schedulers;
import ru.mostrans.tram.ingest.IngestService;

@RestController
@RequestMapping("/api/v1")
@Tag(name = "Приём данных", description = "Потоковый приём сырых валидаций и мониторинг факта против прогноза. "
        + "Данные разделены по рабочим областям: заголовок X-Workspace (интерфейс присылает идентификатор своей "
        + "вкладки); без заголовка — общая область «default»")
public class IngestController {

    static final String WS = "X-Workspace";

    /** Построчный декодер: тело CSV не собирается в память целиком. */
    private static final StringDecoder LINES = StringDecoder.textPlainOnly(List.of("\n"), true);

    private final IngestService ingest;

    public IngestController(IngestService ingest) {
        this.ingest = ingest;
    }

    // text/plain не принимается: иначе чужой сайт мог бы отправить данные «простым» запросом без CORS-проверки
    @PostMapping(value = "/ingest/validations", consumes = {"text/csv", "application/csv"})
    @Operation(summary = "Приём сырых валидаций: CSV (формат train.csv, «;», потоково) или JSON",
            description = "Посадка = validation_result 1, маршрут — число из ngpt_route, время — tran_date_time. "
                    + "Для CSV нужны колонки tran_date_time, validation_result, ngpt_route (остальные колонки train.csv "
                    + "допустимы). Записи с датами вне 2025-01-01 … 2026-12-31 отклоняются. Сброс — DELETE /api/v1/ingest.",
            requestBody = @io.swagger.v3.oas.annotations.parameters.RequestBody(required = true, content = {
                    @Content(mediaType = "text/csv", schema = @Schema(type = "string"), examples = @ExampleObject(
                            name = "csv", value = "tran_date_time;validation_result;ngpt_route;bus_exit_no\n"
                            + "2025-11-10 08:10:00;1;7 трамвай;210\n2025-11-10 08:11:30;1;7 трамвай;210\n"
                            + "2025-11-10 08:12:05;90;7 трамвай;210\n2025-11-10 08:14:45;1;17 трамвай;101")),
                    @Content(mediaType = "application/json", examples = @ExampleObject(name = "json",
                            value = "[{\"tranDateTime\":\"2025-11-10 08:10:00\",\"validationResult\":1,"
                                    + "\"ngptRoute\":\"7 трамвай\"}]"))}))
    public Mono<IngestService.Stats> ingestCsv(@RequestHeader(value = WS, required = false) String ws,
                                               @RequestBody Flux<DataBuffer> body) {
        IngestService.CsvBatch batch = ingest.csvBatch(ws);
        return LINES.decode(body, ResolvableType.forClass(String.class), MediaType.TEXT_PLAIN, null)
                .publishOn(Schedulers.boundedElastic())
                .doOnNext(batch::line)
                .then(Mono.fromCallable(batch::finish));
    }

    @PostMapping(value = "/ingest/validations", consumes = MediaType.APPLICATION_JSON_VALUE)
    @Operation(hidden = true) // тот же путь, описан в ingestCsv (Swagger объединяет операции одного пути)
    public IngestService.Stats ingestJson(@RequestHeader(value = WS, required = false) String ws,
                                          @RequestBody List<IngestService.Validation> items) {
        return ingest.ingestJson(ws, items);
    }

    @DeleteMapping("/ingest")
    @Operation(summary = "Сброс принятых данных рабочей области (X-Workspace, без заголовка — «default»)",
            responses = @ApiResponse(responseCode = "200", content = @Content(mediaType = "application/json",
                    examples = @ExampleObject(value = "{\"status\":\"ok\"}"))))
    public Map<String, String> reset(@RequestHeader(value = WS, required = false) String ws) {
        ingest.reset(ws);
        return Map.of("status", "ok");
    }

    @GetMapping("/monitoring")
    @Operation(summary = "Факт из приёма против прогноза на дату, WAPE-score и детектор смены режима",
            description = "threshold — порог отклонения (0.05–0.9, по умолчанию 0.25), minHours — сколько часов "
                    + "подряд (1–12, по умолчанию 2)")
    public IngestService.Monitoring monitoring(@RequestHeader(value = WS, required = false) String ws,
                                               @RequestParam(required = false) String date,
                                               @RequestParam(required = false) String threshold,
                                               @RequestParam(required = false) String minHours) {
        if (date == null || date.isBlank()) {
            throw ApiException.badRequest("date", "Параметр date обязателен (YYYY-MM-DD)");
        }
        LocalDate d = ForecastController.parseDate(date, "date");
        double th = blank(threshold) ? IngestService.DEFAULT_THRESHOLD : number(threshold, "threshold", 0.05, 0.9);
        int mh = blank(minHours) ? IngestService.DEFAULT_MIN_HOURS : (int) integer(minHours, "minHours", 1, 12);
        return ingest.monitoring(ws, d, th, mh);
    }

    @GetMapping("/map/live")
    @Operation(summary = "Живой факт для карты: принятые посадки по маршрутам и часам на дату и алерты детектора")
    public IngestService.Live live(@RequestHeader(value = WS, required = false) String ws,
                                   @RequestParam(required = false) String date) {
        if (blank(date)) {
            throw ApiException.badRequest("date", "Параметр date обязателен (YYYY-MM-DD)");
        }
        return ingest.live(ws, ForecastController.parseDate(date, "date"));
    }

    @PostMapping("/ingest/simulate")
    @Operation(summary = "Симуляция потока валидаций на дату с внедрённой аномалией — демонстрация детектора",
            description = "Посадки = прогноз ± 5 % шума; anomalyRoute/anomalyFromHour/anomalyToHour/anomalyMult задают "
                    + "аномалию (например, ремонт: ×0.4). Симуляция заменяет принятые данные этой даты в рабочей "
                    + "области (повторный запуск не суммируется).",
            parameters = {
                    @io.swagger.v3.oas.annotations.Parameter(name = "date", description = "Дата", example = "2025-11-10", required = true),
                    @io.swagger.v3.oas.annotations.Parameter(name = "toHour", description = "Поток до часа (0–23)", example = "14"),
                    @io.swagger.v3.oas.annotations.Parameter(name = "anomalyRoute", description = "Маршрут с аномалией (пусто — без аномалии)", example = "7"),
                    @io.swagger.v3.oas.annotations.Parameter(name = "anomalyFromHour", description = "Начало аномалии, час", example = "9"),
                    @io.swagger.v3.oas.annotations.Parameter(name = "anomalyToHour", description = "Конец аномалии, час (включительно)", example = "12"),
                    @io.swagger.v3.oas.annotations.Parameter(name = "anomalyMult", description = "Множитель аномалии 0–5", example = "0.4"),
                    @io.swagger.v3.oas.annotations.Parameter(name = "seed", description = "Зерно шума")},
            responses = @ApiResponse(responseCode = "200", content = @Content(mediaType = "application/json",
                    examples = @ExampleObject(value = "{\"received\":131826,\"accepted\":131826,\"rejected\":0,"
                            + "\"boardings\":131826,\"cells\":113,\"simulated\":true}"))))
    public Map<String, Object> simulate(@RequestHeader(value = WS, required = false) String ws,
                                        @RequestParam(required = false) String date,
                                        @RequestParam(required = false) String toHour,
                                        @RequestParam(required = false) String anomalyRoute,
                                        @RequestParam(required = false) String anomalyFromHour,
                                        @RequestParam(required = false) String anomalyToHour,
                                        @RequestParam(required = false) String anomalyMult,
                                        @RequestParam(required = false) String seed) {
        if (date == null || date.isBlank()) {
            throw ApiException.badRequest("date", "Параметр date обязателен (YYYY-MM-DD)");
        }
        LocalDate d = ForecastController.parseDate(date, "date");
        int to = blank(toHour) ? 23 : (int) integer(toHour, "toHour", 0, 23);
        Integer route = null;
        if (!blank(anomalyRoute)) {
            route = (int) integer(anomalyRoute, "anomalyRoute", 0, 1000);
            boolean known = false;
            for (int r : ru.mostrans.tram.data.DataStore.ROUTES) {
                known |= r == route;
            }
            if (!known) {
                throw ApiException.badRequest("anomalyRoute", "Маршрут " + route + " не найден");
            }
        }
        int af = blank(anomalyFromHour) ? 0 : (int) integer(anomalyFromHour, "anomalyFromHour", 0, 23);
        int at = blank(anomalyToHour) ? 23 : (int) integer(anomalyToHour, "anomalyToHour", 0, 23);
        if (at < af) {
            throw ApiException.badRequest("anomalyToHour", "anomalyToHour меньше anomalyFromHour");
        }
        double mult = blank(anomalyMult) ? 0.4 : number(anomalyMult, "anomalyMult", 0, 5);
        long sd = blank(seed) ? 42 : integer(seed, "seed", Long.MIN_VALUE, Long.MAX_VALUE);
        IngestService.Stats s = ingest.simulate(ws, d, to, route, af, at, mult, sd);
        Map<String, Object> out = new java.util.LinkedHashMap<>();
        out.put("received", s.received());
        out.put("accepted", s.accepted());
        out.put("rejected", s.rejected());
        out.put("boardings", s.boardings());
        out.put("cells", s.cells());
        out.put("simulated", true);
        return out;
    }

    private static boolean blank(String s) {
        return s == null || s.isBlank();
    }

    private static double number(String s, String param, double min, double max) {
        double v;
        try {
            v = Double.parseDouble(s.trim());
        } catch (NumberFormatException e) {
            throw ApiException.badRequest(param, "Параметр " + param + ": «" + s + "» — не число");
        }
        if (!(v >= min && v <= max)) {
            throw ApiException.badRequest(param, "Параметр " + param + " должен быть от " + min + " до " + max);
        }
        return v;
    }

    private static long integer(String s, String param, long min, long max) {
        long v;
        try {
            v = Long.parseLong(s.trim());
        } catch (NumberFormatException e) {
            throw ApiException.badRequest(param, "Параметр " + param + ": «" + s + "» — не целое число");
        }
        if (v < min || v > max) {
            throw ApiException.badRequest(param, "Параметр " + param + " должен быть от " + min + " до " + max);
        }
        return v;
    }
}
