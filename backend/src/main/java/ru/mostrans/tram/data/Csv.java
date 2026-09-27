package ru.mostrans.tram.data;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Минимальный CSV-ридер (RFC 4180 без переносов строк внутри полей) для файлов service_data. */
public final class Csv {

    private Csv() {
    }

    /** Строка таблицы: доступ к полям по имени колонки. */
    public record Row(Map<String, Integer> index, List<String> values) {
        public String str(String col) {
            Integer i = index.get(col);
            if (i == null) {
                throw new IllegalStateException("нет колонки " + col);
            }
            return i < values.size() ? values.get(i) : "";
        }

        public int i(String col) {
            return (int) Math.round(Double.parseDouble(str(col)));
        }

        public double d(String col) {
            String s = str(col);
            return s.isEmpty() ? Double.NaN : Double.parseDouble(s);
        }
    }

    public static List<Row> read(Path file, char sep) {
        try (BufferedReader r = Files.newBufferedReader(file, StandardCharsets.UTF_8)) {
            String header = r.readLine();
            if (header == null) {
                return List.of();
            }
            if (header.startsWith("﻿")) {
                header = header.substring(1);
            }
            List<String> cols = split(header, sep);
            Map<String, Integer> index = new HashMap<>();
            for (int k = 0; k < cols.size(); k++) {
                index.put(cols.get(k), k);
            }
            List<Row> rows = new ArrayList<>();
            String line;
            while ((line = r.readLine()) != null) {
                if (!line.isEmpty()) {
                    rows.add(new Row(index, split(line, sep)));
                }
            }
            return rows;
        } catch (IOException e) {
            throw new UncheckedIOException("Не удалось прочитать " + file, e);
        }
    }

    public static List<Row> read(Path file) {
        return read(file, ',');
    }

    /** Таблица как список объектов с camelCase-ключами и числовыми значениями — для отдачи в API как есть. */
    public static List<Map<String, Object>> readTable(Path file) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Row row : read(file)) {
            Map<String, Object> m = new LinkedHashMap<>();
            row.index().entrySet().stream()
                    .sorted(Map.Entry.comparingByValue())
                    .forEach(e -> m.put(camel(e.getKey()), typed(row.str(e.getKey()))));
            out.add(m);
        }
        return out;
    }

    static Object typed(String s) {
        if (s.isEmpty()) {
            return null;
        }
        try {
            if (s.matches("-?\\d{1,18}")) {
                return Long.parseLong(s);
            }
            if (s.matches("-?\\d*\\.\\d+(e-?\\d+)?")) {
                return Double.parseDouble(s);
            }
        } catch (NumberFormatException ignored) {
            // строка, похожая на число, но не число — отдаём как есть
        }
        return s;
    }

    static String camel(String snake) {
        StringBuilder b = new StringBuilder();
        boolean up = false;
        for (char c : snake.toCharArray()) {
            if (c == '_') {
                up = true;
            } else {
                b.append(up ? Character.toUpperCase(c) : c);
                up = false;
            }
        }
        return b.toString();
    }

    public static List<String> split(String line, char sep) {
        List<String> out = new ArrayList<>();
        StringBuilder cur = new StringBuilder();
        boolean quoted = false;
        for (int k = 0; k < line.length(); k++) {
            char c = line.charAt(k);
            if (quoted) {
                if (c == '"') {
                    if (k + 1 < line.length() && line.charAt(k + 1) == '"') {
                        cur.append('"');
                        k++;
                    } else {
                        quoted = false;
                    }
                } else {
                    cur.append(c);
                }
            } else if (c == '"') {
                quoted = true;
            } else if (c == sep) {
                out.add(cur.toString());
                cur.setLength(0);
            } else {
                cur.append(c);
            }
        }
        out.add(cur.toString());
        return out;
    }
}
