package diecik.perf.seed;

import com.fasterxml.jackson.databind.JsonNode;
import diecik.perf.support.Http;
import diecik.perf.support.KeycloakAdmin;
import diecik.perf.support.PerfUser;
import diecik.perf.support.TestEnvironment;
import diecik.perf.support.TokenProvider;

import java.net.URI;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.stream.IntStream;

/**
 * Seeds dietitian units (W-14, W-15): one dietitian on the PRO plan with an accepted data processing agreement and
 * N linked clients, each with a published 7-day diet plan and some water entries. Every step goes through the public
 * API, the same way the panel and the app do it. A unit whose clients all have a published plan is skipped, so an
 * interrupted run can be repeated.
 */
final class DietitianUnitSeeder {

    private static final String PRO_PLAN = "trainer_pro_monthly";
    private static final String ADMIN = "perf-admin";

    private final TestEnvironment env;
    private final KeycloakAdmin keycloak;
    private final SeedDatabase database = new SeedDatabase();
    private final Map<String, PerfUser> existing = new HashMap<>();
    private PerfUser admin;
    private TokenProvider adminTokens;

    DietitianUnitSeeder(TestEnvironment env) {
        this.env = env;
        this.keycloak = new KeycloakAdmin(env);
        if (java.nio.file.Files.exists(env.usersFile())) {
            PerfUser.load(env.usersFile()).forEach(user -> existing.put(user.username(), user));
        }
    }

    void seed(int dietitians, int clientsPerDietitian, int threads) {
        admin = account(ADMIN, PerfUser.ROLE_ADMIN, null);
        keycloak.addRealmRoles(admin.keycloakId(), List.of("admin"));
        adminTokens = new TokenProvider(env, Map.of(admin.username(), admin.password()));
        provision(adminTokens, admin);
        PerfUser.save(env.usersFile(), List.of(admin));

        ExecutorService pool = Executors.newFixedThreadPool(threads);
        try {
            List<Future<List<PerfUser>>> units = IntStream.rangeClosed(1, dietitians)
                    .mapToObj(number -> pool.submit(() -> seedUnit(number, clientsPerDietitian)))
                    .toList();
            List<PerfUser> seeded = new ArrayList<>();
            for (Future<List<PerfUser>> unit : units) {
                seeded.addAll(unit.get());
                PerfUser.save(env.usersFile(), seeded);
            }
            System.out.printf("Seeded %d dietitian units with %d clients each into %s%n",
                    dietitians, clientsPerDietitian, env.usersFile());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("Interrupted", e);
        } catch (ExecutionException e) {
            throw new IllegalStateException("Seeding a dietitian unit failed", e.getCause());
        } finally {
            pool.shutdownNow();
        }
    }

    private List<PerfUser> seedUnit(int number, int clientCount) {
        String unit = "d%03d".formatted(number);
        List<PerfUser> previous = IntStream.rangeClosed(1, clientCount)
                .mapToObj(index -> existing.get(clientName(number, index)))
                .toList();
        if (previous.stream().allMatch(user -> user != null && user.planId() != null)) {
            List<PerfUser> unitUsers = new ArrayList<>(previous);
            unitUsers.add(existing.get(dietitianName(number)));
            System.out.printf("Unit %s already seeded, skipped%n", unit);
            return unitUsers;
        }

        PerfUser dietitian = account(dietitianName(number), PerfUser.ROLE_DIETITIAN, unit);
        List<PerfUser> clients = IntStream.rangeClosed(1, clientCount)
                .mapToObj(index -> account(clientName(number, index), PerfUser.ROLE_CLIENT, unit))
                .toList();
        Map<String, String> passwords = new HashMap<>();
        passwords.put(dietitian.username(), dietitian.password());
        clients.forEach(client -> passwords.put(client.username(), client.password()));
        TokenProvider tokens = new TokenProvider(env, passwords);

        provision(tokens, dietitian);
        grantProPlan(dietitian);
        acceptDataProcessingAgreement(tokens, dietitian, number);

        List<PerfUser> unitUsers = new ArrayList<>();
        unitUsers.add(dietitian);
        for (int index = 0; index < clients.size(); index++) {
            unitUsers.add(seedClient(tokens, dietitian, clients.get(index), number * 100 + index));
        }
        System.out.printf("Unit %s: dietitian and %d clients ready%n", unit, clients.size());
        return unitUsers;
    }

