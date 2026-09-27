package ru.mostrans.tram.forecast;

import java.time.LocalDate;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

import org.springframework.stereotype.Service;
import org.springframework.util.MultiValueMap;

import ru.mostrans.tram.api.ApiException;
import ru.mostrans.tram.data.DataStore;
import ru.mostrans.tram.data.Stop;

/** Данные карты на день: почасовые посадки по маршрутам и остановкам + геометрия трасс. */
@Service
public class MapService {

    private final DataStore data;
    private final ForecastEngine engine;

    public MapService(DataStore data, ForecastEngine engine) {
        this.data = data;
        this.engine = engine;
    }

    public record DayType(int code, String label) {
    }

    public record Weather(Double precipMm, Double tempC) {
    }

    public record Line(int direction, List<double[]> coords) {
    }

    public record RouteDay(int route, String name, String color, double[] hourly, double total, List<Line> lines) {
    }

    public record StopDay(String stopId, int route, int direction, int seq, String name, double lat, double lon,
                          double weight, double[] hourly, double total) {
    }

    public record MapDay(LocalDate date, String mode, DayType dayType, Weather weather, List<RouteDay> routes,
                         List<StopDay> stops, double maxStopHour, double computeMs) {
    }

    public MapDay day(MultiValueMap<String, String> p) {
        long t0 = System.nanoTime();
        String ds = p.getFirst("date");
        if (ds == null || ds.isBlank()) {
            throw ApiException.badRequest("date", "Параметр date обязателен (YYYY-MM-DD)");
        }
        LocalDate d = Scenario.date(ds, "date");
        Horizon h = Horizon.ofDate(d);
        if (h == null) {
            throw ApiException.badRequest("date", "Дата " + d + " вне доступных периодов: история "
                    + DataStore.HIST_START + " … " + DataStore.HIST_END + ", прогноз " + DataStore.FC_START + " … "
                    + DataStore.FC_END + ", сценарий " + DataStore.YEAR_START + " … " + DataStore.YEAR_END);
        }
        Scenario sc = Scenario.parse(p);
        if (h == Horizon.HISTORY && !sc.isEmpty()) {
            throw ApiException.badRequest("date", "Корректирующие коэффициенты не применяются к истории (факту)");
        }
        Set<Integer> routes = new LinkedHashSet<>();
        String rs = p.getFirst("routes");
        if (rs != null && !rs.isBlank()) {
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
                    throw ApiException.badRequest("routes", "Маршрут " + route + " не найден");
                }
                routes.add(route);
            }
        }
        if (routes.isEmpty()) {
            for (int r : DataStore.ROUTES) {
                routes.add(r);
            }
        }
        double df = sc.isEmpty() ? 1.0 : sc.dayFactor(data, d);
        List<RouteDay> rd = new ArrayList<>();
        List<StopDay> sd = new ArrayList<>();
        double max = 0;
        for (int route : routes) {
            int k = data.indexOf(route);
            double[] hourly = new double[24];
            double total = 0;
            for (int hr = 0; hr < 24; hr++) {
                double v = engine.base(h, k, d, hr);
                if (!sc.isEmpty()) {
                    v *= sc.factor(df, route, d, hr);
                }
                hourly[hr] = ForecastEngine.r1(v);
                total += v;
            }
            List<Stop> stops = data.stopsOf(route);
            List<Line> lines = new ArrayList<>();
            int dir = -1;
            List<double[]> coords = null;
            for (Stop s : stops) {
                if (s.direction() != dir) {
                    dir = s.direction();
                    coords = new ArrayList<>();
                    lines.add(new Line(dir, coords));
                }
                coords.add(new double[]{s.lon(), s.lat()});
                double[] sh = new double[24];
                for (int hr = 0; hr < 24; hr++) {
                    sh[hr] = ForecastEngine.r1(hourly[hr] * s.weight());
                    max = Math.max(max, sh[hr]);
                }
                sd.add(new StopDay(s.stopId(), s.route(), s.direction(), s.seq(), s.name(), s.lat(), s.lon(),
                        s.weight(), sh, ForecastEngine.r1(total * s.weight())));
            }
            rd.add(new RouteDay(route, data.routeName(route), data.routeColor(route), hourly,
                    ForecastEngine.r1(total), lines));
        }
        double[] w = data.weather(d);
        Weather weather = w == null ? null : new Weather(Double.isNaN(w[0]) ? null : w[0],
                Double.isNaN(w[1]) ? null : w[1]);
        String mode = switch (h) {
            case HISTORY -> "history";
            case YEAR -> "year";
            default -> "forecast";
        };
        int code = data.dayCode(d);
        return new MapDay(d, mode, new DayType(code, DataStore.dayLabel(code)), weather, rd, sd, max,
                (System.nanoTime() - t0) / 1e6);
    }
}
