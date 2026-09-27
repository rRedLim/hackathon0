package ru.mostrans.tram.forecast;

import java.time.LocalDate;
import java.util.List;

import ru.mostrans.tram.data.DataStore;

/** Горизонты прогноза и история (факт) для сравнения. */
public enum Horizon {
    DAY("day", "День (почасовой, краткосрочный)", DataStore.FC_START, DataStore.FC_END, Granularity.HOUR,
            List.of(Granularity.HOUR, Granularity.DAY, Granularity.MONTH, Granularity.TOTAL)),
    MONTH("month", "Месяц (среднесрочный)", DataStore.FC_START, DataStore.FC_END, Granularity.DAY,
            List.of(Granularity.HOUR, Granularity.DAY, Granularity.MONTH, Granularity.TOTAL)),
    YEAR("year", "Год 2026 (долгосрочный сценарий)", DataStore.YEAR_START, DataStore.YEAR_END, Granularity.MONTH,
            List.of(Granularity.HOUR, Granularity.DAY, Granularity.MONTH, Granularity.TOTAL)),
    HISTORY("history", "История (факт январь–октябрь 2025)", DataStore.HIST_START, DataStore.HIST_END,
            Granularity.DAY, List.of(Granularity.HOUR, Granularity.DAY, Granularity.MONTH, Granularity.TOTAL));

    public final String id;
    public final String label;
    public final LocalDate from;
    public final LocalDate to;
    public final Granularity defaultGranularity;
    public final List<Granularity> granularities;

    Horizon(String id, String label, LocalDate from, LocalDate to, Granularity def, List<Granularity> gr) {
        this.id = id;
        this.label = label;
        this.from = from;
        this.to = to;
        this.defaultGranularity = def;
        this.granularities = gr;
    }

    public static Horizon parse(String s) {
        for (Horizon h : values()) {
            if (h.id.equalsIgnoreCase(s)) {
                return h;
            }
        }
        return null;
    }

    /** Горизонт, к которому относится дата (для карты и мониторинга). */
    public static Horizon ofDate(LocalDate d) {
        if (!d.isBefore(DataStore.HIST_START) && !d.isAfter(DataStore.HIST_END)) {
            return HISTORY;
        }
        if (!d.isBefore(DataStore.FC_START) && !d.isAfter(DataStore.FC_END)) {
            return DAY;
        }
        if (!d.isBefore(DataStore.YEAR_START) && !d.isAfter(DataStore.YEAR_END)) {
            return YEAR;
        }
        return null;
    }
}
