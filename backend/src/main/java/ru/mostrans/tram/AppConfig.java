package ru.mostrans.tram;

import java.nio.file.Path;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.reactive.config.CorsRegistry;
import org.springframework.web.reactive.config.WebFluxConfigurer;

import io.swagger.v3.oas.models.OpenAPI;
import io.swagger.v3.oas.models.info.Info;
import ru.mostrans.tram.data.DataStore;
import tools.jackson.databind.ObjectMapper;

@Configuration
public class AppConfig implements WebFluxConfigurer {

    /** Разрешённые Origin через запятую; пусто — CORS выключен (интерфейс ходит к API с того же адреса). */
    @Value("${app.cors-origins:}")
    private String corsOrigins;

    @Bean
    public DataStore dataStore(@Value("${app.data-dir}") String dir, ObjectMapper json) {
        return new DataStore(Path.of(dir), json);
    }

    @Override
    public void addCorsMappings(CorsRegistry registry) {
        if (corsOrigins == null || corsOrigins.isBlank()) {
            return;
        }
        registry.addMapping("/api/**")
                .allowedOrigins(corsOrigins.split(","))
                .allowedMethods("GET", "POST", "DELETE", "OPTIONS")
                .allowedHeaders("Content-Type", "X-Workspace")
                .exposedHeaders("Content-Disposition");
    }

    @Bean
    public OpenAPI openApi() {
        // относительный адрес сервера: «Try it out» работает на любом хосте, порту и схеме (в т. ч. за TLS-прокси)
        return new OpenAPI().servers(java.util.List.of(new io.swagger.v3.oas.models.servers.Server().url("/")))
                .info(new Info()
                .title("ИИ-прогноз загрузки трамвайных маршрутов — API")
                .version("1.0")
                .description("Прогноз посадок по маршрутам, остановкам и участкам на горизонтах день / месяц / год, "
                        + "корректирующие коэффициенты, выгрузка CSV/XLSX, приём валидаций. Подробно — docs/api.md"));
    }
}
