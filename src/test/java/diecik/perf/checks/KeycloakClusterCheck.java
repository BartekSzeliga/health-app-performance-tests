package diecik.perf.checks;

import com.fasterxml.jackson.databind.JsonNode;
import diecik.perf.support.Http;
import diecik.perf.support.PerfUser;
import diecik.perf.support.TestEnvironment;

import java.net.CookieManager;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.time.Duration;
import java.time.Instant;
import java.util.Base64;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Do several Keycloak tasks behind the ALB work as one cluster? A browser login takes several requests (login page,
 * form post, code exchange) and keeps its state in Keycloak's caches between them. The ALB has no sticky sessions,
 * so the requests land on different tasks; without a working cluster part of the logins fail. Refreshing one token
 * many times checks the shared user session the same way.
 *
 * <p>Usage: {@code KeycloakClusterCheck <logins> <refreshes> [redirectUri]}. Prints one line per failure and a summary;
 * exits with 1 if anything failed. Uses the first seeded client account.
 */
public final class KeycloakClusterCheck {

    private static final Pattern FORM_ACTION = Pattern.compile("<form[^>]*\\saction=\"([^\"]+)\"", Pattern.CASE_INSENSITIVE);
    private static final SecureRandom RANDOM = new SecureRandom();

    private KeycloakClusterCheck() {
    }

    public static void main(String[] args) throws Exception {
        TestEnvironment env = TestEnvironment.current().requireNonProd();
        int logins = Integer.parseInt(args[0]);
        int refreshes = Integer.parseInt(args[1]);
        String redirectUri = args.length > 2 ? args[2] : "https://dev.diecik.com/";
        PerfUser user = PerfUser.load(env.usersFile(), PerfUser.ROLE_CLIENT).getFirst();

        int loginFailures = 0;
        String refreshToken = null;
        for (int attempt = 1; attempt <= logins; attempt++) {
            try {
                refreshToken = browserLogin(env, user, redirectUri);
            } catch (RuntimeException failure) {
                loginFailures++;
                System.out.printf("%s login %d failed: %s%n", Instant.now(), attempt, failure.getMessage());
            }
        }

        int refreshFailures = 0;
        for (int attempt = 1; attempt <= refreshes && refreshToken != null; attempt++) {
            Http.Response response = Http.postForm(env.tokenEndpoint(), Map.of(
                    "grant_type", "refresh_token", "client_id", env.clientId(), "refresh_token", refreshToken));
            if (response.ok()) {
                refreshToken = response.json().path("refresh_token").asText(refreshToken);
            } else {
                refreshFailures++;
                System.out.printf("%s refresh %d failed: HTTP %d %s%n", Instant.now(), attempt, response.status(),
                        response.body().length() > 200 ? response.body().substring(0, 200) : response.body());
            }
        }

        System.out.printf("Keycloak cluster check: logins %d/%d ok, refreshes %d/%d ok%n",
                logins - loginFailures, logins, refreshToken == null ? 0 : refreshes - refreshFailures, refreshes);
        System.exit(loginFailures + refreshFailures == 0 && refreshToken != null ? 0 : 1);
    }

    /** Authorization code flow with PKCE, like the web panel. Returns the refresh token. */
    private static String browserLogin(TestEnvironment env, PerfUser user, String redirectUri) throws Exception {
        HttpClient browser = HttpClient.newBuilder()
                .cookieHandler(new CookieManager())
                .followRedirects(HttpClient.Redirect.NEVER)
                .connectTimeout(Duration.ofSeconds(10))
                .build();
        String verifier = Base64.getUrlEncoder().withoutPadding().encodeToString(bytes(32));
        String challenge = Base64.getUrlEncoder().withoutPadding().encodeToString(
                MessageDigest.getInstance("SHA-256").digest(verifier.getBytes(StandardCharsets.US_ASCII)));
        String base = env.keycloakBase() + "/realms/" + env.realm() + "/protocol/openid-connect";

        URI authorize = URI.create(base + "/auth?" + form(Map.of(
                "client_id", env.clientId(), "redirect_uri", redirectUri, "response_type", "code", "scope", "openid",
                "state", Base64.getUrlEncoder().withoutPadding().encodeToString(bytes(12)),
                "code_challenge", challenge, "code_challenge_method", "S256")));
        HttpResponse<String> page = browser.send(HttpRequest.newBuilder(authorize).GET().build(),
                HttpResponse.BodyHandlers.ofString());
        Matcher action = FORM_ACTION.matcher(page.body());
        if (page.statusCode() != 200 || !action.find()) {
            throw new IllegalStateException("login page: HTTP " + page.statusCode());
        }

        HttpResponse<String> submitted = browser.send(HttpRequest.newBuilder(URI.create(action.group(1).replace("&amp;", "&")))
                        .header("Content-Type", "application/x-www-form-urlencoded")
                        .POST(HttpRequest.BodyPublishers.ofString(form(Map.of(
                                "username", user.username(), "password", user.password(), "credentialId", ""))))
                        .build(),
                HttpResponse.BodyHandlers.ofString());
        String location = submitted.headers().firstValue("Location").orElse("");
        if (submitted.statusCode() != 302 || !location.startsWith(redirectUri) || !location.contains("code=")) {
            throw new IllegalStateException("form post: HTTP " + submitted.statusCode() + " " + shorten(location.isEmpty()
                    ? submitted.body() : location));
        }
        String code = URI.create(location).getQuery().replaceAll(".*(?:^|&)code=([^&]+).*", "$1");

        Http.Response tokens = Http.postForm(URI.create(base + "/token"), Map.of(
                "grant_type", "authorization_code", "client_id", env.clientId(), "code", code,
                "redirect_uri", redirectUri, "code_verifier", verifier));
        JsonNode json = tokens.requireOk("code exchange").json();
        return json.path("refresh_token").asText();
    }

    private static String form(Map<String, String> values) {
        return String.join("&", values.entrySet().stream()
                .map(e -> URLEncoder.encode(e.getKey(), StandardCharsets.UTF_8) + "="
                        + URLEncoder.encode(e.getValue(), StandardCharsets.UTF_8))
                .toList());
    }

    private static byte[] bytes(int length) {
        byte[] bytes = new byte[length];
        RANDOM.nextBytes(bytes);
        return bytes;
    }

    private static String shorten(String text) {
        String flat = text.replaceAll("\\s+", " ");
        return flat.length() > 200 ? flat.substring(0, 200) : flat;
    }
}
