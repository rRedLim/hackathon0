package ru.mostrans.tram.ingest;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.LocalDate;
import java.util.List;

import org.junit.jupiter.api.Test;

import ru.mostrans.tram.data.DataStore;

/** Сигнал «обвал всей сети»: часы, которые не принимаются в WAPE-score и не видны маршрутному детектору. */
class NetworkDropTest {

    static final LocalDate D = LocalDate.of(2025, 11, 20);
    static final int N = DataStore.ROUTES.length;

    static List<IngestService.Alert> run(double factShare, int routesWithData) {
        long[][] actual = new long[N][24];
        double[][] fcst = new double[N][24];
        long[] netAct = new long[24];
        double[] netFc = new double[24];
        for (int i = 0; i < N; i++) {
            for (int h = 0; h < 24; h++) {
                fcst[i][h] = 1000;
                actual[i][h] = i < routesWithData && h >= 8 && h <= 12 ? Math.round(1000 * factShare) : 0;
                netAct[h] += actual[i][h];
                netFc[h] += fcst[i][h];
            }
        }
        return IngestService.networkDrop(D, actual, fcst, netAct, netFc, 12, 2);
    }

    @Test
    void wholeNetworkCollapseRaisesAlert() {
        List<IngestService.Alert> a = run(0.3, N);
        assertThat(a).hasSize(1);
        assertThat(a.get(0).kind()).isEqualTo("network_drop");
        assertThat(a.get(0).fromHour()).isEqualTo(8);
        assertThat(a.get(0).toHour()).isEqualTo(12);
    }

    @Test
    void partialUploadOfOneRouteIsNotACollapse() {
        assertThat(run(1.0, 1)).isEmpty();
    }

    @Test
    void normalFlowIsQuiet() {
        assertThat(run(0.95, N)).isEmpty();
    }
}
