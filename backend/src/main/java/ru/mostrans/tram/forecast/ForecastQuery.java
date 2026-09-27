package ru.mostrans.tram.forecast;

import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import org.springframework.util.MultiValueMap;

import ru.mostrans.tram.api.ApiException;
import ru.mostrans.tram.data.DataStore;
import ru.mostrans.tram.data.Stop;

/**
 * Разобранный и проверенный запрос прогноза: горизонт, маршруты, остановки/участок, даты, часы, гранулярность.
 * stopFactor — доля посадок маршрута, приходящаяся на выбранные остановки (1 — весь маршрут);
 * selected — выбранные вхождения остановок (маршрут, направление, порядок) — для выгрузки по остановкам.
 */
public record ForecastQuery(Horizon horizon, List<Integer> routes, List<String> stops, String segment,
                            LocalDate from, LocalDate to, int hourFrom, int hourTo, Granularity granularity,
                            boolean splitByRoute, Map<Integer, Double> stopFactor, List<Stop> selected,
                            Scenario scenario) {

    public static ForecastQuery parse(MultiValueMap<String, String> p, DataStore data) {
        Horizon horizon = Horizon.DAY;
        String hs = first(p, "horizon");
        if (hs != null) {
            horizon = Horizon.parse(hs);
            if (horizon == null) {
                throw ApiException.badRequest("horizon", "Неизвестный горизонт «" + hs
                        + "». Допустимо: day, month, year, history");
            }
        }

        // маршруты
        Set<Integer> routes = new LinkedHashSet<>();
        String rs = joined(p, "routes");
        if (rs != null) {
            for (String r : rs.split(",")) {
                if (r.isBlank()) {
                    continue;
                }
                int route;
                try {
                    route = Integer.parseInt(r.trim());
                } catch (NumberFormatException e) {
                    throw ApiException.badRequest("routes", "Номер маршрута «" + r.trim() + "» — не число");
                }
                if (!data.hasRoute(route)) {
                    throw ApiException.badRequest("routes", "Маршрут " + route + " не найден. Доступные маршруты: "
                            + Arrays.toString(DataStore.ROUTES));
                }
                routes.add(route);
            }
        }

        // остановки или участок
        List<String> stops = new ArrayList<>();
        Map<Integer, Double> factor = new LinkedHashMap<>();
        String ss = joined(p, "stops");
        List<Stop> selected = new ArrayList<>();
        String segment = first(p, "segment");
        if (ss != null && segment != null) {
            throw ApiException.badRequest("stops", "Укажите либо stops, либо segment, но не оба сразу");
        }
        if (ss != null) {
            for (String id : ss.split(",")) {
                if (id.isBlank()) {
                    continue;
                }
                String sid = id.trim();
                List<Stop> entries = data.stopEntries(sid);
                if (entries.isEmpty()) {
                    throw ApiException.badRequest("stops", "Остановка «" + sid + "» не найдена");
                }
                if (stops.contains(sid)) {
                    continue;
                }
                boolean used = false;
                for (Stop x : entries) {  // остановка может обслуживать несколько маршрутов
                    if (routes.isEmpty() || routes.contains(x.route())) {
                        factor.merge(x.route(), x.weight(), Double::sum);
                        selected.add(x);
                        used = true;
                    }
                }
                if (!used) {
                    throw ApiException.badRequest("stops", "Остановка " + sid + " не обслуживается выбранными "
                            + "маршрутами " + routes);
                }
                stops.add(sid);
            }
            if (stops.isEmpty()) {
                throw ApiException.badRequest("stops", "Список остановок пуст");
            }
            routes.clear();
            routes.addAll(factor.keySet());
        } else if (segment != null) {
            Map<Integer, Double> seg = parseSegment(segment, data, stops, selected);
            if (!routes.isEmpty() && !routes.containsAll(seg.keySet())) {
                throw ApiException.badRequest("segment", "Участок " + segment + " относится к маршруту "
                        + seg.keySet() + ", который не выбран в routes " + routes);
            }
            factor.putAll(seg);
            routes.clear();
            routes.addAll(factor.keySet());
        }
        if (routes.isEmpty()) {
            for (int r : DataStore.ROUTES) {
                routes.add(r);
            }
        }

        // даты
        LocalDate from = horizon.from;
        LocalDate to = horizon.to;
        if (first(p, "from") != null) {
            from = Scenario.date(first(p, "from"), "from");
        }
        if (first(p, "to") != null) {
            to = Scenario.date(first(p, "to"), "to");
        }
        checkDate(from, horizon, "from");
        checkDate(to, horizon, "to");
        if (to.isBefore(from)) {
            throw ApiException.badRequest("to", "Дата to (" + to + ") раньше from (" + from + ")");
        }

        int hourFrom = hour(p, "hourFrom", 0);
        int hourTo = hour(p, "hourTo", 23);
        if (hourTo < hourFrom) {
            throw ApiException.badRequest("hourTo", "hourTo (" + hourTo + ") меньше hourFrom (" + hourFrom + ")");
        }

        Granularity g = horizon.defaultGranularity;
        String gs = first(p, "granularity");
        if (gs != null) {
            g = Granularity.parse(gs);
            if (g == null || !horizon.granularities.contains(g)) {
                throw ApiException.badRequest("granularity", "Гранулярность «" + gs + "» недоступна для горизонта "
                        + horizon.id + ". Допустимо: " + horizon.granularities.stream().map(Granularity::id).toList());
            }
        }
        String split = first(p, "split");
        if (split != null && !split.equals("route") && !split.equals("none")) {
            throw ApiException.badRequest("split", "split: ожидается route или none, получено «" + split + "»");
        }
        Scenario sc = Scenario.parse(p);
        if (horizon == Horizon.HISTORY && !sc.isEmpty()) {
            throw ApiException.badRequest("horizon", "Корректирующие коэффициенты не применяются к истории (факту)");
        }
        return new ForecastQuery(horizon, List.copyOf(routes), List.copyOf(stops), segment, from, to, hourFrom,
                hourTo, g, !"none".equals(split), Map.copyOf(factor), List.copyOf(selected), sc);
    }

    /** Участок «маршрут:направление:seqFrom-seqTo» → доля посадок маршрута на остановках участка. */
    static Map<Integer, Double> parseSegment(String s, DataStore data, List<String> stopsOut, List<Stop> selected) {
        String fmt = "формат участка: маршрут:направление:seqFrom-seqTo, например 1:0:3-10";
        String[] f = s.split(":");
        if (f.length != 3 || !f[2].contains("-")) {
            throw ApiException.badRequest("segment", "Не разобран участок «" + s + "»: " + fmt);
        }
        int route = Scenario.integer(f[0], "segment");
        int dir = Scenario.integer(f[1], "segment");
        String[] ab = f[2].split("-", 2);
        int a = Scenario.integer(ab[0], "segment");
        int b = Scenario.integer(ab[1], "segment");
        if (!data.hasRoute(route)) {
            throw ApiException.badRequest("segment", "Маршрут " + route + " не найден");
        }
        List<Stop> st = data.stopsOf(route).stream().filter(x -> x.direction() == dir).toList();
        if (st.isEmpty()) {
            throw ApiException.badRequest("segment", "Для маршрута " + route + " (направление " + dir
                    + ") нет остановок с координатами — участок задать нельзя");
        }
        if (a > b) {
            throw ApiException.badRequest("segment", "Начало участка (" + a + ") больше конца (" + b + ")");
        }
        double w = 0;
        for (Stop x : st) {
            if (x.seq() >= a && x.seq() <= b) {
                w += x.weight();
                stopsOut.add(x.stopId());
                selected.add(x);
            }
        }
        if (stopsOut.isEmpty()) {
            throw ApiException.badRequest("segment", "На участке " + s + " нет остановок (номера в направлении: "
                    + st.get(0).seq() + "–" + st.get(st.size() - 1).seq() + ")");
        }
        return Map.of(route, w);
    }

    static void checkDate(LocalDate d, Horizon h, String param) {
        if (d.isBefore(h.from) || d.isAfter(h.to)) {
            throw ApiException.badRequest(param, "Параметр " + param + ": дата " + d + " вне горизонта " + h.id
                    + " (" + h.from + " … " + h.to + ")");
        }
    }

    static int hour(MultiValueMap<String, String> p, String name, int def) {
        String s = first(p, name);
        if (s == null) {
            return def;
        }
        int h;
        try {
            h = Integer.parseInt(s.trim());
        } catch (NumberFormatException e) {
            throw ApiException.badRequest(name, "Параметр " + name + ": «" + s + "» — не целое число");
        }
        if (h < 0 || h > 23) {
            throw ApiException.badRequest(name, "Параметр " + name + " должен быть от 0 до 23, получено " + h);
        }
        return h;
    }

    /** Все значения параметра через запятую: routes=1&routes=7 ≡ routes=1,7. */
    static String joined(MultiValueMap<String, String> p, String name) {
        List<String> v = p.get(name);
        if (v == null) {
            return null;
        }
        String s = String.join(",", v.stream().filter(x -> x != null && !x.isBlank()).toList());
        return s.isBlank() ? null : s;
    }

    static String first(MultiValueMap<String, String> p, String name) {
        String v = p.getFirst(name);
        return v == null || v.isBlank() ? null : v.trim();
    }

    public double factorOf(int route) {
        return stopFactor.isEmpty() ? 1.0 : stopFactor.getOrDefault(route, 0.0);
    }
}
