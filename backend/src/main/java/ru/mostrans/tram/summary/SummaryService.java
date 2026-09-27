package ru.mostrans.tram.summary;

import java.io.IOException;
import java.io.OutputStream;
import java.io.UncheckedIOException;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;

import org.dhatim.fastexcel.Workbook;
import org.dhatim.fastexcel.Worksheet;
import org.springframework.stereotype.Service;
import org.springframework.util.MultiValueMap;

import ru.mostrans.tram.api.ApiException;
import ru.mostrans.tram.data.DataStore;
import ru.mostrans.tram.data.Stop;
import ru.mostrans.tram.forecast.ForecastEngine;
import ru.mostrans.tram.forecast.Horizon;
import ru.mostrans.tram.forecast.Scenario;
import tools.jackson.databind.JsonNode;

/**
 * Сводка диспетчера на день: пик сети, маршруты с изменением к прошлой неделе, где усиливать выпуск, самые
 * загруженные остановки, действующие события и список рекомендаций. Собирается из тех же рядов, что и прогноз.
 */
@Service
public class SummaryService {

    private static final int TOP_STOPS = 10;
    private static final java.util.Locale RU = java.util.Locale.forLanguageTag("ru");

    private final DataStore data;
    private final ForecastEngine engine;

    public SummaryService(DataStore data, ForecastEngine engine) {
        this.data = data;
        this.engine = engine;
    }

    public record DayType(int code, String label) {
    }

    public record Weather(Double precipMm, Double tempC) {
    }

    public record Network(double total, int peakHour, double peakValue, double[] hourly, LocalDate weekAgoDate,
                          Double weekAgoTotal, Double deltaWeekPct) {
    }

    public record Reinforce(int hour, double pred, double nPlan, double norm, int extra) {
    }

    public record Cut(int hour, double pred, double nPlan, double norm, int need, int reserve) {
    }

    public record RouteDay(int route, String name, String color, double total, int peakHour, double peakValue,
                           double[] hourly, Double weekAgoTotal, Double deltaWeekPct, int extraVehicleHours,
                           List<Reinforce> reinforceHours, int reserveVehicleHours, List<Cut> reserveHours) {
    }

    public record HotStop(String stopId, int route, int direction, int seq, String name, int hour, double value,
                          double dayTotal) {
    }

    public record Fleet(boolean available, int candidateHours, int extraVehicleHours, int reserveHours,
                        int reserveVehicleHours) {
    }

    public record Event(String kind, String name, List<Integer> routes, String note, List<String> source) {
    }

    public record Summary(LocalDate date, String mode, DayType dayType, Weather weather, Network network,
                          List<RouteDay> routes, List<HotStop> hotStops, Fleet fleet, List<Event> events,
                          List<String> recommendations, boolean scenario, double computeMs) {
    }