    private PerfUser seedClient(TokenProvider tokens, PerfUser dietitian, PerfUser client, int profile) {
        provision(tokens, client);
        String dietitianToken = tokens.accessToken(dietitian.username());
        String clientToken = tokens.accessToken(client.username());

        // The dietitian opens a card with the client's e-mail and sends the verification e-mail; the client opens the
        // link and connects the account. Mail is off here, so only the e-mail delivery is replaced: the token row
        // gets the hash of a token we know. Accepting and linking go through the API like in the app.
        String cardId = clientCardId(dietitianToken, client, profile);
        String status = api("GET", "/api/trainer/clients/" + cardId, dietitianToken, null)
                .requireOk("Card of " + client.username()).json().path("status").asText();
        if (!"LINKED".equals(status) && !"EMAIL_VERIFIED".equals(status)) {
            api("POST", "/api/trainer/clients/" + cardId + "/email-verification", dietitianToken, null)
                    .requireOk("Send verification to " + client.username());
            String rawToken = "perf-" + UUID.randomUUID() + UUID.randomUUID();
            database.deliverVerificationToken(UUID.fromString(cardId), rawToken);
            api("POST", "/api/trainer/client-invitations/" + rawToken + "/accept", clientToken, null)
                    .requireOk("Accept invitation of " + client.username());
            status = "EMAIL_VERIFIED";
        }
        if (!"LINKED".equals(status)) {
            api("POST", "/api/trainer/client-links/" + cardId + "/link-account", clientToken, null)
                    .requireOk("Link " + client.username());
        }

        // A published 7-day plan, generated on the server like "Generuj" in the panel. An interrupted run may have
        // left a published plan or an unpublished draft.
        String publishedId = null;
        JsonNode draft = null;
        for (JsonNode snapshot : api("GET", "/api/trainer/clients/" + cardId + "/snapshots", dietitianToken, null)
                .requireOk("Plans of " + client.username()).json()) {
            String snapshotStatus = snapshot.path("status").asText();
            if ("PUBLISHED".equals(snapshotStatus)) {
                publishedId = snapshot.path("id").asText();
            } else if ("DRAFT".equals(snapshotStatus) || "READY".equals(snapshotStatus)) {
                draft = snapshot;
            }
        }
        if (publishedId == null) {
            publishedId = publish(tokens, dietitian, client, cardId,
                    draft != null ? draft : generatePlan(tokens, dietitian, client, cardId, profile));
        }

        for (int glass = 0; glass < 2; glass++) {
            api("PUT", "/api/water-intake/" + UUID.randomUUID(), tokens.accessToken(client.username()),
                    Map.of("date", LocalDate.now().toString(), "amountMl", 250))
                    .requireOk("Water entry for " + client.username());
        }
        return client.withClientCard(cardId, publishedId);
    }

    /**
     * Generates on the server like "Generuj" in the panel. Some extreme targets have no practical plan (422
     * DIET_TARGET_UNMET, the same answer a dietitian would get); then the default targets are used.
     */
    private JsonNode generatePlan(TokenProvider tokens, PerfUser dietitian, PerfUser client, String cardId, int profile) {
        LocalDate start = LocalDate.now().plusDays(1);
        String path = "/api/trainer/clients/" + cardId + "/diet-snapshot/generate";
        Http.Response first = withRetry(() -> api("POST", path, tokens.accessToken(dietitian.username()),
                generateRequest(start, profile)));
        if (first.status() == 422 && first.body().contains("DIET_TARGET_UNMET")) {
            return withRetry(() -> api("POST", path, tokens.accessToken(dietitian.username()),
                    generateRequest(start, -1))).requireOk("Generate default plan for " + client.username()).json();
        }
        return first.requireOk("Generate plan for " + client.username()).json();
    }

    private String publish(TokenProvider tokens, PerfUser dietitian, PerfUser client, String cardId, JsonNode draft) {
        Map<String, Object> publish = new LinkedHashMap<>();
        publish.put("writePrecondition", Map.of(
                "type", "MATCH_SNAPSHOT",
                "snapshotId", draft.path("id").asText(),
                "expectedWriteRevision", draft.path("writeRevision").asLong()));
        publish.put("expectedCurrentPublishedSnapshotId", null);
        // Publishing is limited to 3 per minute per dietitian (burst 12); the retry waits as the panel user would.
        return withRetry(() -> api("POST", "/api/trainer/clients/" + cardId + "/snapshots/" + draft.path("id").asText()
                        + "/publish-v2", tokens.accessToken(dietitian.username()), publish))
                .requireOk("Publish plan for " + client.username()).json().path("id").asText();
    }

    /** Creates the card, or finds the one left by an interrupted run (the API allows one card per e-mail). */
    private String clientCardId(String dietitianToken, PerfUser client, int profile) {
        Http.Response created = api("POST", "/api/trainer/clients", dietitianToken, clientCard(client, profile));
        if (created.ok()) {
            return created.json().path("id").asText();
        }
        if (created.status() != 409) {
            created.requireOk("Create client card " + client.username());
        }
        JsonNode cards = api("GET", "/api/trainer/clients", dietitianToken, null).requireOk("List client cards").json();
        for (JsonNode card : cards.isArray() ? cards : cards.path("items")) {
            if (client.email().equalsIgnoreCase(card.path("email").asText())) {
                return card.path("id").asText();
            }
        }
        throw new IllegalStateException("Card of " + client.username() + " exists but was not listed");
    }

    private void grantProPlan(PerfUser dietitian) {
        Http.Response response = api("POST", "/api/admin/subscriptions/manual-grants",
                adminTokens.accessToken(admin.username()), Map.of("email", dietitian.email(), "planCode", PRO_PLAN));
        // A repeated grant is a conflict; the backend currently answers it with 500 instead of 409.
        boolean alreadyGranted = response.status() == 409
                || (response.status() == 500 && response.body().contains("already has an active subscription"));
        if (!response.ok() && !alreadyGranted) {
            response.requireOk("Grant " + PRO_PLAN + " to " + dietitian.username());
        }
    }

