package ru.mostrans.tram.ingest;

import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.LongAdder;

import org.springframework.stereotype.Service;

import ru.mostrans.tram.api.ApiException;
import ru.mostrans.tram.data.Csv;
import ru.mostrans.tram.data.DataStore;
import ru.mostrans.tram.forecast.ForecastEngine;
import ru.mostrans.tram.forecast.Horizon;

/**
 * Приём сырых валидаций (формат train.csv/test.csv) и нормализация по правилам датасета:
 * посадка = validation_result == 1, время — tran_date_time, маршрут — число из ngpt_route.
 * Агрегаты (маршрут, дата, час) — в памяти, потокобезопасно; мониторинг сравнивает их с прогнозом.
 */
@Service
public class IngestService {

    private static final DateTimeFormatter TS = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss");

    private final DataStore data;
    private final ForecastEngine engine;
    private final Map<Long, LongAdder> cells = new ConcurrentHashMap<>();
    private final LongAdder totalReceived = new LongAdder();
    private final LongAdder totalBoardings = new LongAdder();

    public IngestService(DataStore data, ForecastEngine engine) {
        this.data = data;
        this.engine = engine;
    }

    public record Stats(long received, long accepted, long rejected, long boardings, int cells) {
    }

    public record Validation(String tranDateTime, Integer validationResult, String ngptRoute) {
    }

    /** Разбор CSV построчно (заголовок — первая строка); состояние разбора одного запроса. */
    public final class CsvBatch {
        private int iTs = -1;
        private int iRes = -1;
        private int iRoute = -1;
        private char sep = ';';
        private long received;
        private long accepted;
        private long rejected;
        private long boardings;

        public void line(String raw) {
            String line = raw.endsWith("\r") ? raw.substring(0, raw.length() - 1) : raw;
            if (line.isBlank()) {
                return;
            }
            if (iTs < 0) {
                header(line);
                return;
            }
            received++;
            List<String> f = Csv.split(line, sep);  // поля могут быть в кавычках
            int max = Math.max(iTs, Math.max(iRes, iRoute));
            if (f.size() <= max) {
                rejected++;
                return;
            }
            Integer res = parseInt(f.get(iRes).trim());
            int r = record(f.get(iTs).trim(), res, f.get(iRoute).trim());
            if (r < 0) {
                rejected++;
            } else {
                accepted++;
                boardings += r;
            }
        }

        private void header(String line) {
            String h = line.startsWith("﻿") ? line.substring(1) : line;
            sep = h.indexOf(';') >= 0 ? ';' : ',';
            String[] cols = h.split(String.valueOf(sep));
            for (int k = 0; k < cols.length; k++) {
                switch (cols[k].trim().replace("\"", "")) {
                    case "tran_date_time" -> iTs = k;
                    case "validation_result" -> iRes = k;
                    case "ngpt_route" -> iRoute = k;
                    default -> {
                    }
                }
            }
            if (iTs < 0 || iRes < 0 || iRoute < 0) {
                throw ApiException.badRequest("body", "В заголовке CSV нужны колонки tran_date_time, "
                        + "validation_result, ngpt_route (формат train.csv, разделитель «;»)");
            }
        }

        public Stats finish() {
            if (iTs < 0) {
                throw ApiException.badRequest("body", "Пустое тело: ожидается CSV с заголовком (формат train.csv)");
            }
            totalReceived.add(received);
            return new Stats(received, accepted, rejected, boardings, cells.size());
        }
    }

    public CsvBatch csvBatch() {
        return new CsvBatch();
    }

    public Stats ingestJson(List<Validation> items) {
        long acc = 0;
        long rej = 0;
        long b = 0;
        for (Validation v : items) {
            int r = v == null ? -1 : record(v.tranDateTime(), v.validationResult(), v.ngptRoute());
            if (r < 0) {
                rej++;
            } else {
                acc++;
                b += r;
            }
        }
        totalReceived.add(items.size());
        return new Stats(items.size(), acc, rej, b, cells.size());
    }

