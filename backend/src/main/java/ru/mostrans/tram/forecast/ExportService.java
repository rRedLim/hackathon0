package ru.mostrans.tram.forecast;

import java.io.IOException;
import java.io.OutputStream;
import java.io.UncheckedIOException;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.NoSuchElementException;

import org.dhatim.fastexcel.Workbook;
import org.dhatim.fastexcel.Worksheet;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

import ru.mostrans.tram.api.ApiException;
import ru.mostrans.tram.data.DataStore;
import ru.mostrans.tram.data.Stop;

/** Выгрузка прогноза в CSV/XLSX: уровень маршрута или остановки. Строки генерируются лениво. */
@Service
public class ExportService {

    public static final int MAX_ROWS = 1_000_000;
    public static final String[] HEADER = {"horizon", "route", "direction", "seq", "stop_id", "stop_name", "period",
            "base", "pred", "lo", "hi"};

    private final DataStore data;
    private final ForecastEngine engine;

    public ExportService(DataStore data, ForecastEngine engine) {
        this.data = data;
        this.engine = engine;
    }

    /** Строка выгрузки. */
    public record Row(String horizon, String route, String direction, String seq, String stopId, String stopName,
                      String period, double base, double pred, Double lo, Double hi) {
    }

    /** Подготовленная выгрузка: число строк известно заранее, сами строки — ленивый итератор. */
    public record Export(int rows, Iterable<Row> iterable) {
    }

    public Export prepare(ForecastQuery q, String level) {
        if (level == null || level.isBlank() || level.equals("route")) {
            return routeLevel(q);
        }
        if (level.equals("stop")) {
            return stopLevel(q);
        }
        throw ApiException.badRequest("level", "level: ожидается route или stop, получено «" + level + "»");
    }

    private Export routeLevel(ForecastQuery q) {
        long total = (long) ForecastEngine.bucketCount(q) * (q.splitByRoute() ? q.routes().size() : 1);
        if (total > MAX_ROWS) {
            throw new ApiException(HttpStatus.PAYLOAD_TOO_LARGE, "granularity", "Выгрузка: " + total
                    + " строк — больше лимита " + MAX_ROWS + ". Укрупните гранулярность или сократите интервал");
        }
        ForecastEngine.Buckets a = engine.aggregate(q);
        double u = ForecastEngine.band(q);
        String stopIds = String.join(" ", q.stops());
        List<Row> rows = new ArrayList<>();
        for (int s = 0; s < a.seriesKeys().size(); s++) {
            for (int k = 0; k < a.keys().size(); k++) {
                double pred = a.pred()[s][k];
                rows.add(new Row(q.horizon().id, a.seriesKeys().get(s), "", "", stopIds, "", a.keys().get(k),
                        ForecastEngine.r2(a.base()[s][k]), ForecastEngine.r2(pred),
                        ForecastEngine.bandFor(q, a.keys().get(k)) ? ForecastEngine.r1(pred * (1 - u)) : null,
                        ForecastEngine.bandFor(q, a.keys().get(k)) ? ForecastEngine.r1(pred * (1 + u)) : null));
            }
        }
        return new Export(rows.size(), rows);
    }

    private Export stopLevel(ForecastQuery q) {
        // маршрутный прогноз без доли остановок, затем × вес каждой остановки
        ForecastQuery perRoute = new ForecastQuery(q.horizon(), q.routes(), q.stops(), q.segment(), q.from(), q.to(),
                q.hourFrom(), q.hourTo(), q.granularity(), true, Map.of(), List.of(), q.scenario());
        List<Stop> stops = new ArrayList<>();
        List<Integer> seriesIdx = new ArrayList<>();
        for (int i = 0; i < q.routes().size(); i++) {
            for (Stop s : data.stopsOf(q.routes().get(i))) {
                // выбранные вхождения (маршрут, направление, порядок), а не все одноимённые stop_id
                if (q.selected().isEmpty() || q.selected().contains(s)) {
                    stops.add(s);
                    seriesIdx.add(i);
                }
            }
        }
        if (stops.isEmpty()) {
            throw ApiException.badRequest("level", "У выбранных маршрутов нет остановок с координатами "
                    + "(справочник покрывает маршруты с геометрией) — выгрузите level=route");
        }
        int nBuckets = ForecastEngine.bucketCount(q);
        long total = (long) stops.size() * nBuckets;
        if (total > MAX_ROWS) {
            throw new ApiException(HttpStatus.PAYLOAD_TOO_LARGE, "level", "Выгрузка по остановкам: " + total
                    + " строк — больше лимита " + MAX_ROWS + ". Укрупните гранулярность, сократите интервал "
                    + "или выберите меньше маршрутов/остановок");
        }
        ForecastEngine.Buckets a = engine.aggregate(perRoute);
        String h = q.horizon().id;
        int rows = stops.size() * a.keys().size();
        Iterable<Row> it = () -> new Iterator<>() {
            int si = 0;
            int bi = 0;

            @Override
            public boolean hasNext() {
                return si < stops.size() && !a.keys().isEmpty();
            }

            @Override
            public Row next() {
                if (!hasNext()) {
                    throw new NoSuchElementException();
                }
                Stop s = stops.get(si);
                int ser = seriesIdx.get(si);
                double base = a.base()[ser][bi] * s.weight();
                double pred = a.pred()[ser][bi] * s.weight();
                Double lo = null; // интервал по остановкам не измерен (доли априорные)
                Double hi = null;
                Row r = new Row(h, String.valueOf(s.route()), String.valueOf(s.direction()), String.valueOf(s.seq()),
                        s.stopId(), s.name(), a.keys().get(bi),
                        ForecastEngine.r2(base), ForecastEngine.r2(pred), lo, hi);
                if (++bi == a.keys().size()) {
                    bi = 0;
                    si++;
                }
                return r;
            }
        };
        return new Export(rows, it);
    }

