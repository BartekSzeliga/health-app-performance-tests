package diecik.perf.support;

import java.net.URI;
import java.nio.file.Path;
import java.util.Locale;
import java.util.Map;

/**
 * Target of a test run. Values come from system properties ({@code -Dperf.apiBase=...}), then environment
 * variables ({@code PERF_API_BASE=...}), then the defaults of the named environment.
 * Production is never a default: see {@link ProdGuard}.
 */
public record TestEnvironment(
        String name,
        URI apiBase,
        URI keycloakBase,
        String realm,
        String clientId,
        String adminUser,
        String adminPassword,
        Path resultsDir
) {

    private static final Map<String, Map<String, String>> DEFAULTS = Map.of(
            // Separate local stack from local/docker-compose.yml (prod-sized limits), not the developer stack.
            "local", Map.of(
                    "apiBase", "http://localhost:18081",
                    "keycloakBase", "http://localhost:18080",
                    "realm", "health-realm",
                    "clientId", "health-app-frontend",
                    "adminUser", "admin",
                    "adminPassword", "admin"),
            // Rebuilt AWS dev environment. Admin credentials and client id must be passed explicitly.
            "dev", Map.of(
                    "apiBase", "https://api.dev.diecik.com",
                    "keycloakBase", "https://auth.dev.diecik.com",
                    "realm", "health-realm"));

    public static TestEnvironment current() {
        String name = setting("env", null, "local").toLowerCase(Locale.ROOT);
        Map<String, String> defaults = DEFAULTS.getOrDefault(name, Map.of());
        TestEnvironment env = new TestEnvironment(
                name,
                URI.create(stripSlash(required("apiBase", defaults))),
                URI.create(stripSlash(required("keycloakBase", defaults))),
                required("realm", defaults),
                required("clientId", defaults),
                setting("adminUser", defaults, null),
                setting("adminPassword", defaults, null),
                Path.of(setting("resultsDir", defaults, "results")).resolve(name));
        ProdGuard.check(env);
        return env;
    }

    /** Load, stress and isolation runs must never target production, with or without the smoke flag. */
    public TestEnvironment requireNonProd() {
        if (ProdGuard.isProduction(this)) {
            throw new IllegalStateException("This run is never allowed against production: " + apiBase);
        }
        return this;
    }

    public URI tokenEndpoint() {
        return keycloakBase.resolve("/realms/" + realm + "/protocol/openid-connect/token");
    }

    public Path usersFile() {
        return resultsDir.resolve("users.json");
    }

    private static String required(String key, Map<String, String> defaults) {
        String value = setting(key, defaults, null);
        if (value == null || value.isBlank()) {
            throw new IllegalStateException("Missing setting perf." + key + " (or " + envName(key) + ")");
        }
        return value;
    }

    private static String setting(String key, Map<String, String> defaults, String fallback) {
        String property = System.getProperty("perf." + key);
        if (property != null && !property.isBlank()) {
            return property;
        }
        String variable = System.getenv(envName(key));
        if (variable != null && !variable.isBlank()) {
            return variable;
        }
        if (defaults != null && defaults.containsKey(key)) {
            return defaults.get(key);
        }
        return fallback;
    }

    private static String envName(String key) {
        return "PERF_" + key.replaceAll("([a-z])([A-Z])", "$1_$2").toUpperCase(Locale.ROOT);
    }

    private static String stripSlash(String value) {
        return value.endsWith("/") ? value.substring(0, value.length() - 1) : value;
    }
}