    /** @return 1 — посадка учтена, 0 — валидная запись-отказ, −1 — запись не разобрана. */
    int record(String ts, Integer result, String route) {
        if (ts == null || result == null || route == null) {
            return -1;
        }
        LocalDateTime t;
        try {
            t = LocalDateTime.parse(ts.length() > 19 ? ts.substring(0, 19) : ts, TS);
        } catch (DateTimeParseException e) {
            return -1;
        }
        int sp = route.indexOf(' ');
        Integer r = parseInt(sp > 0 ? route.substring(0, sp) : route);
        if (r == null || !data.hasRoute(r)) {
            return -1;
        }
        LocalDate day = t.toLocalDate();
        if (day.isBefore(DataStore.HIST_START) || day.isAfter(DataStore.YEAR_END)) {
            return -1; // вне периода сервиса: не храним (защита памяти от произвольных дат)
        }
        if (result != 1) {
            return 0;
        }
        cells.computeIfAbsent(key(r, t.toLocalDate(), t.getHour()), x -> new LongAdder()).increment();
        totalBoardings.increment();
        return 1;
    }

    private static Integer parseInt(String s) {
        try {
            return Integer.parseInt(s);
        } catch (NumberFormatException e) {
            return null;
        }
    }

    private static long key(int route, LocalDate d, int hour) {
        return (d.toEpochDay() * 24 + hour) * 1000 + route;
    }

    public void reset() {
        cells.clear();
        totalReceived.reset();
        totalBoardings.reset();
    }

    // ------------------------------------------------------------------ мониторинг

    public record Cell(int route, int hour, long actual, Double forecast) {
    }

    public record Monitoring(LocalDate date, String forecastSource, Integer lastHour, List<Integer> hoursCovered,
                             List<Cell> cells, long actualTotal, Double forecastTotal, long coveredActual,
                             Double coveredForecast, Double wapeScore, long receivedTotal, long boardingsTotal,
                             List<Alert> alerts, AlertParams alertParams) {
    }

    /** Алерт детектора смены режима: факт маршрута отклоняется от прогноза сильнее порога N часов подряд. */
    public record Alert(int route, int fromHour, int toHour, int hours, long actual, double forecast,
                        double deviationPct, String kind, String message) {
    }

    public record AlertParams(double threshold, int minHours, double minForecast) {
    }

    /** Ячейки с прогнозом меньше этого числа посадок не оцениваются: относительный шум малых чисел велик. */
    public static final double ALERT_MIN_FORECAST = 30;
    public static final double DEFAULT_THRESHOLD = 0.25;
    public static final int DEFAULT_MIN_HOURS = 2;

