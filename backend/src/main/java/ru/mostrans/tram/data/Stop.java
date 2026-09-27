package ru.mostrans.tram.data;

/** Остановка маршрута с априорной долей посадок маршрута ({@code weight}, сумма по маршруту = 1). */
public record Stop(String stopId, int route, int direction, int seq, String name, double lat, double lon,
                   double weight) {
}
