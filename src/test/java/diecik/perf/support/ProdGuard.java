package diecik.perf.support;

import java.net.URI;
import java.util.Locale;
import java.util.Set;

/**
 * Refuses to point the toolkit at production. Only an explicit {@code -Dperf.allowProdSmoke=true} lets a smoke run
 * through, and {@link TestEnvironment#requireNonProd()} blocks even that for load, stress and isolation runs.
 */
public final class ProdGuard {

    static final Set<String> PRODUCTION_HOSTS = Set.of(
            "diecik.com", "www.diecik.com", "api.diecik.com", "auth.diecik.com",
            "diecik.pl", "www.diecik.pl");

    private ProdGuard() {
    }

    public static boolean isProduction(TestEnvironment env) {
        return isProductionHost(env.apiBase()) || isProductionHost(env.keycloakBase());
    }

    static boolean isProductionHost(URI uri) {
        String host = uri.getHost();
        if (host == null) {
            return false;
        }
        String normalized = host.toLowerCase(Locale.ROOT);
        if (normalized.endsWith(".")) {
            normalized = normalized.substring(0, normalized.length() - 1);
        }
        return PRODUCTION_HOSTS.contains(normalized) || normalized.endsWith(".elb.amazonaws.com");
    }

    static void check(TestEnvironment env) {
        if (isProduction(env) && !Boolean.getBoolean("perf.allowProdSmoke")) {
            throw new IllegalStateException("Refusing to target production (" + env.apiBase() + ", "
                    + env.keycloakBase() + "). Only a smoke run with -Dperf.allowProdSmoke=true is possible.");
        }
    }
}