    /**
     * Факт против прогноза на дату. WAPE-score считается только по «принятым» часам — где факт по сети
     * набрал не меньше половины прогноза: незавершённый поток и единичные запоздавшие записи не штрафуются.
     */
    public Monitoring monitoring(LocalDate d, double threshold, int minHours) {
        Horizon h = Horizon.ofDate(d);
        long[][] actual = new long[DataStore.ROUTES.length][24];
        double[][] fcst = new double[DataStore.ROUTES.length][24];
        boolean[][] hasFc = new boolean[DataStore.ROUTES.length][24];
        long[] netAct = new long[24];
        double[] netFc = new double[24];
        int lastHour = -1;
        for (int i = 0; i < DataStore.ROUTES.length; i++) {
            int route = DataStore.ROUTES[i];
            int k = data.indexOf(route);
            for (int hour = 0; hour < 24; hour++) {
                LongAdder a = cells.get(key(route, d, hour));
                actual[i][hour] = a == null ? 0 : a.sum();
                if (a != null) {
                    lastHour = Math.max(lastHour, hour);
                }
                if (h == Horizon.DAY || h == Horizon.YEAR) {
                    fcst[i][hour] = Math.round(engine.base(h, k, d, hour) * 10.0) / 10.0;
                    hasFc[i][hour] = true;
                } else if (h == Horizon.HISTORY) {
                    fcst[i][hour] = data.historyAt(k, d, hour); // эталонный факт: сверка приёма
                    hasFc[i][hour] = true;
                }
                netAct[hour] += actual[i][hour];
                netFc[hour] += fcst[i][hour];
            }
        }
        List<Integer> covered = new ArrayList<>();
        for (int hour = 0; hour < 24; hour++) {
            // 1.11 00–01 ч — в прогнозе уже факт (хвост test.csv): сравнение было бы тождественным, не оцениваем
            boolean factInForecast = d.equals(DataStore.FC_START) && hour <= 1;
            if (!factInForecast && netAct[hour] > 0 && netAct[hour] >= 0.5 * netFc[hour]) {
                covered.add(hour);
            }
        }
        List<Cell> out = new ArrayList<>();
        long act = 0;
        double fc = 0;
        long covAct = 0;
        double covFc = 0;
        double absErr = 0;
        double actOnForecast = 0;
        for (int i = 0; i < DataStore.ROUTES.length; i++) {
            for (int hour = 0; hour < 24; hour++) {
                Double f = hasFc[i][hour] ? fcst[i][hour] : null;
                act += actual[i][hour];
                fc += f == null ? 0 : f;
                if (actual[i][hour] == 0 && (f == null || f == 0)) {
                    continue;
                }
                out.add(new Cell(DataStore.ROUTES[i], hour, actual[i][hour], f));
                if (covered.contains(hour) && f != null) {
                    covAct += actual[i][hour];
                    covFc += f;
                    absErr += Math.abs(actual[i][hour] - f);
                    actOnForecast += actual[i][hour];
                }
            }
        }
        Double score = actOnForecast > 0 ? Math.max(0, 1 - absErr / actOnForecast) : null;
        String src = h == null ? "нет" : switch (h) {
            case HISTORY -> "эталонный факт labels (сверка приёма)";
            case YEAR -> "сценарий 2026";
            default -> "прогноз модели";
        };
        List<Alert> alerts = detect(actual, fcst, hasFc, covered, threshold, minHours);
        return new Monitoring(d, src, lastHour < 0 ? null : lastHour, covered, out, act,
                h == null ? null : Math.round(fc * 10.0) / 10.0, covAct,
                h == null ? null : Math.round(covFc * 10.0) / 10.0,
                score == null ? null : Math.round(score * 10000.0) / 10000.0, totalReceived.sum(),
                totalBoardings.sum(), alerts, new AlertParams(threshold, minHours, ALERT_MIN_FORECAST));
    }

    /**
     * Детектор смены режима: по каждому маршруту ищет серии подряд идущих «принятых» часов, где факт отклоняется
     * от прогноза больше порога в одну сторону. Падение — вероятный ремонт, перекрытие, сход с линии; рост —
     * перераспределение пассажиров (закрыт параллельный маршрут) или массовое событие. Модель режимов не знает,
     * поэтому такой сигнал — повод внести режим в regimes.json и пересчитать прогноз.
     */
    static List<Alert> detect(long[][] actual, double[][] fcst, boolean[][] hasFc, List<Integer> covered,
                              double threshold, int minHours) {
        List<Alert> out = new ArrayList<>();
        for (int i = 0; i < DataStore.ROUTES.length; i++) {
            int start = -1;
            int sign = 0;
            for (int hour = 0; hour <= 24; hour++) {
                int s = 0;
                if (hour < 24 && covered.contains(hour) && hasFc[i][hour] && fcst[i][hour] >= ALERT_MIN_FORECAST) {
                    double dev = (actual[i][hour] - fcst[i][hour]) / fcst[i][hour];
                    s = dev <= -threshold ? -1 : dev >= threshold ? 1 : 0;
                }
                if (s != 0 && s == sign) {
                    continue;
                }
                if (sign != 0 && hour - start >= minHours) {
                    out.add(alert(DataStore.ROUTES[i], start, hour - 1, actual[i], fcst[i], sign));
                }
                start = hour;
                sign = s;
            }
        }
        out.sort((a, b) -> Double.compare(Math.abs(b.deviationPct()), Math.abs(a.deviationPct())));
        return out;
    }

