package ru.mostrans.tram.data;

import java.nio.file.Files;
import java.nio.file.Path;
import java.time.LocalDate;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * Неизменяемое in-memory хранилище данных сервиса (результат pipeline/build_service_data.py).
 * Все ряды — плоские массивы double: [индекс маршрута][день · 24 + час]; ~2 МБ на весь набор.
 */
public final class DataStore {

    private static final Logger log = LoggerFactory.getLogger(DataStore.class);

    public static final int[] ROUTES = {1, 5, 7, 11, 12, 17, 25, 26, 28, 50};
    public static final LocalDate HIST_START = LocalDate.of(2025, 1, 1);
    public static final LocalDate HIST_END = LocalDate.of(2025, 10, 31);
    public static final LocalDate FC_START = LocalDate.of(2025, 11, 1);
    public static final LocalDate FC_END = LocalDate.of(2025, 12, 31);
    public static final LocalDate YEAR_START = LocalDate.of(2026, 1, 1);
    public static final LocalDate YEAR_END = LocalDate.of(2026, 12, 31);
    /** Окно, по которому строится почасовая форма суток для горизонта «год» (обычные недели ноября–декабря). */
    private static final LocalDate PROFILE_FROM = LocalDate.of(2025, 11, 17);
    private static final LocalDate PROFILE_TO = LocalDate.of(2025, 12, 28);

    private final Map<Integer, Integer> routeIndex = new HashMap<>();
    private final double[][] forecast;      // 61 день × 24
    private final double[][] history;       // 304 дня × 24
    private final double[][] historyVeh;    // вагонов в час, факт
    private final double[][] yearDaily;     // 365 дней
    private final double[][][] yearProfile; // [маршрут][0 — рабочий, 1 — выходной][час], сумма = 1
    private final Map<LocalDate, Integer> calendar = new HashMap<>();
    private final Map<LocalDate, double[]> weather = new HashMap<>();  // {осадки мм, температура °C}
    private final double[] monthNormLog = new double[13];              // средний ln(1+мм) по месяцу
    private final double[] monthNormMm = new double[13];
    private final List<Stop> stops = new ArrayList<>();
    private final Map<Integer, List<Stop>> stopsByRoute = new TreeMap<>();
    private final Map<String, List<Stop>> stopById = new LinkedHashMap<>();
    private final Map<Integer, Map<String, Object>> routeInfo = new LinkedHashMap<>();
    private final JsonNode meta;
    private final List<Map<String, Object>> fleetSummary;
    private final List<Map<String, Object>> fleetCandidates;
    private final List<Map<String, Object>> fleetReserve;
    private final List<Map<String, Object>> errorByHorizon;
    private final List<Map<String, Object>> errorByLevel;
    private final List<Map<String, Object>> errorByRoute;