    public Summary summary(MultiValueMap<String, String> p) {
        long t0 = System.nanoTime();
        String ds = p.getFirst("date");
        if (ds == null || ds.isBlank()) {
            throw ApiException.badRequest("date", "Параметр date обязателен (YYYY-MM-DD)");
        }
        LocalDate d;
        try {
            d = LocalDate.parse(ds.trim());
        } catch (java.time.format.DateTimeParseException e) {
            throw ApiException.badRequest("date", "Параметр date: «" + ds + "» — не дата формата YYYY-MM-DD");
        }
        Horizon h = Horizon.ofDate(d);
        if (h == null) {
            throw ApiException.badRequest("date", "Дата " + d + " вне доступных периодов: "
                    + DataStore.HIST_START + " … " + DataStore.YEAR_END);
        }
        Scenario sc = Scenario.parse(p);
        if (h == Horizon.HISTORY && !sc.isEmpty()) {
            throw ApiException.badRequest("date", "Корректирующие коэффициенты не применяются к истории (факту)");
        }
        double df = sc.isEmpty() ? 1.0 : sc.dayFactor(data, d);
        LocalDate weekAgo = comparableDay(d);
        Horizon hw = weekAgo == null ? null : Horizon.ofDate(weekAgo);

        // маршруты
        double[] net = new double[24];
        double netWeek = 0;
        boolean weekOk = hw != null;
        List<RouteDay> routes = new ArrayList<>();
        Map<Integer, List<Reinforce>> reinforce = reinforcements(d);
        Map<Integer, List<Cut>> cuts = reserves(d);
        double[] row = new double[24];
        for (int route : DataStore.ROUTES) {
            int k = data.indexOf(route);
            engine.day(h, k, d, row);
            double[] hourly = new double[24];
            double total = 0;
            int peak = 0;
            for (int hr = 0; hr < 24; hr++) {
                double v = sc.isEmpty() ? row[hr] : row[hr] * sc.factor(df, route, d, hr);
                hourly[hr] = r1(v);
                total += v;
                net[hr] += v;
                if (v > hourly[peak]) {
                    peak = hr;
                }
            }
            Double wTotal = null;
            if (weekOk) {
                engine.day(hw, k, weekAgo, row);
                double s = 0;
                for (double v : row) {
                    s += v;
                }
                wTotal = r1(s);
                netWeek += s;
            }
            List<Reinforce> rf = reinforce.getOrDefault(route, List.of());
            int extra = rf.stream().mapToInt(Reinforce::extra).sum();
            List<Cut> cut = cuts.getOrDefault(route, List.of());
            routes.add(new RouteDay(route, data.routeName(route), data.routeColor(route), r1(total), peak,
                    hourly[peak], hourly, wTotal, pct(wTotal, total), extra, rf,
                    cut.stream().mapToInt(Cut::reserve).sum(), cut));
        }
        routes.sort(Comparator.comparingDouble(RouteDay::total).reversed());
        int netPeak = 0;
        double netTotal = 0;
        for (int hr = 0; hr < 24; hr++) {
            netTotal += net[hr];
            if (net[hr] > net[netPeak]) {
                netPeak = hr;
            }
            net[hr] = r1(net[hr]);
        }
        Network network = new Network(r1(netTotal), netPeak, net[netPeak], net, weekOk ? weekAgo : null,
                weekOk ? r1(netWeek) : null, weekOk ? pct(netWeek, netTotal) : null);

        // самые загруженные остановки (оценка по доле остановки)
        List<HotStop> hot = new ArrayList<>();
        for (RouteDay rd : routes) {
            for (Stop s : data.stopsOf(rd.route())) {
                int hr = rd.peakHour();
                hot.add(new HotStop(s.stopId(), s.route(), s.direction(), s.seq(), s.name(), hr,
                        r1(rd.hourly()[hr] * s.weight()), r1(rd.total() * s.weight())));
            }
        }
        hot.sort(Comparator.comparingDouble(HotStop::value).reversed());
        hot = List.copyOf(hot.subList(0, Math.min(TOP_STOPS, hot.size())));

        boolean fleetOk = !d.isBefore(DataStore.FC_START) && !d.isAfter(DataStore.FC_END);
        int candHours = reinforce.values().stream().mapToInt(List::size).sum();
        int extraHours = routes.stream().mapToInt(RouteDay::extraVehicleHours).sum();
        Fleet fleet = new Fleet(fleetOk, candHours, extraHours, cuts.values().stream().mapToInt(List::size).sum(),
                routes.stream().mapToInt(RouteDay::reserveVehicleHours).sum());

        double[] w = data.weather(d);
        Weather weather = w == null ? null : new Weather(Double.isNaN(w[0]) ? null : w[0],
                Double.isNaN(w[1]) ? null : w[1]);
        List<Event> events = events(d);
        int code = data.dayCode(d);
        String mode = switch (h) {
            case HISTORY -> "history";
            case YEAR -> "year";
            default -> "forecast";
        };
        List<String> rec = recommendations(d, h, network, routes, hot, fleet, weather, events, !sc.isEmpty());
        return new Summary(d, mode, new DayType(code, DataStore.dayLabel(code)), weather, network, routes, hot, fleet,
                events, rec, !sc.isEmpty(), (System.nanoTime() - t0) / 1e6);
    }

