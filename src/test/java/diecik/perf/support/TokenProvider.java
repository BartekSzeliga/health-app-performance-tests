package diecik.perf.support;

import com.fasterxml.jackson.databind.JsonNode;

import java.time.Duration;
import java.time.Instant;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Caches one token pair per test user and refreshes it shortly before expiry, so long runs do not turn token expiry
 * into a storm of 401s. Uses the password grant, which only the test realm enables (see SeedMain setup-realm).
 */
public final class TokenProvider {

    private static final Duration REFRESH_MARGIN = Duration.ofSeconds(30);

    private final TestEnvironment env;
    private final Map<String, String> passwords;
    private final Map<String, Token> tokens = new ConcurrentHashMap<>();

    private record Token(String access, String refresh, Instant expiresAt) {
        boolean fresh() {
            return Instant.now().plus(REFRESH_MARGIN).isBefore(expiresAt);
        }
    }

    public TokenProvider(TestEnvironment env, Map<String, String> passwordsByUsername) {
        this.env = env;
        this.passwords = Map.copyOf(passwordsByUsername);
    }

    public String accessToken(String username) {
        return tokens.compute(username, (user, current) -> {
            if (current != null && current.fresh()) {
                return current;
            }
            if (current != null && current.refresh() != null) {
                Token refreshed = request(Map.of(
                        "grant_type", "refresh_token",
                        "client_id", env.clientId(),
                        "refresh_token", current.refresh()), false);
                if (refreshed != null) {
                    return refreshed;
                }
            }
            String password = passwords.get(user);
            if (password == null) {
                throw new IllegalArgumentException("Unknown test user: " + user);
            }
            return request(Map.of(
                    "grant_type", "password",
                    "client_id", env.clientId(),
                    "scope", "openid",
                    "username", user,
                    "password", password), true);
        }).access();
    }

    private Token request(Map<String, String> form, boolean required) {
        Http.Response response = Http.postForm(env.tokenEndpoint(), form);
        if (!response.ok()) {
            if (required) {
                throw new IllegalStateException("Token request failed for " + form.get("username")
                        + ": HTTP " + response.status() + " " + Http.abbreviate(response.body()));
            }
            return null;
        }
        JsonNode json = response.json();
        return new Token(
                json.path("access_token").asText(),
                json.path("refresh_token").asText(null),
                Instant.now().plusSeconds(json.path("expires_in").asLong(60)));
    }
}
