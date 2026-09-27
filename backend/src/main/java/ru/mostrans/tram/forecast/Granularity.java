package ru.mostrans.tram.forecast;

import java.time.LocalDate;

public enum Granularity {
    HOUR, DAY, MONTH, TOTAL;

    public String id() {
        return name().toLowerCase();
    }

    public static Granularity parse(String s) {
        for (Granularity g : values()) {
            if (g.id().equalsIgnoreCase(s)) {
                return g;
            }
        }
        return null;
    }

    /** Ключ корзины агрегации: 2025-11-10T08:00 / 2025-11-10 / 2025-11 / total. */
    public String key(LocalDate d, int hour) {
        return switch (this) {
            case HOUR -> d + (hour < 10 ? "T0" : "T") + hour + ":00";
            case DAY -> d.toString();
            case MONTH -> d.toString().substring(0, 7);
            case TOTAL -> "total";
        };
    }
}