    private void acceptDataProcessingAgreement(TokenProvider tokens, PerfUser dietitian, int number) {
        String token = tokens.accessToken(dietitian.username());
        JsonNode status = api("GET", "/api/trainer/dpa", token, null).requireOk("DPA status").json();
        if (status.path("accepted").asBoolean(false)) {
            return;
        }
        Map<String, Object> acceptance = new LinkedHashMap<>();
        acceptance.put("version", status.path("currentVersion").asText());
        acceptance.put("documentSha256", status.path("documentSha256").asText(null));
        acceptance.put("controllerName", "Perf Dietetyk %03d".formatted(number));
        acceptance.put("controllerAddress", "ul. Testowa 1, 00-001 Warszawa");
        acceptance.put("controllerTaxId", "5260250274");
        acceptance.put("representativeConfirmed", true);
        api("PUT", "/api/trainer/dpa/acceptance", token, acceptance).requireOk("Accept DPA for " + dietitian.username());
    }

    private Map<String, Object> clientCard(PerfUser client, int profile) {
        Map<String, Object> card = new LinkedHashMap<>();
        card.put("displayName", "Perf " + client.username());
        card.put("email", client.email());
        card.put("goal", "Zdrowe nawyki");
        card.put("profile", Map.of(
                "gender", profile % 2 == 0 ? "FEMALE" : "MALE",
                "age", 20 + profile % 45,
                "weightKg", 55 + profile % 50,
                "heightCm", 155 + profile % 40,
                "activity", List.of("SEDENTARY", "LIGHT", "MODERATE", "ACTIVE").get(profile % 4),
                "nutritionGoal", List.of("LOSE_WEIGHT", "MAINTAIN", "GAIN_WEIGHT").get(profile % 3)));
        return card;
    }

    /**
     * Deterministic per-client targets like in GeneratorCapacitySimulation: 1500–3000 kcal, 3–6 meals. A negative
     * profile gives the default targets (2200 kcal, 5 meals, no allergies).
     */
    private Map<String, Object> generateRequest(LocalDate start, int profile) {
        boolean defaults = profile < 0;
        int calories = defaults ? 2200 : 1500 + (profile * 137) % 1501;
        Map<String, Object> request = new LinkedHashMap<>();
        request.put("title", "Plan perf");
        request.put("startDate", start.toString());
        request.put("days", 7);
        request.put("dietType", "standard");
        request.put("mealsPerDay", defaults ? 5 : 3 + profile % 4);
        request.put("kcal", calories);
        request.put("protein", calories / 16);
        request.put("carbs", calories / 8);
        request.put("fat", calories / 36);
        request.put("fiber", 30);
        request.put("allergies", !defaults && profile % 5 == 0 ? List.of("gluten") : List.of());
        request.put("tastePreferences", List.of());
        request.put("writePrecondition", Map.of("type", "NO_EDITABLE_SNAPSHOT"));
        return request;
    }

    /** The generator gate and the rate limits answer 429 with Retry-After; wait like the panel user and try again. */
    private Http.Response withRetry(java.util.function.Supplier<Http.Response> call) {
        for (int attempt = 1; ; attempt++) {
            Http.Response response = call.get();
            if (response.ok() || attempt == 30 || (response.status() != 429 && response.status() < 500)) {
                return response;
            }
            long seconds = response.raw().headers().firstValue("Retry-After").map(Long::parseLong).orElse(5L);
            sleep(seconds);
        }
    }

    private PerfUser account(String username, String role, String unit) {
        PerfUser known = existing.get(username);
        String email = username + "@perf.diecik.invalid";
        String password = known != null ? known.password() : SeedMain.randomPassword();
        String id = keycloak.ensureUser(username, email, "Perf", username, password);
        String canary = known != null ? known.canary() : username + "-" + UUID.randomUUID().toString().substring(0, 8);
        return new PerfUser(username, email, password, role, unit, canary, id,
                known == null ? null : known.trainerClientId(), known == null ? null : known.planId());
    }

    /** Like the app at start-up: this call creates the backend user record, then the profile is read. */
    private void provision(TokenProvider tokens, PerfUser user) {
        String token = tokens.accessToken(user.username());
        api("GET", "/api/users/user/details", token, null).requireOk("Provision " + user.username());
        api("GET", "/api/user-details", token, null).requireOk("Profile of " + user.username());
    }

    private Http.Response api(String method, String path, String token, Object body) {
        return Http.send(method, URI.create(env.apiBase() + path), token, body);
    }

    private static String dietitianName(int number) {
        return "perf-diet-%03d".formatted(number);
    }

    private static String clientName(int number, int index) {
        return "perf-cli-%03d-%02d".formatted(number, index);
    }

    private static void sleep(long seconds) {
        try {
            Thread.sleep(seconds * 1000);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("Interrupted", e);
        }
    }
}
