package ru.mostrans.tram.forecast;

import java.time.LocalDate;
import java.util.ArrayList;
import java.util.List;

import org.springframework.stereotype.Service;

import ru.mostrans.tram.api.ApiException;
import ru.mostrans.tram.data.DataStore;

/**
 * Агрегация прогноза: ячейка (маршрут, дата, час) = базовый прогноз × доля остановок × корректирующие коэффициенты,
 * затем сумма по корзинам гранулярности. Всё в памяти, без аллокаций на ячейку, кроме ключа корзины.
 */
@Service
public class ForecastEngine {

    /** Ошибка месячного прогноза (80 % ошибок меньше) на бэктесте: маршрут / сеть — ml/outputs/model_error_by_level.csv. */
    public static final double BAND_ROUTE = 0.141;
    public static final double BAND_NETWORK = 0.113;
    /** Максимум точек в ответе /forecast (для больших выгрузок — /export). */
    public static final int MAX_POINTS = 60_000;

    private final DataStore data;

    public ForecastEngine(DataStore data) {
        this.data = data;
    }

    public record Point(String t, double base, double pred, Double lo, Double hi) {
    }

    public record Series(String key, String label, String color, List<Point> points) {
    }

    public record Totals(double base, double pred, double delta, Double deltaPct) {
    }

    public record RouteImpact(int route, double base, double pred, double delta, Double deltaPct) {
    }

    public record QueryEcho(String horizon, LocalDate from, LocalDate to, int hourFrom, int hourTo,
                            String granularity, String split, List<Integer> routes, List<String> stops,
                            String segment, boolean scenario) {
    }

    public record Result(QueryEcho query, String unit, List<Series> series, Totals totals, List<RouteImpact> byRoute,
                         List<String> notes, double computeMs) {
    }

    /** Базовое значение ячейки маршрута (индекс k) по горизонту. */
    public double base(Horizon h, int k, LocalDate d, int hour) {
        return switch (h) {
            case DAY, MONTH -> data.forecastAt(k, d, hour);
            case YEAR -> data.yearAt(k, d, hour);
            case HISTORY -> data.historyAt(k, d, hour);
        };
    }

    /** Сырые суммы по корзинам: [серия][корзина] → {base, pred}; порядок корзин — хронологический. */
    public record Buckets(List<String> keys, List<String> seriesKeys, double[][] base, double[][] pred,
                          double[] routeBase, double[] routePred) {
    }

    /** 24 значения дня маршрута k в буфер out (без аллокаций). */
    public void day(Horizon h, int k, LocalDate d, double[] out) {
        switch (h) {
            case DAY, MONTH -> data.forecastDay(k, d, out);
            case YEAR -> data.yearDay(k, d, out);
            case HISTORY -> data.historyDay(k, d, out);
        }
    }