    public DataStore(Path dir, ObjectMapper json) {
        if (!Files.isDirectory(dir)) {
            throw new IllegalStateException("Каталог данных не найден: " + dir.toAbsolutePath()
                    + " — запустите pipeline/build_service_data.py или задайте APP_DATA_DIR");
        }
        long t0 = System.nanoTime();
        for (int k = 0; k < ROUTES.length; k++) {
            routeIndex.put(ROUTES[k], k);
        }
        int n = ROUTES.length;
        forecast = new double[n][days(FC_START, FC_END) * 24];
        history = new double[n][days(HIST_START, HIST_END) * 24];
        historyVeh = new double[n][days(HIST_START, HIST_END) * 24];
        yearDaily = new double[n][days(YEAR_START, YEAR_END)];
        yearProfile = new double[n][2][24];

        for (Csv.Row r : Csv.read(dir.resolve("forecast_hourly.csv"))) {
            put(forecast, r.i("route"), FC_START, LocalDate.parse(r.str("date")), r.i("hour"), r.d("pred"));
        }
        for (Csv.Row r : Csv.read(dir.resolve("history_hourly.csv"))) {
            LocalDate d = LocalDate.parse(r.str("date"));
            put(history, r.i("route"), HIST_START, d, r.i("hour"), r.d("boardings"));
            put(historyVeh, r.i("route"), HIST_START, d, r.i("hour"), r.d("n_veh"));
        }
        for (Csv.Row r : Csv.read(dir.resolve("forecast_year_daily.csv"))) {
            Integer k = routeIndex.get(r.i("route"));
            LocalDate d = LocalDate.parse(r.str("date"));
            if (k != null && !d.isBefore(YEAR_START) && !d.isAfter(YEAR_END)) {
                yearDaily[k][(int) ChronoUnit.DAYS.between(YEAR_START, d)] = r.d("pred");
            }
        }
        for (Csv.Row r : Csv.read(dir.resolve("calendar.csv"))) {
            calendar.put(LocalDate.parse(r.str("date")), r.i("code"));
        }
        double[] sumLog = new double[13];
        double[] sumMm = new double[13];
        int[] cnt = new int[13];
        for (Csv.Row r : Csv.read(dir.resolve("weather_daily.csv"))) {
            LocalDate d = LocalDate.parse(r.str("date"));
            double mm = r.d("precip_mm");
            double t = r.index().containsKey("temp_c") ? r.d("temp_c") : Double.NaN;
            weather.put(d, new double[]{mm, t});
            if (!Double.isNaN(mm)) {
                sumLog[d.getMonthValue()] += Math.log1p(mm);
                sumMm[d.getMonthValue()] += mm;
                cnt[d.getMonthValue()]++;
            }
        }
        for (int m = 1; m <= 12; m++) {
            monthNormLog[m] = cnt[m] > 0 ? sumLog[m] / cnt[m] : 0.0;
            monthNormMm[m] = cnt[m] > 0 ? sumMm[m] / cnt[m] : 0.0;
        }
        List<Csv.Row> stopRows = Csv.read(dir.resolve("stops.csv"));
        Map<Integer, Double> weightSum = new HashMap<>();
        for (Csv.Row r : stopRows) {
            weightSum.merge(r.i("route"), r.d("weight"), Double::sum);
        }
        for (Csv.Row r : stopRows) {
            int route = r.i("route");
            if (!routeIndex.containsKey(route) || weightSum.get(route) <= 0) {
                continue;
            }
            // нормировка: сумма долей остановок маршрута ровно 1 (в CSV веса округлены)
            Stop s = new Stop(r.str("stop_id"), route, r.i("direction"), r.i("seq"), r.str("stop_name"),
                    r.d("lat"), r.d("lon"), r.d("weight") / weightSum.get(route));
            stops.add(s);
            stopsByRoute.computeIfAbsent(s.route(), x -> new ArrayList<>()).add(s);
            stopById.computeIfAbsent(s.stopId(), x -> new ArrayList<>()).add(s);
        }
        stopsByRoute.values().forEach(l -> l.sort((a, b) -> a.direction() != b.direction()
                ? Integer.compare(a.direction(), b.direction()) : Integer.compare(a.seq(), b.seq())));
        buildYearProfile();

        meta = json.readTree(dir.resolve("meta.json").toFile());
        for (JsonNode rn : meta.get("routes")) {
            routeInfo.put(rn.get("route").asInt(), json.convertValue(rn, Map.class));
        }
        fleetSummary = Csv.readTable(dir.resolve("fleet_summary.csv"));
        fleetCandidates = Csv.readTable(dir.resolve("fleet_reinforcement.csv"));
        fleetReserve = Csv.readTable(dir.resolve("fleet_reserve.csv"));
        errorByHorizon = Csv.readTable(dir.resolve("model_error_by_horizon.csv"));
        errorByLevel = Csv.readTable(dir.resolve("model_error_by_level.csv"));
        errorByRoute = Csv.readTable(dir.resolve("model_error_by_route.csv"));
        log.info("Данные загружены из {} за {} мс: прогноз {} ячеек, история {} ячеек, остановок {} ({} маршрутов)",
                dir.toAbsolutePath(), (System.nanoTime() - t0) / 1_000_000, n * forecast[0].length,
                n * history[0].length, stops.size(), stopsByRoute.size());
    }

    private void buildYearProfile() {
        for (int k = 0; k < ROUTES.length; k++) {
            double[][] acc = new double[2][24];
            for (LocalDate d = PROFILE_FROM; !d.isAfter(PROFILE_TO); d = d.plusDays(1)) {
                int off = isOff(d) ? 1 : 0;
                for (int h = 0; h < 24; h++) {
                    acc[off][h] += forecastAt(k, d, h);
                }
            }
            for (int off = 0; off < 2; off++) {
                double s = Arrays.stream(acc[off]).sum();
                for (int h = 0; h < 24; h++) {
                    yearProfile[k][off][h] = s > 0 ? acc[off][h] / s : 1.0 / 24;
                }
            }
        }
    }

