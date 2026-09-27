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

    @Value("${app.cors-origins:*}")
    private String corsOrigins;

    @Bean
    public DataStore dataStore(@Value("${app.data-dir}") String dir, ObjectMapper json) {
        return new DataStore(Path.of(dir), json);
    }

    @Override
    public void addCorsMappings(CorsRegistry registry) {
        registry.addMapping("/api/**")
                .allowedOrigins(corsOrigins.split(","))
                .allowedMethods("GET", "POST", "DELETE", "OPTIONS")
                .exposedHeaders("Content-Disposition");
    }

    @Bean
    public OpenAPI openApi() {
        return new OpenAPI().info(new Info()
                .title("ИИ-прогноз загрузки трамвайных маршрутов — API")
                .version("1.0")
                .description("Прогноз посадок по маршрутам, остановкам и участкам на горизонтах день / месяц / год, "
                        + "корректирующие коэффициенты, выгрузка CSV/XLSX, приём валидаций. Подробно — docs/api.md"));
    }
}
