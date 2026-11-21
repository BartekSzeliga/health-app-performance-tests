package diecik.perf.support;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.net.URI;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.List;
import java.util.Map;

/** Keycloak Admin REST calls used to prepare the test realm. Never used against production. */
public final class KeycloakAdmin {

    private final TestEnvironment env;
    private String token;
    private Instant tokenExpiresAt = Instant.EPOCH;

    public KeycloakAdmin(TestEnvironment env) {
        this.env = env.requireNonProd();
        if (env.adminUser() == null || env.adminPassword() == null) {
            throw new IllegalStateException("Keycloak admin credentials are required (perf.adminUser, perf.adminPassword)");
        }
    }

    /**
     * Enables the password grant on the app client so test users can log in without a browser. The client id stays the
     * same as in production, so the backend sees the same {@code azp}.
     */
    public void enableDirectAccessGrants(String clientId) {
        JsonNode clients = call("GET", "/clients?clientId=" + encode(clientId), null).requireOk("Find client").json();
        if (!clients.isArray() || clients.isEmpty()) {
            throw new IllegalStateException("Client " + clientId + " not found in realm " + env.realm());
        }
        ObjectNode client = (ObjectNode) clients.get(0);
        if (client.path("directAccessGrantsEnabled").asBoolean(false)) {
            return;
        }
        client.put("directAccessGrantsEnabled", true);
        call("PUT", "/clients/" + client.path("id").asText(), client).requireOk("Enable direct access grants");
    }

    /** Creates a verified user with a password, or returns the id of the existing one. */
    public String ensureUser(String username, String email, String firstName, String lastName, String password) {
        Map<String, Object> body = Map.of(
                "username", username,
                "email", email,
                "firstName", firstName,
                "lastName", lastName,
                "enabled", true,
                "emailVerified", true,
                "requiredActions", List.of(),
                "credentials", List.of(Map.of("type", "password", "value", password, "temporary", false)));
        Http.Response created = call("POST", "/users", body);
        if (created.status() == 201) {
            String location = created.raw().headers().firstValue("Location").orElseThrow();
            return location.substring(location.lastIndexOf('/') + 1);
        }
        if (created.status() != 409) {
            created.requireOk("Create user " + username);
        }
        JsonNode existing = call("GET", "/users?exact=true&username=" + encode(username), null)
                .requireOk("Find user " + username).json();
        String id = existing.get(0).path("id").asText();
        call("PUT", "/users/" + id + "/reset-password",
                Map.of("type", "password", "value", password, "temporary", false))
                .requireOk("Reset password of " + username);
        return id;
    }

    public void addRealmRoles(String userId, List<String> roles) {
        List<JsonNode> representations = roles.stream()
                .map(role -> call("GET", "/roles/" + encode(role), null).requireOk("Find role " + role).json())
                .toList();
        call("POST", "/users/" + userId + "/role-mappings/realm", representations).requireOk("Assign roles " + roles);
    }

    private Http.Response call(String method, String path, Object body) {
        URI uri = URI.create(env.keycloakBase() + "/admin/realms/" + env.realm() + path);
        return Http.send(method, uri, adminToken(), body);
    }

    private synchronized String adminToken() {
        if (token != null && Instant.now().plusSeconds(10).isBefore(tokenExpiresAt)) {
            return token;
        }
        JsonNode json = Http.postForm(env.keycloakBase().resolve("/realms/master/protocol/openid-connect/token"), Map.of(
                        "grant_type", "password",
                        "client_id", "admin-cli",
                        "username", env.adminUser(),
                        "password", env.adminPassword()))
                .requireOk("Keycloak admin login").json();
        token = json.path("access_token").asText();
        tokenExpiresAt = Instant.now().plusSeconds(json.path("expires_in").asLong(60));
        return token;
    }

    private static String encode(String value) {
        return URLEncoder.encode(value, StandardCharsets.UTF_8);
    }
}
