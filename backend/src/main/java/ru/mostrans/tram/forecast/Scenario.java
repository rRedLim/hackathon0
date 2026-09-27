package ru.mostrans.tram.forecast;

import java.time.LocalDate;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import org.springframework.util.MultiValueMap;

import ru.mostrans.tram.api.ApiException;
import ru.mostrans.tram.data.DataStore;

/**
 * Корректирующие коэффициенты поверх готового прогноза (ml/scenario.py): погода, сезон, маршрут, день недели,
 * события. Модель не переобучается — ячейки умножаются на k, поэтому пересчёт мгновенный.
 */
public final class Scenario {

    public static final double DEFAULT_BETA = -0.012;
    private static final double MULT_MIN = 0.0;
    private static final double MULT_MAX = 10.0;

    /** Событие: множитель на маршруты (null = все) в интервале дат и (опционально) часов. */
    public record Event(Set<Integer> routes, LocalDate start, LocalDate end, Set<Integer> hours, double mult) {
        boolean matches(int route, LocalDate d, int hour) {
            return !d.isBefore(start) && !d.isAfter(end)
                    && (routes == null || routes.contains(route))
                    && (hours == null || hours.contains(hour));
        }
    }

    private final Map<LocalDate, Double> precip;
    private final double beta;
    private final Map<Integer, Double> monthMult;
    private final Map<Integer, Double> routeMult;
    private final Map<Integer, Double> dowMult;
    private final List<Event> events;

    public static final Scenario NONE = new Scenario(Map.of(), DEFAULT_BETA, Map.of(), Map.of(), Map.of(), List.of());

    public Scenario(Map<LocalDate, Double> precip, double beta, Map<Integer, Double> monthMult,
                    Map<Integer, Double> routeMult, Map<Integer, Double> dowMult, List<Event> events) {
        this.precip = precip;
        this.beta = beta;
        this.monthMult = monthMult;
        this.routeMult = routeMult;
        this.dowMult = dowMult;
        this.events = events;
    }

    public boolean isEmpty() {
        return precip.isEmpty() && monthMult.isEmpty() && routeMult.isEmpty() && dowMult.isEmpty() && events.isEmpty();
    }

    /** Множитель дня, не зависящий от маршрута и часа (погода × сезон × день недели). */
    public double dayFactor(DataStore data, LocalDate d) {
        double k = 1.0;
        Double mm = precip.get(d);
        if (mm != null) {
            k *= Math.exp(beta * (Math.log1p(mm) - data.monthNormLog(d.getMonthValue())));
        }
        k *= monthMult.getOrDefault(d.getMonthValue(), 1.0);
        k *= dowMult.getOrDefault(d.getDayOfWeek().getValue(), 1.0);
        return k;
    }

    public double routeFactor(int route) {
        return routeMult.getOrDefault(route, 1.0);
    }

    /** Полный множитель ячейки; dayFactor передаётся заранее посчитанным (один раз на дату). */
    public double factor(double dayFactor, int route, LocalDate d, int hour) {
        if (d.equals(DataStore.FC_START) && hour <= 1) {
            return 1.0; // факт 1.11 00–01 ч из хвоста test.csv — не корректируется
        }
        double k = dayFactor * routeFactor(route);
        for (Event e : events) {
            if (e.matches(route, d, hour)) {
                k *= e.mult();
            }
        }
        return k;
    }

    // ------------------------------------------------------------------ разбор параметров

    public static Scenario parse(MultiValueMap<String, String> p) {
        Map<LocalDate, Double> precip = new HashMap<>();
        for (String item : items(p, "precip")) {
            String[] kv = pair(item, "precip", "дата:мм, например 2025-12-05:15");
            double mm = number(kv[1], "precip");
            if (mm < 0 || mm > 300) {
                throw ApiException.badRequest("precip", "Осадки должны быть от 0 до 300 мм, получено " + kv[1]);
            }
            precip.put(date(kv[0], "precip"), mm);
        }
        double beta = DEFAULT_BETA;
        if (p.getFirst("beta") != null && !p.getFirst("beta").isBlank()) {
            beta = number(p.getFirst("beta"), "beta");
            if (beta < -1 || beta > 1) {
                throw ApiException.badRequest("beta", "beta должна быть в диапазоне −1…1, получено " + beta);
            }
        }
        Map<Integer, Double> month = intMults(p, "monthMult", 1, 12, "месяц 1–12");
        Map<Integer, Double> route = intMults(p, "routeMult", Integer.MIN_VALUE, Integer.MAX_VALUE, "маршрут");
        for (Integer r : route.keySet()) {
            checkRoute(r, "routeMult");
        }
        Map<Integer, Double> dow = intMults(p, "dowMult", 1, 7, "день недели 1–7 (1 = Пн)");
        List<Event> events = new ArrayList<>();
        List<String> raw = p.get("event");
        if (raw != null) {
            for (String e : raw) {
                if (e != null && !e.isBlank()) {
                    events.add(parseEvent(e.trim()));
                }
            }
        }
        return new Scenario(precip, beta, month, route, dow, events);
    }