    private static Alert alert(int route, int from, int to, long[] actual, double[] fcst, int sign) {
        long a = 0;
        double f = 0;
        for (int h = from; h <= to; h++) {
            a += actual[h];
            f += fcst[h];
        }
        double dev = Math.round((a - f) / f * 1000.0) / 10.0;
        int hours = to - from + 1;
        String span = String.format("%02d:00–%02d:00", from, to + 1);
        String msg = sign < 0
                ? String.format(java.util.Locale.forLanguageTag("ru"), "Маршрут %d: факт ниже прогноза на %.0f %% %d ч подряд (%s) — "
                        + "вероятно ремонт, перекрытие или сход с линии. Проверьте оперативную обстановку; при "
                        + "подтверждении внесите режим в regimes.json и пересчитайте прогноз.", route, -dev, hours, span)
                : String.format(java.util.Locale.forLanguageTag("ru"), "Маршрут %d: факт выше прогноза на %.0f %% %d ч подряд (%s) — "
                        + "вероятно перераспределение пассажиров (закрыт параллельный маршрут) или массовое событие. "
                        + "Проверьте наполнение и при необходимости усильте выпуск.", route, dev, hours, span);
        return new Alert(route, from, to, hours, a, Math.round(f * 10.0) / 10.0, dev, sign < 0 ? "drop" : "surge", msg);
    }

    // ------------------------------------------------------------------ живой факт для карты

    public record LiveRoute(int route, long[] hourly) {
    }

    public record Live(LocalDate date, boolean available, Integer lastHour, long receivedTotal, List<LiveRoute> routes,
                       List<Alert> alerts) {
    }

    /** Принятый факт по маршрутам и часам на дату + алерты детектора (порог по умолчанию) — для карты «live». */
    public Live live(LocalDate d) {
        List<LiveRoute> routes = new ArrayList<>();
        int last = -1;
        for (int route : DataStore.ROUTES) {
            long[] h = new long[24];
            for (int hour = 0; hour < 24; hour++) {
                LongAdder a = cells.get(key(route, d, hour));
                if (a != null) {
                    h[hour] = a.sum();
                    last = Math.max(last, hour);
                }
            }
            routes.add(new LiveRoute(route, h));
        }
        List<Alert> alerts = last < 0 || Horizon.ofDate(d) == null ? List.of()
                : monitoring(d, DEFAULT_THRESHOLD, DEFAULT_MIN_HOURS).alerts();
        return new Live(d, last >= 0, last < 0 ? null : last, totalReceived.sum(), routes, alerts);
    }

    // ------------------------------------------------------------------ симуляция потока (демо детектора)

    /**
     * Симуляция потока валидаций на дату: посадки = прогноз (для истории — факт) × шум ±5 %, с внедрённой
     * аномалией (множитель на маршрут в часах). Нужна для демонстрации детектора: реальных данных за
     * прогнозный период нет. Результат помечается simulated=true.
     */
    public Stats simulate(LocalDate d, int toHour, Integer anomalyRoute, int anomalyFrom, int anomalyTo,
                          double anomalyMult, long seed) {
        Horizon h = Horizon.ofDate(d);
        if (h == null) {
            throw ApiException.badRequest("date", "Дата " + d + " вне периода сервиса (" + DataStore.HIST_START
                    + " … " + DataStore.YEAR_END + ")");
        }
        java.util.Random rnd = new java.util.Random(seed);
        double[] row = new double[24];
        long boardings = 0;
        for (int route : DataStore.ROUTES) {
            engine.day(h, data.indexOf(route), d, row);
            for (int hour = 0; hour <= toHour; hour++) {
                double v = row[hour] * (1 + 0.05 * rnd.nextGaussian());
                if (anomalyRoute != null && anomalyRoute == route && hour >= anomalyFrom && hour <= anomalyTo) {
                    v *= anomalyMult;
                }
                long n = Math.max(0, Math.round(v));
                if (n > 0) {
                    cells.computeIfAbsent(key(route, d, hour), x -> new LongAdder()).add(n);
                    boardings += n;
                }
            }
        }
        totalReceived.add(boardings);
        totalBoardings.add(boardings);
        return new Stats(boardings, boardings, 0, boardings, cells.size());
    }
}