    /** Сопоставимый день: ближайший прошлый день с тем же днём недели и тем же типом дня (до 8 недель назад). */
    private LocalDate comparableDay(LocalDate d) {
        for (int w = 1; w <= 8; w++) {
            LocalDate c = d.minusWeeks(w);
            if (Horizon.ofDate(c) != null && data.dayCode(c) == data.dayCode(d)) {
                return c;
            }
        }
        return null;
    }

    private Map<Integer, List<Reinforce>> reinforcements(LocalDate d) {
        Map<Integer, List<Reinforce>> out = new java.util.TreeMap<>();
        String ds = d.toString();
        for (Map<String, Object> m : data.fleetCandidates()) {
            if (!ds.equals(m.get("date"))) {
                continue;
            }
            int route = ((Number) m.get("route")).intValue();
            int extra = ((Number) m.get("extra")).intValue();
            if (extra <= 0) {
                continue;
            }
            out.computeIfAbsent(route, x -> new ArrayList<>()).add(new Reinforce(((Number) m.get("hour")).intValue(),
                    ((Number) m.get("pred")).doubleValue(), ((Number) m.get("nPlan")).doubleValue(),
                    ((Number) m.get("norm")).doubleValue(), extra));
        }
        out.values().forEach(l -> l.sort(Comparator.comparingInt(Reinforce::hour)));
        return out;
    }

    private Map<Integer, List<Cut>> reserves(LocalDate d) {
        Map<Integer, List<Cut>> out = new java.util.TreeMap<>();
        String ds = d.toString();
        for (Map<String, Object> m : data.fleetReserve()) {
            if (!ds.equals(m.get("date"))) {
                continue;
            }
            out.computeIfAbsent(((Number) m.get("route")).intValue(), x -> new ArrayList<>()).add(new Cut(
                    ((Number) m.get("hour")).intValue(), ((Number) m.get("pred")).doubleValue(),
                    ((Number) m.get("nPlan")).doubleValue(), ((Number) m.get("norm")).doubleValue(),
                    ((Number) m.get("need")).intValue(), ((Number) m.get("reserve")).intValue()));
        }
        out.values().forEach(l -> l.sort(Comparator.comparingInt(Cut::hour)));
        return out;
    }

    private List<Event> events(LocalDate d) {
        List<Event> out = new ArrayList<>();
        JsonNode m = data.meta();
        for (JsonNode e : m.get("events")) {
            if (inRange(d, e)) {
                out.add(new Event("event", text(e, "name"), ints(e.get("routes")), text(e, "note"), strs(e.get("source"))));
            }
        }
        for (JsonNode r : m.get("regimes")) {
            boolean offOnly = "offdays".equals(text(r, "days"));
            if (inRange(d, r) && (!offOnly || data.isOff(d))) {
                String state = text(r, "state");
                String name = "Маршрут " + r.get("route").asInt() + ": " + switch (state == null ? "" : state) {
                    case "closed" -> "не ходит";
                    case "short" -> "укорочен";
                    case "exclude" -> "перестройка сети";
                    default -> state;
                };
                out.add(new Event("regime", name, List.of(r.get("route").asInt()), text(r, "note"),
                        strs(r.get("source"))));
            }
        }
        return out;
    }

