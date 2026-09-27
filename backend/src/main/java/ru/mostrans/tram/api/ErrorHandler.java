package ru.mostrans.tram.api;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.codec.DecodingException;
import org.springframework.core.io.buffer.DataBufferLimitException;
import org.springframework.http.HttpStatus;
import org.springframework.http.ProblemDetail;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.server.ResponseStatusException;
import org.springframework.web.server.ServerWebExchange;
import org.springframework.web.server.ServerWebInputException;
import org.springframework.web.server.UnsupportedMediaTypeStatusException;

/** Все ошибки API — problem+json (RFC 9457) с сообщением для пользователя, без стектрейсов. */
@RestControllerAdvice
public class ErrorHandler {

    private static final Logger log = LoggerFactory.getLogger(ErrorHandler.class);

    @ExceptionHandler(ApiException.class)
    public ResponseEntity<ProblemDetail> api(ApiException e, ServerWebExchange ex) {
        ProblemDetail pd = problem(e.status(), e.getMessage(), ex);
        if (e.parameter() != null) {
            pd.setProperty("parameter", e.parameter());
        }
        return ResponseEntity.status(e.status()).body(pd);
    }

    @ExceptionHandler(UnsupportedMediaTypeStatusException.class)
    public ResponseEntity<ProblemDetail> media(UnsupportedMediaTypeStatusException e, ServerWebExchange ex) {
        return ResponseEntity.status(HttpStatus.UNSUPPORTED_MEDIA_TYPE).body(problem(HttpStatus.UNSUPPORTED_MEDIA_TYPE,
                "Неподдерживаемый Content-Type: ожидается text/csv или application/json", ex));
    }

    @ExceptionHandler({ServerWebInputException.class, DecodingException.class})
    public ResponseEntity<ProblemDetail> input(Exception e, ServerWebExchange ex) {
        return ResponseEntity.badRequest().body(problem(HttpStatus.BAD_REQUEST,
                "Не удалось разобрать запрос: проверьте параметры и формат тела", ex));
    }

    @ExceptionHandler(DataBufferLimitException.class)
    public ResponseEntity<ProblemDetail> tooLarge(DataBufferLimitException e, ServerWebExchange ex) {
        return ResponseEntity.status(HttpStatus.PAYLOAD_TOO_LARGE).body(problem(HttpStatus.PAYLOAD_TOO_LARGE,
                "Слишком большое тело запроса или строка: для больших объёмов используйте text/csv", ex));
    }

    @ExceptionHandler(ResponseStatusException.class)
    public ResponseEntity<ProblemDetail> status(ResponseStatusException e, ServerWebExchange ex) {
        HttpStatus st = HttpStatus.resolve(e.getStatusCode().value());
        if (st == null) {
            st = HttpStatus.INTERNAL_SERVER_ERROR;
        }
        String msg = st == HttpStatus.NOT_FOUND ? "Ресурс не найден: " + ex.getRequest().getPath()
                : st == HttpStatus.METHOD_NOT_ALLOWED ? "Метод " + ex.getRequest().getMethod() + " не поддерживается"
                : e.getReason() != null ? e.getReason() : st.getReasonPhrase();
        return ResponseEntity.status(st).body(problem(st, msg, ex));
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<ProblemDetail> other(Exception e, ServerWebExchange ex) {
        log.error("Ошибка обработки {} {}", ex.getRequest().getMethod(), ex.getRequest().getPath(), e);
        return ResponseEntity.internalServerError().body(problem(HttpStatus.INTERNAL_SERVER_ERROR,
                "Внутренняя ошибка сервиса. Повторите запрос; если ошибка повторяется — сообщите администратору", ex));
    }

    static ProblemDetail problem(HttpStatus st, String detail, ServerWebExchange ex) {
        ProblemDetail pd = ProblemDetail.forStatusAndDetail(st, detail);
        pd.setTitle(switch (st) {
            case BAD_REQUEST -> "Некорректный запрос";
            case NOT_FOUND -> "Не найдено";
            case PAYLOAD_TOO_LARGE -> "Слишком большой объём";
            case UNSUPPORTED_MEDIA_TYPE -> "Неподдерживаемый формат";
            case METHOD_NOT_ALLOWED -> "Метод не поддерживается";
            default -> st.is5xxServerError() ? "Внутренняя ошибка" : st.getReasonPhrase();
        });
        pd.setInstance(java.net.URI.create(ex.getRequest().getPath().value()));
        return pd;
    }
}