    private void put(double[][] grid, int route, LocalDate start, LocalDate d, int hour, double v) {
        Integer k = routeIndex.get(route);
        if (k == null || hour < 0 || hour > 23 || Double.isNaN(v)) {
            return;
        }
        long day = ChronoUnit.DAYS.between(start, d);
        int idx = (int) day * 24 + hour;
        if (day >= 0 && idx < grid[k].length) {
            grid[k][idx] = v;
        }
    }

    public static int days(LocalDate from, LocalDate to) {
        return (int) ChronoUnit.DAYS.between(from, to) + 1;
    }

    // ------------------------------------------------------------------ доступ

    public int indexOf(int route) {
        Integer k = routeIndex.get(route);
        if (k == null) {
            throw new IllegalArgumentException("unknown route " + route);
        }
        return k;
    }

    public boolean hasRoute(int route) {
        return routeIndex.containsKey(route);
    }

    public double forecastAt(int k, LocalDate d, int h) {
        return forecast[k][(int) ChronoUnit.DAYS.between(FC_START, d) * 24 + h];
    }

    public double historyAt(int k, LocalDate d, int h) {
        return history[k][(int) ChronoUnit.DAYS.between(HIST_START, d) * 24 + h];
    }

    public double historyVehiclesAt(int k, LocalDate d, int h) {
        return historyVeh[k][(int) ChronoUnit.DAYS.between(HIST_START, d) * 24 + h];
    }

    public void forecastDay(int k, LocalDate d, double[] out) {
        System.arraycopy(forecast[k], (int) ChronoUnit.DAYS.between(FC_START, d) * 24, out, 0, 24);
    }

    public void historyDay(int k, LocalDate d, double[] out) {
        System.arraycopy(history[k], (int) ChronoUnit.DAYS.between(HIST_START, d) * 24, out, 0, 24);
    }

    public void yearDay(int k, LocalDate d, double[] out) {
        double daily = yearDailyAt(k, d);
        double[] prof = yearProfile[k][isOff(d) ? 1 : 0];
        for (int h = 0; h < 24; h++) {
            out[h] = daily * prof[h];
        }
    }

    public double yearDailyAt(int k, LocalDate d) {
        return yearDaily[k][(int) ChronoUnit.DAYS.between(YEAR_START, d)];
    }

    public double yearAt(int k, LocalDate d, int h) {
        return yearDailyAt(k, d) * yearProfile[k][isOff(d) ? 1 : 0][h];
    }

    /** Код дня: 0 — рабочий, 1 — выходной/праздник, 2 — сокращённый (isdayoff.ru). */
    public int dayCode(LocalDate d) {
        Integer c = calendar.get(d);
        if (c != null) {
            return c;
        }
        return d.getDayOfWeek().getValue() >= 6 ? 1 : 0;
    }

    public boolean isOff(LocalDate d) {
        return dayCode(d) == 1;
    }

    public static String dayLabel(int code) {
        return switch (code) {
            case 1 -> "выходной";
            case 2 -> "сокращённый";
            default -> "рабочий";
        };
    }

    /** {осадки мм, температура °C} или null, если погоды на дату нет. */
    public double[] weather(LocalDate d) {
        return weather.get(d);
    }

    public double monthNormLog(int month) {
        return monthNormLog[month];
    }

    public double monthNormMm(int month) {
        return monthNormMm[month];
    }

    public List<Stop> stops() {
        return Collections.unmodifiableList(stops);
    }

    public List<Stop> stopsOf(int route) {
        return stopsByRoute.getOrDefault(route, List.of());
    }

    /** Все вхождения остановки (по маршрутам и направлениям); пусто, если остановки нет. */
    public List<Stop> stopEntries(String id) {
        return stopById.getOrDefault(id, List.of());
    }

    public Map<String, Object> routeInfo(int route) {
        return routeInfo.getOrDefault(route, Map.of());
    }

    public String routeColor(int route) {
        Object c = routeInfo(route).get("color");
        return c == null ? "#555555" : c.toString();
    }

    public String routeName(int route) {
        Object c = routeInfo(route).get("name");
        return c == null ? "" : c.toString();
    }

    public JsonNode meta() {
        return meta;
    }

    public List<Map<String, Object>> fleetSummary() {
        return fleetSummary;
    }

    public List<Map<String, Object>> fleetCandidates() {
        return fleetCandidates;
    }

    public List<Map<String, Object>> fleetReserve() {
        return fleetReserve;
    }

    public List<Map<String, Object>> errorByHorizon() {
        return errorByHorizon;
    }

    public List<Map<String, Object>> errorByLevel() {
        return errorByLevel;
    }

    public List<Map<String, Object>> errorByRoute() {
        return errorByRoute;
    }
}