    private List<String> recommendations(LocalDate d, Horizon h, Network net, List<RouteDay> routes,
                                         List<HotStop> hot, Fleet fleet, Weather weather, List<Event> events,
                                         boolean scenario) {
        List<String> r = new ArrayList<>();
        String what = h == Horizon.HISTORY ? "факт" : h == Horizon.YEAR ? "сценарий 2026" : "прогноз";
        r.add(String.format(RU, "Пик сети — %02d:00: %s посадок (%s %% суток, %s).", net.peakHour(),
                num(net.peakValue()), num1(100 * net.peakValue() / Math.max(net.total(), 1)), what));
        if (fleet.available()) {
            List<RouteDay> byExtra = routes.stream().filter(x -> x.extraVehicleHours() > 0)
                    .sorted(Comparator.comparingInt(RouteDay::extraVehicleHours).reversed()).limit(5).toList();
            if (byExtra.isEmpty()) {
                r.add("Усиление выпуска не требуется: прогноз посадок на вагон нигде не превышает норму маршрута.");
            }
            for (RouteDay x : byExtra) {
                StringBuilder sb = new StringBuilder("Маршрут " + x.route() + ": усилить выпуск — ");
                List<Reinforce> hrs = x.reinforceHours();
                for (int i = 0; i < Math.min(hrs.size(), 6); i++) {
                    sb.append(i == 0 ? "" : ", ").append(hrs.get(i).hour()).append(":00 +").append(hrs.get(i).extra());
                }
                if (hrs.size() > 6) {
                    sb.append(" и ещё ").append(hrs.size() - 6).append(" ч");
                }
                sb.append(" (всего +").append(x.extraVehicleHours()).append(" ваг.-ч; норма ")
                        .append(num(hrs.get(0).norm())).append(" посадок на вагон в час).");
                r.add(sb.toString());
            }
            List<RouteDay> byCut = routes.stream().filter(x -> x.reserveVehicleHours() > 0)
                    .sorted(Comparator.comparingInt(RouteDay::reserveVehicleHours).reversed()).limit(3).toList();
            for (RouteDay x : byCut) {
                StringBuilder sb = new StringBuilder("Маршрут " + x.route() + ": можно сократить выпуск — ");
                List<Cut> hrs = x.reserveHours();
                for (int i = 0; i < Math.min(hrs.size(), 6); i++) {
                    sb.append(i == 0 ? "" : ", ").append(hrs.get(i).hour()).append(":00 −").append(hrs.get(i).reserve());
                }
                if (hrs.size() > 6) {
                    sb.append(" и ещё ").append(hrs.size() - 6).append(" ч");
                }
                sb.append(" (всего −").append(x.reserveVehicleHours())
                        .append(" ваг.-ч; нагрузка останется не выше 70 % нормы).");
                r.add(sb.toString());
            }
            if (fleet.reserveVehicleHours() > 0 || fleet.extraVehicleHours() > 0) {
                r.add(String.format(RU, "Баланс выпуска за день: усиление +%d ваг.-ч, резерв −%d ваг.-ч — сальдо %+d ваг.-ч.",
                        fleet.extraVehicleHours(), fleet.reserveVehicleHours(),
                        fleet.extraVehicleHours() - fleet.reserveVehicleHours()));
            }
            if (scenario && !byExtra.isEmpty()) {
                r.add("Усиление и резерв рассчитаны по базовому прогнозу модели, без корректирующих коэффициентов.");
            }
        } else {
            r.add("Расчёт усиления и резерва выпуска доступен для прогнозного периода 01.11–31.12.2025.");
        }
        if (net.deltaWeekPct() != null && Math.abs(net.deltaWeekPct()) >= 5) {
            r.add(String.format(RU, "Посадки сети на %s %% %s, чем в сопоставимый день %s (тот же день недели и "
                    + "тип дня) — сезонное изменение спроса или смена режима маршрутов.",
                    num1(Math.abs(net.deltaWeekPct())), net.deltaWeekPct() > 0 ? "выше" : "ниже", net.weekAgoDate()));
        }
        routes.stream().filter(x -> x.deltaWeekPct() != null && Math.abs(x.deltaWeekPct()) >= 15
                        && x.weekAgoTotal() != null && x.weekAgoTotal() > 1000)
                .limit(3)
                .forEach(x -> r.add(String.format(RU, "Маршрут %d: %s посадок, %+.1f %% к сопоставимому дню %s — "
                        + "возможна смена режима движения; сверьте с оперативной обстановкой.",
                        x.route(), num(x.total()), x.deltaWeekPct(), net.weekAgoDate())));
        if (!hot.isEmpty()) {
            HotStop s = hot.get(0);
            r.add(String.format(RU, "Самая загруженная остановка — %s (маршрут %d): ~%s посадок в %02d:00 "
                    + "(оценка по доле остановки).", s.name(), s.route(), num(s.value()), s.hour()));
        }
        if (weather != null && weather.precipMm() != null && weather.precipMm() >= 5) {
            double k = Math.exp(Scenario.DEFAULT_BETA * (Math.log1p(weather.precipMm())
                    - data.monthNormLog(d.getMonthValue())));
            r.add(String.format(RU, "Осадки %s мм (архив Open-Meteo): ожидаемый эффект на спрос %+.1f %% — учтите на "
                    + "вкладке «Сценарии».", num1(weather.precipMm()), 100 * (k - 1)));
        }
        for (Event e : events) {
            r.add("Действует: " + e.name() + (e.routes() == null || e.routes().isEmpty() ? " (все маршруты)"
                    : " (маршруты " + e.routes() + ")") + ".");
        }
        return r;
    }