    public Buckets aggregate(ForecastQuery q) {
        List<Integer> routes = q.routes();
        int nr = routes.size();
        int[] idx = new int[nr];
        double[] share = new double[nr];
        for (int i = 0; i < nr; i++) {
            idx[i] = data.indexOf(routes.get(i));
            share[i] = q.factorOf(routes.get(i));
        }
        int nBuckets = bucketCount(q);
        int nSeries = q.splitByRoute() ? nr : 1;
        int nHours = q.hourTo() - q.hourFrom() + 1;
        int month0 = q.from().getYear() * 12 + q.from().getMonthValue();
        double[][] b = new double[nSeries][nBuckets];
        double[][] p = new double[nSeries][nBuckets];
        double[] rb = new double[nr];
        double[] rp = new double[nr];
        double[] row = new double[24];
        Scenario sc = q.scenario();
        boolean noScenario = sc.isEmpty();
        int di = 0;
        for (LocalDate d = q.from(); !d.isAfter(q.to()); d = d.plusDays(1), di++) {
            double df = noScenario ? 1.0 : sc.dayFactor(data, d);
            int dayBucket = switch (q.granularity()) {
                case HOUR -> di * nHours;
                case DAY -> di;
                case MONTH -> d.getYear() * 12 + d.getMonthValue() - month0;
                case TOTAL -> 0;
            };
            for (int i = 0; i < nr; i++) {
                day(q.horizon(), idx[i], d, row);
                int s = q.splitByRoute() ? i : 0;
                int route = routes.get(i);
                for (int h = q.hourFrom(); h <= q.hourTo(); h++) {
                    double v = row[h] * share[i];
                    if (v == 0) {
                        continue;
                    }
                    double adj = noScenario ? v : v * sc.factor(df, route, d, h);
                    int bi = q.granularity() == Granularity.HOUR ? dayBucket + h - q.hourFrom() : dayBucket;
                    b[s][bi] += v;
                    p[s][bi] += adj;
                    rb[i] += v;
                    rp[i] += adj;
                }
            }
        }
        List<String> keys = new ArrayList<>(nBuckets);
        switch (q.granularity()) {
            case HOUR -> {
                for (LocalDate d = q.from(); !d.isAfter(q.to()); d = d.plusDays(1)) {
                    for (int h = q.hourFrom(); h <= q.hourTo(); h++) {
                        keys.add(Granularity.HOUR.key(d, h));
                    }
                }
            }
            case DAY -> {
                for (LocalDate d = q.from(); !d.isAfter(q.to()); d = d.plusDays(1)) {
                    keys.add(d.toString());
                }
            }
            case MONTH -> {
                for (int m = 0; m < nBuckets; m++) {
                    keys.add(Granularity.MONTH.key(q.from().withDayOfMonth(1).plusMonths(m), 0));
                }
            }
            case TOTAL -> keys.add("total");
        }
        List<String> seriesKeys = new ArrayList<>();
        if (q.splitByRoute()) {
            routes.forEach(r -> seriesKeys.add(String.valueOf(r)));
        } else {
            seriesKeys.add("all");
        }
        return new Buckets(keys, seriesKeys, b, p, rb, rp);
    }

    /**
     * Интервал (80 % ошибок меньше) измерен на бэктесте для месяца маршрута/сети целиком. Поэтому показываем его
     * только для корзины «полный месяц» или «итог не короче 28 дней», по всем часам суток и без доли остановок;
     * для часов, дней, остановок и неполных периодов интервал не измерен — null.
     */
    public static boolean bandFor(ForecastQuery q, String key) {
        if (q.horizon() == Horizon.HISTORY || !q.stopFactor().isEmpty() || q.hourFrom() != 0 || q.hourTo() != 23) {
            return false;
        }
        return switch (q.granularity()) {
            case MONTH -> {
                java.time.YearMonth ym = java.time.YearMonth.parse(key);
                yield !q.from().isAfter(ym.atDay(1)) && !q.to().isBefore(ym.atEndOfMonth());
            }
            case TOTAL -> DataStore.days(q.from(), q.to()) >= 28;
            default -> false;
        };
    }

    /** Относительная полуширина интервала: маршрут ±14.1 %, сумма нескольких маршрутов ±11.3 %. */
    public static double band(ForecastQuery q) {
        return (q.splitByRoute() || q.routes().size() == 1) ? BAND_ROUTE : BAND_NETWORK;
    }

    /** Число корзин агрегации (точек одного ряда). */
    public static int bucketCount(ForecastQuery q) {
        int nDays = DataStore.days(q.from(), q.to());
        return switch (q.granularity()) {
            case HOUR -> nDays * (q.hourTo() - q.hourFrom() + 1);
            case DAY -> nDays;
            case MONTH -> q.to().getYear() * 12 + q.to().getMonthValue()
                    - q.from().getYear() * 12 - q.from().getMonthValue() + 1;
            case TOTAL -> 1;
        };
    }

