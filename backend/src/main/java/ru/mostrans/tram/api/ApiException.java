package ru.mostrans.tram.api;

import org.springframework.http.HttpStatus;

/** Ошибка запроса с понятным пользователю сообщением; превращается в problem+json. */
public class ApiException extends RuntimeException {

    private final HttpStatus status;
    private final String parameter;

    public ApiException(HttpStatus status, String parameter, String message) {
        super(message);
        this.status = status;
        this.parameter = parameter;
    }

    public static ApiException badRequest(String parameter, String message) {
        return new ApiException(HttpStatus.BAD_REQUEST, parameter, message);
    }

    public static ApiException notFound(String parameter, String message) {
        return new ApiException(HttpStatus.NOT_FOUND, parameter, message);
    }

    public HttpStatus status() {
        return status;
    }

    public String parameter() {
        return parameter;
    }
}
