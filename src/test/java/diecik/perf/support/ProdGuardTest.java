package diecik.perf.support;

import org.junit.jupiter.api.Test;

import java.net.URI;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class ProdGuardTest {

    @Test
    void productionHostsAreRecognised() {
        assertTrue(ProdGuard.isProductionHost(URI.create("https://api.diecik.com")));
        assertTrue(ProdGuard.isProductionHost(URI.create("https://AUTH.diecik.com/realms/x")));
        assertTrue(ProdGuard.isProductionHost(URI.create("https://api.diecik.com./")));
        assertTrue(ProdGuard.isProductionHost(URI.create("http://helti-prod-public-1.eu-central-1.elb.amazonaws.com")));
    }

    @Test
    void testHostsAreAllowed() {
        assertFalse(ProdGuard.isProductionHost(URI.create("https://api.dev.diecik.com")));
        assertFalse(ProdGuard.isProductionHost(URI.create("http://localhost:18081")));
    }

    @Test
    void productionIsRefusedWithoutSmokeFlagAndAlwaysForLoad() {
        TestEnvironment prod = environment("https://api.diecik.com", "https://auth.diecik.com");
        assertThrows(IllegalStateException.class, () -> ProdGuard.check(prod));
        assertThrows(IllegalStateException.class, prod::requireNonProd);
    }

    @Test
    void devEnvironmentPasses() {
        TestEnvironment dev = environment("https://api.dev.diecik.com", "https://auth.dev.diecik.com");
        assertDoesNotThrow(() -> ProdGuard.check(dev));
        assertDoesNotThrow(dev::requireNonProd);
    }

    private static TestEnvironment environment(String api, String keycloak) {
        return new TestEnvironment("test", URI.create(api), URI.create(keycloak), "health-realm", "client",
                null, null, Path.of("results/test"));
    }
}