    // ------------------------------------------------------------------ форматы

    public static String csvLine(Row r) {
        return r.horizon() + ';' + r.route() + ';' + r.direction() + ';' + r.seq() + ';' + r.stopId() + ';'
                + csvText(r.stopName()) + ';' + r.period() + ';'
                + num(r.base()) + ';' + num(r.pred()) + ';' + (r.lo() == null ? "" : num(r.lo())) + ';'
                + (r.hi() == null ? "" : num(r.hi())) + '\n';
    }

    public static String csvHeader() {
        return String.join(";", HEADER) + '\n';
    }

    private static String csvText(String s) {
        if (s.indexOf(';') >= 0 || s.indexOf('"') >= 0 || s.indexOf('\n') >= 0) {
            return '"' + s.replace("\"", "\"\"") + '"';
        }
        return s;
    }

    private static String num(double v) {
        if (v == Math.rint(v) && Math.abs(v) < 1e15) {
            return Long.toString((long) v);
        }
        return Double.toString(v);
    }

    public void writeXlsx(Export e, ForecastQuery q, OutputStream out) {
        try {
            Workbook wb = new Workbook(out, "tram-forecast", "1.0");
            Worksheet ws = wb.newWorksheet("Прогноз");
            for (int c = 0; c < HEADER.length; c++) {
                ws.value(0, c, HEADER[c]);
                ws.style(0, c).bold().set();
            }
            int r = 1;
            for (Row row : e.iterable()) {
                ws.value(r, 0, row.horizon());
                ws.value(r, 1, row.route());
                ws.value(r, 2, row.direction());
                ws.value(r, 3, row.seq());
                ws.value(r, 4, row.stopId());
                ws.value(r, 5, row.stopName());
                ws.value(r, 6, row.period());
                ws.value(r, 7, row.base());
                ws.value(r, 8, row.pred());
                if (row.lo() != null) {
                    ws.value(r, 9, row.lo());
                    ws.value(r, 10, row.hi());
                }
                if (r % 10_000 == 0) {
                    ws.flush();
                }
                r++;
            }
            ws.freezePane(0, 1);
            Worksheet info = wb.newWorksheet("Параметры");
            String[][] kv = {
                    {"Горизонт", q.horizon().label},
                    {"Период", q.from() + " … " + q.to()},
                    {"Часы", q.hourFrom() + "–" + q.hourTo()},
                    {"Гранулярность", q.granularity().id()},
                    {"Маршруты", q.routes().toString()},
                    {"Остановки", q.stops().isEmpty() ? "все / маршрут целиком" : String.join(", ", q.stops())},
                    {"Участок", q.segment() == null ? "—" : q.segment()},
                    {"Корректирующие коэффициенты", q.scenario().isEmpty() ? "нет" : "да (pred — с поправками)"},
                    {"Единица", "посадки (успешные валидации)"},
                    {"Модель", "структурная модель, WAPE-score 0.9125 на лидерборде"},
            };
            for (int i = 0; i < kv.length; i++) {
                info.value(i, 0, kv[i][0]);
                info.value(i, 1, kv[i][1]);
            }
            wb.finish();
        } catch (IOException ex) {
            throw new UncheckedIOException(ex);
        }
    }
}