    static Event parseEvent(String s) {
        String fmt = "формат события: маршруты:начало:конец:множитель[:часы], например 7|50:2025-12-20:2025-12-21:0.5:10-18";
        String[] f = s.split(":");
        if (f.length < 4 || f.length > 5) {
            throw ApiException.badRequest("event", "Не разобрано событие «" + s + "»: " + fmt);
        }
        Set<Integer> routes = null;
        if (!f[0].equals("*") && !f[0].isBlank()) {
            routes = new HashSet<>();
            for (String r : f[0].split("\\|")) {
                int route = integer(r, "event");
                checkRoute(route, "event");
                routes.add(route);
            }
        }
        LocalDate start = date(f[1], "event");
        LocalDate end = date(f[2], "event");
        if (end.isBefore(start)) {
            throw ApiException.badRequest("event", "Событие «" + s + "»: дата конца раньше даты начала");
        }
        double mult = mult(f[3], "event");
        Set<Integer> hours = null;
        if (f.length == 5 && !f[4].isBlank()) {
            hours = hours(f[4], "event");
        }
        return new Event(routes, start, end, hours, mult);
    }

    static Set<Integer> hours(String s, String param) {
        Set<Integer> out = new HashSet<>();
        for (String part : s.split("\\|")) {
            if (part.contains("-")) {
                String[] ab = part.split("-", 2);
                int a = integer(ab[0], param);
                int b = integer(ab[1], param);
                if (a < 0 || b > 23 || a > b) {
                    throw ApiException.badRequest(param, "Интервал часов «" + part + "» должен быть внутри 0–23");
                }
                for (int h = a; h <= b; h++) {
                    out.add(h);
                }
            } else {
                int h = integer(part, param);
                if (h < 0 || h > 23) {
                    throw ApiException.badRequest(param, "Час должен быть 0–23, получено " + part);
                }
                out.add(h);
            }
        }
        return out;
    }

    private static void checkRoute(int route, String param) {
        for (int r : DataStore.ROUTES) {
            if (r == route) {
                return;
            }
        }
        throw ApiException.badRequest(param, "Маршрут " + route + " не найден. Доступные маршруты: "
                + java.util.Arrays.toString(DataStore.ROUTES));
    }

    private static Map<Integer, Double> intMults(MultiValueMap<String, String> p, String param, int min, int max,
                                                 String what) {
        Map<Integer, Double> out = new HashMap<>();
        for (String item : items(p, param)) {
            String[] kv = pair(item, param, what + ":множитель, например 12:1.03");
            int key = integer(kv[0], param);
            if (key < min || key > max) {
                throw ApiException.badRequest(param, "В " + param + " ожидается " + what + ", получено " + kv[0]);
            }
            out.put(key, mult(kv[1], param));
        }
        return out;
    }

    private static List<String> items(MultiValueMap<String, String> p, String param) {
        List<String> out = new ArrayList<>();
        List<String> raw = p.get(param);
        if (raw != null) {
            for (String v : raw) {
                if (v == null) {
                    continue;
                }
                for (String item : v.split(",")) {
                    if (!item.isBlank()) {
                        out.add(item.trim());
                    }
                }
            }
        }
        return out;
    }

    private static String[] pair(String item, String param, String hint) {
        int i = item.lastIndexOf(':');
        if (i <= 0 || i == item.length() - 1) {
            throw ApiException.badRequest(param, "Не разобрано значение «" + item + "» параметра " + param
                    + ": ожидается " + hint);
        }
        return new String[]{item.substring(0, i), item.substring(i + 1)};
    }

    private static double mult(String s, String param) {
        double m = number(s, param);
        if (m < MULT_MIN || m > MULT_MAX) {
            throw ApiException.badRequest(param, "Множитель должен быть от 0 до 10, получено " + s);
        }
        return m;
    }

    static double number(String s, String param) {
        try {
            double v = Double.parseDouble(s.trim());
            if (Double.isNaN(v) || Double.isInfinite(v)) {
                throw new NumberFormatException();
            }
            return v;
        } catch (NumberFormatException e) {
            throw ApiException.badRequest(param, "Параметр " + param + ": «" + s + "» — не число");
        }
    }

    static int integer(String s, String param) {
        try {
            return Integer.parseInt(s.trim());
        } catch (NumberFormatException e) {
            throw ApiException.badRequest(param, "Параметр " + param + ": «" + s + "» — не целое число");
        }
    }

    static LocalDate date(String s, String param) {
        try {
            return LocalDate.parse(s.trim());
        } catch (DateTimeParseException e) {
            throw ApiException.badRequest(param, "Параметр " + param + ": «" + s + "» — не дата формата YYYY-MM-DD");
        }
    }
}