    // ------------------------------------------------------------------ XLSX

    public void writeXlsx(Summary s, OutputStream out) {
        try {
            Workbook wb = new Workbook(out, "tram-forecast", "1.0");
            Worksheet a = wb.newWorksheet("Сводка");
            int r = 0;
            a.value(r, 0, "Сводка диспетчера на " + s.date() + " (" + switch (s.mode()) {
                case "history" -> "факт";
                case "year" -> "сценарий 2026";
                default -> "прогноз";
            } + ")");
            a.style(r, 0).bold().set();
            r += 2;
            Object[][] kv = {
                    {"Тип дня", s.dayType().label()},
                    {"Осадки, мм", s.weather() == null ? null : s.weather().precipMm()},
                    {"Температура, °C", s.weather() == null ? null : s.weather().tempC()},
                    {"Посадки за сутки", s.network().total()},
                    {"Сопоставимый день (" + s.network().weekAgoDate() + ")", s.network().weekAgoTotal()},
                    {"Изменение к сопоставимому дню, %", s.network().deltaWeekPct()},
                    {"Пиковый час", s.network().peakHour() + ":00"},
                    {"Посадки в пиковый час", s.network().peakValue()},
                    {"Часов усиления", s.fleet().available() ? s.fleet().candidateHours() : null},
                    {"Дополнительных вагоно-часов", s.fleet().available() ? s.fleet().extraVehicleHours() : null},
                    {"Резерв (экономия), вагоно-часов", s.fleet().available() ? s.fleet().reserveVehicleHours() : null},
            };
            for (Object[] row : kv) {
                a.value(r, 0, (String) row[0]);
                cell(a, r++, 1, row[1]);
            }
            r++;
            a.value(r++, 0, "Рекомендации");
            a.style(r - 1, 0).bold().set();
            for (String rec : s.recommendations()) {
                a.value(r++, 0, "• " + rec);
            }
            a.width(0, 40);
            a.width(1, 18);

            Worksheet b = wb.newWorksheet("Маршруты");
            String[] hb = {"Маршрут", "Название", "Посадки за сутки", "Сопоставимый день", "Изменение, %", "Пиковый час",
                    "Посадки в пик", "Доп. вагоно-часы", "Резерв, вагоно-часы"};
            header(b, hb);
            r = 1;
            for (RouteDay x : s.routes()) {
                b.value(r, 0, x.route());
                b.value(r, 1, x.name());
                b.value(r, 2, x.total());
                cell(b, r, 3, x.weekAgoTotal());
                cell(b, r, 4, x.deltaWeekPct());
                b.value(r, 5, x.peakHour() + ":00");
                b.value(r, 6, x.peakValue());
                b.value(r, 7, x.extraVehicleHours());
                b.value(r, 8, x.reserveVehicleHours());
                r++;
            }
            r++;
            b.value(r, 0, "Посадки по часам");
            b.style(r++, 0).bold().set();
            b.value(r, 0, "Маршрут");
            for (int hr = 0; hr < 24; hr++) {
                b.value(r, 1 + hr, hr + ":00");
            }
            r++;
            for (RouteDay x : s.routes()) {
                b.value(r, 0, x.route());
                for (int hr = 0; hr < 24; hr++) {
                    b.value(r, 1 + hr, x.hourly()[hr]);
                }
                r++;
            }

            Worksheet c = wb.newWorksheet("Усиление");
            header(c, new String[]{"Маршрут", "Час", "Прогноз посадок", "Выпуск (вагонов)", "Норма посадок на вагон",
                    "Добавить вагонов"});
            r = 1;
            for (RouteDay x : s.routes()) {
                for (Reinforce f : x.reinforceHours()) {
                    c.value(r, 0, x.route());
                    c.value(r, 1, f.hour() + ":00");
                    c.value(r, 2, f.pred());
                    c.value(r, 3, f.nPlan());
                    c.value(r, 4, f.norm());
                    c.value(r, 5, f.extra());
                    r++;
                }
            }
            if (r == 1) {
                c.value(1, 0, s.fleet().available() ? "Усиление не требуется"
                        : "Расчёт усиления доступен для 01.11–31.12.2025");
            }

            Worksheet rv = wb.newWorksheet("Резерв");
            header(rv, new String[]{"Маршрут", "Час", "Прогноз посадок", "Выпуск (вагонов)", "Норма посадок на вагон",
                    "Оставить вагонов", "Снять вагонов"});
            r = 1;
            for (RouteDay x : s.routes()) {
                for (Cut f : x.reserveHours()) {
                    rv.value(r, 0, x.route());
                    rv.value(r, 1, f.hour() + ":00");
                    rv.value(r, 2, f.pred());
                    rv.value(r, 3, f.nPlan());
                    rv.value(r, 4, f.norm());
                    rv.value(r, 5, f.need());
                    rv.value(r, 6, f.reserve());
                    r++;
                }
            }
            if (r == 1) {
                rv.value(1, 0, s.fleet().available() ? "Резерва нет" : "Расчёт резерва доступен для 01.11–31.12.2025");
            }

            Worksheet e = wb.newWorksheet("Остановки");
            header(e, new String[]{"Остановка", "Маршрут", "Направление", "Порядок", "Час пика маршрута",
                    "Посадки в этот час (оценка)", "Посадки за сутки (оценка)"});
            r = 1;
            for (HotStop x : s.hotStops()) {
                e.value(r, 0, x.name());
                e.value(r, 1, x.route());
                e.value(r, 2, x.direction());
                e.value(r, 3, x.seq());
                e.value(r, 4, x.hour() + ":00");
                e.value(r, 5, x.value());
                e.value(r, 6, x.dayTotal());
                r++;
            }
            wb.finish();
        } catch (IOException ex) {
            throw new UncheckedIOException(ex);
        }
    }