    public Result forecast(ForecastQuery q) {
        long t0 = System.nanoTime();
        long points = (long) bucketCount(q) * (q.splitByRoute() ? q.routes().size() : 1);
        if (points > MAX_POINTS) {
            throw ApiException.badRequest("granularity", "Слишком детальный запрос: " + points
                    + " точек (максимум " + MAX_POINTS + "). Укрупните гранулярность, сократите интервал или "
                    + "используйте выгрузку /api/v1/export");
        }
        Buckets a = aggregate(q);
        double u = band(q);
        List<Series> series = new ArrayList<>();
        for (int s = 0; s < a.seriesKeys().size(); s++) {
            List<Point> pts = new ArrayList<>(a.keys().size());
            for (int k = 0; k < a.keys().size(); k++) {
                double pred = a.pred()[s][k];
                pts.add(new Point(a.keys().get(k), r1(a.base()[s][k]), r1(pred),
                        bandFor(q, a.keys().get(k)) ? r1(pred * (1 - u)) : null,
                        bandFor(q, a.keys().get(k)) ? r1(pred * (1 + u)) : null));
            }
            String key = a.seriesKeys().get(s);
            boolean all = key.equals("all");
            int route = all ? 0 : Integer.parseInt(key);
            series.add(new Series(key, all ? "Все выбранные маршруты" : "Маршрут " + key,
                    all ? "#1f2937" : data.routeColor(route), pts));
        }
        double tb = 0;
        double tp = 0;
        List<RouteImpact> byRoute = new ArrayList<>();
        for (int i = 0; i < q.routes().size(); i++) {
            tb += a.routeBase()[i];
            tp += a.routePred()[i];
            byRoute.add(new RouteImpact(q.routes().get(i), r1(a.routeBase()[i]), r1(a.routePred()[i]),
                    r1(a.routePred()[i] - a.routeBase()[i]), pct(a.routeBase()[i], a.routePred()[i])));
        }
        QueryEcho echo = new QueryEcho(q.horizon().id, q.from(), q.to(), q.hourFrom(), q.hourTo(),
                q.granularity().id(), q.splitByRoute() ? "route" : "none", q.routes(), q.stops(), q.segment(),
                !q.scenario().isEmpty());
        return new Result(echo, "посадки", series, new Totals(r1(tb), r1(tp), r1(tp - tb), pct(tb, tp)), byRoute,
                notes(q), (System.nanoTime() - t0) / 1e6);
    }

    List<String> notes(ForecastQuery q) {
        List<String> n = new ArrayList<>();
        switch (q.horizon()) {
            case HISTORY -> n.add("Факт: успешные валидации (validation_result = 1) по tran_date_time.");
            case YEAR -> n.add("Год 2026 — качественный сценарий: месяц 2025 года, переложенный на календарь 2026, "
                    + "плюс Т1 и маршрут 5; почасовая форма — профиль суток ноября–декабря 2025 по типу дня. "
                    + "Интервал ±14 % — нижняя граница неопределённости.");
            default -> n.add("Структурная модель, WAPE-score на лидерборде 0.9125 (почасовая ошибка на фолдах: "
                    + "0.86–0.90 в зависимости от горизонта).");
        }
        if (!q.stopFactor().isEmpty()) {
            n.add("Посадки по остановкам = прогноз маршрута × априорная доля остановки: в валидациях нет "
                    + "идентификатора остановки (place_id — площадка/депо). Доли заменяются фактическими "
                    + "при поступлении валидаций с геопозицией.");
        }
        if (q.routes().contains(5) && q.horizon() != Horizon.HISTORY) {
            n.add("Маршрут 5 запущен 16.12.2025 — холодный старт от аналога (профиль маршрута 7), точность ниже.");
        }
        if (!q.scenario().isEmpty()) {
            n.add("Применены корректирующие коэффициенты: base — прогноз модели, pred — с поправками.");
        }
        return n;
    }

    static double r2(double v) {
        return Math.round(v * 100.0) / 100.0;
    }

    static double r1(double v) {
        return Math.round(v * 10.0) / 10.0;
    }

    static Double pct(double base, double pred) {
        return base > 0 ? Math.round((pred - base) / base * 10000.0) / 100.0 : null;
    }
}
