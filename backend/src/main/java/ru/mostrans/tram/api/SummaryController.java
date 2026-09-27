package ru.mostrans.tram.api;

import java.nio.charset.StandardCharsets;

import org.springframework.http.ContentDisposition;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.util.MultiValueMap;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.Parameter;
import io.swagger.v3.oas.annotations.enums.ParameterIn;
import io.swagger.v3.oas.annotations.tags.Tag;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Schedulers;
import ru.mostrans.tram.summary.SummaryService;

@RestController
@RequestMapping("/api/v1")
@Tag(name = "Сводка диспетчера", description = "Что будет в день: пик, маршруты, усиление выпуска, остановки, события")
public class SummaryController {

    private static final MediaType XLSX =
            MediaType.parseMediaType("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");

    private final SummaryService summary;

    public SummaryController(SummaryService summary) {
        this.summary = summary;
    }

    @GetMapping("/summary")
    @Operation(summary = "Сводка диспетчера на дату: пик, маршруты, усиление и резерв, горячие остановки, рекомендации",
            parameters = {
            @Parameter(name = "date", in = ParameterIn.QUERY, description = "Дата 2025-01-01 … 2026-12-31", example = "2025-11-10", required = true),
            @Parameter(name = "precip", in = ParameterIn.QUERY, description = "Осадки по дням, мм: дата:мм через запятую, напр. 2025-12-05:15"),
            @Parameter(name = "beta", in = ParameterIn.QUERY, description = "Чувствительность к осадкам β (по умолчанию −0.012), −1…1"),
            @Parameter(name = "monthMult", in = ParameterIn.QUERY, description = "Множитель месяца: месяц:множитель, напр. 12:1.03"),
            @Parameter(name = "routeMult", in = ParameterIn.QUERY, description = "Множитель маршрута: маршрут:множитель, напр. 17:0.95"),
            @Parameter(name = "dowMult", in = ParameterIn.QUERY, description = "Множитель дня недели (1 = Пн … 7 = Вс): день:множитель, напр. 6:0.9"),
            @Parameter(name = "event", in = ParameterIn.QUERY, description = "Событие маршруты:начало:конец:множитель[:часы], маршруты через | или *, напр. 7|50:2025-12-20:2025-12-21:0.5:10-17")})
    public SummaryService.Summary summary(@Parameter(hidden = true) @RequestParam MultiValueMap<String, String> p) {
        return summary.summary(p);
    }

    @GetMapping("/summary/export")
    @Operation(summary = "Сводка диспетчера в XLSX: сводка и рекомендации, маршруты, усиление, резерв, остановки",
            parameters = {
            @Parameter(name = "date", in = ParameterIn.QUERY, description = "Дата 2025-01-01 … 2026-12-31", example = "2025-11-10", required = true),
            @Parameter(name = "precip", in = ParameterIn.QUERY, description = "Осадки по дням, мм: дата:мм через запятую, напр. 2025-12-05:15"),
            @Parameter(name = "beta", in = ParameterIn.QUERY, description = "Чувствительность к осадкам β (по умолчанию −0.012), −1…1"),
            @Parameter(name = "monthMult", in = ParameterIn.QUERY, description = "Множитель месяца: месяц:множитель, напр. 12:1.03"),
            @Parameter(name = "routeMult", in = ParameterIn.QUERY, description = "Множитель маршрута: маршрут:множитель, напр. 17:0.95"),
            @Parameter(name = "dowMult", in = ParameterIn.QUERY, description = "Множитель дня недели (1 = Пн … 7 = Вс): день:множитель, напр. 6:0.9"),
            @Parameter(name = "event", in = ParameterIn.QUERY, description = "Событие маршруты:начало:конец:множитель[:часы], маршруты через | или *, напр. 7|50:2025-12-20:2025-12-21:0.5:10-17")})
    public Mono<ResponseEntity<byte[]>> export(@Parameter(hidden = true) @RequestParam MultiValueMap<String, String> p) {
        SummaryService.Summary s = summary.summary(p);
        return Mono.fromCallable(() -> {
                    java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream(1 << 15);
                    summary.writeXlsx(s, bos);
                    return bos.toByteArray();
                })
                .subscribeOn(Schedulers.boundedElastic())
                .map(bytes -> {
                    HttpHeaders h = new HttpHeaders();
                    h.setContentType(XLSX);
                    h.setContentDisposition(ContentDisposition.attachment()
                            .filename("summary_" + s.date() + ".xlsx", StandardCharsets.UTF_8).build());
                    return ResponseEntity.ok().headers(h).body(bytes);
                });
    }
}