    private static void header(Worksheet ws, String[] cols) {
        for (int i = 0; i < cols.length; i++) {
            ws.value(0, i, cols[i]);
            ws.style(0, i).bold().set();
        }
    }

    private static void cell(Worksheet ws, int r, int c, Object v) {
        if (v instanceof Number n) {
            ws.value(r, c, n);
        } else if (v != null) {
            ws.value(r, c, v.toString());
        }
    }

    // ------------------------------------------------------------------ утилиты

    private static boolean inRange(LocalDate d, JsonNode n) {
        return !d.isBefore(LocalDate.parse(n.get("start").asText())) && !d.isAfter(LocalDate.parse(n.get("end").asText()));
    }

    private static String text(JsonNode n, String f) {
        JsonNode v = n.get(f);
        return v == null || v.isNull() ? null : v.asText();
    }

    private static List<Integer> ints(JsonNode n) {
        List<Integer> out = new ArrayList<>();
        if (n != null && n.isArray()) {
            n.forEach(x -> out.add(x.asInt()));
        }
        return out;
    }

    private static List<String> strs(JsonNode n) {
        List<String> out = new ArrayList<>();
        if (n != null && n.isArray()) {
            n.forEach(x -> out.add(x.asText()));
        } else if (n != null && !n.isNull()) {
            out.add(n.asText());
        }
        return out;
    }

    private static Double pct(Double base, double v) {
        return base == null || base <= 0 ? null : Math.round((v - base) / base * 1000.0) / 10.0;
    }

    private static double r1(double v) {
        return Math.round(v * 10.0) / 10.0;
    }

    private static String num(double v) {
        return String.format(RU, "%,d", Math.round(v));
    }

    private static String num1(double v) {
        return String.format(RU, "%.1f", v);
    }
}
