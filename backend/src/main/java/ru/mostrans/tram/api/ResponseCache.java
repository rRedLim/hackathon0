package ru.mostrans.tram.api;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Supplier;
import java.util.zip.GZIPOutputStream;

import org.springframework.util.MultiValueMap;

/**
 * Кэш сериализованных ответов для тяжёлых детерминированных запросов (карта дня). Данные прогноза неизменны
 * в течение жизни процесса, поэтому ответ зависит только от параметров запроса. Хранится и сжатая gzip-версия:
 * сжатие большого ответа на каждый запрос — основная нагрузка на CPU. Ограничен по числу записей.
 */
final class ResponseCache {

    record Entry(byte[] raw, byte[] gzip) {
    }

    private final int maxEntries;
    private final Map<String, Entry> map = new ConcurrentHashMap<>();

    ResponseCache(int maxEntries) {
        this.maxEntries = maxEntries;
    }

    static String key(MultiValueMap<String, String> params) {
        return new TreeMap<>(params).toString();
    }

    Entry get(String key, Supplier<byte[]> compute) {
        Entry v = map.get(key);
        if (v != null) {
            return v;
        }
        byte[] raw = compute.get();
        v = new Entry(raw, gzip(raw));
        if (map.size() >= maxEntries) {
            map.clear(); // простая политика: кэш небольшой, промахи дешёвые
        }
        map.put(key, v);
        return v;
    }

    static byte[] gzip(byte[] raw) {
        ByteArrayOutputStream bos = new ByteArrayOutputStream(raw.length / 4 + 64);
        try (GZIPOutputStream gz = new GZIPOutputStream(bos)) {
            gz.write(raw);
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
        return bos.toByteArray();
    }
}
